"""Aplicação principal do CRM continnumOS.

Este módulo concentra a API FastAPI, a renderização dos dashboards Jinja2,
os endpoints consumidos pelo frontend, os webhooks da Evolution API e as
rotinas assíncronas de apoio como SLA, limpeza de mídia e monitoramento.
"""

import sys
import asyncio
import os
import random
import time
import re
import json
import hashlib
import hmac
import mimetypes
import bcrypt
import base64
import psutil
import aiofiles
import csv
import io
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from pydantic import BaseModel, Field
from fastapi import FastAPI, Request, BackgroundTasks, Form, Depends, status, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager
from starlette.concurrency import run_in_threadpool
from loguru import logger
import redis.asyncio as redis
from config import settings
import database as db
import evolution_service as evo
import auth
from auth import get_current_user
from tenant_context import set_current_tenant, get_current_tenant, DEFAULT_TENANT_ID
import tenant_config as tc
from importar_vcf import LIST_COLORS, PREFIX_ALIASES, detect_known_prefix, extract_leading_tokens, normalize_token
from services.runtime import check_redis, redis_lock, uptime_seconds
from services import storage
from services import payments
from services import ratelimit
from services import observability
from services import jobqueue
from services import webhooks
import permissions
import integrations
import uuid

BR_TIMEZONE = timezone(timedelta(hours=-3))
EDIT_MESSAGE_LIMIT_SECONDS = 15 * 60
DELETE_MESSAGE_LIMIT_SECONDS = 48 * 60 * 60
CALENDAR_REMINDER_INTERVAL_SECONDS = 30 * 60
AUTO_REPLY_LIMIT = 3
AUTO_REPLY_WINDOW_SECONDS = 30 * 60
BOT_SUPPRESSION_SECONDS = 12 * 60 * 60
OFF_HOURS_REPLY_EXTRA_SECONDS = 60 * 60
BOT_LOOP_WINDOW_SECONDS = 5 * 60
BOT_LOOP_EVENT_LIMIT = 30
BOT_LOOP_INBOUND_LIMIT = 10
BOT_LOOP_REPEAT_LIMIT = 4
BOT_LOOP_ALTERNATION_SWITCHES = 7
WEBHOOK_MESSAGE_LOCK_SECONDS = 120
GLOBAL_AUTO_REPLY_LIMIT = 60
GLOBAL_AUTO_REPLY_WINDOW_SECONDS = 5 * 60
GLOBAL_AUTOMATION_SUPPRESSION_SECONDS = 10 * 60
OPERATOR_ROLES = {"vendas_1", "vendas_2", "financeiro", "engenharia"}
ADMIN_READ_ROLES = {"admin", "financeiro"}
ADMIN_WRITE_ROLES = {"admin"}
ADMIN_PERMISSION_GROUPS = {
    "admin": {
        "communication",
        "contacts",
        "audit",
        "reports",
        "users",
        "sessions",
        "evolution",
        "settings",
        "maintenance",
    },
    "financeiro": {"communication", "contacts", "audit", "reports"},
}
CHANGELOG_PATH = "CHANGELOG.md"
MAX_MEDIA_UPLOAD_BYTES = int(getattr(settings, "MAX_MEDIA_UPLOAD_MB", 32)) * 1024 * 1024
STATUS_WA_JID_PATTERN = re.compile(r"^55\d{10,11}@s\.whatsapp\.net$")
AUDIT_LOG_ACTION_CATEGORIES = {
    "attendance": ["chat_taken", "admin_takeover", "chat_transferred", "chat_finished", "external_whatsapp_opened"],
    "contacts": ["contact_created", "contact_existing_added", "contact_edited", "contact_reviewed", "contact_bulk_action_applied", "contact_list_created", "contact_list_updated", "contact_prefix_lists_applied", "contact_owner_repair_applied", "admin_contacts_synced", "contacts_synced"],
    "users": ["session_cleared", "system_user_created", "system_user_updated"],
    "sync": ["contact_prefix_lists_applied", "contact_owner_repair_applied", "admin_contacts_synced", "contacts_synced"],
    "communication": ["panel_notice_sent", "calendar_event_created", "calendar_event_completed", "calendar_event_rescheduled", "calendar_event_cancelled", "status_post_deleted"],
    "system": ["external_whatsapp_opened", "session_cleared"],
}
AUDIT_LOG_ACTION_LABELS = {
    "contact_created": "Contato criado",
    "contact_existing_added": "Contato já existente",
    "contact_edited": "Contato editado",
    "contact_reviewed": "Contato revisado",
    "contact_bulk_action_applied": "Ação em lote nos contatos",
    "contact_list_created": "Lista criada",
    "contact_list_updated": "Lista atualizada",
    "chat_taken": "Atendimento assumido",
    "admin_takeover": "Admin assumiu",
    "chat_transferred": "Atendimento transferido",
    "chat_finished": "Atendimento finalizado",
    "external_whatsapp_opened": "WhatsApp externo",
    "session_cleared": "Sessão liberada",
    "system_user_created": "Usuário criado",
    "system_user_updated": "Usuário editado",
    "contact_prefix_lists_applied": "Listas por prefixo",
    "contact_owner_repair_applied": "Carteira corrigida",
    "admin_contacts_synced": "Sync contatos admin",
    "contacts_synced": "Sync contatos",
    "panel_notice_sent": "Comunicado em tela",
    "calendar_event_created": "Agenda criada",
    "calendar_event_completed": "Agenda contatada",
    "calendar_event_rescheduled": "Agenda remarcada",
    "calendar_event_cancelled": "Agenda cancelada",
    "status_post_deleted": "Status apagado",
}

def audit_log_category_actions(category: str) -> list[str]:
    """Converte a categoria visual da aba Logs em ações persistidas no banco."""
    return AUDIT_LOG_ACTION_CATEGORIES.get(category, [])

def can_read_admin_panel(user: dict) -> bool:
    """Permite leitura administrativa para admin e financeiro sem mudar o papel operacional."""
    return bool(user and user.get("role") in ADMIN_READ_ROLES)

def can_write_admin_panel(user: dict) -> bool:
    """Restringe ações administrativas sensíveis ao administrador raiz."""
    return bool(user and user.get("role") in ADMIN_WRITE_ROLES)

def can_admin_action(user: dict, group: str) -> bool:
    """Valida permissão administrativa por grupo funcional."""
    if not user:
        return False
    return group in ADMIN_PERMISSION_GROUPS.get(user.get("role"), set())

def can_edit_contact_record(user: dict, contact: dict) -> bool:
    """Valida se o usuário pode alterar dados de um contato específico."""
    if not user or not contact:
        return False
    if can_write_admin_panel(user) or can_admin_action(user, "contacts") or can_admin_action(user, "audit"):
        return True
    allowed_owners = {user.get("name"), user.get("role"), user.get("username")}
    return contact.get("assigned_to") in allowed_owners

def internal_chat_preview(text: str) -> str:
    """Gera prévia curta e segura de texto/mídia para avisos do chat interno."""
    clean = re.sub(r"\s+", " ", text or "").strip()
    if clean.startswith("[IMAGE]"):
        clean = "Enviou uma imagem."
    elif clean.startswith("[AUDIO]") or clean.startswith("[PTT]"):
        clean = "Enviou um áudio."
    elif clean.startswith("[VIDEO]"):
        clean = "Enviou um vídeo."
    elif clean.startswith("[DOCUMENT]"):
        clean = "Enviou um documento."
    return clean[:120] or "Nova mensagem interna."

def extract_internal_mentions(text: str) -> list[str]:
    """Extrai menções `@nome` ou `@perfil` do chat interno, aceitando acentos."""
    mentions = []
    for raw in re.findall(r"@([\wÀ-ÿ.-]+)", text or ""):
        value = raw.strip()
        if value and value not in mentions:
            mentions.append(value)
    return mentions

async def broadcast_internal_chat_event(
    user: dict,
    target: str,
    text: str,
    mentions: Optional[list[str]] = None,
    message_id: Optional[int] = None,
    created_at: Optional[datetime] = None,
    request_started_at: Optional[float] = None,
):
    """Publica evento WebSocket JSON e registra rastreio de latência do chat interno."""
    broadcast_started_at = time.perf_counter()
    event_created_at = created_at or datetime.now(BR_TIMEZONE)
    event_id = f"internal_message:{message_id}" if message_id else f"internal_message:{user.get('name', '')}:{target}:{event_created_at.timestamp()}"
    payload = {
        "type": "internal_message",
        "event_id": event_id,
        "message_id": message_id,
        "created_at": event_created_at.isoformat(timespec="milliseconds"),
        "conversation_target": target,
        "target": target,
        "sender": user.get("name", ""),
        "sender_role": user.get("role", ""),
        "preview": internal_chat_preview(text),
        "mentions": mentions or [],
    }
    logger.info(
        "[INTERNAL_CHAT_BROADCAST_START] message_id={} sender={} role={} target={} created_at={} active_connections={} preview={}",
        message_id or "-",
        user.get("name", ""),
        user.get("role", ""),
        target,
        event_created_at.isoformat(timespec="milliseconds"),
        len(manager.active_connections),
        payload["preview"],
    )
    await manager.broadcast(json.dumps(payload, ensure_ascii=True))
    broadcast_ms = (time.perf_counter() - broadcast_started_at) * 1000
    total_ms = (time.perf_counter() - request_started_at) * 1000 if request_started_at else broadcast_ms
    logger.info(
        "[INTERNAL_CHAT_BROADCAST_OK] message_id={} sender={} role={} target={} created_at={} broadcast_ms={:.2f} total_ms={:.2f} active_connections={}",
        message_id or "-",
        user.get("name", ""),
        user.get("role", ""),
        target,
        event_created_at.isoformat(timespec="milliseconds"),
        broadcast_ms,
        total_ms,
        len(manager.active_connections),
    )

async def broadcast_internal_chat_update(message: dict):
    """Publica recarga silenciosa após editar, apagar, reagir, fixar ou desfixar."""
    payload = {
        "type": "internal_message_update",
        "target": message.get("target_name") or "geral",
        "sender": message.get("sender_name") or "",
    }
    await manager.broadcast(json.dumps(payload, ensure_ascii=True))

def can_read_internal_message(message: dict, user: dict) -> bool:
    """Confere se o usuário participa da conversa geral ou privada da mensagem."""
    return bool(
        message
        and (
            message.get("target_name") == "geral"
            or message.get("sender_name") == user.get("name")
            or message.get("target_name") == user.get("name")
        )
    )

def can_manage_internal_message(message: dict, user: dict) -> bool:
    """Permite editar/apagar mensagens próprias e concede override ao admin."""
    return bool(message and (message.get("sender_name") == user.get("name") or user.get("role") == "admin"))

def internal_message_event_payload(message: dict, current_user: dict = None) -> dict:
    """Converte uma linha de `internal_messages` para o evento consumido pelo frontend."""
    message_id = message.get("id")
    target = message.get("target_name") or "geral"
    sender = message.get("sender_name") or ""
    user_name = (current_user or {}).get("name", "")
    conversation_target = "geral" if target == "geral" else (target if sender == user_name else sender)
    text = message.get("msg_text") or ""
    return {
        "type": "internal_message",
        "event_id": f"internal_message:{message_id}",
        "message_id": message_id,
        "created_at": format_datetime_br_iso(message.get("timestamp")),
        "conversation_target": conversation_target,
        "target": target,
        "sender": sender,
        "sender_role": message.get("sender_role") or "",
        "preview": internal_chat_preview(text),
        "mentions": message.get("mentions") or [],
    }

def format_datetime_br(dt):
    """Converte timestamps do banco para o horário de Brasília exibido no painel."""
    if not dt:
        return None
    if isinstance(dt, str):
        try:
            normalized = dt.replace("T", " ").split(".")[0].replace("Z", "")
            dt = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BR_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")

def format_datetime_br_iso(dt):
    """Converte timestamps do banco para ISO em Brasília, legível pelo JavaScript."""
    if not dt:
        return datetime.now(BR_TIMEZONE).isoformat(timespec="milliseconds")
    if isinstance(dt, str):
        try:
            normalized = dt.replace("T", " ").split(".")[0].replace("Z", "")
            dt = datetime.strptime(normalized, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(BR_TIMEZONE).isoformat(timespec="milliseconds")

def format_message_timestamps(messages: list):
    """Normaliza horários de mensagens antes de enviar JSON para o frontend."""
    for message in messages:
        if message.get("timestamp"):
            message["timestamp"] = format_datetime_br(message["timestamp"])
    return messages

def format_client_datetimes(clients: list):
    """Normaliza horários de contatos usados nas listas e cards do painel."""
    for client in clients:
        for field in ("last_interaction", "attendance_start"):
            if client.get(field):
                client[field] = format_datetime_br(client[field])
    return clients

def message_age_seconds(message: dict) -> float:
    """Calcula a idade da mensagem considerando timestamp naive do banco como UTC."""
    timestamp = message.get("timestamp")
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.strptime(timestamp.replace("T", " ").split(".")[0].replace("Z", ""), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return float("inf")
    if not timestamp:
        return float("inf")
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()

def is_remote_outgoing_message(message: dict, phone: str) -> bool:
    """Valida se a mensagem pode ser gerenciada pela instância sem tocar entrada/local."""
    waha_id = str(message.get("waha_id") or "")
    sender = message.get("sender")
    return bool(
        waha_id
        and not waha_id.startswith(("local_", "sys_"))
        and sender != phone
        and sender != "sistema"
        and message.get("msg_type") != "deleted"
    )

def normalize_whatsapp_jid(raw_phone: str) -> str:
    """Normaliza telefone configurado para o formato JID aceito pelo WhatsApp."""
    if not raw_phone:
        return ""
    if "@s.whatsapp.net" in raw_phone:
        return raw_phone
    nums = re.sub(r"\D", "", raw_phone)
    if not nums:
        return ""
    if len(nums) <= 11:
        nums = "55" + nums
    return f"{nums}@s.whatsapp.net"

def normalize_status_target_jid(raw_phone: str) -> Tuple[str, str]:
    """Normaliza telefone para status e retorna motivo quando o contato é inválido."""
    if not raw_phone:
        return "", "phone_empty"
    raw = str(raw_phone).strip()
    if raw.endswith("@lid"):
        return "", "lid_contact"

    digits = re.sub(r"\D", "", raw)
    if not digits:
        return "", "digits_empty"

    if digits.startswith("55"):
        normalized_digits = digits
    else:
        without_zeros = digits.lstrip("0")
        if len(without_zeros) in (10, 11):
            normalized_digits = f"55{without_zeros}"
        else:
            return "", "missing_country_code"

    jid = f"{normalized_digits}@s.whatsapp.net"
    if not STATUS_WA_JID_PATTERN.match(jid):
        return "", "invalid_jid_pattern"
    return jid, ""

def summarize_skip_reasons(skip_reasons: Dict[str, int]) -> str:
    """Compacta motivos de descarte para logs operacionais."""
    if not skip_reasons:
        return "none"
    ordered = sorted(skip_reasons.items(), key=lambda item: (-item[1], item[0]))
    return ", ".join(f"{reason}:{count}" for reason, count in ordered)

def nested_value(source: dict, *paths: str):
    """Busca o primeiro valor existente em caminhos aninhados separados por ponto."""
    for path in paths:
        current = source
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                current = None
                break
            current = current.get(part)
        if current not in (None, ""):
            return current
    return None

def normalize_status_ack(raw_status) -> int:
    """Converte variações de ACK da Evolution para nível numérico de leitura."""
    if raw_status is None:
        return 0
    if isinstance(raw_status, (int, float)):
        value = int(raw_status)
        return 3 if value >= 3 else value
    status_text = str(raw_status).strip().upper().replace("-", "_").replace(" ", "_")
    if status_text.isdigit():
        value = int(status_text)
        return 3 if value >= 3 else value
    ack_map = {
        "SERVER_ACK": 1,
        "DELIVERY_ACK": 2,
        "READ": 3,
        "READ_ACK": 3,
        "READ_RECEIPT": 3,
        "READ_MESSAGE": 3,
        "READ_MESSAGES": 3,
        "PLAYED": 3,
        "PLAYED_ACK": 3,
        "PLAYED_RECEIPT": 3,
    }
    return ack_map.get(status_text, 0)

def normalize_status_viewer_jid(raw_jid: str) -> str:
    """Normaliza o JID do visualizador para comparar com `status_targets`."""
    if not raw_jid:
        return ""
    text = str(raw_jid).strip()
    if text.endswith("@s.whatsapp.net"):
        return text
    if text.endswith("@c.us"):
        return text.replace("@c.us", "@s.whatsapp.net")
    digits = re.sub(r"\D", "", text)
    if not digits:
        return text
    if not digits.startswith("55") and len(digits) in (10, 11):
        digits = f"55{digits}"
    return f"{digits}@s.whatsapp.net"

def extract_status_update_context(update: dict) -> dict:
    """Extrai campos de leitura de status aceitando variações do payload Evolution."""
    status_id = nested_value(
        update,
        "key.id",
        "id",
        "messageId",
        "message_id",
        "update.key.id",
        "update.id",
        "update.messageId",
    )
    raw_status = nested_value(
        update,
        "update.status",
        "status",
        "ack",
        "update.ack",
        "receipt.status",
        "update.receipt.status",
    )
    participant = nested_value(
        update,
        "key.participant",
        "participant",
        "update.participant",
        "update.key.participant",
        "viewerJid",
        "viewer",
        "sender",
        "from",
    )
    return {
        "status_id": str(status_id) if status_id not in (None, "") else "",
        "raw_status": raw_status,
        "ack": normalize_status_ack(raw_status),
        "remote_jid": nested_value(
            update,
            "key.remoteJid",
            "remoteJid",
            "remote_jid",
            "update.key.remoteJid",
            "update.remoteJid",
            "statusJid",
            "jid",
        ) or "",
        "participant": normalize_status_viewer_jid(participant),
        "raw_participant": participant or "",
    }

def parse_int_or_zero(value) -> int:
    """Converte campos opcionais de formulario/query em inteiro seguro."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0

def normalize_finish_result(result: str) -> str:
    """Normaliza o resultado de finalização para venda ou sem venda."""
    token = str(result or "").strip().lower()
    sold = {"vendeu", "com_venda", "finalizado_com_venda", "vendido"}
    return "vendeu" if token in sold else "nao_vendeu"

def guess_media_mimetype(filename: str, msg_type: str) -> str:
    """Deduz MIME de midia encaminhada usando extensao e tipo salvo no historico."""
    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        return guessed
    fallback = {
        "image": "image/jpeg",
        "audio": "audio/ogg",
        "video": "video/mp4",
        "document": "application/octet-stream",
    }
    return fallback.get(msg_type, "application/octet-stream")

def sanitize_upload_filename(filename: str) -> str:
    """Remove caminhos e caracteres problemáticos antes de salvar/enviar mídia."""
    safe_name = os.path.basename(filename or "arquivo").replace(" ", "_")
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", safe_name)
    return safe_name or "arquivo"

def detect_media_payload(filename: str, browser_mime: str, sample: bytes) -> tuple[str, str]:
    """Define `mediatype` e MIME confiáveis para a Evolution API."""
    sample = sample or b""
    detected = browser_mime if browser_mime and browser_mime != "application/octet-stream" else None
    guessed, _ = mimetypes.guess_type(filename or "")
    if sample.startswith(b"%PDF"):
        detected = "application/pdf"
    elif sample.startswith(b"\x89PNG\r\n\x1a\n"):
        detected = "image/png"
    elif sample.startswith(b"\xff\xd8\xff"):
        detected = "image/jpeg"
    elif sample.startswith(b"GIF87a") or sample.startswith(b"GIF89a"):
        detected = "image/gif"
    elif sample.startswith(b"RIFF") and sample[8:12] == b"WEBP":
        detected = "image/webp"
    elif not detected and guessed:
        detected = guessed
    mime = detected or "application/octet-stream"
    if mime.startswith("image/"):
        return "image", mime
    if mime.startswith("video/"):
        return "video", mime
    if mime.startswith("audio/"):
        return "audio", mime
    return "document", mime

def load_latest_changelog():
    """Lê a primeira versao do changelog e cria um identificador para aviso unico."""
    try:
        with open(CHANGELOG_PATH, "r", encoding="utf-8") as file:
            lines = file.readlines()
    except OSError:
        return {"id": "", "title": "", "sections": []}

    title = ""
    body = []
    collecting = False
    for line in lines:
        if line.startswith("## "):
            if collecting:
                break
            title = line.replace("##", "", 1).strip()
            collecting = True
            continue
        if collecting:
            body.append(line.rstrip())

    sections = []
    current = None
    for line in body:
        if line.startswith("### "):
            if current:
                sections.append(current)
            current = {"title": line.replace("###", "", 1).strip(), "items": []}
        elif current and line.startswith("- "):
            current["items"].append(line[2:].strip())
    if current:
        sections.append(current)

    fingerprint = hashlib.sha256((title + "\n" + "\n".join(body)).encode("utf-8")).hexdigest()[:16]
    return {"id": fingerprint, "title": title, "sections": sections}

def parse_prefix_selection(data) -> list:
    """Normaliza prefixos escolhidos automaticamente ou digitados no painel admin."""
    raw = data.get("prefixes", [])
    if isinstance(raw, str):
        raw = raw.split(",")
    prefixes = []
    for item in raw:
        token = normalize_token(str(item))
        if token and token not in prefixes:
            prefixes.append(token)
    return prefixes

def clean_phone_display(phone: str) -> str:
    """Exibe telefone/JID sem sufixos técnicos da Evolution/WhatsApp."""
    return re.sub(r"@(s\.whatsapp\.net|c\.us|lid)$", "", str(phone or ""), flags=re.I)

def sanitize_contact_name_for_bulk(name: str, phone: str = "") -> str:
    """Remove artefatos comuns da Evolution sem alterar telefone ou responsável."""
    clean = str(name or "").strip()
    clean = re.sub(r"@(s\.whatsapp\.net|c\.us|lid)\b", "", clean, flags=re.I)
    clean = clean.replace("[", " ").replace("]", " ").replace("{", " ").replace("}", " ")
    clean = re.sub(r"\s+", " ", clean).strip(" -_")
    if not clean or clean.lower() in {"none", "null"}:
        clean = clean_phone_display(phone)
    return clean.strip()

def contact_initial_key(name: str, phone: str = "") -> str:
    """Calcula a inicial visual usada pela barra alfabetica da carteira."""
    clean = re.sub(
        r"^(kim|kimberly|rosana|ro|fla|flavia|flávia|thalita|sandro|denise|ronaldo|rodrigo)\s*[-_]?\s*",
        "",
        str(name or "").strip(),
        flags=re.I,
    )
    clean = clean or clean_phone_display(phone)
    clean = clean.strip()
    if not clean:
        return "#"
    normalized = normalize_token(clean[0])
    return normalized[:1].upper() if normalized[:1].isalpha() else "#"

def build_prefix_scan_payload(contacts: list, min_count: int = 2, limit_examples: int = 5):
    """Monta o relatório admin de primeiras palavras e prefixos conhecidos."""
    first_tokens = {}
    known_tokens = {}
    examples = {}
    for contact in contacts:
        tokens = extract_leading_tokens(contact.get("name") or "")
        if not tokens:
            continue
        first = tokens[0]
        first_tokens[first] = first_tokens.get(first, 0) + 1
        examples.setdefault(first, [])
        if len(examples[first]) < limit_examples:
            examples[first].append({
                "name": contact.get("name") or "",
                "phone": contact.get("phone") or "",
                "assigned_to": contact.get("assigned_to") or "",
            })
        prefix, canonical = detect_known_prefix(contact.get("name") or "")
        if prefix:
            known_tokens[prefix] = {
                "prefix": prefix,
                "canonical": canonical,
                "count": known_tokens.get(prefix, {}).get("count", 0) + 1,
            }

    prefixes = []
    for token, count in sorted(first_tokens.items(), key=lambda item: item[1], reverse=True):
        if count < min_count:
            continue
        prefixes.append({
            "prefix": token,
            "canonical": PREFIX_ALIASES.get(token, ""),
            "count": count,
            "examples": examples.get(token, []),
            "known": token in PREFIX_ALIASES,
        })

    known = sorted(known_tokens.values(), key=lambda item: item["count"], reverse=True)
    return {"total": len(contacts), "known_prefixes": known, "prefixes": prefixes}

def build_prefix_groups(contacts: list, prefixes: list, limit_examples: int = 5):
    """Agrupa contatos por prefixos aprovados, inclusive manuais, para listas."""
    grouped = {}
    for contact in contacts:
        prefix, canonical = detect_known_prefix(contact.get("name") or "", prefixes)
        if not prefix:
            continue
        grouped.setdefault(canonical, {"canonical": canonical, "prefixes": set(), "contacts": [], "examples": []})
        grouped[canonical]["prefixes"].add(prefix)
        grouped[canonical]["contacts"].append(contact)
        if len(grouped[canonical]["examples"]) < limit_examples:
            grouped[canonical]["examples"].append({
                "name": contact.get("name") or "",
                "phone": contact.get("phone") or "",
                "assigned_to": contact.get("assigned_to") or "",
            })

    response_groups = []
    apply_groups = {}
    for canonical, data in sorted(grouped.items()):
        contacts_for_group = data["contacts"]
        apply_groups[canonical] = contacts_for_group
        response_groups.append({
            "list_name": f"Herdados {canonical}",
            "canonical": canonical,
            "prefixes": sorted(data["prefixes"]),
            "count": len(contacts_for_group),
            "examples": data["examples"],
        })
    return response_groups, apply_groups

def detect_sales_owner_from_name(name: str, sales_names: list, prefixes: list = None):
    """Detecta Rosana/Kimberly pelo prefixo inicial do nome do contato."""
    _prefix, canonical = detect_known_prefix(name or "", prefixes)
    if not canonical:
        return None
    names_by_key = {normalize_token(owner): owner for owner in sales_names}
    return names_by_key.get(normalize_token(canonical))

def choose_balanced_owner(counts: dict):
    """Escolhe a vendedora com menor quantidade planejada."""
    if not counts:
        return None
    return sorted(counts.items(), key=lambda item: (item[1], item[0]))[0][0]

def build_owner_assignment_plan(contacts: list, sales_names: list, owner_mode: str = "auto", prefixes: list = None):
    """Monta plano de correção/divisão entre vendedoras sem tocar atendimentos ativos."""
    counts = {name: 0 for name in sales_names}
    for contact in contacts:
        owner = contact.get("assigned_to")
        if owner in counts and contact.get("stage") == "carteira":
            counts[owner] += 1

    assignments = []
    summary = {
        "total_candidates": len(contacts),
        "prefix_matched": 0,
        "balanced": 0,
        "kept": 0,
        "by_owner": {name: 0 for name in sales_names},
    }
    direct_owner = owner_mode if owner_mode in sales_names else None

    for contact in contacts:
        current_owner = contact.get("assigned_to") or ""
        target_owner = direct_owner
        reason = "manual" if direct_owner else ""

        if not target_owner:
            prefix_owner = detect_sales_owner_from_name(contact.get("name") or "", sales_names, prefixes)
            if prefix_owner:
                target_owner = prefix_owner
                reason = "prefix"
            elif current_owner not in sales_names:
                target_owner = choose_balanced_owner(counts)
                reason = "balance"

        if not target_owner or target_owner == current_owner:
            summary["kept"] += 1
            continue

        if current_owner in counts and contact.get("stage") == "carteira":
            counts[current_owner] = max(0, counts[current_owner] - 1)
        counts[target_owner] += 1
        if reason == "prefix":
            summary["prefix_matched"] += 1
        elif reason == "balance":
            summary["balanced"] += 1
        summary["by_owner"][target_owner] = summary["by_owner"].get(target_owner, 0) + 1
        assignments.append({
            "phone": contact.get("phone"),
            "name": contact.get("name") or "",
            "from": current_owner,
            "assigned_to": target_owner,
            "stage": contact.get("stage") or "",
            "reason": reason,
        })

    summary["total_updates"] = len(assignments)
    return assignments, summary

def collect_recent_evolution_failures(limit: int = 8, scan_lines: int = 800):
    """Lê logs recentes e retorna falhas Evolution resumidas para o painel admin."""
    patterns = (
        "Erro Evolution",
        "Evolution nao confirmou",
        "Evolution API nao confirmou",
        "Evolution API recusou",
        "Falha ao postar status na Evolution",
        "Erro ao enviar texto pela Evolution",
        "Erro ao enviar mídia pela Evolution",
        "Erro ao buscar contatos pela Evolution",
    )
    failures = []
    try:
        log_files = []
        for filename in os.listdir("logs"):
            if filename.startswith("crm_") and filename.endswith(".log"):
                path = os.path.join("logs", filename)
                log_files.append((os.path.getmtime(path), path))
        for _mtime, path in sorted(log_files, reverse=True):
            with open(path, "r", encoding="utf-8", errors="ignore") as file:
                lines = file.readlines()[-scan_lines:]
            for line in reversed(lines):
                if any(pattern in line for pattern in patterns):
                    failures.append(line.strip()[:500])
                    if len(failures) >= limit:
                        return failures
    except Exception as exc:
        return [f"Falha ao ler logs operacionais: {exc}"]
    return failures

async def build_operations_status():
    """Monta um retrato operacional em tempo de consulta para health/painel admin."""
    db_status = await db.health_check()
    redis_status = await check_redis(redis_client)
    evo_status_data = {"ok": False, "status": "unknown"}
    try:
        res = await evo.client.get(f"{settings.EVO_API_URL}/instance/connectionState/vendas")
        evo_status_data = {
            "ok": res.status_code in [200, 201],
            "status_code": res.status_code,
            "status": res.json().get("instance", {}).get("state", "unknown") if res.text else "unknown",
        }
    except Exception as exc:
        evo_status_data = {"ok": False, "error": str(exc)}

    try:
        sla_count = await redis_client.zcard("sla_queue")
        morning_count = await redis_client.zcard("morning_followup_queue")
        next_sla = await redis_client.zrange("sla_queue", 0, 0, withscores=True)
        next_morning = await redis_client.zrange("morning_followup_queue", 0, 0, withscores=True)
        queue_status = {
            "sla": sla_count,
            "morning_followup": morning_count,
            "next_sla_at": datetime.fromtimestamp(next_sla[0][1], BR_TIMEZONE).isoformat() if next_sla else None,
            "next_morning_followup_at": datetime.fromtimestamp(next_morning[0][1], BR_TIMEZONE).isoformat() if next_morning else None,
        }
    except Exception as exc:
        queue_status = {"error": str(exc)}

    disk = psutil.disk_usage("/")
    system_status = {
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory_percent": psutil.virtual_memory().percent,
        "disk_percent": disk.percent,
        "uptime_seconds": uptime_seconds(),
    }
    checks = {"database": db_status, "redis": redis_status, "evolution_vendas": evo_status_data}
    status_label = "ok" if all(check.get("ok") for check in checks.values()) else "degraded"
    if system_status["memory_percent"] >= 90 or system_status["cpu_percent"] >= 95 or system_status["disk_percent"] >= 90:
        status_label = "attention"
    return {
        "status": status_label,
        "checked_at": datetime.now(BR_TIMEZONE).isoformat(),
        "checks": checks,
        "queues": queue_status,
        "system": system_status,
        "evolution_failures": collect_recent_evolution_failures(),
    }


os.makedirs("static", exist_ok=True)
os.makedirs(storage.MEDIA_ROOT, exist_ok=True)
os.makedirs("logs", exist_ok=True)
os.makedirs(getattr(settings, "BACKUP_DIR", "/data/backups"), exist_ok=True)

# Fase 5 (Observabilidade): logging estruturado configurável.
# Com LOG_JSON=true os registros saem serializados (JSON) tanto no stdout quanto
# no arquivo, prontos para coletores centralizados (Loki/Datadog/ELK). O nível e
# a retenção são controláveis por env (LOG_LEVEL / LOG_RETENTION_DAYS).
_LOG_LEVEL = getattr(settings, "LOG_LEVEL", "INFO")
_LOG_JSON = getattr(settings, "LOG_JSON", False)
_LOG_RETENTION = f"{getattr(settings, 'LOG_RETENTION_DAYS', 7)} days"
if _LOG_JSON:
    logger.configure(handlers=[{"sink": sys.stdout, "serialize": True, "level": _LOG_LEVEL}])
    logger.add("logs/crm_{time:YYYY-MM-DD}.log", rotation="00:00", retention=_LOG_RETENTION, level=_LOG_LEVEL, serialize=True)
else:
    logger.configure(handlers=[{"sink": sys.stdout, "format": "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>", "level": _LOG_LEVEL}])
    logger.add("logs/crm_{time:YYYY-MM-DD}.log", rotation="00:00", retention=_LOG_RETENTION, level=_LOG_LEVEL)

templates = Jinja2Templates(directory="templates")
redis_client = redis.from_url(settings.REDIS_URL)

ROLE_SESSION_MAP = {
    "vendas_1": "vendas",
    "vendas_2": "vendas",
    "financeiro": "financeiro",
    "engenharia": "engenharia"
}

MORNING_FOLLOWUP_MESSAGE = (
    "Bom dia! Estamos online novamente. Seu atendimento foi registrado, "
    "o vendedor já foi notificado e ele irá entrar em contato."
)

class ApiPayload(BaseModel):
    """Base dos contratos JSON da API interna, aceitando evolução gradual do frontend."""

    class Config:
        extra = "ignore"

class CleanupRequest(ApiPayload):
    """Contrato da requisição de limpeza manual de arquivos de mídia."""

    media_types: List[str]
    days_old: int = 30

class PhonePayload(ApiPayload):
    """Contrato mínimo para ações que atuam sobre um contato por telefone."""

    phone: str

class AdminTakeoverRequest(PhonePayload):
    """Contrato para o admin assumir um atendimento."""

class ClearSessionRequest(ApiPayload):
    """Contrato para liberar uma sessão travada de usuário."""

    username: str

class EvolutionSessionRequest(ApiPayload):
    """Contrato para ações administrativas de conexão Evolution."""

    session: str = "vendas"

class TagCreateRequest(ApiPayload):
    """Contrato para criar uma tag de contato."""

    name: str
    color: str = "#00a884"

class UserTagToggleRequest(PhonePayload):
    """Contrato para vincular ou remover tag de um contato."""

    tag_id: int
    action: str = "add"

class ContactListCreateRequest(ApiPayload):
    """Contrato para criar lista operacional de contatos."""

    name: str
    color: str = "#00a884"
    is_global: bool = False

class ContactListToggleRequest(PhonePayload):
    """Contrato para vincular ou remover contato de uma lista."""

    list_id: int
    action: str = "add"

class ContactBulkActionRequest(ApiPayload):
    """Contrato para ações em lote controladas na carteira de contatos."""

    phones: List[str] = Field(default_factory=list)
    action: str = ""
    list_id: int = 0
    prefix: str = ""
    find: str = ""
    replace: str = ""
    dry_run: bool = False

class PrefixSelectionRequest(ApiPayload):
    """Contrato para simular ou aplicar listas por prefixos."""

    prefixes: List[str] = Field(default_factory=list)
    limit_examples: int = 5

class ContactOwnerRequest(ApiPayload):
    """Contrato para sincronizar ou redistribuir carteira entre vendedoras."""

    owner_mode: str = "auto"
    prefixes: List[str] = Field(default_factory=list)

class SnippetRequest(ApiPayload):
    """Contrato para criar atalho de mensagem."""

    shortcut: str
    content: str

class NewContactRequest(ApiPayload):
    """Contrato para cadastrar contato manualmente."""

    phone: str
    name: str = ""

class ProfileUpdateRequest(ApiPayload):
    """Contrato para atualizar dados visuais e cadastrais do perfil."""

    name: Optional[str] = None
    bio: Optional[str] = None
    avatar: Optional[str] = None
    bg_color: Optional[str] = None
    primary_color: Optional[str] = None
    text_color: Optional[str] = None

class InternalChatRequest(ApiPayload):
    """Contrato para mensagem textual do chat interno."""

    target: str
    text: str
    reply_to_id: Optional[int] = None

class InternalChatReactionRequest(ApiPayload):
    """Contrato para reagir a uma mensagem do chat interno."""

    message_id: int
    emoji: str

class InternalChatEditRequest(ApiPayload):
    """Contrato para editar uma mensagem textual do chat interno."""

    message_id: int
    text: str

class InternalChatMessageActionRequest(ApiPayload):
    """Contrato comum para apagar, fixar e desfixar por id no chat interno."""

    message_id: int

class InternalChatForwardRequest(ApiPayload):
    """Contrato para encaminhar uma mensagem interna a outro destino interno."""

    message_id: int
    target: str

class AdminPanelNoticeRequest(ApiPayload):
    """Contrato para comunicado visual enviado pelo admin aos painéis."""

    text: str
    targets: List[str] = Field(default_factory=list)

class CalendarEventRequest(ApiPayload):
    """Contrato para criar evento/anotação de agenda."""

    date: str
    note: str
    time: str = "09:00"
    phone: str = ""
    assigned_to: str = ""
    repeat_until_done: bool = True

class CalendarRescheduleRequest(ApiPayload):
    """Contrato para remarcar tarefa inteligente de agenda."""

    date: str
    time: str = "09:00"

class AdminUserRequest(ApiPayload):
    """Contrato para criação/edição de usuário interno pelo admin."""

    username: str
    role: str
    name: str
    is_active: int = 1
    password: Optional[str] = None

class AdminSettingsRequest(ApiPayload):
    """Contrato para atualizar textos operacionais do bot."""

    greeting: str
    menu: str
    off_hours: str

class ChatSendRequest(PhonePayload):
    """Contrato para envio de texto ao cliente."""

    text: str
    quoted_id: Optional[str] = None

class ReactionRequest(PhonePayload):
    """Contrato para reação em mensagem do WhatsApp."""

    message_id: str
    emoji: str

class EditMessageRequest(PhonePayload):
    """Contrato para edição de mensagem textual enviada."""

    message_id: str
    new_text: str

class DeleteMessageRequest(PhonePayload):
    """Contrato para exclusão de mensagem enviada."""

    message_id: str

class PinMessageRequest(PhonePayload):
    """Contrato para fixar mensagem no contato."""

    text: str

class ForwardMessageRequest(ApiPayload):
    """Contrato para encaminhar mensagem local para outro contato."""

    target_phone: str
    message_id: int

class TransferRequest(PhonePayload):
    """Contrato para transferir atendimento."""

    target: str

class StatusUpdateRequest(ApiPayload):
    """Contrato para atualização manual de presença."""

    status: str = "online"

class EditContactRequest(PhonePayload):
    """Contrato para editar dados de um contato."""

    name: str = ""
    notes: str = ""
    extra: str = ""

class FinishChatRequest(PhonePayload):
    """Contrato para finalizar atendimento."""

    result: str = ""

class ConnectionManager:
    """Gerencia WebSockets locais e replica eventos via Redis Pub/Sub."""

    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self.pubsub = redis_client.pubsub()

    async def connect(self, websocket: WebSocket):
        """Aceita uma conexão WebSocket e registra o cliente no processo atual."""
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        """Remove um WebSocket encerrado da lista local de conexões."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        """Publica um evento no canal Redis compartilhado entre workers."""
        await redis_client.publish("crm_channel", message)

    async def listen(self):
        """Escuta o canal Redis e entrega eventos aos WebSockets conectados.

        Reconecta com backoff se a conexao Redis cair, para o realtime nao
        morrer de vez apos um blip de rede (antes a task morria e nunca voltava).
        """
        backoff = 1
        while True:
            try:
                await self.pubsub.subscribe("crm_channel")
                backoff = 1
                async for message in self.pubsub.listen():
                    if message["type"] == "message":
                        data = message["data"].decode("utf-8")
                        dead_connections = []
                        for connection in list(self.active_connections):
                            try:
                                await connection.send_text(data)
                            except Exception as exc:
                                dead_connections.append(connection)
                                logger.debug("WebSocket removido da lista realtime apos falha de envio: {}", exc)
                        for connection in dead_connections:
                            self.disconnect(connection)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Realtime pub/sub caiu ({}); reconectando em {}s.", exc, backoff)
                try:
                    await self.pubsub.unsubscribe("crm_channel")
                except Exception:
                    pass
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

manager = ConnectionManager()

async def record_audit_log(actor: dict, action: str, phone: str = "", target: str = "", details: dict = None):
    """Grava auditoria operacional e notifica painéis administrativos em tempo real."""
    await db.save_audit_log(actor, action, phone=phone, target=target, details=details)
    await manager.broadcast("audit_logs")

def parse_calendar_due_at(date_str: str, time_str: str = "09:00") -> datetime:
    """Converte data/hora da agenda em `datetime` local sem timezone para PostgreSQL."""
    clean_date = (date_str or "").strip()
    clean_time = (time_str or "09:00").strip() or "09:00"
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", clean_date):
        raise ValueError("Data inválida.")
    if not re.match(r"^\d{2}:\d{2}$", clean_time):
        raise ValueError("Horário inválido.")
    return datetime.strptime(f"{clean_date} {clean_time}", "%Y-%m-%d %H:%M")

def calendar_event_owner(event: dict, user: dict) -> bool:
    """Valida se o usuário pode concluir, remarcar ou cancelar uma tarefa de agenda."""
    if not event or not user:
        return False
    return bool(
        can_write_admin_panel(user)
        or event.get("user_name") == user.get("name")
        or event.get("assigned_to") == user.get("name")
    )

async def check_rate_limit(key: str, limit: int, window: int) -> bool:
    """Controla taxa de uso no Redis e retorna `True` quando o limite estoura."""
    current = await redis_client.get(key)
    if current and int(current) >= limit:
        return True
    async with redis_client.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, window, nx=True)
        await pipe.execute()
    return False

def is_business_hours():
    """Fase 3: usa o expediente configurado do tenant atual (fallback 08:00-18:00)."""
    return tc.is_business_hours()

def seconds_until_business_hours(max_sleep: int = 900) -> int:
    """Calcula uma pausa curta até a próxima janela operacional do tenant."""
    agora = datetime.now(BR_TIMEZONE)
    open_m, close_m, _, _ = tc.business_hours()
    abertura = agora.replace(hour=open_m // 60, minute=open_m % 60, second=0, microsecond=0)
    fechamento = agora.replace(hour=close_m // 60, minute=close_m % 60, second=0, microsecond=0)
    if abertura <= agora <= fechamento:
        return 60
    if agora > fechamento:
        abertura += timedelta(days=1)
    seconds = int((abertura - agora).total_seconds())
    return max(60, min(seconds, max_sleep))

def get_seller_auto_status(now: datetime = None) -> str:
    """Calcula status das vendedoras: online, almoço ausente ou offline (expediente do tenant)."""
    agora = now or datetime.now(BR_TIMEZONE)
    minutos = agora.hour * 60 + agora.minute
    open_m, close_m, lunch_start, lunch_end = tc.business_hours()
    if open_m <= minutos <= lunch_start:
        return "online"
    if lunch_start < minutos <= lunch_end:
        return "ausente"
    if lunch_end < minutos <= close_m:
        return "online"
    return "offline"

def next_business_morning_timestamp() -> float:
    """Retorna o timestamp da próxima abertura de atendimento (expediente do tenant)."""
    agora = datetime.now(BR_TIMEZONE)
    open_m, _, _, _ = tc.business_hours()
    proxima_abertura = agora.replace(hour=open_m // 60, minute=open_m % 60, second=0, microsecond=0)
    if agora >= proxima_abertura:
        proxima_abertura += timedelta(days=1)
    return proxima_abertura.timestamp()

async def resolve_pending_assignee(user: dict, session_id: str) -> str:
    """Resolve o responsável que verá no painel um contato recebido fora do horário."""
    if session_id in ["financeiro", "engenharia"]:
        return session_id
    if user and user.get("assigned_to") and user.get("assigned_to") not in ["Não Atribuído", "Nao Atribuido"]:
        return user["assigned_to"]
    sales_users = await db.get_sales_users()
    sales_names = [u["name"] for u in sales_users if u.get("name")]
    return random.choice(sales_names) if sales_names else "vendas_1"

def off_hours_reply_ttl() -> int:
    """Calcula por quanto tempo o contato não deve receber novo aviso fora do horário."""
    seconds = int(next_business_morning_timestamp() - time.time()) + OFF_HOURS_REPLY_EXTRA_SECONDS
    return max(30 * 60, min(seconds, 18 * 60 * 60))

def should_enforce_automation_pause() -> bool:
    """Restringe pausas anti-loop ao periodo fora do horario comercial."""
    return not is_business_hours()

def decode_redis_value(value) -> str:
    """Converte valores do Redis para texto seguro em respostas administrativas."""
    if isinstance(value, bytes):
        return value.decode("utf-8", "ignore")
    return str(value or "")

def normalize_redis_ttl(ttl: int) -> int:
    """Normaliza TTL do Redis para exposicao em endpoints de observabilidade."""
    return ttl if isinstance(ttl, int) and ttl > 0 else 0

async def is_contact_automation_suppressed(phone: str) -> bool:
    """Indica se respostas automáticas foram pausadas temporariamente para um contato."""
    return bool(phone and await redis_client.exists(f"bot:suppressed:{phone}"))

async def is_global_automation_suppressed() -> bool:
    """Indica se o disjuntor global de automações está ativo."""
    return bool(await redis_client.exists("bot:global_suppressed"))

async def suppress_global_automation(reason: str):
    """Pausa temporariamente todas as respostas automáticas após pico anormal."""
    created = await redis_client.set(
        "bot:global_suppressed",
        reason or "global_auto_reply_limit",
        ex=GLOBAL_AUTOMATION_SUPPRESSION_SECONDS,
        nx=True,
    )
    if not created:
        return

    payload = {
        "type": "automation_global_suppressed",
        "reason": reason or "global_auto_reply_limit",
        "title": "Automação pausada globalmente",
        "text": "Pico anormal de respostas automáticas detectado. O bot foi pausado temporariamente para evitar sobrecarga.",
        "ttl_seconds": GLOBAL_AUTOMATION_SUPPRESSION_SECONDS,
    }
    logger.warning(
        "[BOT_GLOBAL_AUTOMATION_SUPPRESSED] reason={} limit={}/{}s ttl={}s",
        reason,
        GLOBAL_AUTO_REPLY_LIMIT,
        GLOBAL_AUTO_REPLY_WINDOW_SECONDS,
        GLOBAL_AUTOMATION_SUPPRESSION_SECONDS,
    )
    await manager.broadcast(json.dumps(payload, ensure_ascii=True))
    await manager.broadcast("update")

    admin_phone = normalize_whatsapp_jid(tc.admin_phone())
    if admin_phone:
        alert = (
            "*Alerta do continnumOS*\n"
            "Disjuntor global acionado: pico anormal de respostas automaticas.\n"
            f"Motivo: {reason or 'limite global'}.\n"
            f"As automacoes ficam pausadas por {GLOBAL_AUTOMATION_SUPPRESSION_SECONDS // 60} minutos; mensagens manuais seguem liberadas."
        )
        await evo.send_message(admin_phone, alert, await db.get_tenant_instance(get_current_tenant()))

async def suppress_contact_automation(phone: str, session_id: str, reason: str, user: dict = None):
    """Pausa respostas automáticas de um contato e avisa a operação para intervenção humana."""
    if not phone:
        return
    created = await redis_client.set(
        f"bot:suppressed:{phone}",
        reason or "auto_reply_limit",
        ex=BOT_SUPPRESSION_SECONDS,
        nx=True,
    )
    if not created:
        return

    contact = user or await db.get_user(phone)
    assigned_to = await resolve_pending_assignee(contact, session_id)
    if contact and contact.get("stage") != "em_atendimento":
        await db.update_user_stage(phone, "pendente_assumir", assigned_to)

    payload = {
        "type": "automation_suppressed",
        "phone": phone,
        "session_id": session_id,
        "assigned_to": assigned_to,
        "reason": reason or "auto_reply_limit",
        "title": "Automação pausada",
        "text": f"Possível loop de mensagens detectado em {clean_phone_display(phone)}. Atendimento enviado para revisão humana.",
    }
    logger.warning(
        "[BOT_AUTOMATION_SUPPRESSED] phone={} session={} assigned_to={} reason={} ttl={}s",
        phone,
        session_id,
        assigned_to,
        reason,
        BOT_SUPPRESSION_SECONDS,
    )
    await manager.broadcast(json.dumps(payload, ensure_ascii=True))
    await manager.broadcast("update")

    admin_phone = normalize_whatsapp_jid(tc.admin_phone())
    if admin_phone and admin_phone != phone:
        alert = (
            "*Alerta do continnumOS*\n"
            f"Automacao pausada para {clean_phone_display(phone)}.\n"
            f"Motivo: {reason or 'limite de respostas automaticas'}.\n"
            "Verifique o atendimento no painel para evitar loop entre bots."
        )
        await evo.send_message(admin_phone, alert, await db.get_tenant_instance(get_current_tenant()))

def normalize_loop_text(text: str) -> str:
    """Normaliza uma mensagem para comparar repetição sem depender de maiúsculas/espaços."""
    clean = re.sub(r"\s+", " ", (text or "").strip().lower())
    return clean[:180]

async def record_bot_loop_event(phone: str, actor: str, text: str):
    """Guarda uma memória curta da conversa para detectar ping-pong entre automações."""
    if not phone or actor not in {"client", "bot"}:
        return
    event = {
        "ts": time.time(),
        "actor": actor,
        "text_hash": hashlib.sha1(normalize_loop_text(text).encode("utf-8")).hexdigest(),
    }
    key = f"bot:loop_events:{phone}"
    await redis_client.lpush(key, json.dumps(event, ensure_ascii=True))
    await redis_client.ltrim(key, 0, BOT_LOOP_EVENT_LIMIT - 1)
    await redis_client.expire(key, BOT_LOOP_WINDOW_SECONDS * 3)

async def detect_bot_loop_risk(phone: str, session_id: str, current_text: str, user: dict = None) -> bool:
    """Detecta volume, repetição e alternância compatíveis com conversa entre bots."""
    if not phone:
        return False
    if not should_enforce_automation_pause():
        return False
    if await is_contact_automation_suppressed(phone):
        return True

    key = f"bot:loop_events:{phone}"
    raw_events = await redis_client.lrange(key, 0, BOT_LOOP_EVENT_LIMIT - 1)
    now = time.time()
    events = []
    for raw in raw_events:
        try:
            item = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        except Exception:
            continue
        if now - float(item.get("ts", 0)) <= BOT_LOOP_WINDOW_SECONDS:
            events.append(item)

    client_events = [item for item in events if item.get("actor") == "client"]
    bot_events = [item for item in events if item.get("actor") == "bot"]
    current_hash = hashlib.sha1(normalize_loop_text(current_text).encode("utf-8")).hexdigest()
    repeated_current = sum(1 for item in client_events if item.get("text_hash") == current_hash)

    ordered = sorted(events, key=lambda item: item.get("ts", 0))
    switches = 0
    previous_actor = ""
    for item in ordered:
        actor = item.get("actor")
        if previous_actor and actor and actor != previous_actor:
            switches += 1
        if actor:
            previous_actor = actor

    reason = ""
    if len(client_events) >= BOT_LOOP_INBOUND_LIMIT and len(bot_events) >= 2:
        reason = f"high_volume:{len(client_events)}in/{len(bot_events)}bot"
    elif repeated_current >= BOT_LOOP_REPEAT_LIMIT and bot_events:
        reason = f"repeated_text:{repeated_current}"
    elif switches >= BOT_LOOP_ALTERNATION_SWITCHES and len(client_events) >= 4 and len(bot_events) >= 4:
        reason = f"alternating:{switches}"

    if not reason:
        return False

    logger.warning(
        "[BOT_LOOP_DETECTED] phone={} session={} reason={} client_events={} bot_events={} switches={}",
        phone,
        session_id,
        reason,
        len(client_events),
        len(bot_events),
        switches,
    )
    await suppress_contact_automation(phone, session_id, f"loop_detected:{reason}", user)
    return True

async def can_send_automated_reply(phone: str, session_id: str, reason: str, user: dict = None) -> bool:
    """Aplica pausas anti-loop somente fora do expediente comercial."""
    if not phone:
        return False
    if not should_enforce_automation_pause():
        contact_paused = await is_contact_automation_suppressed(phone)
        global_paused = await is_global_automation_suppressed()
        if contact_paused or global_paused:
            logger.info(
                "[BOT_AUTOMATION_ALLOWED_BUSINESS_HOURS] phone={} session={} reason={} contact_paused={} global_paused={}",
                phone,
                session_id,
                reason,
                contact_paused,
                global_paused,
            )
        return True

    if await is_contact_automation_suppressed(phone):
        logger.info("[BOT_AUTOMATION_SKIPPED] phone={} session={} reason={} status=suppressed", phone, session_id, reason)
        return False

    if await is_global_automation_suppressed():
        logger.info("[BOT_AUTOMATION_SKIPPED] phone={} session={} reason={} status=global_suppressed", phone, session_id, reason)
        return False

    if reason == "off_hours":
        cooldown_key = f"bot:offhours_reply:{phone}"
        first_reply = await redis_client.set(cooldown_key, "1", ex=off_hours_reply_ttl(), nx=True)
        if not first_reply:
            logger.info("[BOT_AUTOMATION_SKIPPED] phone={} session={} reason=off_hours status=cooldown", phone, session_id)
            return False

    if await check_rate_limit("bot:auto_reply:global", limit=GLOBAL_AUTO_REPLY_LIMIT, window=GLOBAL_AUTO_REPLY_WINDOW_SECONDS):
        await suppress_global_automation(f"global_auto_reply_limit:{reason}")
        return False

    if await check_rate_limit(f"bot:auto_reply:{phone}", limit=AUTO_REPLY_LIMIT, window=AUTO_REPLY_WINDOW_SECONDS):
        await suppress_contact_automation(phone, session_id, f"auto_reply_limit:{reason}", user)
        return False
    return True

async def build_automation_guard_status(limit: int = 50) -> dict:
    """Monta retrato administrativo das protecoes anti-loop de automacao."""
    global_reason = decode_redis_value(await redis_client.get("bot:global_suppressed"))
    global_count_raw = decode_redis_value(await redis_client.get("bot:auto_reply:global"))
    global_ttl = normalize_redis_ttl(await redis_client.ttl("bot:global_suppressed"))
    global_window_ttl = normalize_redis_ttl(await redis_client.ttl("bot:auto_reply:global"))

    paused_contacts = []
    async for raw_key in redis_client.scan_iter(match="bot:suppressed:*", count=100):
        key = decode_redis_value(raw_key)
        phone = key.split("bot:suppressed:", 1)[-1]
        reason = decode_redis_value(await redis_client.get(key))
        ttl = normalize_redis_ttl(await redis_client.ttl(key))
        contact = await db.get_user(phone)
        paused_contacts.append(
            {
                "phone": phone,
                "display_phone": clean_phone_display(phone),
                "name": contact.get("name") if contact else "",
                "stage": contact.get("stage") if contact else "",
                "assigned_to": contact.get("assigned_to") if contact else "",
                "session_id": contact.get("session_id") if contact else "",
                "reason": reason,
                "ttl_seconds": ttl,
            }
        )
        if len(paused_contacts) >= limit:
            break

    paused_contacts.sort(key=lambda item: item.get("ttl_seconds", 0), reverse=True)
    return {
        "business_hours": is_business_hours(),
        "suppression_enforced": should_enforce_automation_pause(),
        "checked_at": datetime.now(BR_TIMEZONE).isoformat(timespec="seconds"),
        "global": {
            "active": bool(global_reason),
            "reason": global_reason,
            "ttl_seconds": global_ttl,
            "window_count": parse_int_or_zero(global_count_raw),
            "window_ttl_seconds": global_window_ttl,
            "limit": GLOBAL_AUTO_REPLY_LIMIT,
            "window_seconds": GLOBAL_AUTO_REPLY_WINDOW_SECONDS,
            "suppression_seconds": GLOBAL_AUTOMATION_SUPPRESSION_SECONDS,
        },
        "contacts": paused_contacts,
        "limits": {
            "contact_limit": AUTO_REPLY_LIMIT,
            "contact_window_seconds": AUTO_REPLY_WINDOW_SECONDS,
            "contact_suppression_seconds": BOT_SUPPRESSION_SECONDS,
            "loop_window_seconds": BOT_LOOP_WINDOW_SECONDS,
            "loop_inbound_limit": BOT_LOOP_INBOUND_LIMIT,
            "loop_repeat_limit": BOT_LOOP_REPEAT_LIMIT,
            "loop_alternation_switches": BOT_LOOP_ALTERNATION_SWITCHES,
            "off_hours_extra_seconds": OFF_HOURS_REPLY_EXTRA_SECONDS,
        },
    }

async def send_automated_message(
    phone: str,
    text: str,
    session_id: str,
    reason: str,
    user: dict = None,
    enforce_guard: bool = True,
) -> str:
    """Envia mensagem automática, salva histórico e alimenta o detector anti-loop."""
    if enforce_guard and not await can_send_automated_reply(phone, session_id, reason, user):
        return ""
    wid = await evo.send_message(phone, text, session_id)
    if wid:
        await db.save_message(phone, "bot", text, "text", str(wid), 1, session_id)
        await record_bot_loop_event(phone, "bot", text)
    return str(wid or "")

async def cleanup_media_files(media_types: list, days_old: int = 30):
    """Remove mídias antigas de `/data/midias` conforme tipo e idade em dias."""
    # Fase 5: no backend s3/r2 a retenção é gerenciada por lifecycle do bucket.
    if storage.backend() != "local":
        return {"status": "skipped", "message": "Backend de storage não é local; retenção gerenciada pelo bucket."}
    media_dir = storage.MEDIA_ROOT
    if not os.path.exists(media_dir):
        return {"status": "error", "message": "Diretório não encontrado."}

    ext_map = {"image": [".jpg", ".jpeg", ".png"], "audio": [".ogg", ".mp3", ".wav", ".aac"], "video": [".mp4", ".mov"], "document": [".pdf", ".doc", ".docx"]}
    target_exts = []
    for m_type in media_types:
        if m_type in ext_map: 
            target_exts.extend(ext_map[m_type])

    now = time.time()
    cutoff_time = now - (days_old * 86400)
    deleted_count = 0
    freed_space_bytes = 0

    try:
        # Fase 5: mídia agora fica namespaceada por tenant (tN/...); varre recursivamente.
        for root, _dirs, files in os.walk(media_dir):
            for filename in files:
                file_path = os.path.join(root, filename)
                if not os.path.isfile(file_path):
                    continue
                _, ext = os.path.splitext(filename)
                if ext.lower() in target_exts:
                    if os.path.getmtime(file_path) < cutoff_time:
                        freed_space_bytes += os.path.getsize(file_path)
                        os.remove(file_path)
                        deleted_count += 1

        freed_mb = round(freed_space_bytes / (1024 * 1024), 2)
        logger.info(f"Limpeza de mídia concluída: {deleted_count} apagados. {freed_mb} MB liberados.")
        return {"status": "success", "message": f"{deleted_count} apagados. {freed_mb} MB liberados."}
    except Exception as e:
        logger.error(f"Erro na limpeza de mídias: {e}")
        return {"status": "error", "message": str(e)}

async def media_cleanup_loop():
    """Executa a limpeza automática diária de mídias antigas."""
    while True:
        try:
            now = datetime.now(timezone(timedelta(hours=-3)))
            if now.hour == 3 and now.minute == 0:
                async with redis_lock(redis_client, "job:media_cleanup", 120) as acquired:
                    if acquired:
                        await cleanup_media_files(["image", "audio", "video", "document"], 30)
                await asyncio.sleep(60)
            else: 
                await asyncio.sleep(30)
        except Exception as e: 
            logger.error(f"Erro no loop de limpeza: {e}")
            await asyncio.sleep(30)

async def schedule_sla(phone: str, role_name: str, session_id: str):
    """Agenda um alerta de SLA para um atendimento pendente no Redis."""
    execute_at = time.time() + 300
    await redis_client.zadd("sla_queue", {f"{phone}:{role_name}:{session_id}": execute_at})

async def schedule_morning_followup(phone: str, assigned_to: str, session_id: str):
    """Agenda no Redis a mensagem de retomada da próxima manhã às 08:00."""
    execute_at = next_business_morning_timestamp()
    await redis_client.zadd("morning_followup_queue", {f"{phone}:{assigned_to}:{session_id}": execute_at})

async def sla_monitor_loop():
    """Processa a fila de SLA e envia aviso quando um lead demora a ser assumido."""
    while True:
        try:
            now = time.time()
            tasks = await redis_client.zrangebyscore("sla_queue", 0, now)
            for task_raw in tasks:
                task = task_raw.decode("utf-8") if isinstance(task_raw, bytes) else task_raw
                phone, role_name, session_id = task.split(":", 2)
                removed = await redis_client.zrem("sla_queue", task)
                if not removed:
                    continue
                user = await db.get_user(phone)
                if user and user['stage'] == 'pendente_assumir':
                    msg = "Nossa equipe está em atendimento no momento. Por favor, aguarde que logo retornaremos."
                    evo_id = await send_automated_message(phone, msg, session_id, "sla_waiting", user)
                    if evo_id:
                        logger.info(f"Alerta de SLA enviado para {phone}")
            await asyncio.sleep(10)
        except Exception as e:
            logger.error(f"Erro no monitor de SLA: {e}")
            await asyncio.sleep(10)

async def morning_followup_loop():
    """Envia retomada matinal para contatos que ainda estão pendentes."""
    while True:
        try:
            now = time.time()
            tasks = await redis_client.zrangebyscore("morning_followup_queue", 0, now)
            for task_raw in tasks:
                task = task_raw.decode("utf-8") if isinstance(task_raw, bytes) else task_raw
                phone, assigned_to, session_id = task.split(":", 2)
                removed = await redis_client.zrem("morning_followup_queue", task)
                if not removed:
                    continue
                user = await db.get_user(phone)
                if not user or user.get("stage") != "pendente_assumir":
                    continue
                evo_id = await send_automated_message(phone, tc.morning_followup_message(), session_id, "morning_followup", user)
                if not evo_id:
                    continue
                logger.info(f"Mensagem de retomada matinal enviada para {phone} ({assigned_to}).")
                await manager.broadcast(f"chat_{phone}")
                await manager.broadcast("update")
            await asyncio.sleep(30)
        except Exception as e:
            logger.error(f"Erro no monitor de retorno matinal: {e}")
            await asyncio.sleep(30)

async def calendar_reminder_loop():
    """Dispara lembretes da agenda durante horário comercial até conclusão/remarcação."""
    while True:
        try:
            if not is_business_hours():
                await asyncio.sleep(seconds_until_business_hours())
                continue
            async with redis_lock(redis_client, "job:calendar_reminders", 55) as acquired:
                if acquired:
                    events = await db.get_due_calendar_events()
                    for event in events:
                        event_id = event.get("id")
                        reminder_key = f"calendar_reminder:{event_id}"
                        can_notify = await redis_client.set(
                            reminder_key,
                            "1",
                            ex=CALENDAR_REMINDER_INTERVAL_SECONDS,
                            nx=True,
                        )
                        if not can_notify:
                            continue
                        due_at = event.get("due_at")
                        payload = {
                            "type": "calendar_reminder",
                            "event_id": event_id,
                            "title": "Agenda pendente",
                            "text": event.get("note") or "Tarefa de agenda pendente.",
                            "phone": event.get("phone") or "",
                            "assigned_to": event.get("assigned_to") or event.get("user_name") or "",
                            "user_name": event.get("user_name") or "",
                            "due_at": due_at.isoformat() if due_at else "",
                        }
                        await manager.broadcast(json.dumps(payload, ensure_ascii=True))
                        logger.info("Lembrete de agenda enviado: id={} assigned_to={}", event_id, payload["assigned_to"])
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Erro no monitor de agenda inteligente: {e}")
            await asyncio.sleep(60)

async def seller_status_schedule_loop():
    """Sincroniza a presença das vendedoras com as faixas 08-12, almoço e 13-18."""
    while True:
        try:
            async with redis_lock(redis_client, "job:seller_status_schedule", 55) as acquired:
                if acquired:
                    desired_status = get_seller_auto_status()
                    changed = False
                    sys_users = await db.get_all_system_users()
                    for seller in sys_users:
                        if seller.get("role") not in ["vendas_1", "vendas_2"] or not seller.get("is_active", 1):
                            continue
                        seller_name = seller["name"]
                        current_status = await db.get_seller_status(seller_name)
                        if current_status != desired_status:
                            await db.set_seller_status(seller_name, desired_status)
                            changed = True
                            logger.info(f"Status automático atualizado: {seller_name} -> {desired_status}")
                    if changed:
                        await manager.broadcast("update")
            await asyncio.sleep(60)
        except Exception as e:
            logger.error(f"Erro no agendamento automático de status: {e}")
            await asyncio.sleep(60)

async def system_monitor_loop():
    """Monitora CPU/memória e alerta o administrador quando há sobrecarga."""
    while True:
        try:
            async with redis_lock(redis_client, "job:system_monitor", 290) as acquired:
                if acquired:
                    mem_usage = psutil.virtual_memory().percent
                    cpu_usage = psutil.cpu_percent(interval=1)
                    admin_phone = normalize_whatsapp_jid(tc.admin_phone())
                    if admin_phone and (mem_usage > 90 or cpu_usage > 95):
                        can_alert = await redis_client.set("alert:system_overload", "1", ex=900, nx=True)
                        if can_alert:
                            msg = f"*Alerta do Servidor*\nUso de Memória: {mem_usage}%\nUso de CPU: {cpu_usage}%\nPossível sobrecarga detectada."
                            await evo.send_message(admin_phone, msg, await db.get_tenant_instance(get_current_tenant()))
                            logger.warning(f"Alerta de sistema enviado ao admin: Mem={mem_usage}%, CPU={cpu_usage}%")
            await asyncio.sleep(300)
        except Exception as e:
            logger.error(f"Erro no monitor de sistema: {e}")
            await asyncio.sleep(300)

async def billing_enforcement_loop():
    """Fase 6: suspensão automática por inadimplência.

    Periodicamente marca faturas vencidas (open -> past_due, respeitando o grace
    period) e suspende tenants ativos com débito vencido. Idempotente e protegido
    por lock distribuído (seguro com múltiplas réplicas).
    """
    while True:
        try:
            if not settings.BILLING_ENFORCEMENT_ENABLED:
                await asyncio.sleep(3600)
                continue
            async with redis_lock(redis_client, "job:billing_enforcement", 300) as acquired:
                if acquired:
                    overdue = await db.mark_overdue_invoices(settings.BILLING_GRACE_DAYS)
                    if overdue:
                        logger.info(f"[BILLING] {overdue} fatura(s) marcada(s) como past_due.")
                    tenants = await db.get_tenants_with_overdue()
                    for tid in tenants:
                        await db.set_tenant_status(tid, "suspended")
                        logger.warning(f"[BILLING] Tenant {tid} suspenso por inadimplência.")
            await asyncio.sleep(3600)
        except Exception as e:
            logger.error(f"Erro no enforcement de billing: {e}")
            await asyncio.sleep(3600)

async def data_retention_loop():
    """Fase 8 (LGPD): limpeza automática de dados por política de retenção.

    Diariamente, para cada tenant, apaga mensagens mais antigas que
    DATA_RETENTION_DAYS. Idempotente e protegido por lock distribuído (seguro
    com múltiplas réplicas). Desativado quando DATA_RETENTION_DAYS <= 0.
    """
    while True:
        try:
            days = int(getattr(settings, "DATA_RETENTION_DAYS", 0) or 0)
            if days <= 0:
                await asyncio.sleep(24 * 3600)
                continue
            async with redis_lock(redis_client, "job:data_retention", 600) as acquired:
                if acquired:
                    total = 0
                    for t in await db.list_tenants():
                        tid = t.get("id") if isinstance(t, dict) else t
                        prev = get_current_tenant()
                        try:
                            set_current_tenant(tid)
                            total += await db.purge_old_messages(days)
                        finally:
                            set_current_tenant(prev)
                    if total:
                        logger.info(f"[LGPD] Retenção: {total} mensagem(ns) expiradas removidas.")
            await asyncio.sleep(24 * 3600)
        except Exception as e:
            logger.error(f"Erro no loop de retenção (LGPD): {e}")
            await asyncio.sleep(24 * 3600)


SCHEDULER_LOCK_KEY = "continnumos:scheduler:leader"
SCHEDULER_LOCK_TTL = 90


async def _acquire_scheduler_lock():
    """Elege um unico worker lider para os loops de fundo (single-flight/Redis).

    Com N workers, apenas um deve rodar os loops periodicos para nao duplicar
    follow-ups, lembretes e limpezas. Se o Redis falhar, assume lideranca para
    nao deixar o sistema sem scheduler.
    """
    try:
        got = await redis_client.set(SCHEDULER_LOCK_KEY, str(os.getpid()), nx=True, ex=SCHEDULER_LOCK_TTL)
        return bool(got)
    except Exception as exc:
        logger.warning("Falha ao adquirir lock do scheduler ({}); assumindo lider por seguranca.", exc)
        return True


async def _scheduler_lock_keeper():
    """Renova o lock de lideranca periodicamente enquanto este worker viver."""
    while True:
        try:
            await asyncio.sleep(SCHEDULER_LOCK_TTL // 3)
            await redis_client.set(SCHEDULER_LOCK_KEY, str(os.getpid()), ex=SCHEDULER_LOCK_TTL)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Falha ao renovar lock do scheduler: {}", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Inicializa conexões e tarefas de fundo no ciclo de vida do FastAPI."""
    logger.info("Iniciando a aplicação...")
    # Fase 7: alertas (Sentry) e tracing (OpenTelemetry) — opcionais/guardados.
    observability.init_sentry()
    observability.init_tracing(app)
    # Fase 5: falha cedo se o storage s3 estiver mal configurado.
    settings.validate_storage()
    logger.info(f"Storage backend ativo: {storage.backend()}")
    await db.init_db()
    # Fase 3: aquece a config do tenant padrão para leituras síncronas (horário, etc.).
    await tc.load_tenant_config(DEFAULT_TENANT_ID)
    # Quick wins + Fases 10-13: migracoes de boot idempotentes, serializadas por
    # advisory lock para nao correr entre workers (evita UniqueViolationError em
    # pg_type no primeiro boot com varios workers).
    await db.create_all_phase_tables()
    # WebSocket/realtime replica via Redis pub/sub — precisa rodar em TODA réplica.
    asyncio.create_task(manager.listen())
    # Fase 7: os loops periódicos rodam em UMA única réplica/scheduler para não
    # duplicar follow-ups, lembretes e limpezas quando houver N réplicas web.
    # Em deploy escalado, deixe RUN_BACKGROUND_JOBS=true só no scheduler.
    if settings.RUN_BACKGROUND_JOBS and await _acquire_scheduler_lock():
        logger.info("[SCHEDULER] Loops de fundo ATIVOS neste worker (lider eleito).")
        asyncio.create_task(_scheduler_lock_keeper())
        asyncio.create_task(media_cleanup_loop())
        asyncio.create_task(sla_monitor_loop())
        asyncio.create_task(morning_followup_loop())
        asyncio.create_task(calendar_reminder_loop())
        asyncio.create_task(seller_status_schedule_loop())
        asyncio.create_task(system_monitor_loop())
        asyncio.create_task(billing_enforcement_loop())
        asyncio.create_task(data_retention_loop())
        asyncio.create_task(scheduled_messages_loop())
        asyncio.create_task(cadence_loop())
        asyncio.create_task(team_sla_loop())
    elif settings.RUN_BACKGROUND_JOBS:
        logger.info("[SCHEDULER] Loops de fundo NAO iniciados neste worker (outro worker e o lider).")
    else:
        logger.info("[SCHEDULER] Loops de fundo DESATIVADOS (RUN_BACKGROUND_JOBS=false).")
    yield
    logger.info("Encerrando a aplicação...")
    await db.close_db()
    await evo.client.aclose()
    await redis_client.close()

app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/midias", StaticFiles(directory=storage.MEDIA_ROOT), name="midias")


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Fase 5 (Observabilidade): correlação de request e access-log estruturado.

    Gera/propaga um ``X-Request-ID`` e registra método, rota, status e latência
    com o tenant corrente, facilitando rastrear uma requisição ponta a ponta em
    logs centralizados.
    """
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.exception(
            "[HTTP_ERROR] rid={} method={} path={} elapsed_ms={:.1f}",
            request_id, request.method, request.url.path, elapsed_ms,
        )
        # Fase 7: contabiliza a falha nas métricas (status 500).
        observability.record_request(request.method, request.url.path, 500, elapsed_ms / 1000)
        raise
    elapsed_ms = (time.perf_counter() - started) * 1000
    try:
        current_tenant = get_current_tenant()
    except Exception:
        current_tenant = None
    if request.url.path.startswith("/api/") or request.url.path in {"/webhook", "/login"}:
        logger.info(
            "[HTTP] rid={} tenant={} method={} path={} status={} elapsed_ms={:.1f}",
            request_id, current_tenant, request.method, request.url.path,
            response.status_code, elapsed_ms,
        )
    # Fase 7: métricas Prometheus (contador de requests + histograma de latência).
    observability.record_request(
        request.method, request.url.path, response.status_code, elapsed_ms / 1000
    )
    response.headers["X-Request-ID"] = request_id
    return response


@app.get("/metrics", include_in_schema=False)
async def metrics_endpoint(request: Request):
    """Fase 7: exposição Prometheus. Protegida por token opcional (METRICS_TOKEN)."""
    if not settings.METRICS_ENABLED:
        return Response(status_code=404)
    if settings.METRICS_TOKEN:
        auth = request.headers.get("authorization", "")
        token = auth.replace("Bearer ", "").strip()
        if not hmac.compare_digest(token, settings.METRICS_TOKEN):
            return JSONResponse(status_code=401, content={"detail": "Não autorizado"})
    return Response(content=observability.render_metrics(), media_type=observability.CONTENT_TYPE)

@app.get("/service-worker.js", include_in_schema=False)
async def service_worker():
    """Entrega o service worker no escopo raiz exigido pela instalação PWA."""
    return FileResponse(
        "static/service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )

# Fase 0 (Hardening): sem wildcard. Origens explícitas via ALLOWED_ORIGINS.
# Combinar allow_origins=["*"] com allow_credentials=True é inválido/inseguro.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

@app.middleware("http")
async def accept_legacy_json_fetches(request: Request, call_next):
    """Aceita fetches antigos com JSON em `text/plain` nos endpoints internos."""
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.url.path.startswith("/api/"):
        headers = list(request.scope.get("headers", []))
        normalized_headers = []
        changed = False
        for key, value in headers:
            if key.lower() == b"content-type" and value.lower().startswith(b"text/plain"):
                normalized_headers.append((key, b"application/json"))
                changed = True
            else:
                normalized_headers.append((key, value))
        if changed:
            request.scope["headers"] = normalized_headers
    return await call_next(request)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Canal realtime usado pelos dashboards para receber atualizações."""
    await manager.connect(websocket)
    try:
        while True: 
            await websocket.receive_text()
    except WebSocketDisconnect: 
        manager.disconnect(websocket)

@app.get("/health")
async def healthcheck():
    """Retorna saúde operacional básica para monitoramento externo."""
    ops = await build_operations_status()
    status_label = ops["status"]
    # Liveness: o container esta vivo enquanto o processo responde e o banco
    # esta acessivel. Dependencias externas degradadas (ex.: Evolution/WhatsApp
    # desconectado) NAO devem retornar 503 e reiniciar o container em loop.
    db_ok = bool(ops["checks"].get("database", {}).get("ok"))
    status_code = 200 if db_ok else 503
    return JSONResponse(
        status_code=status_code,
        content={
            "status": status_label,
            "uptime_seconds": ops["system"]["uptime_seconds"],
            "checks": ops["checks"],
            "queues": {
                "sla": ops["queues"].get("sla"),
                "morning_followup": ops["queues"].get("morning_followup"),
            },
        },
    )

@app.get("/api/admin/operations_status")
async def admin_operations_status(user: dict = Depends(get_current_user)):
    """Retorna status operacional para admin e financeiro com leitura administrativa."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await build_operations_status()

@app.get("/api/admin/automation_guard")
async def admin_automation_guard(user: dict = Depends(get_current_user)):
    """Retorna o estado das protecoes anti-loop de automacao."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await build_automation_guard_status()

@app.get("/", response_class=HTMLResponse)
async def root(): 
    """Redireciona a raiz da aplicação para a tela de login."""
    return RedirectResponse(url="/login")

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request): 
    """Renderiza o formulário de autenticação."""
    return templates.TemplateResponse(request=request, name="login.html", context={"request": request})

@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    """Valida credenciais, aplica rate limit e grava o JWT em cookie HTTP-only."""
    client_ip = request.client.host if request.client else "unknown"
    if await check_rate_limit(f"rate:login:{client_ip}", limit=5, window=60):
        logger.warning(f"Rate limit acionado para login IP: {client_ip}")
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "Muitas tentativas! Aguarde."})
    
    # Fase 1 (Multi-tenancy): resolve e ativa o tenant antes de validar as
    # credenciais, para que a busca do usuário seja escopada por RLS/GUC.
    tenant_id = await db.resolve_request_tenant_id(request)
    set_current_tenant(tenant_id)
    await tc.load_tenant_config(tenant_id)
    # Fase 4: bloqueia login de tenants suspensos.
    if await db.get_tenant_status(tenant_id) != "active":
        logger.warning(f"Login bloqueado: tenant {tenant_id} suspenso.")
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "Conta suspensa. Fale com o suporte."})
    user = await auth.verify_user(username, password)
    if not user: 
        logger.info(f"Falha de login para o usuário: {username}")
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": "Credenciais Inválidas"})
        
    session_id, session_error = await auth.register_login_session(username, user.get("role"))
    if session_error:
        logger.warning(f"Login bloqueado por sessão ativa: {username}")
        return templates.TemplateResponse(request=request, name="login.html", context={"request": request, "error": session_error})

    token = auth.create_access_token({"sub": username.strip().lower(), "jti": session_id})
    response = RedirectResponse(url="/painel", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=bool(getattr(settings, "COOKIE_SECURE", True)),
        samesite="Lax",
        max_age=auth.SESSION_TTL_SECONDS,
    )
    logger.info(f"Login bem-sucedido: {username}")
    return response

@app.get("/logout")
async def logout(request: Request):
    """Remove o cookie de sessão e retorna o usuário para o login."""
    await auth.clear_session_from_request(request)
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("session_token")
    return response

def _load_superadmin_html():
    import os
    _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "superadmin.html")
    with open(_p, encoding="utf-8") as _f:
        return _f.read()


SUPERADMIN_HTML = _load_superadmin_html()


def _superadmin_authorized(request: Request) -> bool:
    """Fase 4: valida o token do super-admin (header X-Superadmin-Token)."""
    token = getattr(settings, "SUPER_ADMIN_TOKEN", "") or ""
    if not token:
        return False
    provided = request.headers.get("X-Superadmin-Token", "") or ""
    return hmac.compare_digest(provided, token)


@app.get("/superadmin", response_class=HTMLResponse)
async def superadmin_page():
    """Fase 4: painel mínimo de gestão de clientes (autentica via token no navegador)."""
    return HTMLResponse(SUPERADMIN_HTML)


@app.get("/api/superadmin/tenants")
async def sa_list_tenants(request: Request):
    if not _superadmin_authorized(request):
        return JSONResponse(status_code=401, content={"error": "Não autorizado"})
    return {"tenants": await db.list_tenants(), "plans": await db.get_all_plans()}


@app.post("/api/superadmin/tenants")
async def sa_create_tenant(request: Request):
    if not _superadmin_authorized(request):
        return JSONResponse(status_code=401, content={"error": "Não autorizado"})
    body = await request.json()
    slug = (body.get("slug") or "").strip().lower()
    display_name = (body.get("display_name") or "").strip()
    if not slug or not display_name:
        return JSONResponse(status_code=400, content={"error": "slug e display_name são obrigatórios"})
    tenant = await db.create_tenant(
        slug=slug,
        display_name=display_name,
        domain=(body.get("domain") or None),
        evolution_instance=(body.get("evolution_instance") or None),
        plan=(body.get("plan") or "free"),
        brand_color=(body.get("brand_color") or "#FF6600"),
    )
    if not tenant:
        return JSONResponse(status_code=409, content={"error": "Slug já existe"})
    # Fase 7: enforcement de max_instances por plano antes de provisionar.
    ok, current, limit = await db.check_instance_limit(tenant["id"])
    if not ok:
        logger.warning(
            f"[LIMITE] Tenant {slug} atingiu max_instances ({current}/{limit}); "
            f"instância não provisionada."
        )
        return {
            "status": "ok",
            "tenant": tenant,
            "warning": f"Limite de instâncias do plano atingido ({current}/{limit}). "
                       f"Faça upgrade do plano para provisionar mais instâncias.",
            "provisioned": False,
        }
    try:
        await evo.ensure_instance(tenant["evolution_instance"])
    except Exception as e:
        logger.warning(f"Falha ao provisionar instância do tenant {slug}: {e}")
    return {"status": "ok", "tenant": tenant, "provisioned": True}


@app.post("/api/superadmin/tenants/{tenant_id}/plan")
async def sa_set_plan(tenant_id: int, request: Request):
    if not _superadmin_authorized(request):
        return JSONResponse(status_code=401, content={"error": "Não autorizado"})
    body = await request.json()
    await db.set_tenant_plan(tenant_id, plan_code=body.get("plan"), overrides=body.get("overrides"))
    tc.invalidate(tenant_id)
    return {"status": "ok", "usage": await db.get_tenant_usage(tenant_id)}


@app.post("/api/superadmin/tenants/{tenant_id}/suspend")
async def sa_suspend(tenant_id: int, request: Request):
    if not _superadmin_authorized(request):
        return JSONResponse(status_code=401, content={"error": "Não autorizado"})
    await db.set_tenant_status(tenant_id, "suspended")
    tc.invalidate(tenant_id)
    return {"status": "ok"}


@app.post("/api/superadmin/tenants/{tenant_id}/activate")
async def sa_activate(tenant_id: int, request: Request):
    if not _superadmin_authorized(request):
        return JSONResponse(status_code=401, content={"error": "Não autorizado"})
    await db.set_tenant_status(tenant_id, "active")
    tc.invalidate(tenant_id)
    return {"status": "ok"}


@app.get("/api/superadmin/tenants/{tenant_id}/usage")
async def sa_usage(tenant_id: int, request: Request):
    if not _superadmin_authorized(request):
        return JSONResponse(status_code=401, content={"error": "Não autorizado"})
    return await db.get_tenant_usage(tenant_id)


@app.get("/api/superadmin/tenants/{tenant_id}/invoices")
async def sa_list_invoices(tenant_id: int, request: Request):
    if not _superadmin_authorized(request):
        return JSONResponse(status_code=401, content={"error": "Não autorizado"})
    return {"invoices": await db.list_invoices(tenant_id)}


@app.post("/api/superadmin/tenants/{tenant_id}/invoices")
async def sa_create_invoice(tenant_id: int, request: Request):
    if not _superadmin_authorized(request):
        return JSONResponse(status_code=401, content={"error": "Não autorizado"})
    body = await request.json()
    amount = body.get("amount_cents")
    if amount is None:
        limits = await db.get_tenant_limits(tenant_id)
        plan = await db.get_plan(limits.get("plan", "free")) or {}
        amount = plan.get("price_cents", 0)
    invoice = await db.create_invoice(
        tenant_id, int(amount),
        period_start=body.get("period_start"),
        period_end=body.get("period_end"),
        due_date=body.get("due_date"),
    )
    return {"status": "ok", "invoice": invoice}


@app.post("/api/superadmin/invoices/{invoice_id}/pay")
async def sa_pay_invoice(invoice_id: int, request: Request):
    if not _superadmin_authorized(request):
        return JSONResponse(status_code=401, content={"error": "Não autorizado"})
    await db.mark_invoice_paid(invoice_id)
    return {"status": "ok"}


@app.post("/api/billing/webhook/{provider}")
async def billing_payment_webhook(provider: str, request: Request):
    """Fase 6: webhook de pagamento do gateway (Stripe/Mercado Pago/Asaas/manual).

    Verifica a assinatura do provedor, e ao confirmar um pagamento: marca a fatura
    como paga (por invoice_id no metadata ou pelo external_id da cobrança) e, se
    configurado, reativa automaticamente o tenant suspenso por inadimplência.
    Sempre responde 200 em eventos ignorados (evita retries desnecessários do
    gateway); responde 400 apenas quando a assinatura é inválida.
    """
    raw_body = await request.body()
    try:
        event = payments.verify_and_parse(dict(request.headers), raw_body, provider)
    except payments.PaymentWebhookError as exc:
        logger.warning(f"[BILLING] Webhook rejeitado ({provider}): {exc}")
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    if not event.paid:
        logger.info(f"[BILLING] Webhook {provider} ignorado (evento={event.event_type}).")
        return {"status": "ignored", "event": event.event_type}

    invoice = None
    if event.invoice_id is not None:
        await db.mark_invoice_paid(event.invoice_id)
        if event.external_id:
            await db.set_invoice_external_ref(event.invoice_id, event.provider, event.external_id)
        invoice = await db.get_invoice(event.invoice_id)
    elif event.external_id:
        invoice = await db.mark_invoice_paid_by_external_id(event.external_id, event.provider)

    if not invoice:
        logger.warning(
            f"[BILLING] Pagamento confirmado mas fatura não encontrada "
            f"(invoice_id={event.invoice_id}, external_id={event.external_id})."
        )
        return {"status": "paid_no_match"}

    reactivated = False
    if settings.BILLING_REACTIVATE_ON_PAYMENT:
        reactivated = await db.reactivate_tenant_if_paid(invoice["tenant_id"])
    logger.info(
        f"[BILLING] Fatura {invoice['id']} paga via {event.provider}; "
        f"tenant={invoice['tenant_id']} reativado={reactivated}."
    )
    return {"status": "ok", "invoice_id": invoice["id"], "reactivated": reactivated}


@app.get("/api/tenant/branding")
async def tenant_branding(request: Request):
    """Fase 3: expõe a marca (nome, cor, logo) do tenant resolvido pelo host.

    Endpoint público: usado pela tela de login antes da autenticação.
    """
    tenant_id = await db.resolve_request_tenant_id(request)
    await tc.load_tenant_config(tenant_id)
    return tc.brand(tenant_id)

@app.post("/api/admin/tenant/config")
async def update_tenant_config(request: Request, user: dict = Depends(get_current_user)):
    """Fase 3: atualiza configurações/marca do tenant atual (papéis/setores,
    horário comercial, admin_phone, textos do bot, cores, logo). Somente admin.
    """
    if not user or user.get("role") != "admin":
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    try:
        patch = await request.json()
    except Exception:
        patch = None
    if not isinstance(patch, dict) or not patch:
        return JSONResponse(status_code=400, content={"error": "Corpo inválido"})
    allowed = {
        "display_name", "brand_color", "logo_url", "admin_phone",
        "role_session_map", "departamentos", "business_hours",
        "morning_followup_message",
    }
    clean = {k: v for k, v in patch.items() if k in allowed}
    if not clean:
        return JSONResponse(status_code=400, content={"error": "Nenhum campo permitido"})
    tenant_id = getattr(request.state, "tenant_id", DEFAULT_TENANT_ID)
    await db.update_tenant_settings(tenant_id, clean)
    tc.invalidate(tenant_id)
    await tc.load_tenant_config(tenant_id, force=True)
    return {"status": "ok", "config": tc.get_cached_config(tenant_id)}

@app.get("/painel", response_class=HTMLResponse)
async def dashboard(request: Request, user: dict = Depends(get_current_user)):
    """Renderiza o painel administrativo ou de atendimento conforme o papel."""
    if not user: return RedirectResponse(url="/login")
    transfer_targets = await db.get_transfer_targets()
    sys_users = await db.get_all_system_users()
    if user['role'] == 'admin':
        return await render_admin_dashboard(request, user, sys_users, transfer_targets)
    else:
        clientes = await db.get_clients(user['role'], user['name'], tab='home')
        carteira = await db.get_clients(user['role'], user['name'], tab='contatos', limit=30, offset=0)
        metrics = await db.get_seller_metrics(user['name'])
        current_status = await db.get_seller_status(user['name'])
        format_client_datetimes(clientes)
        format_client_datetimes(carteira)
        return templates.TemplateResponse(request=request, name="dashboard_vendas.html", context={"request": request, "user": user, "clientes": clientes, "carteira": carteira, "metrics": metrics, "status": current_status, "transfer_targets": transfer_targets, "sys_users": sys_users})

async def render_admin_dashboard(request: Request, user: dict, sys_users: list = None, transfer_targets: list = None):
    """Monta a visão administrativa com ações habilitadas por grupos de permissão."""
    sys_users = sys_users if sys_users is not None else await db.get_all_system_users()
    transfer_targets = transfer_targets if transfer_targets is not None else await db.get_transfer_targets()
    reports = await db.get_admin_reports()
    bot_set = await db.get_bot_settings()
    global_metrics = await db.get_global_metrics()
    sellers_list = [s['name'] for s in sys_users if s['role'] in ['vendas_1', 'vendas_2', 'financeiro']]
    return templates.TemplateResponse(
        request=request,
        name="dashboard_admin.html",
        context={
            "request": request,
            "user": user,
            "reports": reports,
            "sys_users": sys_users,
            "bot_settings": bot_set,
            "global_metrics": global_metrics,
            "sellers_list": sellers_list,
            "transfer_targets": transfer_targets,
            "admin_can_write": can_write_admin_panel(user),
            "admin_can_communicate": can_admin_action(user, "communication"),
            "admin_can_manage_contacts": can_admin_action(user, "contacts"),
            "admin_can_audit": can_admin_action(user, "audit"),
            "admin_can_reports": can_admin_action(user, "reports"),
            "admin_can_manage_users": can_admin_action(user, "users"),
            "admin_can_manage_sessions": can_admin_action(user, "sessions"),
            "admin_can_evolution": can_admin_action(user, "evolution"),
            "admin_can_global_settings": can_admin_action(user, "settings"),
            "admin_can_maintenance": can_admin_action(user, "maintenance"),
            "admin_can_use_tools": any(
                can_admin_action(user, group)
                for group in ["communication", "contacts", "users", "sessions", "evolution", "settings", "maintenance"]
            ),
            "admin_read_only": can_read_admin_panel(user) and not can_write_admin_panel(user),
        },
    )

@app.get("/painel/admin", response_class=HTMLResponse)
async def dashboard_admin_read(request: Request, user: dict = Depends(get_current_user)):
    """Abre a visão administrativa híbrida; financeiro mantém `/painel` operacional."""
    if not user:
        return RedirectResponse(url="/login")
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await render_admin_dashboard(request, user)

@app.post("/api/admin/cleanup-media")
async def api_cleanup_media(payload: CleanupRequest, user: dict = Depends(get_current_user)):
    """Executa limpeza manual de mídias para perfis com grupo `maintenance`."""
    if not can_admin_action(user, "maintenance"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    result = await cleanup_media_files(payload.media_types, payload.days_old)
    return result

@app.post("/api/admin/takeover")
async def admin_takeover(payload: AdminTakeoverRequest, user: dict = Depends(get_current_user)):
    """Permite responder via auditoria sem remover o contato da carteira/fila original."""
    if not can_admin_action(user, "audit"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    phone = payload.phone
    previous = await db.get_user(phone)
    await db.update_user_stage(phone, 'em_atendimento')
    await record_audit_log(
        user,
        "admin_takeover",
        phone=phone,
        target=user['name'],
        details={
            "previous_assigned_to": previous.get("assigned_to") if previous else "",
            "previous_stage": previous.get("stage") if previous else "",
        }
    )
    await manager.broadcast("update")
    await manager.broadcast(f"chat_{phone}")
    logger.info(f"Admin assumiu chat: {phone}")
    return {"status": "ok"}

@app.post("/api/admin/sessions/clear")
async def admin_clear_session(payload: ClearSessionRequest, user: dict = Depends(get_current_user)):
    """Libera login travado para perfis com grupo administrativo `sessions`."""
    if not can_admin_action(user, "sessions"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    username = (payload.username or "").strip().lower()
    if not username:
        return {"status": "error", "message": "Informe o usuário para liberar."}
    if not auth.ENFORCE_SINGLE_SESSION:
        return {"status": "ok", "message": "Trava de sessão única está desativada; não há sessão Redis para liberar."}
    await auth.clear_login_session(username)
    await record_audit_log(user, "session_cleared", target=username, details={"released_username": username})
    logger.info(f"Sessão ativa liberada pelo admin: {username}")
    return {"status": "ok"}

@app.get("/api/admin/spy_chat/{phone}")
async def admin_spy_chat(phone: str, user: dict = Depends(get_current_user)):
    """Retorna histórico de contato para auditoria de admin e financeiro."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return format_message_timestamps(await db.get_messages(phone))

@app.get("/api/admin/evo/status")
async def evo_status(request: Request, user: dict = Depends(get_current_user)):
    """Consulta o estado da Evolution API para perfis com grupo `evolution`."""
    if not can_admin_action(user, "evolution"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    # Fase 2: a instância é derivada do tenant da requisição (nunca do cliente).
    tenant_id = getattr(request.state, "tenant_id", DEFAULT_TENANT_ID)
    session = await db.get_tenant_instance(tenant_id)
    url = f"{settings.EVO_API_URL}/instance/connectionState/{session}"
    try:
        res = await evo.client.get(url)
        return {"status": res.json().get("instance", {}).get("state", "OFFLINE")}
    except Exception as e:
        logger.error(f"Erro ao checar status da Evo API ({session}): {e}")
        return {"status": "OFFLINE"}

@app.post("/api/admin/evo/connect")
async def evo_connect(request: Request, payload: EvolutionSessionRequest, user: dict = Depends(get_current_user)):
    """Cria/reconecta instância Evolution para perfis com grupo `evolution`."""
    if not can_admin_action(user, "evolution"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})

    # Fase 2: cada tenant conecta apenas à SUA instância (QR por cliente). O nome
    # da instância vem do cadastro do tenant, nunca do corpo enviado pelo cliente.
    tenant_id = getattr(request.state, "tenant_id", DEFAULT_TENANT_ID)
    session = await db.get_tenant_instance(tenant_id)
    logger.info(f"Iniciando processo de QR Code para tenant={tenant_id} instância={session.upper()}")
    
    state_url = f"{settings.EVO_API_URL}/instance/connectionState/{session}"
    create_url = f"{settings.EVO_API_URL}/instance/create"
    connect_url = f"{settings.EVO_API_URL}/instance/connect/{session}?b64=true"
    logout_url = f"{settings.EVO_API_URL}/instance/logout/{session}"
    
    try:
        state_res = await evo.client.get(state_url)
        res = None
        
        if state_res.status_code in [404, 400]:
            logger.info("Instância não existe. Criando nova instância...")
            create_payload = {
                "instanceName": session,
                "token": settings.EVO_API_KEY,
                "qrcode": True,
                "integration": "WHATSAPP-BAILEYS"
            }
            res = await evo.client.post(create_url, json=create_payload)
        else:
            estado_atual = state_res.json().get("instance", {}).get("state", "DESCONHECIDO")
            logger.info(f"Instância encontrada. Estado: {estado_atual}")
            
            if estado_atual == "open":
                return {"qr": None, "message": "O aparelho já está conectado e online!"}
            
            if estado_atual == "connecting":
                logger.warning("Instância presa em loop. Forçando limpeza (logout)...")
                await evo.client.delete(logout_url)
                await asyncio.sleep(1)
                
            logger.info("Solicitando conexão...")
            res = await evo.client.get(connect_url)
        
        res_data = res.json()
        qr_base64 = None
        
        if "base64" in res_data and res_data["base64"]:
            qr_base64 = res_data["base64"]
        elif "qrcode" in res_data:
            if isinstance(res_data["qrcode"], dict):
                qr_base64 = res_data["qrcode"].get("base64")
            elif isinstance(res_data["qrcode"], str):
                qr_base64 = res_data["qrcode"]
                
        if qr_base64:
            logger.info("QR Code extraído com sucesso.")
            if not qr_base64.startswith("data:image"):
                qr_base64 = f"data:image/png;base64,{qr_base64}"
            return {"qr": qr_base64}
            
        logger.warning(f"API respondeu sem QR Code. Status: {res.status_code}")
        return {"qr": None, "message": "O motor do WhatsApp está acordando. Aguarde 5 segundos e clique em 'Ler QR' novamente."}
        
    except Exception as e:
        logger.error(f"Erro crítico no processo de conexão: {e}")
        return {"error": f"Erro interno de comunicação: {str(e)}"}

@app.delete("/api/admin/evo/logout")
async def evo_logout(request: Request, payload: EvolutionSessionRequest, user: dict = Depends(get_current_user)):
    """Desconecta instância Evolution para perfis com grupo `evolution`."""
    if not can_admin_action(user, "evolution"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    # Fase 2: sempre desloga a instância do tenant atual.
    tenant_id = getattr(request.state, "tenant_id", DEFAULT_TENANT_ID)
    session = await db.get_tenant_instance(tenant_id)
    url = f"{settings.EVO_API_URL}/instance/logout/{session}"
    try:
        await evo.client.delete(url)
        logger.info(f"Sessão deslogada: {session}")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"Erro ao deslogar sessão {session}: {e}")
        return {"status": "error"}

@app.get("/api/changelog/latest")
async def api_latest_changelog(user: dict = Depends(get_current_user)):
    """Retorna a versao mais recente do changelog para aviso unico no painel."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return load_latest_changelog()

@app.get("/api/tags")
async def api_get_tags(user: dict = Depends(get_current_user)):
    """Lista as tags disponíveis para classificação de contatos."""
    if not user: return []
    return await db.get_all_tags()

@app.post("/api/tags")
async def api_create_tag(payload: TagCreateRequest, user: dict = Depends(get_current_user)):
    """Cria uma nova tag de contato."""
    if not user: return {}
    await db.create_tag(payload.name, payload.color)
    return {"status": "ok"}

@app.post("/api/contacts/tags")
async def api_toggle_user_tag(payload: UserTagToggleRequest, user: dict = Depends(get_current_user)):
    """Adiciona ou remove uma tag de um contato."""
    if not user: return {}
    contact = await db.get_user(payload.phone)
    if not can_edit_contact_record(user, contact):
        return JSONResponse(status_code=403, content={"error": "Contato fora do escopo de edição"})
    await db.toggle_user_tag(payload.phone, payload.tag_id, payload.action)
    await manager.broadcast("update")
    return {"status": "ok"}

@app.get("/api/contact_lists")
async def api_contact_lists(user: dict = Depends(get_current_user)):
    """Lista listas de contatos globais e do usuario logado."""
    if not user:
        return []
    return await db.get_contact_lists(user.get("name", ""), user.get("role", ""))

@app.post("/api/contact_lists")
async def api_create_contact_list(payload: ContactListCreateRequest, user: dict = Depends(get_current_user)):
    """Cria uma lista de contatos estilo WhatsApp para segmentacao da carteira."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    name = (payload.name or "").strip()
    if not name:
        return {"status": "error", "message": "Informe o nome da lista."}
    owner_name = "" if user.get("role") == "admin" and payload.is_global else user["name"]
    contact_list = await db.create_contact_list(name, payload.color, owner_name)
    await record_audit_log(user, "contact_list_created", target=name, details={"owner_name": owner_name, "color": payload.color})
    await manager.broadcast("update")
    return {"status": "ok", "list": contact_list}

@app.post("/api/contacts/list_toggle")
async def api_toggle_contact_list(payload: ContactListToggleRequest, user: dict = Depends(get_current_user)):
    """Adiciona ou remove um contato de uma lista operacional."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    phone = payload.phone
    list_id = parse_int_or_zero(payload.list_id)
    action = payload.action
    if not phone or not list_id:
        return {"status": "error", "message": "Contato e lista são obrigatórios."}
    contact = await db.get_user(phone)
    if not can_edit_contact_record(user, contact):
        return JSONResponse(status_code=403, content={"error": "Contato fora do escopo de edição"})
    await db.toggle_contact_list_member(phone, list_id, action)
    await record_audit_log(user, "contact_list_updated", phone=phone, target=str(list_id), details={"action": action, "list_id": list_id})
    await manager.broadcast("update")
    return {"status": "ok"}

@app.post("/api/contacts/bulk_action")
async def api_contacts_bulk_action(payload: ContactBulkActionRequest, user: dict = Depends(get_current_user)):
    """Aplica ações em lote somente em contatos dentro do escopo editável."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    unique_phones = []
    for phone in payload.phones:
        raw_phone = str(phone or "").strip()
        normalized = raw_phone if "@" in raw_phone else normalize_whatsapp_jid(raw_phone)
        if normalized and normalized not in unique_phones:
            unique_phones.append(normalized)
    if not unique_phones:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Selecione ao menos um contato."})
    if len(unique_phones) > 100:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Ação em lote limitada a 100 contatos por vez."})
    action = (payload.action or "").strip().lower()
    allowed_actions = {"add_list", "add_prefix", "replace_word", "sanitize_evolution"}
    if action not in allowed_actions:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Ação em lote inválida."})
    if action == "add_list" and not payload.list_id:
        return JSONResponse(status_code=400, content={"status": "error", "message": "Selecione uma lista para aplicar."})
    if action == "add_prefix" and not payload.prefix.strip():
        return JSONResponse(status_code=400, content={"status": "error", "message": "Informe o prefixo que será adicionado."})
    if action == "replace_word" and not payload.find.strip():
        return JSONResponse(status_code=400, content={"status": "error", "message": "Informe a palavra que será substituída."})

    updated = 0
    skipped = []
    examples = []
    for phone in unique_phones:
        contact = await db.get_user(phone)
        if not can_edit_contact_record(user, contact):
            skipped.append({"phone": phone, "reason": "fora_do_escopo"})
            continue
        current_name = contact.get("name") or ""
        new_name = current_name
        if action == "add_list":
            if not payload.dry_run:
                await db.toggle_contact_list_member(phone, payload.list_id, "add")
            updated += 1
        elif action == "add_prefix":
            prefix = re.sub(r"\s+", " ", payload.prefix).strip()
            if not re.match(rf"^{re.escape(prefix)}(\s|[-_])", current_name or "", flags=re.I):
                new_name = f"{prefix} {current_name}".strip()
        elif action == "replace_word":
            new_name = re.sub(re.escape(payload.find), payload.replace, current_name or "", flags=re.I).strip()
        elif action == "sanitize_evolution":
            new_name = sanitize_contact_name_for_bulk(current_name, phone)

        if action != "add_list":
            if new_name != current_name:
                if not payload.dry_run:
                    await db.update_contact_details(phone, new_name, contact.get("notes") or "", contact.get("extra_info") or "")
                updated += 1
            else:
                skipped.append({"phone": phone, "reason": "sem_alteracao"})
        if len(examples) < 8:
            examples.append({"phone": phone, "old_name": current_name, "new_name": new_name})

    response = {
        "status": "ok",
        "dry_run": payload.dry_run,
        "requested": len(unique_phones),
        "updated": updated,
        "skipped": len(skipped),
        "examples": examples,
    }
    if payload.dry_run:
        return response
    await record_audit_log(
        user,
        "contact_bulk_action_applied",
        target=action,
        details={
            "action": action,
            "requested": len(unique_phones),
            "updated": updated,
            "skipped": skipped[:20],
            "list_id": payload.list_id,
            "prefix": payload.prefix,
            "find": payload.find,
            "replace": payload.replace,
            "examples": examples,
        }
    )
    await manager.broadcast("update")
    return response

@app.post("/api/contacts/mark_reviewed")
async def api_mark_contact_reviewed(payload: PhonePayload, user: dict = Depends(get_current_user)):
    """Marca um contato como revisado durante o fluxo assistido."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    phone = payload.phone
    contact = await db.get_user(phone)
    if not can_edit_contact_record(user, contact):
        return JSONResponse(status_code=403, content={"error": "Contato fora do escopo de edição"})
    result = await db.mark_contact_reviewed(phone, user["name"])
    await record_audit_log(user, "contact_reviewed", phone=phone, details={"name": contact.get("name") if contact else "", **result})
    await manager.broadcast("update")
    return {"status": "ok", "result": result}

@app.get("/api/admin/contact_sanity")
async def api_contact_sanity(user: dict = Depends(get_current_user)):
    """Retorna relatório técnico de contatos a revisar, sem item visual no menu."""
    if not can_admin_action(user, "contacts"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await db.get_contact_sanity_report()

@app.get("/api/admin/contact_prefix_scan")
async def api_contact_prefix_scan(min_count: int = 2, limit_examples: int = 5, user: dict = Depends(get_current_user)):
    """Varre nomes de contatos para alimentar a ferramenta admin de prefixos."""
    if not can_admin_action(user, "contacts"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    contacts = await db.get_contacts_for_prefix_scan()
    return build_prefix_scan_payload(contacts, max(1, min_count), max(1, min(limit_examples, 20)))

@app.post("/api/admin/contact_prefix_preview")
async def api_contact_prefix_preview(payload: PrefixSelectionRequest, user: dict = Depends(get_current_user)):
    """Simula listas por prefixos aprovados ou manuais sem gravar vínculos."""
    if not can_admin_action(user, "contacts"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    data = payload.dict()
    prefixes = parse_prefix_selection(data)
    if not prefixes:
        return {"status": "error", "message": "Selecione ao menos um prefixo."}
    contacts = await db.get_contacts_for_prefix_scan()
    groups, _apply_groups = build_prefix_groups(contacts, prefixes, parse_int_or_zero(data.get("limit_examples")) or 5)
    return {"status": "ok", "prefixes": prefixes, "groups": groups, "total": sum(group["count"] for group in groups)}

@app.post("/api/admin/contact_prefix_apply")
async def api_contact_prefix_apply(payload: PrefixSelectionRequest, user: dict = Depends(get_current_user)):
    """Vincula contatos às listas por prefixos sem renomear ou trocar responsável."""
    if not can_admin_action(user, "contacts"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    data = payload.dict()
    prefixes = parse_prefix_selection(data)
    if not prefixes:
        return {"status": "error", "message": "Selecione ao menos um prefixo."}
    contacts = await db.get_contacts_for_prefix_scan()
    groups, apply_groups = build_prefix_groups(contacts, prefixes, parse_int_or_zero(data.get("limit_examples")) or 5)
    if not apply_groups:
        return {"status": "error", "message": "Nenhum contato encontrado com os prefixos selecionados."}
    result = await db.apply_contact_prefix_lists(apply_groups, LIST_COLORS)
    await record_audit_log(
        user,
        "contact_prefix_lists_applied",
        target=",".join(prefixes),
        details={"prefixes": prefixes, "groups": groups, "result": result}
    )
    await manager.broadcast("update")
    return {"status": "ok", "prefixes": prefixes, "groups": groups, "result": result, "total": sum(group["count"] for group in groups)}

async def get_sales_names_for_admin():
    """Retorna nomes das vendedoras ativas usadas em sync/divisão."""
    sales_users = await db.get_sales_users()
    return [item["name"] for item in sales_users if item.get("name")]

@app.post("/api/admin/contact_owner_preview")
async def api_contact_owner_preview(payload: ContactOwnerRequest, user: dict = Depends(get_current_user)):
    """Simula correção de responsáveis por prefixo e divisão de excedentes."""
    if not can_admin_action(user, "contacts"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    sales_names = await get_sales_names_for_admin()
    if len(sales_names) < 2:
        return {"status": "error", "message": "Cadastre ao menos duas vendedoras ativas."}
    owner_mode = payload.owner_mode if payload.owner_mode in sales_names else "auto"
    prefixes = parse_prefix_selection(payload.model_dump())
    contacts = await db.get_contacts_for_owner_repair()
    assignments, summary = build_owner_assignment_plan(contacts, sales_names, owner_mode, prefixes or None)
    return {"status": "ok", "owner_mode": owner_mode, "prefixes": prefixes, "sales_names": sales_names, "summary": summary, "examples": assignments[:20]}

@app.post("/api/admin/contact_owner_apply")
async def api_contact_owner_apply(payload: ContactOwnerRequest, user: dict = Depends(get_current_user)):
    """Aplica correção de responsáveis por prefixo e divisão de excedentes."""
    if not can_admin_action(user, "contacts"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    sales_names = await get_sales_names_for_admin()
    if len(sales_names) < 2:
        return {"status": "error", "message": "Cadastre ao menos duas vendedoras ativas."}
    owner_mode = payload.owner_mode if payload.owner_mode in sales_names else "auto"
    prefixes = parse_prefix_selection(payload.model_dump())
    contacts = await db.get_contacts_for_owner_repair()
    assignments, summary = build_owner_assignment_plan(contacts, sales_names, owner_mode, prefixes or None)
    result = await db.apply_contact_owner_assignments(assignments)
    await record_audit_log(user, "contact_owner_repair_applied", target=owner_mode, details={"prefixes": prefixes, "summary": summary, "result": result, "examples": assignments[:20]})
    await manager.broadcast("update")
    return {"status": "ok", "owner_mode": owner_mode, "prefixes": prefixes, "sales_names": sales_names, "summary": summary, "result": result, "examples": assignments[:20]}

async def build_admin_contact_sync_plan(owner_mode: str, prefixes: list = None):
    """Monta plano de novos contatos Evolution para admin sem gravar no banco."""
    sales_names = await get_sales_names_for_admin()
    if len(sales_names) < 2:
        return {"status": "error", "message": "Cadastre ao menos duas vendedoras ativas."}
    owner_mode = owner_mode if owner_mode in sales_names else "auto"
    result = await evo.fetch_contacts("vendas")
    if not result.get("ok"):
        return {"status": "error", "message": "Falha na comunicação com a Evolution API."}

    owner_counts = {name: 0 for name in sales_names}
    for contact in await db.get_contacts_for_owner_repair():
        owner = contact.get("assigned_to")
        if owner in owner_counts and contact.get("stage") == "carteira":
            owner_counts[owner] += 1

    seen = set()
    new_contacts = []
    existing = 0
    updated_names = 0
    unnamed_contacts = 0
    by_owner = {name: 0 for name in sales_names}
    examples = []

    for contact in result.get("contacts", []):
        phone = contact.get("phone")
        if not phone or phone in seen:
            continue
        seen.add(phone)
        if not contact.get("name"):
            unnamed_contacts += 1
        existing_contact = await db.get_user(phone)
        if existing_contact:
            existing += 1
            if contact.get("name") and (not existing_contact.get("name") or existing_contact.get("name") == "None"):
                updated_names += 1
            continue
        if owner_mode in sales_names:
            owner = owner_mode
            reason = "manual"
        else:
            owner = detect_sales_owner_from_name(contact.get("name") or "", sales_names, prefixes)
            reason = "prefix" if owner else "balance"
            owner = owner or choose_balanced_owner(owner_counts)
        owner_counts[owner] += 1
        by_owner[owner] += 1
        item = {**contact, "assigned_to": owner, "reason": reason}
        new_contacts.append(item)
        if len(examples) < 20:
            examples.append(item)

    return {
        "status": "ok",
        "owner_mode": owner_mode,
        "prefixes": prefixes or [],
        "sales_names": sales_names,
        "contacts": new_contacts,
        "summary": {
            "total_records": result.get("total_records", 0),
            "valid_contacts": len(seen),
            "ignored": result.get("ignored_records", 0),
            "new_contacts": len(new_contacts),
            "existing": existing,
            "updated_names": updated_names,
            "unnamed_contacts": unnamed_contacts,
            "by_owner": by_owner,
        },
        "examples": examples,
    }

@app.post("/api/admin/contacts/sync_preview")
async def api_admin_contacts_sync_preview(payload: ContactOwnerRequest, user: dict = Depends(get_current_user)):
    """Simula sincronização admin da agenda Evolution com divisão de novos contatos."""
    if not can_admin_action(user, "contacts"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    prefixes = parse_prefix_selection(payload.model_dump())
    plan = await build_admin_contact_sync_plan(payload.owner_mode, prefixes or None)
    if plan.get("status") != "ok":
        return plan
    return {key: value for key, value in plan.items() if key != "contacts"}

@app.post("/api/admin/contacts/sync_apply")
async def api_admin_contacts_sync_apply(payload: ContactOwnerRequest, user: dict = Depends(get_current_user)):
    """Aplica sincronização admin da agenda Evolution com divisão de novos contatos."""
    if not can_admin_action(user, "contacts"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    prefixes = parse_prefix_selection(payload.model_dump())
    plan = await build_admin_contact_sync_plan(payload.owner_mode, prefixes or None)
    if plan.get("status") != "ok":
        return plan
    created = 0
    for contact in plan.get("contacts", []):
        sync_result = await db.upsert_synced_contact(contact["phone"], "vendas", contact.get("name", ""), contact["assigned_to"], "carteira")
        if sync_result.get("inserted"):
            created += 1
    await record_audit_log(user, "admin_contacts_synced", target=plan.get("owner_mode", "auto"), details={"prefixes": plan.get("prefixes", []), "summary": plan.get("summary", {}), "created": created, "examples": plan.get("examples", [])})
    await manager.broadcast("update")
    summary = plan.get("summary", {})
    summary["created"] = created
    return {"status": "ok", "owner_mode": plan.get("owner_mode"), "sales_names": plan.get("sales_names", []), "summary": summary, "examples": plan.get("examples", [])}

@app.get("/api/snippets")
async def get_user_snippets(user: dict = Depends(get_current_user)):
    """Lista atalhos de mensagem cadastrados para o usuário logado."""
    if not user: return []
    return await db.get_snippets(user['name'])

@app.post("/api/snippets")
async def create_user_snippet(payload: SnippetRequest, user: dict = Depends(get_current_user)):
    """Cria um novo atalho de mensagem para o usuário logado."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.add_snippet(user['name'], payload.shortcut, payload.content)
    return {"status": "ok"}

@app.get("/api/contacts/load")
async def load_contacts(offset: int = 0, search: str = "", list_id: str = "", bucket: str = "", initial: str = "", user: dict = Depends(get_current_user)):
    """Carrega contatos da carteira com busca ampliada, listas, depósitos, inicial e paginação."""
    if not user: return []
    contacts = await db.get_clients(user['role'], user['name'], tab='contatos', search=search, limit=30, offset=offset, list_id=parse_int_or_zero(list_id), bucket=bucket, initial=initial, broaden_search=bool(search))
    return format_client_datetimes(contacts)

@app.get("/api/contacts/buckets")
async def contact_buckets(user: dict = Depends(get_current_user)):
    """Retorna contadores dos depósitos virtuais da carteira do usuário logado."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await db.get_contact_bucket_summary(user["role"], user["name"])

@app.get("/api/contacts/initial_counts")
async def contact_initial_counts(search: str = "", list_id: str = "", bucket: str = "", user: dict = Depends(get_current_user)):
    """Conta contatos por inicial visual respeitando busca, lista e depósito atuais."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    contacts = await db.get_clients(
        user["role"],
        user["name"],
        tab="contatos",
        search=search,
        limit=50000,
        offset=0,
        list_id=parse_int_or_zero(list_id),
        bucket=bucket,
        broaden_search=bool(search),
    )
    counts = {letter: 0 for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"}
    counts["#"] = 0
    for contact in contacts:
        key = contact_initial_key(contact.get("name") or "", contact.get("phone") or "")
        counts[key if key in counts else "#"] += 1
    counts["all"] = len(contacts)
    return counts

@app.get("/api/admin/contacts_by_seller")
async def admin_contacts_by_seller(seller: str, offset: int = 0, search: str = "", list_id: str = "", bucket: str = "", user: dict = Depends(get_current_user)):
    """Carrega filas e carteira para auditoria de admin e financeiro."""
    if not can_read_admin_panel(user): return []
    if seller == "all":
        contacts = await db.get_clients('admin', user['name'], tab='contatos', search=search, limit=30, offset=offset, list_id=parse_int_or_zero(list_id), bucket=bucket, broaden_search=True, order_mode="audit")
    else:
        contacts = await db.get_clients('vendas', seller, tab='contatos', search=search, limit=30, offset=offset, list_id=parse_int_or_zero(list_id), bucket=bucket, broaden_search=True, order_mode="audit")
    return format_client_datetimes(contacts)

@app.get("/api/admin/contact_details/{phone:path}")
async def admin_contact_details(phone: str, user: dict = Depends(get_current_user)):
    """Retorna dados do contato para ficha de auditoria de admin e financeiro."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    contact = await db.get_contact_details(phone)
    if not contact:
        return JSONResponse(status_code=404, content={"error": "Contato não encontrado"})
    return format_client_datetimes([contact])[0]

@app.get("/api/admin/audit_logs")
async def admin_audit_logs(
    action: str = "",
    actor: str = "",
    search: str = "",
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    offset: int = 0,
    user: dict = Depends(get_current_user),
):
    """Lista logs operacionais para admin e financeiro com leitura administrativa."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    try:
        logs = await db.get_audit_logs(
            action=action,
            actor=actor,
            search=search,
            category_actions=audit_log_category_actions(category),
            date_from=date_from,
            date_to=date_to,
            limit=50,
            offset=offset,
        )
        for entry in logs:
            entry["created_at"] = format_datetime_br(entry.get("created_at"))
        return logs
    except Exception as exc:
        logger.exception(
            "Falha ao listar audit_logs action={} actor={} category={} date_from={} date_to={} offset={}",
            action, actor, category, date_from, date_to, offset
        )
        return JSONResponse(status_code=500, content={"error": "Falha ao carregar logs.", "detail": str(exc)})

@app.get("/api/admin/audit_logs/export")
async def admin_audit_logs_export(
    action: str = "",
    actor: str = "",
    search: str = "",
    category: str = "",
    date_from: str = "",
    date_to: str = "",
    user: dict = Depends(get_current_user),
):
    """Exporta logs em CSV para admin e financeiro com leitura administrativa."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    try:
        logs = await db.get_audit_logs(
            action=action,
            actor=actor,
            search=search,
            category_actions=audit_log_category_actions(category),
            date_from=date_from,
            date_to=date_to,
            limit=5000,
            offset=0,
        )
    except Exception as exc:
        logger.exception(
            "Falha ao exportar audit_logs action={} actor={} category={} date_from={} date_to={}",
            action, actor, category, date_from, date_to
        )
        return JSONResponse(status_code=500, content={"error": "Falha ao exportar logs.", "detail": str(exc)})
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["data_hora", "ator", "perfil", "acao", "telefone", "cliente", "destino", "detalhes"])
    for entry in logs:
        writer.writerow([
            format_datetime_br(entry.get("created_at")),
            entry.get("actor_name", ""),
            entry.get("actor_role", ""),
            AUDIT_LOG_ACTION_LABELS.get(entry.get("action"), entry.get("action", "")),
            entry.get("phone", ""),
            entry.get("contact_name", ""),
            entry.get("target", ""),
            json.dumps(entry.get("details") or {}, ensure_ascii=False, default=str),
        ])
    filename = f"audit_logs_{datetime.now(BR_TIMEZONE).strftime('%Y%m%d_%H%M%S')}.csv"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(output.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)

@app.post("/api/contacts/sync")
async def sync_contacts(request: Request, user: dict = Depends(get_current_user)):
    """Sincroniza contatos salvos da Evolution para a carteira do usuário logado."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    if user.get("role") == "admin":
        return JSONResponse(
            status_code=400,
            content={"status": "error", "message": "Use Organizar Contatos em /painel/admin para sincronizar com escolha/divisão de responsável."}
        )

    target_session = tc.role_session_map().get(user['role'], await db.get_tenant_instance(get_current_tenant()))
    assigned_to = user.get("name", "")

    try:
        result = await evo.fetch_contacts(target_session)
        if not result.get("ok"):
            return {"status": "error", "message": "Falha na comunicação com a Evolution API."}

        created_count = 0
        updated_name_count = 0
        existing_count = 0
        seen = set()
        for contact in result.get("contacts", []):
            phone = contact.get("phone")
            if not phone or phone in seen:
                continue
            seen.add(phone)
            sync_result = await db.upsert_synced_contact(
                phone,
                target_session,
                name=contact.get("name", ""),
                assigned_to=assigned_to,
                stage="carteira"
            )
            if sync_result.get("inserted"):
                created_count += 1
            elif sync_result.get("filled_name"):
                updated_name_count += 1
            else:
                existing_count += 1

        await record_audit_log(
            user,
            "contacts_synced",
            target=assigned_to,
            details={
                "session": target_session,
                "total_records": result.get("total_records", 0),
                "valid_contacts": len(seen),
                "created": created_count,
                "updated_names": updated_name_count,
                "existing": existing_count,
                "ignored": result.get("ignored_records", 0),
            }
        )
        await manager.broadcast("update")
        message = f"{created_count} novos contatos adicionados e {updated_name_count} nomes preenchidos."
        logger.info(f"Sincronização concluída por {user['name']}: {message}")
        return {"status": "ok", "message": message, "created": created_count, "updated_names": updated_name_count, "ignored": result.get("ignored_records", 0)}
    except Exception as e:
        logger.error(f"Erro ao sincronizar contatos: {e}")
        return {"status": "error", "message": "Servidor offline."}

@app.post("/api/contacts/new")
async def add_new_contact(payload: NewContactRequest, user: dict = Depends(get_current_user)):
    """Normaliza um telefone e cria contato manual na carteira do operador."""
    if not user: 
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    phone_raw = payload.phone
    nums = re.sub(r'\D', '', phone_raw)
    if len(nums) <= 11: 
        nums = "55" + nums
    clean_number = f"{nums}@s.whatsapp.net"
    existing_contact = await db.get_user(clean_number)
    # Fase 4: aplica o limite de contatos do plano ao criar um novo contato.
    if not existing_contact:
        ok, current, limit = await db.check_tenant_limit(get_current_tenant(), "contacts")
        if not ok:
            return JSONResponse(status_code=402, content={"error": f"Limite de contatos do plano atingido ({current}/{limit}). Faça upgrade do plano."})
    await db.create_user(clean_number, tc.role_session_map().get(user['role'], await db.get_tenant_instance(get_current_tenant())), payload.name, user['name'], "carteira")
    await record_audit_log(
        user,
        "contact_created" if not existing_contact else "contact_existing_added",
        phone=clean_number,
        target=user['name'],
        details={"name": payload.name, "already_existed": bool(existing_contact)}
    )
    logger.info(f"Novo contato criado: {clean_number} por {user['name']}")
    return {"status": "ok"}

@app.post("/api/profile/update")
async def update_profile(payload: ProfileUpdateRequest, user: dict = Depends(get_current_user)):
    """Atualiza dados visuais e cadastrais do perfil do usuário."""
    if not user: 
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.update_user_profile(
        user['username'], 
        payload.name or user['name'], 
        payload.bio if payload.bio is not None else user.get('bio', ''), 
        payload.avatar or user.get('avatar', ''),
        payload.bg_color or user.get('bg_color', '#0B1014'),
        payload.primary_color or user.get('primary_color', '#ff6600'),
        payload.text_color or user.get('text_color', '#E9EDEF')
    )
    return {"status": "ok"}

@app.post("/api/profile/upload_avatar")
async def upload_avatar(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """Recebe e grava um avatar de perfil em `/data/midias`."""
    if not user: 
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    filename = f"avatar_{user['username']}_{int(time.time())}.jpg"
    # Fase 5: grava via camada de storage (local ou S3/R2), namespaceado por tenant.
    avatar_bytes = b""
    while content := await file.read(1024 * 1024):
        avatar_bytes += content
    web_url = await storage.save_media(get_current_tenant(), filename, avatar_bytes)

    await db.update_user_profile(
        user['username'], 
        user['name'], 
        user.get('bio', ''), 
        web_url,
        user.get('bg_color', '#0B1014'),
        user.get('primary_color', '#ff6600'),
        user.get('text_color', '#E9EDEF')
    )
    return {"status": "ok", "url": web_url}

@app.get("/api/internal_chat/unread_since")
async def get_internal_chat_unread_since(last_id: int = 0, limit: int = 80, user: dict = Depends(get_current_user)):
    """Sincroniza mensagens internas relevantes perdidas desde o último ID visto."""
    if not user:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Acesso negado"})
    last_id = max(0, parse_int_or_zero(last_id))
    limit = max(1, min(parse_int_or_zero(limit) or 80, 200))
    latest_id = await db.get_latest_internal_message_id(user["name"])
    if last_id <= 0:
        logger.info(
            "[INTERNAL_CHAT_SYNC_BASELINE] user={} role={} latest_id={} limit={}",
            user.get("name", ""),
            user.get("role", ""),
            latest_id,
            limit,
        )
        return {"status": "ok", "last_id": last_id, "latest_id": latest_id, "has_more": False, "messages": []}

    started_at = time.perf_counter()
    rows = await db.get_internal_messages_since(user["name"], last_id, limit)
    messages = [internal_message_event_payload(row, user) for row in rows]
    last_returned_id = max([parse_int_or_zero(item.get("message_id")) for item in messages] or [last_id])
    has_more = bool(last_returned_id < latest_id)
    logger.info(
        "[INTERNAL_CHAT_SYNC] user={} role={} last_id={} latest_id={} returned={} has_more={} total_ms={:.2f}",
        user.get("name", ""),
        user.get("role", ""),
        last_id,
        latest_id,
        len(messages),
        has_more,
        (time.perf_counter() - started_at) * 1000,
    )
    return {
        "status": "ok",
        "last_id": last_id,
        "latest_id": latest_id,
        "has_more": has_more,
        "messages": messages,
    }

@app.get("/api/internal_chat/{target}")
async def get_internal_chat(target: str, user: dict = Depends(get_current_user)):
    """Lista as mensagens recentes do chat interno com metadados de ações."""
    if not user: return []
    return format_message_timestamps(await db.get_internal_messages(user['name'], target))

@app.post("/api/internal_chat/send")
async def send_internal_chat(payload: InternalChatRequest, user: dict = Depends(get_current_user)):
    """Grava texto interno, preserva resposta citada e retorna o id persistido."""
    if not user: return {"status": "error"}
    request_started_at = time.perf_counter()
    created_at = datetime.now(BR_TIMEZONE)
    text = (payload.text or "").strip()
    if not text:
        return {"status": "error", "message": "Mensagem vazia."}
    mentions = extract_internal_mentions(text)
    logger.info(
        "[INTERNAL_CHAT_SEND_START] sender={} role={} target={} reply_to_id={} created_at={} preview={}",
        user.get("name", ""),
        user.get("role", ""),
        payload.target,
        payload.reply_to_id or "-",
        created_at.isoformat(timespec="milliseconds"),
        internal_chat_preview(text),
    )
    try:
        db_started_at = time.perf_counter()
        message_id = await db.save_internal_message(user['name'], payload.target, text, reply_to_id=payload.reply_to_id, mentions=mentions)
        db_ms = (time.perf_counter() - db_started_at) * 1000
        await broadcast_internal_chat_event(
            user,
            payload.target,
            text,
            mentions,
            message_id=message_id,
            created_at=created_at,
            request_started_at=request_started_at,
        )
        total_ms = (time.perf_counter() - request_started_at) * 1000
        logger.info(
            "[INTERNAL_CHAT_SEND_OK] message_id={} sender={} role={} target={} created_at={} db_ms={:.2f} total_ms={:.2f}",
            message_id,
            user.get("name", ""),
            user.get("role", ""),
            payload.target,
            created_at.isoformat(timespec="milliseconds"),
            db_ms,
            total_ms,
        )
        return {"status": "ok", "message_id": message_id}
    except Exception as exc:
        total_ms = (time.perf_counter() - request_started_at) * 1000
        logger.exception(
            "[INTERNAL_CHAT_SEND_ERROR] sender={} role={} target={} created_at={} total_ms={:.2f} error={}",
            user.get("name", ""),
            user.get("role", ""),
            payload.target,
            created_at.isoformat(timespec="milliseconds"),
            total_ms,
            exc,
        )
        raise

@app.post("/api/admin/panel_notice")
async def send_admin_panel_notice(payload: AdminPanelNoticeRequest, user: dict = Depends(get_current_user)):
    """Envia um comunicado visual em tempo real para perfis selecionados."""
    if not can_admin_action(user, "communication"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    text = (payload.text or "").strip()
    if not text:
        return {"status": "error", "message": "Digite uma mensagem para enviar."}

    allowed_targets = {"vendas_1", "vendas_2", "financeiro", "engenharia", "admin"}
    targets = []
    for item in payload.targets or []:
        role = (item or "").strip().lower()
        if role in allowed_targets and role not in targets:
            targets.append(role)
    if not targets:
        return {"status": "error", "message": "Selecione ao menos um destino."}

    event_payload = {
        "type": "panel_notice",
        "text": text,
        "targets": targets,
        "sender": user.get("name", "Admin"),
        "created_at": datetime.now(BR_TIMEZONE).isoformat(),
    }
    await manager.broadcast(json.dumps(event_payload, ensure_ascii=True))
    await record_audit_log(
        user,
        "panel_notice_sent",
        target=",".join(targets),
        details={"text": text, "targets": targets},
    )
    return {"status": "ok", "targets": targets}

@app.post("/api/internal_chat/send_media")
async def send_internal_media(target: str = Form(...), file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """Grava mídia local do chat interno e retorna o id da mensagem criada."""
    if not user: return {"status": "error"}
    request_started_at = time.perf_counter()
    created_at = datetime.now(BR_TIMEZONE)
    safe_filename = sanitize_upload_filename(file.filename)
    filename_with_time = f"int_{int(time.time())}_{safe_filename}"
    logger.info(
        "[INTERNAL_CHAT_MEDIA_START] sender={} role={} target={} filename={} content_type={} created_at={}",
        user.get("name", ""),
        user.get("role", ""),
        target,
        safe_filename,
        file.content_type or "",
        created_at.isoformat(timespec="milliseconds"),
    )
    try:
        upload_started_at = time.perf_counter()
        media_bytes = b""
        while content := await file.read(1024 * 1024):
            media_bytes += content
        uploaded_bytes = len(media_bytes)

        detect_started_at = time.perf_counter()
        sample = media_bytes[:512]
        media_type, _ = detect_media_payload(safe_filename, file.content_type or "", sample)
        detect_ms = (time.perf_counter() - detect_started_at) * 1000

        # Fase 5: persiste via storage (local/S3) e usa a referência retornada.
        media_ref = await storage.save_media(get_current_tenant(), filename_with_time, media_bytes)
        upload_ms = (time.perf_counter() - upload_started_at) * 1000

        body = f"[{media_type.upper()}] {media_ref}"
        db_started_at = time.perf_counter()
        message_id = await db.save_internal_message(user['name'], target, body)
        db_ms = (time.perf_counter() - db_started_at) * 1000
        await broadcast_internal_chat_event(
            user,
            target,
            body,
            message_id=message_id,
            created_at=created_at,
            request_started_at=request_started_at,
        )
        total_ms = (time.perf_counter() - request_started_at) * 1000
        logger.info(
            "[INTERNAL_CHAT_MEDIA_OK] message_id={} sender={} role={} target={} created_at={} media_type={} bytes={} upload_ms={:.2f} detect_ms={:.2f} db_ms={:.2f} total_ms={:.2f}",
            message_id,
            user.get("name", ""),
            user.get("role", ""),
            target,
            created_at.isoformat(timespec="milliseconds"),
            media_type,
            uploaded_bytes,
            upload_ms,
            detect_ms,
            db_ms,
            total_ms,
        )
        return {"status": "ok", "message_id": message_id}
    except Exception as exc:
        total_ms = (time.perf_counter() - request_started_at) * 1000
        logger.exception(
            "[INTERNAL_CHAT_MEDIA_ERROR] sender={} role={} target={} filename={} created_at={} total_ms={:.2f} error={}",
            user.get("name", ""),
            user.get("role", ""),
            target,
            safe_filename,
            created_at.isoformat(timespec="milliseconds"),
            total_ms,
            exc,
        )
        raise

@app.post("/api/internal_chat/react")
async def react_internal_chat(payload: InternalChatReactionRequest, user: dict = Depends(get_current_user)):
    """Registra reação em mensagem interna visível e recarrega conversas abertas."""
    if not user: return {"status": "error"}
    emoji = (payload.emoji or "").strip()[:8]
    if not emoji:
        return {"status": "error", "message": "Reação inválida."}
    original = await db.get_internal_message(payload.message_id)
    if not original:
        return {"status": "error", "message": "Mensagem não encontrada."}
    if not can_read_internal_message(original, user):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Sem acesso à mensagem."})
    if original.get("deleted_at"):
        return {"status": "error", "message": "Mensagem apagada não pode receber reação."}
    await db.react_internal_message(payload.message_id, user['name'], emoji)
    await broadcast_internal_chat_update(original)
    return {"status": "ok"}

@app.post("/api/internal_chat/edit")
async def edit_internal_chat(payload: InternalChatEditRequest, user: dict = Depends(get_current_user)):
    """Edita mensagem interna textual própria ou por admin e recarrega painéis."""
    if not user: return {"status": "error"}
    original = await db.get_internal_message(payload.message_id)
    if not original:
        return {"status": "error", "message": "Mensagem não encontrada."}
    if not can_manage_internal_message(original, user):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Sem permissão para editar."})
    if original.get("deleted_at"):
        return {"status": "error", "message": "Mensagem apagada não pode ser editada."}
    text = (payload.text or "").strip()
    if not text:
        return {"status": "error", "message": "Mensagem vazia."}
    original_text = original.get("msg_text") or ""
    if original_text.startswith(("[IMAGE] ", "[VIDEO] ", "[AUDIO] ", "[PTT] ", "[DOCUMENT] ")):
        return {"status": "error", "message": "Mídia interna não pode ser editada por texto."}
    mentions = extract_internal_mentions(text)
    await db.edit_internal_message(payload.message_id, text, mentions)
    await broadcast_internal_chat_update(original)
    return {"status": "ok"}

@app.post("/api/internal_chat/delete")
async def delete_internal_chat(payload: InternalChatMessageActionRequest, user: dict = Depends(get_current_user)):
    """Apaga mensagem interna para todos por exclusão lógica auditável."""
    if not user: return {"status": "error"}
    original = await db.get_internal_message(payload.message_id)
    if not original:
        return {"status": "error", "message": "Mensagem não encontrada."}
    if not can_manage_internal_message(original, user):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Sem permissão para apagar."})
    await db.delete_internal_message(payload.message_id)
    await broadcast_internal_chat_update(original)
    return {"status": "ok"}

@app.post("/api/internal_chat/pin")
async def pin_internal_chat(payload: InternalChatMessageActionRequest, user: dict = Depends(get_current_user)):
    """Fixa mensagem interna na conversa acessível ao usuário atual."""
    if not user: return {"status": "error"}
    original = await db.get_internal_message(payload.message_id)
    if not original:
        return {"status": "error", "message": "Mensagem não encontrada."}
    if not can_read_internal_message(original, user):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Sem acesso à mensagem."})
    if original.get("deleted_at"):
        return {"status": "error", "message": "Mensagem apagada não pode ser fixada."}
    await db.pin_internal_message(payload.message_id, user['name'])
    await broadcast_internal_chat_update(original)
    return {"status": "ok"}

@app.post("/api/internal_chat/unpin")
async def unpin_internal_chat(payload: InternalChatMessageActionRequest, user: dict = Depends(get_current_user)):
    """Remove fixação de mensagem interna na conversa acessível ao usuário."""
    if not user: return {"status": "error"}
    original = await db.get_internal_message(payload.message_id)
    if not original:
        return {"status": "error", "message": "Mensagem não encontrada."}
    if not can_read_internal_message(original, user):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Sem acesso à mensagem."})
    await db.unpin_internal_message(payload.message_id)
    await broadcast_internal_chat_update(original)
    return {"status": "ok"}

@app.post("/api/internal_chat/forward")
async def forward_internal_chat(payload: InternalChatForwardRequest, user: dict = Depends(get_current_user)):
    """Encaminha mensagem interna não apagada e retorna o id da cópia criada."""
    if not user: return {"status": "error"}
    request_started_at = time.perf_counter()
    created_at = datetime.now(BR_TIMEZONE)
    original = await db.get_internal_message(payload.message_id)
    if not original:
        return {"status": "error", "message": "Mensagem não encontrada."}
    if not can_read_internal_message(original, user):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Sem acesso à mensagem."})
    if original.get("deleted_at"):
        return {"status": "error", "message": "Mensagem apagada não pode ser encaminhada."}
    text = f"[ENCAMINHADA de {original.get('sender_name') or 'Equipe'}] {original.get('msg_text') or ''}"
    mentions = extract_internal_mentions(text)
    message_id = await db.save_internal_message(user['name'], payload.target, text, forwarded_from_id=payload.message_id, mentions=mentions)
    await broadcast_internal_chat_event(
        user,
        payload.target,
        text,
        mentions,
        message_id=message_id,
        created_at=created_at,
        request_started_at=request_started_at,
    )
    return {"status": "ok", "message_id": message_id}

@app.get("/api/calendar/{year_month}")
async def get_calendar(year_month: str, user: dict = Depends(get_current_user)):
    """Lista eventos da agenda no mês `YYYY-MM` solicitado."""
    if not user: return []
    target_user = user['name'] if user['role'] != 'admin' else 'admin'
    return format_client_datetimes(await db.get_calendar_events_month(target_user, year_month, user.get("role", "")))

@app.get("/api/calendar/due/tasks")
async def get_due_calendar_tasks(user: dict = Depends(get_current_user)):
    """Lista tarefas vencidas da agenda inteligente para destaque operacional."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return format_client_datetimes(await db.get_due_calendar_events_for_user(user["name"], user["role"]))

@app.post("/api/calendar")
async def post_calendar(payload: CalendarEventRequest, user: dict = Depends(get_current_user)):
    """Cria tarefa inteligente de agenda com horário, responsável e lembrete recorrente."""
    if not user: return {"status": "error"}
    target_user = user['name'] if user['role'] != 'admin' else 'admin'
    assigned_to = (payload.assigned_to or "").strip() or target_user
    try:
        due_at = parse_calendar_due_at(payload.date, payload.time)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    clean_phone = normalize_whatsapp_jid(payload.phone) if payload.phone else ""
    if clean_phone and not payload.assigned_to:
        contact = await db.get_user(clean_phone)
        if contact and contact.get("assigned_to"):
            assigned_to = contact["assigned_to"]
    await db.add_calendar_event(
        target_user,
        payload.date,
        payload.note,
        phone=clean_phone,
        assigned_to=assigned_to,
        due_at=due_at,
        repeat_until_done=payload.repeat_until_done,
    )
    await record_audit_log(
        user,
        "calendar_event_created",
        phone=clean_phone,
        target=assigned_to,
        details={"date": payload.date, "time": payload.time, "note": payload.note, "repeat_until_done": payload.repeat_until_done},
    )
    return {"status": "ok"}

@app.post("/api/calendar/{event_id}/complete")
async def complete_calendar(event_id: int, user: dict = Depends(get_current_user)):
    """Marca tarefa de agenda como cliente contatado."""
    if not user: return {"status": "error"}
    event = await db.get_calendar_event(event_id)
    if not calendar_event_owner(event, user):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Sem acesso à tarefa."})
    await db.complete_calendar_event(event_id)
    await record_audit_log(
        user,
        "calendar_event_completed",
        phone=event.get("phone") or "",
        target=event.get("assigned_to") or event.get("user_name") or "",
        details={"event_id": event_id, "note": event.get("note"), "due_at": event.get("due_at")},
    )
    await manager.broadcast("calendar_update")
    return {"status": "ok"}

@app.post("/api/calendar/{event_id}/reschedule")
async def reschedule_calendar(event_id: int, payload: CalendarRescheduleRequest, user: dict = Depends(get_current_user)):
    """Remarca tarefa de agenda e mantém lembretes ativos."""
    if not user: return {"status": "error"}
    event = await db.get_calendar_event(event_id)
    if not calendar_event_owner(event, user):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Sem acesso à tarefa."})
    try:
        due_at = parse_calendar_due_at(payload.date, payload.time)
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    await db.reschedule_calendar_event(event_id, payload.date, due_at)
    await redis_client.delete(f"calendar_reminder:{event_id}")
    await record_audit_log(
        user,
        "calendar_event_rescheduled",
        phone=event.get("phone") or "",
        target=event.get("assigned_to") or event.get("user_name") or "",
        details={"event_id": event_id, "old_due_at": event.get("due_at"), "new_date": payload.date, "new_time": payload.time},
    )
    await manager.broadcast("calendar_update")
    return {"status": "ok"}

@app.delete("/api/calendar/{event_id}")
async def del_calendar(event_id: int, user: dict = Depends(get_current_user)):
    """Cancela uma tarefa de agenda pelo identificador."""
    if not user: return {"status": "error"}
    event = await db.get_calendar_event(event_id)
    if not calendar_event_owner(event, user):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Sem acesso à tarefa."})
    await db.delete_calendar_event(event_id)
    await redis_client.delete(f"calendar_reminder:{event_id}")
    await record_audit_log(
        user,
        "calendar_event_cancelled",
        phone=event.get("phone") or "",
        target=event.get("assigned_to") or event.get("user_name") or "",
        details={"event_id": event_id, "note": event.get("note"), "due_at": event.get("due_at")},
    )
    await manager.broadcast("calendar_update")
    return {"status": "ok"}

@app.post("/api/admin/users")
async def admin_save_user(payload: AdminUserRequest, user: dict = Depends(get_current_user)):
    """Cria ou atualiza usuário quando o perfil possui grupo `users`."""
    if not can_admin_action(user, "users"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    pwd = payload.password
    username = payload.username
    role = payload.role
    name = payload.name
    is_active = int(payload.is_active)
    existing_user = await db.get_system_user(username)

    # Fase 4: aplica o limite de usuários do plano ao criar um novo usuário.
    if not existing_user:
        ok, current, limit = await db.check_tenant_limit(get_current_tenant(), "users")
        if not ok:
            return JSONResponse(status_code=402, content={"error": f"Limite de usuários do plano atingido ({current}/{limit}). Faça upgrade do plano."})

    if pwd:
        pwd_hash_bytes = await run_in_threadpool(bcrypt.hashpw, pwd.encode('utf-8'), bcrypt.gensalt())
        pwd_hash = pwd_hash_bytes.decode('utf-8')
    else:
        pwd_hash = None
        
    await db.upsert_system_user(username, pwd_hash, role, name, is_active)
    if existing_user:
        await db.rename_seller_status(existing_user.get("name"), name)
    if role in OPERATOR_ROLES:
        if is_active:
            default_status = get_seller_auto_status() if role in ["vendas_1", "vendas_2"] else "online"
            await db.ensure_seller_status(name, default_status)
        else:
            await db.set_seller_status(name, "offline")
    await record_audit_log(
        user,
        "system_user_updated" if existing_user else "system_user_created",
        target=name,
        details={
            "username": username,
            "role": role,
            "is_active": is_active,
            "previous_name": existing_user.get("name") if existing_user else "",
            "previous_role": existing_user.get("role") if existing_user else "",
            "previous_is_active": existing_user.get("is_active") if existing_user else "",
        }
    )
    logger.info(f"Usuário atualizado pelo admin: {username}")
    return {"status": "ok"}

@app.post("/api/admin/settings")
async def admin_save_settings(payload: AdminSettingsRequest, user: dict = Depends(get_current_user)):
    """Atualiza textos globais do bot para perfis com grupo `settings`."""
    if not can_admin_action(user, "settings"):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.update_bot_setting('greeting', payload.greeting)
    await db.update_bot_setting('menu', payload.menu)
    await db.update_bot_setting('off_hours', payload.off_hours)
    logger.info("Configurações do bot atualizadas.")
    return {"status": "ok"}

@app.post("/api/status/send")
async def send_status_api(request: Request, user: dict = Depends(get_current_user)):
    """Dispara status do WhatsApp para a carteira segmentada do operador."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    form = await request.form()
    status_type = form.get('type')
    filter_word = form.get('filter_word', '').strip().lower()
    seller_name = user.get("name", "")
    seller_role = user.get("role", "")
    limit_val = str(form.get('limit', '50')).strip()
    allowed_status_limits = {"10", "20", "30", "50"}
    if limit_val not in allowed_status_limits:
        logger.warning(
            f"[STATUS_SEND_LIMIT_NORMALIZED] seller={seller_name} raw_limit={limit_val or '-'} normalized=50"
        )
        limit_val = "50"
    list_id = parse_int_or_zero(form.get('list_id'))
    logger.info(
        f"[STATUS_SEND_START] seller={seller_name} role={seller_role} type={status_type} "
        f"limit={limit_val} list_id={list_id or 'all'} filter='{filter_word or '-'}'"
    )

    if status_type not in {"text", "image"}:
        logger.warning(f"[STATUS_SEND_REJECTED] seller={seller_name} reason=invalid_type type={status_type}")
        return {"status": "error", "message": "Tipo de status inválido. Use texto ou imagem."}

    content = ""
    if status_type == 'text':
        content = (form.get('content') or "").strip()
        if not content:
            logger.warning(f"[STATUS_SEND_REJECTED] seller={seller_name} reason=empty_text")
            return {"status": "error", "message": "Digite a mensagem do status antes de publicar."}
    else:
        file = form.get('file')
        if not file:
            logger.warning(f"[STATUS_SEND_REJECTED] seller={seller_name} reason=image_missing")
            return {"status": "error", "message": "Nenhuma imagem selecionada."}
        mime = file.content_type or "image/jpeg"
        if not mime.startswith("image/"):
            logger.warning(
                f"[STATUS_SEND_REJECTED] seller={seller_name} reason=image_mime_invalid mime={mime}"
            )
            return {"status": "error", "message": "Status de mídia aceita apenas imagem neste painel."}
        content_bytes = await file.read()
        if not content_bytes:
            logger.warning(f"[STATUS_SEND_REJECTED] seller={seller_name} reason=image_empty_file")
            return {"status": "error", "message": "A imagem selecionada está vazia."}
        content_b64 = base64.b64encode(content_bytes).decode('utf-8')
        content = f"data:{mime};base64,{content_b64}"

    target_session = tc.role_session_map().get(seller_role, await db.get_tenant_instance(get_current_tenant()))
    clients = await db.get_clients(user['role'], user['name'], tab='contatos', limit=50000, offset=0, list_id=list_id)
    total_clients = len(clients)
    filtered_out_by_text = 0
    invalid_reason_counts: Dict[str, int] = {}
    invalid_samples: List[str] = []
    seen_jids = set()
    target_records: List[Dict[str, str]] = []

    for c in clients:
        if filter_word:
            searchable_text = f"{c.get('name', '')} {c.get('notes', '')} {c.get('tags_data', '')} {c.get('list_data', '')} {c.get('extra_info', '')}".lower()
            if filter_word not in searchable_text:
                filtered_out_by_text += 1
                continue
        phone = c.get('phone', '')
        normalized_jid, reason = normalize_status_target_jid(phone)
        if reason:
            invalid_reason_counts[reason] = invalid_reason_counts.get(reason, 0) + 1
            if len(invalid_samples) < 5:
                invalid_samples.append(str(phone))
            continue
        if normalized_jid in seen_jids:
            invalid_reason_counts["duplicated_target"] = invalid_reason_counts.get("duplicated_target", 0) + 1
            continue
        seen_jids.add(normalized_jid)
        target_records.append(
            {
                "target_jid": normalized_jid,
                "contact_name": c.get("name", "") or "",
                "assigned_to": c.get("assigned_to", "") or "",
            }
        )

    total_valid_before_limit = len(target_records)
    limit_int = min(int(limit_val), 50)
    target_records = target_records[:limit_int]
    jid_list = [target["target_jid"] for target in target_records]

    logger.info(
        f"[STATUS_TARGETS_READY] seller={seller_name} session={target_session} type={status_type} "
        f"base_contacts={total_clients} filtered_by_text={filtered_out_by_text} "
        f"valid_before_limit={total_valid_before_limit} final_targets={len(jid_list)} "
        f"skipped={sum(invalid_reason_counts.values())} reasons={summarize_skip_reasons(invalid_reason_counts)} "
        f"samples={invalid_samples if invalid_samples else '[]'}"
    )

    if not jid_list:
        if filter_word:
            return {
                "status": "error",
                "message": (
                    f"Nenhum cliente válido encontrado com a palavra '{filter_word}' na sua carteira. "
                    "Verifique prefixos e números dos contatos."
                ),
            }
        return {
            "status": "error",
            "message": "Nenhum contato válido para status. Revise contatos com telefone fora do padrão do WhatsApp.",
        }

    result = await evo.send_status(target_session, status_type, content, jid_list)
    if result.get("ok"):
        status_id = evo.extract_message_id(result.get("data")) or f"status_{int(time.time())}_{user['username']}"
        preview = content[:180] if status_type == "text" else "Imagem publicada"
        await db.save_status_post(status_id, user['name'], target_session, status_type, preview, len(jid_list), filter_word)
        await db.save_status_targets(status_id, target_records)
        logger.info(
            f"[STATUS_SEND_SUCCESS] seller={seller_name} session={target_session} type={status_type} "
            f"targets={len(jid_list)} tracked_targets={len(target_records)} status_id={status_id} filter='{filter_word or '-'}'"
        )
        logger.info(
            f"[STATUS_VIEW_TRACKING_READY] session={target_session} status_id={status_id} "
            f"tracked_targets={len(target_records)} waiting_event=messages.update remote_jid=status@broadcast"
        )
        await manager.broadcast("status_update")
        return {
            "status": "ok",
            "count": len(jid_list),
            "filter": filter_word,
            "status_id": status_id,
            "skipped_invalid": sum(invalid_reason_counts.values()),
        }
    detail = result.get("body") or result.get("error") or "Sem detalhe retornado pela Evolution API."
    logger.error(
        f"[STATUS_SEND_FAILED] seller={seller_name} session={target_session} type={status_type} "
        f"targets={len(jid_list)} status_code={result.get('status_code')} attempt={result.get('attempt')} "
        f"payload_style={result.get('payload_style')} content_variant={result.get('content_variant')} "
        f"detail={detail}"
    )
    return {
        "status": "error",
        "message": (
            "Falha ao publicar status na Evolution. "
            f"Instância: {target_session}. Detalhe técnico: {detail[:240]}"
        ),
    }

@app.get("/api/status/posts")
async def get_status_posts_api(user: dict = Depends(get_current_user)):
    """Lista status publicados pelo operador com total de visualizações conhecidas."""
    if not user: return []
    posts = await db.get_status_posts(user['name'], user['role'])
    for post in posts:
        if post.get("created_at"):
            post["created_at"] = format_datetime_br(post["created_at"])
    return posts

@app.get("/api/status/{status_id}/viewers")
async def get_status_viewers_api(status_id: str, user: dict = Depends(get_current_user)):
    """Lista contatos que visualizaram um status quando a Evolution envia esse evento."""
    if not user: return []
    viewers = await db.get_status_viewers(status_id, user['name'], user['role'])
    for viewer in viewers:
        if viewer.get("viewed_at"):
            viewer["viewed_at"] = format_datetime_br(viewer["viewed_at"])
    return viewers

@app.get("/api/status/{status_id}/targets")
async def get_status_targets_api(status_id: str, filter: str = "all", user: dict = Depends(get_current_user)):
    """Lista destinatários enviados, visualizados ou pendentes de um status."""
    if not user:
        return {"summary": {"total": 0, "viewed": 0, "pending": 0}, "targets": []}
    summary = await db.get_status_target_summary(status_id, user['name'], user['role'])
    targets = await db.get_status_targets(status_id, user['name'], user['role'], filter)
    for target in targets:
        if target.get("sent_at"):
            target["sent_at"] = format_datetime_br(target["sent_at"])
        if target.get("viewed_at"):
            target["viewed_at"] = format_datetime_br(target["viewed_at"])
    return {"summary": summary, "targets": targets}

@app.delete("/api/status/{status_id}")
async def delete_status_post_api(status_id: str, user: dict = Depends(get_current_user)):
    """Remove um status do histórico local e seus rastros de auditoria."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    deleted = await db.delete_status_post(status_id, user['name'], user['role'])
    if not deleted:
        return JSONResponse(status_code=404, content={"error": "Status não encontrado ou fora do seu escopo"})
    await record_audit_log(
        user,
        "status_post_deleted",
        target=status_id,
        details={
            "seller_name": deleted.get("seller_name"),
            "session_id": deleted.get("session_id"),
            "target_count": deleted.get("target_count"),
            "scope": "local_history",
        }
    )
    logger.info(
        f"[STATUS_POST_DELETED] actor={user.get('name')} role={user.get('role')} "
        f"status_id={status_id} seller={deleted.get('seller_name')} targets={deleted.get('target_count')}"
    )
    await manager.broadcast("status_update")
    return {"status": "ok"}

@app.get("/api/status/diagnostics")
async def get_status_diagnostics_api(user: dict = Depends(get_current_user)):
    """Diagnostica o fluxo ponta a ponta de status e visualizações Evolution."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    rows = await db.get_status_diagnostics(user['name'], user['role'])
    diagnostics = []
    totals = {"posts": 0, "tracked_targets": 0, "tracked_views": 0, "pending_targets": 0}
    for row in rows:
        tracked_targets = int(row.get("tracked_targets") or 0)
        tracked_views = int(row.get("tracked_views") or 0)
        pending_targets = int(row.get("pending_targets") or 0)
        legacy_views = int(row.get("legacy_views") or 0)
        target_count = int(row.get("target_count") or 0)
        status_label = "ok"
        action_hint = "Fluxo completo: envio rastreado e visualizações recebidas."
        if tracked_targets <= 0 and target_count > 0:
            status_label = "legacy_no_targets"
            action_hint = "Status anterior ao rastreio detalhado; publique um novo status teste para validar destinatários reais."
        elif tracked_targets <= 0:
            status_label = "no_targets"
            action_hint = "Sem destinatários rastreados; verifique filtros, lista e contatos válidos."
        elif tracked_views <= 0 and legacy_views <= 0:
            status_label = "waiting_webhook"
            action_hint = "Envio rastreado, mas ainda sem leitura da Evolution; aguarde visualização real ou valide se a instância envia messages.update."
        elif tracked_views <= 0 and legacy_views > 0:
            status_label = "legacy_views_only"
            action_hint = "Há visualizações antigas, mas nenhum alvo detalhado marcado; confira se o JID do participant bate com status_targets."

        totals["posts"] += 1
        totals["tracked_targets"] += tracked_targets
        totals["tracked_views"] += tracked_views
        totals["pending_targets"] += pending_targets

        item = dict(row)
        item.update({
            "status_label": status_label,
            "action_hint": action_hint,
            "progress": round((tracked_views / tracked_targets) * 100) if tracked_targets else 0,
        })
        if item.get("created_at"):
            item["created_at"] = format_datetime_br(item["created_at"])
        if item.get("latest_view_at"):
            item["latest_view_at"] = format_datetime_br(item["latest_view_at"])
        if item.get("latest_legacy_view_at"):
            item["latest_legacy_view_at"] = format_datetime_br(item["latest_legacy_view_at"])
        diagnostics.append(item)

    logger.info(
        f"[STATUS_DIAGNOSTIC_CHECK] actor={user.get('name')} role={user.get('role')} "
        f"posts={totals['posts']} tracked_targets={totals['tracked_targets']} "
        f"tracked_views={totals['tracked_views']} pending={totals['pending_targets']}"
    )
    return {
        "summary": totals,
        "diagnostics": diagnostics,
        "test_guidance": (
            "Para validar com segurança, publique um status de texto com limite baixo e filtro/lista de teste. "
            "Depois visualize pelo WhatsApp de um contato incluído e acompanhe logs STATUS_VIEW_* e este diagnóstico."
        ),
    }

@app.post("/api/chat/send")
async def send_chat_message(payload: ChatSendRequest, user: dict = Depends(get_current_user)):
    """Envia mensagem textual ao cliente e registra a cópia local."""
    if not user: return {"status": "error"}
    client_data = await db.get_user(payload.phone)
    if not can_edit_contact_record(user, client_data):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Contato fora do escopo de envio."})
    target_session = client_data.get('session_id', 'vendas') if client_data else 'vendas'
    
    if client_data and client_data.get('stage') in ['menu_wait', 'pendente_assumir', 'carteira']:
        assigned_to = client_data.get('assigned_to') if user.get('role') == 'admin' else user['name']
        await db.update_user_stage(payload.phone, 'em_atendimento', assigned_to)
        await manager.broadcast("update")
        
    quoted_id = payload.quoted_id
    outgoing_text = await finalize_outgoing_text(payload.phone, user, payload.text, contact=client_data)
    evo_id = await evo.send_message(payload.phone, outgoing_text, target_session, quoted_id)
    if not evo_id:
        logger.error(f"Evolution nao confirmou envio de texto para {payload.phone} na sessao {target_session}.")
        return {"status": "error", "message": "A Evolution API nao confirmou o envio da mensagem."}
    await db.save_message(payload.phone, user['name'], outgoing_text, "text", str(evo_id), 1, target_session)
    await manager.broadcast(f"chat_{payload.phone}")
    return {"status": "ok"}

@app.post("/api/chat/send_media")
async def send_chat_media(phone: str = Form(...), file: UploadFile = File(...), quoted_id: str = Form(None), user: dict = Depends(get_current_user)):
    """Envia mídia para o cliente, salvando arquivo e histórico local."""
    if not user: return {"status": "error"}
    client_data = await db.get_user(phone)
    if not can_edit_contact_record(user, client_data):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Contato fora do escopo de envio."})
    target_session = client_data.get('session_id', 'vendas') if client_data else 'vendas'
    
    if client_data and client_data.get('stage') in ['menu_wait', 'pendente_assumir', 'carteira']:
        assigned_to = client_data.get('assigned_to') if user.get('role') == 'admin' else user['name']
        await db.update_user_stage(phone, 'em_atendimento', assigned_to)
        await manager.broadcast("update")
        
    safe_filename = sanitize_upload_filename(file.filename)
    filename_with_time = f"{int(time.time())}_{safe_filename}"
    # Fase 5: acumula em memória respeitando o limite e persiste via storage.
    content = b""
    while chunk := await file.read(1024 * 1024):
        content += chunk
        if len(content) > MAX_MEDIA_UPLOAD_BYTES:
            limit_mb = getattr(settings, "MAX_MEDIA_UPLOAD_MB", 32)
            return {"status": "error", "message": f"Arquivo acima do limite de {limit_mb} MB."}
    media_ref = await storage.save_media(get_current_tenant(), filename_with_time, content)
    media_type, mime = detect_media_payload(safe_filename, file.content_type or "", content[:512])
    b64_data = base64.b64encode(content).decode('utf-8')
    evo_id = await evo.send_media(phone, b64_data, media_type, mime, safe_filename, caption="", session=target_session, quoted_id=quoted_id)
    if not evo_id:
        logger.error(f"Evolution nao confirmou envio de midia {safe_filename} ({mime}) para {phone} na sessao {target_session}.")
        return {"status": "error", "message": "A Evolution API nao confirmou o envio da midia. Verifique conexão, número e formato do arquivo."}
    body = f"[{media_type.upper()}] {media_ref}"
    await db.save_message(phone, user['name'], body, media_type, str(evo_id), 1, target_session)
    await manager.broadcast(f"chat_{phone}")
    return {"status": "ok"}

@app.post("/api/chat/react")
async def react_to_message(payload: ReactionRequest, user: dict = Depends(get_current_user)):
    """Envia reação a uma mensagem pela Evolution API."""
    if not user: return {"status": "error"}
    client_data = await db.get_user(payload.phone)
    if not can_edit_contact_record(user, client_data):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Contato fora do escopo de edição."})
    target_session = client_data.get('session_id', 'vendas') if client_data else 'vendas'
    message = await db.get_message_by_waha_id(payload.phone, payload.message_id)
    from_me = bool(message and message.get("sender") != payload.phone)
    await evo.send_reaction(payload.phone, payload.message_id, payload.emoji, target_session, from_me)
    return {"status": "ok"}

@app.post("/api/chat/edit")
async def edit_chat_message(payload: EditMessageRequest, user: dict = Depends(get_current_user)):
    """Edita mensagem textual enviada recentemente, respeitando a janela do WhatsApp."""
    if not user:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Acesso negado"})
    phone = payload.phone
    message_id = payload.message_id
    new_text = (payload.new_text or "").strip()
    if not phone or not message_id or not new_text:
        return {"status": "error", "message": "Dados inválidos para edição."}
    if len(new_text) > 4096:
        return {"status": "error", "message": "Mensagem muito longa para edição."}
    client_data = await db.get_user(phone)
    if not can_edit_contact_record(user, client_data):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Contato fora do escopo de edição."})
    message = await db.get_message_by_waha_id(phone, message_id)
    if not message:
        return {"status": "error", "message": "Mensagem não encontrada no histórico local."}
    if not is_remote_outgoing_message(message, phone):
        return {"status": "error", "message": "Apenas mensagens enviadas pela instância podem ser editadas."}
    if user["role"] != "admin" and message.get("sender") not in [user["name"], "bot"]:
        return {"status": "error", "message": "Você só pode editar mensagens enviadas por você ou pelo bot."}
    if message.get("msg_type") != "text":
        return {"status": "error", "message": "A edição é permitida apenas para mensagens de texto."}
    if message_age_seconds(message) > EDIT_MESSAGE_LIMIT_SECONDS:
        return {"status": "error", "message": "A janela segura de edição é de 15 minutos."}
    target_session = client_data.get("session_id", "vendas") if client_data else "vendas"
    result = await evo.edit_message(phone, message_id, new_text, target_session)
    if not result.get("ok"):
        return {"status": "error", "message": "A Evolution API recusou a edição. Verifique conexão e prazo permitido pelo WhatsApp."}
    await db.update_message_text_by_waha_id(phone, message_id, new_text)
    await manager.broadcast(f"chat_{phone}")
    return {"status": "ok"}

@app.post("/api/chat/delete")
async def delete_chat_message(payload: DeleteMessageRequest, user: dict = Depends(get_current_user)):
    """Apaga para todos uma mensagem enviada dentro da janela operacional segura."""
    if not user:
        return JSONResponse(status_code=403, content={"status": "error", "message": "Acesso negado"})
    phone = payload.phone
    message_id = payload.message_id
    if not phone or not message_id:
        return {"status": "error", "message": "Dados inválidos para exclusão."}
    client_data = await db.get_user(phone)
    if not can_edit_contact_record(user, client_data):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Contato fora do escopo de edição."})
    message = await db.get_message_by_waha_id(phone, message_id)
    if not message:
        return {"status": "error", "message": "Mensagem não encontrada no histórico local."}
    if not is_remote_outgoing_message(message, phone):
        return {"status": "error", "message": "Apenas mensagens enviadas pela instância podem ser apagadas para todos."}
    if user["role"] != "admin" and message.get("sender") not in [user["name"], "bot"]:
        return {"status": "error", "message": "Você só pode apagar mensagens enviadas por você ou pelo bot."}
    if message_age_seconds(message) > DELETE_MESSAGE_LIMIT_SECONDS:
        return {"status": "error", "message": "A janela segura para apagar para todos é de 48 horas."}
    target_session = client_data.get("session_id", "vendas") if client_data else "vendas"
    result = await evo.delete_message_for_everyone(phone, message_id, target_session)
    if not result.get("ok"):
        return {"status": "error", "message": "A Evolution API recusou a exclusão. Verifique conexão e prazo permitido pelo WhatsApp."}
    await db.mark_message_deleted_by_waha_id(phone, message_id)
    await manager.broadcast(f"chat_{phone}")
    return {"status": "ok"}

@app.post("/api/chat/pin")
async def api_pin_message(payload: PinMessageRequest, user: dict = Depends(get_current_user)):
    """Fixa um texto importante no cadastro do contato."""
    if not user: return {"status": "error"}
    client_data = await db.get_user(payload.phone)
    if not can_edit_contact_record(user, client_data):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Contato fora do escopo de edição."})
    await db.pin_message(payload.phone, payload.text)
    await manager.broadcast(f"chat_{payload.phone}")
    return {"status": "ok"}

@app.post("/api/chat/unpin")
async def api_unpin_message(payload: PhonePayload, user: dict = Depends(get_current_user)):
    """Remove a mensagem fixada de um contato."""
    if not user: return {"status": "error"}
    client_data = await db.get_user(payload.phone)
    if not can_edit_contact_record(user, client_data):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Contato fora do escopo de edição."})
    await db.unpin_message(payload.phone)
    await manager.broadcast(f"chat_{payload.phone}")
    return {"status": "ok"}

@app.get("/api/chat/{phone}/pinned")
async def get_pinned(phone: str, user: dict = Depends(get_current_user)):
    """Consulta a mensagem fixada de um contato."""
    if not user: return {"status": "error"}
    client_data = await db.get_user(phone)
    return {"pinned_message": client_data.get('pinned_message', '') if client_data else ''}

@app.post("/api/chat/forward")
async def forward_chat_message(payload: ForwardMessageRequest, user: dict = Depends(get_current_user)):
    """Encaminha uma mensagem textual ou mídia já salva para outro contato."""
    if not user: return {"status": "error"}
    target_phone = payload.target_phone
    msg_id = payload.message_id
    msg = await db.get_message_by_id(msg_id)
    if not msg: return {"status": "error", "message": "Mensagem não encontrada."}
    client_data = await db.get_user(target_phone)
    if not can_edit_contact_record(user, client_data):
        return JSONResponse(status_code=403, content={"status": "error", "message": "Contato fora do escopo de envio."})
    target_session = client_data.get('session_id', 'vendas') if client_data else 'vendas'
    text = msg['msg_text']
    msg_type = msg['msg_type']
    
    if msg_type == 'text':
        evo_id = await evo.send_message(target_phone, text, target_session)
        if not evo_id:
            logger.error(f"Evolution nao confirmou encaminhamento de texto {msg_id} para {target_phone}.")
            return {"status": "error", "message": "A Evolution API nao confirmou o encaminhamento."}
        await db.save_message(target_phone, user['name'], text, "text", str(evo_id), 1, target_session)
    else:
        try:
            filepath = text.split("] ")[1].strip()
            # Fase 5: lê via storage (suporta caminho local legado e URL S3).
            content = await storage.read_media(filepath)
            b64_data = base64.b64encode(content).decode('utf-8')
            filename = os.path.basename(filepath)
            mime = guess_media_mimetype(filename, msg_type)
            evo_id = await evo.send_media(target_phone, b64_data, msg_type, mime, filename, caption="Mensagem Encaminhada", session=target_session)
            if not evo_id:
                logger.error(f"Evolution nao confirmou encaminhamento de midia {msg_id} para {target_phone}.")
                return {"status": "error", "message": "A Evolution API nao confirmou o encaminhamento da midia."}
            await db.save_message(target_phone, user['name'], text, msg_type, str(evo_id), 1, target_session)
        except Exception as e:
            logger.error(f"Erro ao encaminhar mídia: {e}")
            return {"status": "error", "message": "Falha na mídia."}
            
    await manager.broadcast(f"chat_{target_phone}")
    return {"status": "ok"}

@app.get("/api/chat/{phone}/picture")
async def get_profile_picture(phone: str, user: dict = Depends(get_current_user)):
    """Busca a foto de perfil do contato na Evolution API."""
    if not user: return {"url": None}
    client_data = await db.get_user(phone)
    target_session = client_data.get('session_id', 'vendas') if client_data else 'vendas'
    url = await evo.get_profile_pic(phone, target_session)
    return {"url": url}

@app.get("/api/chat/{phone}")
async def get_chat_history(phone: str, user: dict = Depends(get_current_user)):
    """Retorna histórico local e sincroniza mensagens antigas sob demanda."""
    if not user: return []
    
    local_msgs = await db.get_messages(phone)
    
    if len(local_msgs) < 2:
        client_data = await db.get_user(phone)
        target_session = client_data.get('session_id', 'vendas') if client_data else 'vendas'
        logger.info(f"Histórico local vazio para {phone}. Iniciando sincronização Lazy Loading...")
        evo_msgs = await evo.fetch_history(phone, 30, target_session)
        
        if evo_msgs:
            for msg in reversed(evo_msgs):
                key = msg.get('key', {})
                remote_jid = key.get('remoteJid', '')
                # Blindagem: evita gravar histórico de outro chat no contato aberto.
                if remote_jid and remote_jid != phone:
                    continue
                from_me = key.get('fromMe', False)
                evo_id = key.get('id')
                sender = phone if not from_me else "bot"
                msg_obj = msg.get('message', {})
                if not msg_obj: continue
                msg_type_raw = msg.get('messageType', '') or list(msg_obj.keys())[0] if msg_obj.keys() else ""
                body = ""
                msg_type = "text"
                if msg_type_raw in ['conversation', 'extendedTextMessage']:
                    body = msg_obj.get('conversation') or msg_obj.get('extendedTextMessage', {}).get('text', '')
                elif msg_type_raw in ['imageMessage', 'videoMessage', 'audioMessage', 'ptvMessage', 'documentMessage']:
                    tipo = msg_type_raw.replace('Message', '').replace('ptv', 'audio').upper()
                    body = f"[{tipo}] Mídia do histórico."
                if body:
                    await db.save_message(phone, sender, body, msg_type, evo_id, 3, target_session)
            
            local_msgs = await db.get_messages(phone)
            logger.info(f"Sincronização de {phone} concluída com sucesso!")
    
    return format_message_timestamps(local_msgs)

@app.post("/api/log_external_access")
async def log_external_access(payload: PhonePayload, user: dict = Depends(get_current_user)):
    """Registra no histórico quando o operador abre o app externo do WhatsApp."""
    if not user: return {"status": "error"}
    phone = payload.phone
    log_msg = f"[Auditoria] {user['name']} acessou o App Externo."
    target_session = tc.role_session_map().get(user['role'], await db.get_tenant_instance(get_current_tenant()))
    await db.save_message(phone, "sistema", log_msg, "system", f"sys_{time.time()}", 3, target_session)
    await record_audit_log(user, "external_whatsapp_opened", phone=phone, details={"session_id": target_session})
    await manager.broadcast(f"chat_{phone}")
    return {"status": "ok"}

@app.post("/api/transfer")
async def transfer_chat(payload: TransferRequest, user: dict = Depends(get_current_user)):
    """Move um atendimento para a fila de outro setor ou vendedor."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    phone = payload.phone
    target = payload.target
    previous = await db.get_user(phone)
    await db.update_user_stage(phone, "pendente_assumir", target)
    await record_audit_log(
        user,
        "chat_transferred",
        phone=phone,
        target=target,
        details={
            "previous_assigned_to": previous.get("assigned_to") if previous else "",
            "previous_stage": previous.get("stage") if previous else "",
        }
    )
    await manager.broadcast("update")
    logger.info(f"Chat {phone} transferido para {target}")
    return {"status": "ok"}

@app.post("/api/update_status")
async def update_status(payload: StatusUpdateRequest, user: dict = Depends(get_current_user)):
    """Atualiza presença manual; vendedoras podem ser sobrescritas pela agenda automática."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.set_seller_status(user['name'], payload.status)
    await manager.broadcast("update")
    return {"status": "ok"}

@app.post("/api/edit_contact")
async def edit_contact(payload: EditContactRequest, user: dict = Depends(get_current_user)):
    """Atualiza nome, observações e informações extras de um contato."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    phone = payload.phone
    previous = await db.get_user(phone)
    if not can_edit_contact_record(user, previous):
        return JSONResponse(status_code=403, content={"error": "Contato fora do escopo de edição"})
    await db.update_contact_details(phone, payload.name, payload.notes, payload.extra)
    await record_audit_log(
        user,
        "contact_edited",
        phone=phone,
        details={
            "old_name": previous.get("name") if previous else "",
            "new_name": payload.name,
            "old_notes": previous.get("notes") if previous else "",
            "new_notes": payload.notes,
            "old_extra_info": previous.get("extra_info") if previous else "",
            "new_extra_info": payload.extra,
        }
    )
    await manager.broadcast("update")
    return {"status": "ok"}

@app.post("/api/take_chat")
async def take_chat(payload: PhonePayload, user: dict = Depends(get_current_user)):
    """Coloca o contato em atendimento pelo usuário logado."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    phone = payload.phone
    previous = await db.get_user(phone)
    await db.update_user_stage(phone, "em_atendimento", user['name'])
    await record_audit_log(
        user,
        "chat_taken",
        phone=phone,
        target=user['name'],
        details={
            "previous_assigned_to": previous.get("assigned_to") if previous else "",
            "previous_stage": previous.get("stage") if previous else "",
        }
    )
    await manager.broadcast("update")
    return {"status": "ok"}

@app.post("/api/finish_chat")
async def finish_chat(payload: FinishChatRequest, user: dict = Depends(get_current_user)):
    """Finaliza o atendimento e atualiza métricas de venda/sem venda e TMA."""
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    phone = payload.phone
    result = normalize_finish_result(payload.result)
    previous = await db.get_user(phone)
    await db.finish_attendance(phone, user['name'], result)
    await record_audit_log(
        user,
        "chat_finished",
        phone=phone,
        target=user['name'],
        details={
            "result": result,
            "previous_assigned_to": previous.get("assigned_to") if previous else "",
            "previous_stage": previous.get("stage") if previous else "",
        }
    )
    await manager.broadcast("update")
    logger.info(f"Chat {phone} finalizado. Resultado: {result}")
    payload = {"status": "ok", "result": result}
    if user["role"] != "admin":
        payload["metrics"] = await db.get_seller_metrics(user["name"])
    return payload

@app.get("/api/poll_updates")
async def poll_updates(user: dict = Depends(get_current_user)):
    """Retorna atendimentos atuais e métricas do dia (quando o usuário é vendas)."""
    if not user: return {}
    clientes = await db.get_clients(user['role'], user['name'], tab='home')
    format_client_datetimes(clientes)
    metrics = None
    if user["role"] != "admin":
        metrics = await db.get_seller_metrics(user["name"])
    return {"clientes": clientes, "metrics": metrics}

@app.get("/api/admin/global_metrics")
async def get_admin_global_metrics(user: dict = Depends(get_current_user)):
    """Fornece métricas globais para dashboards com leitura administrativa."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await db.get_global_metrics()

@app.get("/api/admin/attendance_breakdown")
async def get_admin_attendance_breakdown(user: dict = Depends(get_current_user)):
    """Retorna ativos/aguardando por usuário para admin e financeiro."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await db.get_attendance_breakdown_by_user()

@app.get("/api/admin/monthly_report")
async def get_admin_monthly_report(month: str = "", user: dict = Depends(get_current_user)):
    """Retorna relatório mensal por mês calendário para equipe e auditoria."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    try:
        return await db.get_monthly_team_report(month)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})

@app.get("/api/admin/user_performance")
async def get_admin_user_performance(seller: str, month: str = "", user: dict = Depends(get_current_user)):
    """Retorna 7/15 dias móveis e mês calendário para leitura administrativa."""
    if not can_read_admin_panel(user):
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    try:
        report = await db.get_user_performance_report(seller, month)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"error": str(exc)})
    if not report:
        return JSONResponse(status_code=404, content={"error": "Usuário não encontrado"})
    if report.get("user", {}).get("role") not in ("vendas_1", "vendas_2"):
        return JSONResponse(status_code=400, content={"error": "Relatório de vendas disponível apenas para usuários de vendas"})
    return report

@app.get("/api/seller/metrics")
async def get_current_seller_metrics(user: dict = Depends(get_current_user)):
    """Retorna métricas atuais da vendedora para atualização dinâmica do painel."""
    if not user or user["role"] == "admin":
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await db.get_seller_metrics(user["name"])

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    """Recebe eventos da Evolution API e agenda o processamento assíncrono."""
    headers = request.headers
    try:
        data = await request.json()
    except Exception:
        data = {}
        
    event = data.get("event")
    if event not in ["messages.upsert", "messages.update"]:
        return {"status": "ok"}
        
    token_head = headers.get("apikey") or headers.get("api-key") or headers.get("authorization")
    token_body = data.get("apikey")
    
    # Fase 0 (Hardening): a Evolution API envia o WEBHOOK_GLOBAL_AUTHORIZATION,
    # que corresponde ao WEBHOOK_SECRET. Validamos contra ele e também aceitamos
    # a EVO_API_KEY por compatibilidade com instalações antigas.
    senhas_validas = []
    for segredo in (settings.WEBHOOK_SECRET, settings.EVO_API_KEY):
        if segredo:
            senhas_validas.append(segredo)
            senhas_validas.append(f"Bearer {segredo}")
    
    if (token_head not in senhas_validas) and (token_body not in senhas_validas):
        logger.warning(f"Bloqueio de Segurança: {token_head} | {token_body}")
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    # Fase 7: enfileira o processamento (worker) quando JOB_QUEUE_ENABLED; caso
    # contrário roda inline via BackgroundTasks (comportamento padrão, sem regressão).
    await jobqueue.submit(redis_client, "process_webhook", data, background_tasks=background_tasks)
    return {"status": "ok"}

async def process_webhook(data: dict):
    """Processa mensagens recebidas, atualizações de ACK, roteamento e respostas."""
    try:
        event = data.get('event')
        session_id = data.get('instance')
        if not session_id: return

        # Fase 2 (Multi-tenancy): roteia o evento para o tenant dono da instância
        # Evolution. Executa em background, então precisamos ativar o contexto do
        # tenant AQUI — caso contrário todas as gravações cairiam no tenant padrão.
        # Fase 4: roteia por instância e ignora eventos de tenants suspensos.
        webhook_route = await db.get_tenant_route_by_instance(session_id)
        if webhook_route and webhook_route["status"] != "active":
            logger.info(f"Webhook ignorado: instância '{session_id}' de tenant suspenso.")
            return
        webhook_tenant_id = webhook_route["id"] if webhook_route else DEFAULT_TENANT_ID
        set_current_tenant(webhook_tenant_id)
        await tc.load_tenant_config(webhook_tenant_id)

        if event == 'messages.update':
            updates = data.get('data', [])
            if not isinstance(updates, list): updates = [updates]
            for update in updates:
                status_context = extract_status_update_context(update if isinstance(update, dict) else {})
                evo_id = status_context["status_id"]
                raw_status = status_context["raw_status"]
                ack_val = status_context["ack"]
                remote_jid = status_context["remote_jid"]
                participant = status_context["participant"]
                is_status_update = remote_jid == "status@broadcast" or (
                    not remote_jid and bool(participant) and bool(evo_id)
                )

                if is_status_update:
                    logger.info(
                        f"[STATUS_VIEW_UPDATE_RECEIVED] session={session_id} status_id={evo_id or '-'} "
                        f"remote_jid={remote_jid or '-'} raw_status={raw_status or '-'} ack={ack_val} "
                        f"participant={participant or '-'}"
                    )
                    if not evo_id:
                        logger.warning(
                            f"[STATUS_VIEW_MISSING_STATUS_ID] session={session_id} remote_jid={remote_jid or '-'} "
                            f"raw_status={raw_status or '-'} participant={participant or '-'}"
                        )
                    elif ack_val < 3:
                        logger.info(
                            f"[STATUS_VIEW_ACK_IGNORED] session={session_id} status_id={evo_id} "
                            f"raw_status={raw_status or '-'} ack={ack_val} participant={participant or '-'}"
                        )
                    elif not participant:
                        logger.warning(
                            f"[STATUS_VIEW_MISSING_PARTICIPANT] session={session_id} status_id={evo_id} "
                            f"raw_status={raw_status or '-'} ack={ack_val}"
                        )
                    else:
                        view_result = await db.add_status_view(evo_id, participant)
                        if not view_result.get("post_exists"):
                            logger.warning(
                                f"[STATUS_VIEW_UNKNOWN_STATUS_ID] session={session_id} status_id={evo_id} "
                                f"participant={participant} raw_status={raw_status or '-'} ack={ack_val}"
                            )
                        elif not view_result.get("target_updated"):
                            logger.warning(
                                f"[STATUS_VIEW_TARGET_NOT_TRACKED] session={session_id} status_id={evo_id} "
                                f"participant={participant} raw_status={raw_status or '-'} ack={ack_val}"
                            )
                            await manager.broadcast("status_update")
                        else:
                            logger.info(
                                f"[STATUS_VIEW_RECORDED] session={session_id} status_id={evo_id} "
                                f"participant={participant} raw_status={raw_status or '-'} ack={ack_val}"
                            )
                            await manager.broadcast("status_update")

                if evo_id and raw_status is not None:
                    await db.update_message_ack(evo_id, ack_val)
                    if remote_jid:
                        await manager.broadcast(f"chat_{remote_jid}")
            return

        if event != 'messages.upsert': return
        
        payload_data = data.get('data', {})
        if isinstance(payload_data, dict): msg_list = [payload_data]
        elif isinstance(payload_data, list): msg_list = payload_data
        else: return

        for msg_payload in msg_list:
            key = msg_payload.get('key', {})
            sender = key.get('remoteJid')
            from_me = key.get('fromMe')
            evo_id = key.get('id')
            push_name = msg_payload.get('pushName', '')
            
            if not sender or from_me or '@broadcast' in sender or 'status' in sender: continue
            if evo_id:
                webhook_message_id = f"{session_id}:{evo_id}"
                lock_key = f"webhook:message_lock:{webhook_message_id}"
                locked = await redis_client.set(lock_key, "1", ex=WEBHOOK_MESSAGE_LOCK_SECONDS, nx=True)
                if not locked:
                    logger.info("[WEBHOOK_DUPLICATE_LOCKED] session={} phone={} message_id={}", session_id, sender, evo_id)
                    continue
                claimed = await db.claim_webhook_message(webhook_message_id, sender, session_id, event)
                if not claimed:
                    logger.info("[WEBHOOK_DUPLICATE_SKIPPED] session={} phone={} message_id={}", session_id, sender, evo_id)
                    continue
            else:
                logger.warning("[WEBHOOK_MESSAGE_WITHOUT_ID] session={} phone={} type={}", session_id, sender, msg_payload.get('messageType', '-'))
            if await check_rate_limit(f"rate:webhook:{sender}", limit=10, window=10): continue

            msg_obj = msg_payload.get('message', {})
            if not msg_obj: continue
            
            msg_type_raw = msg_payload.get('messageType', '')
            if not msg_type_raw: msg_type_raw = list(msg_obj.keys())[0] if msg_obj.keys() else ""
                
            body = ""
            msg_type = "text"
            
            if msg_type_raw in ['conversation', 'extendedTextMessage']:
                body = msg_obj.get('conversation') or msg_obj.get('extendedTextMessage', {}).get('text', '')
            elif msg_type_raw == 'imageMessage': msg_type = 'image'
            elif msg_type_raw == 'videoMessage': msg_type = 'video'
            elif msg_type_raw in ['audioMessage', 'ptvMessage']: msg_type = 'audio'
            elif msg_type_raw == 'documentMessage': msg_type = 'document'
            
            if msg_type in ['image', 'audio', 'video', 'document']:
                b64_data = msg_payload.get('base64') or msg_obj.get('base64')
                if not b64_data and msg_type_raw in msg_obj: b64_data = msg_obj[msg_type_raw].get('base64')
                if not b64_data: b64_data = await evo.download_media(msg_payload, session_id)
                if b64_data:
                    ext_map = {"image": "jpg", "audio": "ogg", "video": "mp4", "document": "pdf"}
                    file_ext = ext_map.get(msg_type, "bin")
                    file_name = f"{evo_id}.{file_ext}"
                    b64_clean = b64_data.split("base64,")[1] if "base64," in b64_data else b64_data
                    # Fase 5: persiste a mídia recebida via storage, namespaceada por tenant.
                    media_ref = await storage.save_media(get_current_tenant(), file_name, base64.b64decode(b64_clean))
                    body = f"[{msg_type.upper()}] {media_ref}"
                else: 
                    body = f"[Erro de Download: {msg_type.upper()}]"

            if not body: continue

            user = await db.get_user(sender)
            novo_usuario = False
            
            if not user:
                await db.create_user(sender, session_id, name=push_name)
                user = await db.get_user(sender)
                novo_usuario = True
            elif push_name and (not user.get('name') or user.get('name') == 'None' or user.get('name') == ''):
                await db.update_contact_details(sender, push_name, user.get('notes', ''), user.get('extra_info', ''))

            saved = await db.save_message(sender, sender, body, msg_type, evo_id, 3, session_id)
            if not saved:
                logger.info("[WEBHOOK_DUPLICATE_HISTORY_SKIPPED] session={} phone={} message_id={}", session_id, sender, evo_id or "-")
                continue
            # Fase 9: emite webhook de saída (message.received) para integrações do tenant.
            await webhooks.emit(get_current_tenant(), webhooks.EVENT_MESSAGE_RECEIVED, {
                "phone": sender, "text": body, "type": msg_type,
                "message_id": evo_id, "session_id": session_id,
                "contact_name": push_name or (user.get("name") if user else ""),
            })
            await db.update_last_interaction(sender)
            await record_bot_loop_event(sender, "client", body)
            if await detect_bot_loop_risk(sender, session_id, body, user):
                await manager.broadcast(f"chat_{sender}")
                await manager.broadcast("update")
                continue

            # Fase 10: IA - sentimento, auto-tag e lead scoring da mensagem recebida.
            await apply_ai_pipeline(sender, session_id, body, user)

            bot_settings = await db.get_bot_settings()
            if not bot_settings: bot_settings = {}

            if not is_business_hours():
                assigned_to = await resolve_pending_assignee(user, session_id)
                if user.get("stage") != "em_atendimento":
                    await db.update_user_stage(sender, "pendente_assumir", assigned_to)
                if user.get('session_id') != session_id:
                    await db.update_user_session(sender, session_id)
                msg = bot_settings.get('off_hours', 'Fora do horário de atendimento.')
                await send_automated_message(sender, msg, session_id, "off_hours", user)
                await schedule_morning_followup(sender, assigned_to, session_id)
                await manager.broadcast(f"chat_{sender}")
                await manager.broadcast("update")
                continue

            # Fase 10: se houver fluxo no-code ativo, ele conduz o atendimento (sessoes de vendas).
            if session_id not in ["financeiro", "engenharia"] and user.get('stage') not in ['em_atendimento', 'carteira']:
                if await run_active_flow(sender, session_id, body, user):
                    await manager.broadcast(f"chat_{sender}")
                    await manager.broadcast("update")
                    continue

            if novo_usuario:
                if session_id in ["financeiro", "engenharia"]:
                    await db.update_user_stage(sender, "pendente_assumir", session_id)
                    await schedule_sla(sender, session_id, session_id)
                else:
                    if await can_send_automated_reply(sender, session_id, "new_user_menu", user):
                        greeting = bot_settings.get('greeting', 'Olá!')
                        menu = bot_settings.get('menu', 'Menu')
                        await send_automated_message(sender, greeting, session_id, "new_user_greeting", user, enforce_guard=False)
                        await send_automated_message(sender, menu, session_id, "new_user_menu", user, enforce_guard=False)
                await manager.broadcast(f"chat_{sender}")
                await manager.broadcast("update")
                continue
                
            if user.get('session_id') != session_id: await db.update_user_session(sender, session_id)
            await manager.broadcast(f"chat_{sender}")

            if session_id in ["financeiro", "engenharia"]:
                if user['stage'] == "menu_wait" or (user['stage'] == "carteira" and user['assigned_to'] != session_id):
                    await db.update_user_stage(sender, "pendente_assumir", session_id)
                    await manager.broadcast("update")
                continue

            if user['stage'] == 'carteira':
                await db.update_user_stage(sender, "pendente_assumir", user['assigned_to'])
                await schedule_sla(sender, user['assigned_to'], session_id)
                await manager.broadcast("update")
                continue

            if user['stage'] == 'em_atendimento': continue 

            if user['stage'] == 'menu_wait':
                msg_text = body.strip().lower()
                if msg_text in ['oi', 'ola', 'olá', 'menu', 'iniciar', 'bom dia', 'boa tarde', 'boa noite']:
                    menu = bot_settings.get('menu', 'Menu')
                    await send_automated_message(sender, menu, session_id, "menu_resend", user)
                    await manager.broadcast(f"chat_{sender}")
                    continue
                if msg_text == "1" or "vendas" in msg_text or "venda" in msg_text or "comprar" in msg_text:
                    sys_users = await db.get_all_system_users()
                    sales_users = [u for u in sys_users if u['role'] in ['vendas_1', 'vendas_2'] and u.get('is_active', 1)]
                    online_sales_names = [u['name'] for u in sales_users if await db.get_seller_status(u['name']) == 'online']
                    if not online_sales_names:
                        rep = "A equipe de *Vendas* não está disponível no momento. Sua mensagem foi registrada na fila."
                        await send_automated_message(sender, rep, session_id, "route_sales_unavailable", user)
                        fallback_sales = [u['name'] for u in sales_users]
                        await db.update_user_stage(sender, "pendente_assumir", fallback_sales[0] if fallback_sales else "vendas_1")
                    else:
                        selected_sales_name = random.choice(list(set(online_sales_names)))
                        rep = "Transferindo você para a nossa especialista de vendas. Um momento..."
                        await send_automated_message(sender, rep, session_id, "route_sales", user)
                        await db.update_user_stage(sender, "pendente_assumir", selected_sales_name)
                        await schedule_sla(sender, selected_sales_name, session_id)
                    await manager.broadcast("update")
                elif msg_text == "2" or "financeiro" in msg_text or "boleto" in msg_text or "pagamento" in msg_text:
                    rep = "Transferindo para o *Financeiro*. Um momento..."
                    await send_automated_message(sender, rep, session_id, "route_finance", user)
                    await db.update_user_stage(sender, "pendente_assumir", "financeiro")
                    await schedule_sla(sender, "financeiro", session_id)
                    await manager.broadcast("update")
                elif msg_text == "3" or "engenharia" in msg_text or "tecnico" in msg_text or "suporte" in msg_text:
                    rep = "Para envio de projetos e suporte, fale diretamente com a nossa Engenharia.\n\nhttps://wa.me/5542000000000?text=Olá..."
                    await send_automated_message(sender, rep, session_id, "route_engineering", user)
                    await db.update_user_stage(sender, "menu_wait")
                    await manager.broadcast("update")
                else:
                    rep = "Opção inválida. Por favor, digite um número do menu."
                    await send_automated_message(sender, rep, session_id, "invalid_menu_option", user)
    except Exception as e:
        logger.error(f"ERRO CRÍTICO NO WEBHOOK: {e}")

# ==========================================================================
# Fase 8 — Produto e go-to-market: signup, billing, onboarding, LGPD, status
# ==========================================================================

def _slugify(text: str) -> str:
    """Normaliza um nome em um slug seguro para subdomínio/tenant."""
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:40]


def _tenant_login_url(slug: str) -> str:
    """Monta a URL de acesso do tenant (slug.BASE_DOMAIN) quando configurado."""
    base = getattr(settings, "BASE_DOMAIN", "") or ""
    if base:
        return f"https://{slug}.{base}/login"
    return "/login"


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    """Fase 8: pagina publica de criacao de conta (self-service + trial)."""
    if not settings.SIGNUP_ENABLED:
        return HTMLResponse("<h1>Cadastro indisponivel</h1>", status_code=404)
    trial = settings.TRIAL_DAYS
    html = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Criar conta - continnumOS CRM</title>
<style>
body { font-family:system-ui,Arial,sans-serif; background:#0B1014; color:#E9EDEF; display:flex; justify-content:center; padding:40px; }
.card { background:#151b21; padding:28px; border-radius:14px; max-width:420px; width:100%; }
input { width:100%; padding:10px; margin:6px 0 14px; border-radius:8px; border:1px solid #2a333c; background:#0B1014; color:#E9EDEF; }
button { width:100%; padding:12px; border:0; border-radius:8px; background:#ff6600; color:#fff; font-weight:700; cursor:pointer; }
.msg { padding:10px; border-radius:8px; margin-top:12px; display:none; }
h1 { font-size:20px; } small { color:#8aa0b2; }
</style></head>
<body><div class="card"><h1>Crie sua conta</h1><small>Trial gratis de __TRIAL__ dias.</small>
<label>Nome da empresa</label><input id="company" placeholder="Minha Empresa">
<label>Seu nome</label><input id="name" placeholder="Nome completo">
<label>E-mail (login)</label><input id="email" type="email" placeholder="voce@empresa.com">
<label>Senha</label><input id="password" type="password" placeholder="minimo 8 caracteres">
<button onclick="signup()">Criar conta</button><div id="msg" class="msg"></div></div>
<script>
async function signup(){
  const msg=document.getElementById('msg');msg.style.display='none';
  const body={company:document.getElementById('company').value,name:document.getElementById('name').value,email:document.getElementById('email').value,password:document.getElementById('password').value};
  const r=await fetch('/api/signup',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const d=await r.json();msg.style.display='block';
  if(r.ok){msg.style.background='#123d1f';msg.innerHTML='Conta criada! Acesse: <a href="'+d.login_url+'" style="color:#7CFC9B">'+d.login_url+'</a>';}
  else{msg.style.background='#4d1620';msg.textContent=d.detail||'Erro ao criar conta';}
}
</script></body></html>""".replace("__TRIAL__", str(trial))
    return HTMLResponse(html)


@app.post("/api/signup")
async def api_signup(request: Request):
    """Fase 8: cria tenant + trial + usuário admin do cliente (self-service)."""
    if not settings.SIGNUP_ENABLED:
        return JSONResponse(status_code=404, content={"detail": "Cadastro indisponível"})
    body = await request.json()
    company = (body.get("company") or "").strip()
    name = (body.get("name") or "").strip()
    email = (body.get("email") or "").strip().lower()
    password = body.get("password") or ""
    if not company or not name or not email or len(password) < 8:
        return JSONResponse(status_code=400, content={
            "detail": "Preencha empresa, nome, e-mail válido e senha (mín. 8)."})
    if "@" not in email:
        return JSONResponse(status_code=400, content={"detail": "E-mail inválido."})
    slug = _slugify(company)
    if not slug:
        return JSONResponse(status_code=400, content={"detail": "Nome da empresa inválido."})
    if not await db.slug_available(slug):
        return JSONResponse(status_code=409, content={"detail": "Já existe uma conta com esse nome."})
    if not await db.username_available(email):
        return JSONResponse(status_code=409, content={"detail": "E-mail já cadastrado."})
    pwd_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    tenant = await db.create_tenant_self_service(
        slug=slug, display_name=company, admin_username=email,
        admin_password_hash=pwd_hash, admin_name=name,
        plan=settings.SIGNUP_DEFAULT_PLAN, trial_days=settings.TRIAL_DAYS,
    )
    if not tenant:
        return JSONResponse(status_code=409, content={"detail": "Não foi possível criar a conta."})
    # Provisiona a instância respeitando o enforcement de plano (Fase 7).
    ok, _cur, _lim = await db.check_instance_limit(tenant["id"])
    if ok:
        try:
            await evo.ensure_instance(tenant["evolution_instance"])
        except Exception as e:
            logger.warning(f"[SIGNUP] Falha ao provisionar instância de {slug}: {e}")
    logger.info(f"[SIGNUP] Nova conta self-service: tenant={tenant['id']} slug={slug}")
    return {
        "status": "ok",
        "tenant": {"id": tenant["id"], "slug": slug, "display_name": company},
        "trial_ends_at": str(tenant.get("trial_ends_at") or ""),
        "login_url": _tenant_login_url(slug),
    }


@app.get("/billing", response_class=HTMLResponse)
async def billing_portal_page(request: Request, user: dict = Depends(get_current_user)):
    """Fase 8: portal de billing do cliente (faturas, plano, upgrade/downgrade)."""
    if not user:
        return RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_BILLING):
        return HTMLResponse("<h1>Acesso restrito ao responsável financeiro.</h1>", status_code=403)
    html = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Billing — continnumOS CRM</title>
<style>body{font-family:system-ui,Arial,sans-serif;background:#0B1014;color:#E9EDEF;padding:24px;max-width:820px;margin:auto}
.card{background:#151b21;padding:20px;border-radius:12px;margin-bottom:16px}table{width:100%;border-collapse:collapse}
th,td{padding:8px;border-bottom:1px solid #222c34;text-align:left;font-size:14px}button{padding:8px 14px;border:0;border-radius:8px;background:#ff6600;color:#fff;cursor:pointer}
select{padding:8px;border-radius:8px;background:#0B1014;color:#E9EDEF;border:1px solid #2a333c}.pill{padding:2px 8px;border-radius:99px;font-size:12px;background:#123d1f}</style></head>
<body><h1>Assinatura & Faturas</h1>
<div class="card" id="plan"></div>
<div class="card"><h3>Trocar de plano</h3><select id="planSel"></select>
<button onclick="changePlan()">Aplicar</button> <button onclick="checkout()">Trocar cartão / Pagar</button>
<div id="chkmsg" style="margin-top:10px;color:#8aa0b2"></div></div>
<div class="card"><h3>Faturas</h3><table id="inv"><thead><tr><th>#</th><th>Período</th><th>Valor</th><th>Status</th><th>Vencimento</th></tr></thead><tbody></tbody></table></div>
<script>
async function load(){
  const r=await fetch('/api/billing/me');const d=await r.json();
  const t=d.tenant||{};const u=d.usage||{};
  document.getElementById('plan').innerHTML='<h3>'+(t.display_name||'')+'</h3>Plano atual: <b>'+(t.plan||'-')+'</b> <span class="pill">'+(t.billing_status||'')+'</span><br>Trial até: '+(t.trial_ends_at||'—')+'<br><small>Uso: '+JSON.stringify(u.usage||{})+'</small>';
  const sel=document.getElementById('planSel');sel.innerHTML='';
  (d.plans||[]).forEach(p=>{const o=document.createElement('option');o.value=p.code;o.textContent=p.name+' — R$'+((p.price_cents||0)/100).toFixed(2);if(p.code===t.plan)o.selected=true;sel.appendChild(o);});
  const tb=document.querySelector('#inv tbody');tb.innerHTML='';
  (d.invoices||[]).forEach(i=>{const tr=document.createElement('tr');tr.innerHTML='<td>'+i.id+'</td><td>'+(i.period_start||'')+' → '+(i.period_end||'')+'</td><td>R$'+((i.amount_cents||0)/100).toFixed(2)+'</td><td>'+i.status+'</td><td>'+(i.due_date||'')+'</td>';tb.appendChild(tr);});
}
async function changePlan(){const plan=document.getElementById('planSel').value;const r=await fetch('/api/billing/change-plan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({plan})});if(r.ok){load();}else{alert('Erro ao trocar plano');}}
async function checkout(){const r=await fetch('/api/billing/checkout-session',{method:'POST'});const d=await r.json();const m=document.getElementById('chkmsg');if(d.checkout_url){window.location=d.checkout_url;}else{m.textContent=d.detail||'Checkout indisponível neste ambiente.';}}
load();
</script></body></html>"""
    return HTMLResponse(html)


@app.get("/api/billing/me")
async def api_billing_me(request: Request, user: dict = Depends(get_current_user)):
    """Fase 8: dados de billing do tenant do usuário logado."""
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_BILLING):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão de billing"})
    tid = get_current_tenant()
    tenant = await db.get_tenant(tid)
    if tenant and tenant.get("trial_ends_at"):
        tenant["trial_ends_at"] = str(tenant["trial_ends_at"])
    return {
        "tenant": tenant,
        "plans": await db.get_all_plans(),
        "invoices": await db.list_invoices(tid),
        "usage": await db.get_tenant_usage(tid),
    }


@app.post("/api/billing/change-plan")
async def api_billing_change_plan(request: Request, user: dict = Depends(get_current_user)):
    """Fase 8: upgrade/downgrade do plano pelo próprio cliente."""
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_BILLING):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão de billing"})
    body = await request.json()
    plan = (body.get("plan") or "").strip()
    if not plan or not await db.get_plan(plan):
        return JSONResponse(status_code=400, content={"detail": "Plano inválido"})
    tid = get_current_tenant()
    await db.set_tenant_plan(tid, plan_code=plan)
    tc.invalidate(tid)
    await record_audit_log(user, "billing_change_plan", target=plan)
    return {"status": "ok", "plan": plan}


@app.post("/api/billing/checkout-session")
async def api_billing_checkout(request: Request, user: dict = Depends(get_current_user)):
    """Fase 8: inicia o checkout do gateway para trocar cartão/pagar.

    O provedor real é plugável (Fase 6). Sem chaves configuradas, retornamos uma
    mensagem clara em vez de uma URL, para o front tratar graciosamente.
    """
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_BILLING):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão de billing"})
    provider = getattr(settings, "PAYMENT_PROVIDER", "manual")
    if provider == "manual" or not settings.PAYMENT_WEBHOOK_SECRET:
        return {"checkout_url": None,
                "detail": "Pagamento manual ativo. Configure um gateway (Stripe/Mercado Pago/Asaas) para checkout automático."}
    # Integração real do checkout do provedor entraria aqui (cria sessão e retorna URL).
    return {"checkout_url": None, "detail": f"Provedor '{provider}' configurado; checkout a implementar no gateway."}


@app.get("/api/onboarding/status")
async def api_onboarding_status(request: Request, user: dict = Depends(get_current_user)):
    """Fase 8: estado do onboarding guiado (WhatsApp, contatos, primeiro fluxo)."""
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    tid = get_current_tenant()
    tenant = await db.get_tenant(tid)
    instance = (tenant or {}).get("evolution_instance") or "vendas"
    whatsapp_connected = False
    try:
        state = await evo.get_connection_state(instance)
        whatsapp_connected = str(state).lower() in ("open", "connected", "true")
    except Exception as e:
        logger.debug("[ONBOARDING] estado do WhatsApp indisponível: {}", e)
    contacts = await db.count_scoped(tid, "users")
    messages = await db.count_scoped(tid, "messages")
    steps = [
        {"key": "whatsapp", "label": "Conectar o WhatsApp", "done": whatsapp_connected},
        {"key": "contacts", "label": "Importar contatos", "done": contacts > 0},
        {"key": "first_flow", "label": "Primeira conversa/fluxo", "done": messages > 0},
    ]
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "completed": done, "total": len(steps),
            "progress": round(done / len(steps) * 100)}


@app.get("/api/roles")
async def api_roles(request: Request, user: dict = Depends(get_current_user)):
    """Fase 8: catálogo de papéis e permissões (para telas de gestão de usuários)."""
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_USERS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão"})
    return {"roles": permissions.list_roles(),
            "my_permissions": sorted(permissions.permissions_for(user.get("role", "")))}


@app.get("/api/lgpd/export")
async def api_lgpd_export(phone: str, request: Request, user: dict = Depends(get_current_user)):
    """Fase 8 (LGPD): exporta os dados de um titular (por telefone) do tenant."""
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_LGPD):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão LGPD"})
    phone = (phone or "").strip()
    if not phone:
        return JSONResponse(status_code=400, content={"detail": "Informe o telefone do titular"})
    data = await db.export_contact_data(phone)
    await record_audit_log(user, "lgpd_export", phone=phone)
    return data


@app.post("/api/lgpd/delete")
async def api_lgpd_delete(request: Request, user: dict = Depends(get_current_user)):
    """Fase 8 (LGPD): apaga os dados de um titular (direito ao esquecimento)."""
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_LGPD):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão LGPD"})
    body = await request.json()
    phone = (body.get("phone") or "").strip()
    if not phone or body.get("confirm") is not True:
        return JSONResponse(status_code=400, content={
            "detail": "Informe o telefone e confirm=true para excluir definitivamente."})
    deleted = await db.delete_contact_data(phone)
    await record_audit_log(user, "lgpd_delete", phone=phone, details={"deleted": deleted})
    return {"status": "ok", "deleted": deleted}


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request):
    """Fase 8: status page publica (saude de banco, redis e storage)."""
    db_ok = db.pool is not None
    redis_ok = False
    try:
        redis_ok = await check_redis(redis_client)
    except Exception:
        redis_ok = False
    storage_ok = True
    try:
        storage_backend = storage.backend()
    except Exception:
        storage_backend = "?"
        storage_ok = False
    overall = db_ok and redis_ok and storage_ok
    color = "#7CFC9B" if overall else "#ff6b6b"
    label = "Operacional" if overall else "Degradado"
    def badge(ok):
        return '<span style="color:#7CFC9B">online</span>' if ok else '<span style="color:#ff6b6b">offline</span>'
    html = """<!DOCTYPE html><html lang="pt-br"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Status - continnumOS CRM</title>
<style>
body { font-family:system-ui,Arial,sans-serif; background:#0B1014; color:#E9EDEF; padding:40px; max-width:560px; margin:auto; }
.card { background:#151b21; padding:22px; border-radius:12px; }
.row { display:flex; justify-content:space-between; padding:10px 0; border-bottom:1px solid #222c34; }
h1 { font-size:22px; }
</style></head><body>
<h1>Status do servico <span style="color:__COLOR__">__LABEL__</span></h1>
<div class="card">
<div class="row"><span>Banco de dados</span>__DB__</div>
<div class="row"><span>Redis</span>__REDIS__</div>
<div class="row"><span>Storage (__BACKEND__)</span>__STORAGE__</div>
</div><p style="color:#8aa0b2">Atualizado agora. Para incidentes, contate o suporte.</p>
</body></html>"""
    html = (html.replace("__COLOR__", color).replace("__LABEL__", label)
            .replace("__DB__", badge(db_ok)).replace("__REDIS__", badge(redis_ok))
            .replace("__STORAGE__", badge(storage_ok)).replace("__BACKEND__", str(storage_backend)))
    code = 200 if overall else 503
    return HTMLResponse(html, status_code=code)


# =========================================================================
# Fase 9 — Diferenciais: analytics, API pública, webhooks de saída,
# marketplace de integrações e white-label (domínio próprio por cliente).
# =========================================================================

async def _api_auth(request: Request, write: bool = False):
    """Fase 9: autentica a API pública via 'Authorization: Bearer <api_key>'.

    Retorna dict {tenant_id, scopes} ou levanta JSONResponse-friendly erro. Ao
    autenticar, ativa o contexto do tenant (GUC) para todas as leituras RLS.
    Também aplica rate limiting por tenant (Fase 6).
    """
    auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None, JSONResponse(status_code=401, content={"detail": "API key ausente (use Authorization: Bearer <key>)."})
    raw = auth.split(" ", 1)[1].strip()
    info = await db.resolve_tenant_by_api_key(raw)
    if not info:
        return None, JSONResponse(status_code=401, content={"detail": "API key inválida ou revogada."})
    if write and "write" not in info.get("scopes", []):
        return None, JSONResponse(status_code=403, content={"detail": "API key sem escopo de escrita."})
    # Rate limit por tenant (protege Evolution e custo).
    try:
        allowed = await ratelimit.check(redis_client, f"apikey:{info['tenant_id']}")
        if not allowed:
            return None, JSONResponse(status_code=429, content={"detail": "Limite de requisições excedido."})
    except Exception:
        pass
    set_current_tenant(info["tenant_id"])
    return info, None


# ---- Analytics por tenant (portal interno) ----
@app.get("/api/analytics")
async def api_analytics(request: Request, days: int = 30, user: dict = Depends(get_current_user)):
    """Fase 9: analytics do tenant logado (portal). Requer permissão de métricas."""
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_VIEW_METRICS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão para ver métricas."})
    data = await db.get_tenant_analytics(get_current_tenant(), days)
    return data


# ---- API keys (gerenciadas pelo admin do tenant) ----
@app.get("/api/apikeys")
async def api_list_keys(request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_SETTINGS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão."})
    return {"keys": await db.list_api_keys(get_current_tenant())}


@app.post("/api/apikeys")
async def api_create_key(request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_SETTINGS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão."})
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    name = (body.get("name") or "default").strip()[:60]
    scopes = body.get("scopes") or "read,write"
    record, raw = await db.create_api_key(get_current_tenant(), name=name, scopes=scopes)
    await record_audit_log(user, "apikey.create", target=name)
    # A chave crua é retornada UMA única vez.
    return {"key": raw, "info": record}


@app.delete("/api/apikeys/{key_id}")
async def api_revoke_key(key_id: int, request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_SETTINGS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão."})
    ok = await db.revoke_api_key(get_current_tenant(), key_id)
    await record_audit_log(user, "apikey.revoke", target=str(key_id))
    return {"status": "ok" if ok else "not_found"}


# ---- Webhooks de saída ----
@app.get("/api/webhooks")
async def api_list_webhooks(request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_SETTINGS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão."})
    return {"endpoints": await db.list_webhook_endpoints(get_current_tenant())}


@app.post("/api/webhooks")
async def api_create_webhook(request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_SETTINGS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão."})
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url.startswith("http"):
        return JSONResponse(status_code=400, content={"detail": "URL inválida (use http/https)."})
    events = (body.get("events") or "*").strip()
    secret = body.get("secret") or ""
    ep = await db.create_webhook_endpoint(get_current_tenant(), url, events, secret)
    await record_audit_log(user, "webhook.create", target=url)
    return {"endpoint": ep}


@app.delete("/api/webhooks/{endpoint_id}")
async def api_delete_webhook(endpoint_id: int, request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_SETTINGS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão."})
    ok = await db.delete_webhook_endpoint(get_current_tenant(), endpoint_id)
    await record_audit_log(user, "webhook.delete", target=str(endpoint_id))
    return {"status": "ok" if ok else "not_found"}


# ---- Marketplace de integrações ----
@app.get("/api/integrations")
async def api_list_integrations(request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    catalog = integrations.list_catalog()
    state = await db.list_tenant_integrations(get_current_tenant())
    for item in catalog:
        st = state.get(item["key"], {})
        item["enabled"] = bool(st.get("enabled", False))
        item["config"] = st.get("config", {})
    return {"integrations": catalog}


@app.post("/api/integrations/{key}")
async def api_set_integration(key: str, request: Request, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_SETTINGS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão."})
    if not integrations.is_valid(key):
        return JSONResponse(status_code=404, content={"detail": "Integração desconhecida."})
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    config = body.get("config") or {}
    await db.set_tenant_integration(get_current_tenant(), key, enabled, config)
    await record_audit_log(user, "integration.update", target=key, details={"enabled": enabled})
    return {"status": "ok", "key": key, "enabled": enabled}


# ---- White-label (branding + domínio próprio por tenant) ----
@app.get("/api/branding")
async def api_get_branding(request: Request):
    """Fase 9: branding público resolvido pelo host (white-label). Sem auth."""
    host = (request.headers.get("host") or "").split(":")[0]
    tid = await db.get_tenant_id_by_host(host) if host else None
    if not tid:
        tid = get_current_tenant()
    tenant = await db.get_tenant(tid) if tid else None
    if not tenant:
        return {"display_name": "continnumOS CRM", "brand_color": "#00a884", "logo_url": ""}
    return {
        "display_name": tenant.get("display_name") or "continnumOS CRM",
        "brand_color": tenant.get("brand_color") or "#00a884",
        "logo_url": tenant.get("logo_url") or "",
        "domain": tenant.get("domain") or "",
    }


@app.post("/api/branding")
async def api_set_branding(request: Request, user: dict = Depends(get_current_user)):
    """Fase 9: atualiza branding e domínio próprio do tenant (white-label)."""
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Não autenticado"})
    if not permissions.user_has_permission(user, permissions.PERM_MANAGE_SETTINGS):
        return JSONResponse(status_code=403, content={"detail": "Sem permissão."})
    body = await request.json()
    display_name = (body.get("display_name") or "").strip()
    brand_color = (body.get("brand_color") or "").strip()
    logo_url = (body.get("logo_url") or "").strip()
    domain = (body.get("domain") or "").strip().lower()
    updated = await db.update_tenant_branding(
        get_current_tenant(), display_name=display_name or None,
        brand_color=brand_color or None, logo_url=logo_url or None, domain=domain or None,
    )
    await record_audit_log(user, "branding.update", details={"domain": domain})
    return {"status": "ok", "tenant": updated}


# ---- API pública REST (autenticada por API key) ----
@app.get("/api/v1/ping")
async def api_v1_ping(request: Request):
    info, err = await _api_auth(request)
    if err:
        return err
    return {"status": "ok", "tenant_id": info["tenant_id"]}


@app.get("/api/v1/contacts")
async def api_v1_contacts(request: Request, limit: int = 100, offset: int = 0):
    info, err = await _api_auth(request)
    if err:
        return err
    limit = max(1, min(int(limit), 500))
    rows = await db.list_contacts_paginated(int(limit), int(offset))
    return {"contacts": rows, "limit": limit, "offset": offset}


@app.get("/api/v1/messages")
async def api_v1_messages(request: Request, phone: str, limit: int = 50):
    info, err = await _api_auth(request)
    if err:
        return err
    if not phone:
        return JSONResponse(status_code=400, content={"detail": "Parâmetro 'phone' obrigatório."})
    msgs = await db.get_messages(phone)
    lim = max(1, min(int(limit), 200))
    if isinstance(msgs, list) and len(msgs) > lim:
        msgs = msgs[-lim:]
    return {"phone": phone, "messages": msgs}


@app.get("/api/v1/analytics")
async def api_v1_analytics(request: Request, days: int = 30):
    info, err = await _api_auth(request)
    if err:
        return err
    return await db.get_tenant_analytics(info["tenant_id"], days)


@app.post("/api/v1/messages")
async def api_v1_send_message(request: Request):
    info, err = await _api_auth(request, write=True)
    if err:
        return err
    body = await request.json()
    phone = (body.get("phone") or "").strip()
    text = body.get("text") or ""
    session_id = (body.get("session_id") or "vendas").strip()
    if not phone or not text:
        return JSONResponse(status_code=400, content={"detail": "Campos 'phone' e 'text' são obrigatórios."})
    instance = await db.get_tenant_instance(info["tenant_id"])
    evo_id = await evo.send_message(phone, text, instance or session_id)
    if not evo_id:
        return JSONResponse(status_code=502, content={"detail": "Falha ao enviar pela Evolution."})
    await db.save_message(phone, "bot", text, "text", str(evo_id), 1, session_id)
    # Emite evento de saída (message.sent) para outras integrações.
    await webhooks.emit(info["tenant_id"], webhooks.EVENT_MESSAGE_SENT, {
        "phone": phone, "text": text, "message_id": str(evo_id), "session_id": session_id,
    })
    return {"status": "sent", "message_id": str(evo_id)}


# Fase 7: registra o handler da fila de jobs (usado pelo worker.py e pelo
# fallback inline). Precisa ser no nível do módulo pois o worker importa 'main'
# sem executar o lifespan.
jobqueue.register("process_webhook", process_webhook)


async def _deliver_webhook_job(payload: dict):
    """Fase 9: handler de job para entrega de webhooks de saída (via worker)."""
    await webhooks.deliver(payload)


jobqueue.register("deliver_webhook", _deliver_webhook_job)



# =====================================================================
# ROADMAP DE EVOLUÇÃO — Quick wins (models, helpers, endpoints, loop)
# =====================================================================
import csv as _qw_csv
import io as _qw_io
import json as _qw_json
from datetime import datetime as _qw_dt
from fastapi.responses import Response as _QWResponse


class SnippetCreateRequest(ApiPayload):
    shortcut: str
    content: str
    shared: bool = False


class SignatureRequest(ApiPayload):
    signature: str = ""
    enabled: bool = False


class ScheduleMessageRequest(ApiPayload):
    phone: str
    text: str
    scheduled_at: str
    session_id: Optional[str] = None


class SavedFilterRequest(ApiPayload):
    name: str
    payload: Optional[dict] = None
    scope: str = "private"


class NoteRequest(ApiPayload):
    note: str


class CsatSendRequest(ApiPayload):
    phone: str


class CsatRecordRequest(ApiPayload):
    phone: str
    score: int
    comment: str = ""


def expand_message_variables(text, contact, user):
    """Substitui variáveis de resposta rápida (runtime; não é Jinja)."""
    if not text or ("{" + "{") not in text:
        return text
    name = ((contact or {}).get("name") or "").strip()
    first = name.split(" ")[0] if name else ""
    now = _qw_dt.now()
    hour = now.hour
    saud = "Bom dia" if hour < 12 else ("Boa tarde" if hour < 18 else "Boa noite")
    repl = {
        "nome": name,
        "primeiro_nome": first,
        "atendente": (user or {}).get("name", "") or "",
        "data": now.strftime("%d/%m/%Y"),
        "hora": now.strftime("%H:%M"),
        "saudacao": saud,
    }
    ob = "{" + "{"
    cb = "}" + "}"
    for k, v in repl.items():
        text = text.replace(ob + k + cb, str(v)).replace(ob + " " + k + " " + cb, str(v))
    return text


async def finalize_outgoing_text(phone, user, text, contact=None):
    """Aplica variáveis + assinatura do atendente antes de enviar."""
    try:
        if contact is None:
            contact = await db.get_user(phone)
    except Exception:
        contact = None
    text = expand_message_variables(text, contact, user)
    try:
        uname = (user or {}).get("username") or (user or {}).get("name") or ""
        sig = await db.get_signature(uname)
        if sig.get("signature_enabled") and sig.get("signature"):
            text = text + "\n" + sig["signature"]
    except Exception:
        pass
    return text


# ---- Respostas rápidas ----------------------------------------------
@app.get("/api/snippets/all")
async def qw_snippets_all(user: dict = Depends(get_current_user)):
    if not user:
        return []
    return await db.get_snippets_all(user["name"])


@app.post("/api/snippets/create")
async def qw_snippet_create(payload: SnippetCreateRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.add_snippet_ex(user["name"], payload.shortcut, payload.content, payload.shared)
    return {"status": "ok"}


@app.delete("/api/snippets/{snippet_id}")
async def qw_snippet_delete(snippet_id: int, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.delete_snippet(snippet_id, user["name"], user.get("role", ""))
    return {"status": "ok"}


# ---- Assinatura do atendente ----------------------------------------
@app.get("/api/me/signature")
async def qw_get_signature(user: dict = Depends(get_current_user)):
    if not user:
        return {"signature": "", "signature_enabled": False}
    return await db.get_signature(user.get("username") or user["name"])


@app.post("/api/me/signature")
async def qw_set_signature(payload: SignatureRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.set_signature(user.get("username") or user["name"], payload.signature, payload.enabled)
    return {"status": "ok"}


# ---- Agendamento de mensagens ---------------------------------------
@app.post("/api/chat/schedule")
async def qw_schedule(payload: ScheduleMessageRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    try:
        when = _qw_dt.fromisoformat(payload.scheduled_at.replace("Z", "+00:00"))
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Data/hora inválida"})
    contact = await db.get_user(payload.phone)
    sess = payload.session_id or (contact.get("session_id") if contact else "vendas") or "vendas"
    row = await db.add_scheduled_message(payload.phone, payload.text, when, user["name"], sess)
    return {"status": "ok", "id": row["id"] if row else None}


@app.get("/api/chat/scheduled")
async def qw_list_scheduled(user: dict = Depends(get_current_user)):
    if not user:
        return []
    rows = await db.list_scheduled_messages("pending")
    out = []
    for r in rows:
        out.append({
            "id": r["id"], "phone": r["phone"], "text": r["text"],
            "scheduled_at": r["scheduled_at"].isoformat() if r.get("scheduled_at") else None,
            "created_by": r.get("created_by", ""),
        })
    return out


@app.delete("/api/chat/scheduled/{msg_id}")
async def qw_cancel_scheduled(msg_id: int, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.cancel_scheduled_message(msg_id)
    return {"status": "ok"}


# ---- Filtros salvos --------------------------------------------------
@app.get("/api/saved_filters")
async def qw_list_filters(user: dict = Depends(get_current_user)):
    if not user:
        return []
    rows = await db.list_saved_filters(user["name"])
    out = []
    for r in rows:
        payload = r.get("payload")
        if isinstance(payload, str):
            try:
                payload = _qw_json.loads(payload)
            except Exception:
                payload = {}
        out.append({"id": r["id"], "name": r["name"], "scope": r["scope"], "payload": payload})
    return out


@app.post("/api/saved_filters")
async def qw_add_filter(payload: SavedFilterRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    scope = "team" if (payload.scope == "team" and user.get("role") == "admin") else "private"
    row = await db.add_saved_filter(user["name"], payload.name, _qw_json.dumps(payload.payload or {}), scope)
    return {"status": "ok", "id": row["id"] if row else None}


@app.delete("/api/saved_filters/{filter_id}")
async def qw_del_filter(filter_id: int, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.delete_saved_filter(filter_id, user["name"], user.get("role", ""))
    return {"status": "ok"}


# ---- CSAT ------------------------------------------------------------
@app.post("/api/csat/send")
async def qw_csat_send(payload: CsatSendRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    contact = await db.get_user(payload.phone)
    sess = (contact.get("session_id") if contact else "vendas") or "vendas"
    msg = ("Sua opiniao e muito importante para nos! "
           "De 1 a 5, como voce avalia nosso atendimento? "
           "(responda apenas com um numero de 1 a 5)")
    evo_id = await evo.send_message(payload.phone, msg, sess)
    if not evo_id:
        return JSONResponse(status_code=502, content={"error": "Evolution nao confirmou envio"})
    await db.save_message(payload.phone, user["name"], msg, "text", str(evo_id), 1, sess)
    await db.create_csat(payload.phone, user["name"], sess)
    await manager.broadcast(f"chat_{payload.phone}")
    return {"status": "ok"}


@app.post("/api/csat/record")
async def qw_csat_record(payload: CsatRecordRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    if payload.score < 1 or payload.score > 5:
        return JSONResponse(status_code=400, content={"error": "Nota deve ser de 1 a 5"})
    await db.record_csat_score(payload.phone, payload.score, payload.comment)
    return {"status": "ok"}


@app.get("/api/csat/report")
async def qw_csat_report(days: int = 30, user: dict = Depends(get_current_user)):
    if not user:
        return {}
    return await db.csat_report(days)


# ---- Notas internas por conversa ------------------------------------
@app.get("/api/contacts/{phone}/notes")
async def qw_get_notes(phone: str, user: dict = Depends(get_current_user)):
    if not user:
        return []
    rows = await db.get_conversation_notes(phone)
    out = []
    for r in rows:
        mentions = r.get("mentions")
        if isinstance(mentions, str):
            try:
                mentions = _qw_json.loads(mentions)
            except Exception:
                mentions = []
        out.append({
            "id": r["id"], "author": r.get("author_name", ""), "note": r["note"],
            "mentions": mentions,
            "created_at": r["created_at"].isoformat() if r.get("created_at") else None,
        })
    return out


@app.post("/api/contacts/{phone}/notes")
async def qw_add_note(phone: str, payload: NoteRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    mentions = [w[1:] for w in payload.note.split() if w.startswith("@") and len(w) > 1]
    row = await db.add_conversation_note(phone, user["name"], payload.note, mentions)
    if mentions and hasattr(db, "save_internal_message"):
        for mname in mentions:
            try:
                await db.save_internal_message(user["name"], mname, f"[nota sobre {phone}] {payload.note}")
            except Exception:
                pass
    return {"status": "ok", "id": row["id"] if row else None}


@app.delete("/api/notes/{note_id}")
async def qw_del_note(note_id: int, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.delete_conversation_note(note_id)
    return {"status": "ok"}


# ---- Exportação de conversa -----------------------------------------
@app.get("/api/contacts/{phone}/export")
async def qw_export_conversation(phone: str, fmt: str = "csv", user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    contact = await db.get_user(phone)
    name = (contact.get("name") if contact else "") or phone
    msgs = await db.get_conversation_messages(phone)
    if fmt == "html":
        rows_html = ""
        for m in msgs:
            ts = m["timestamp"].strftime("%d/%m/%Y %H:%M") if m.get("timestamp") else ""
            who = m.get("sender", "")
            txt = (m.get("msg_text") or "").replace("<", "&lt;").replace(">", "&gt;")
            rows_html += "<tr><td>" + ts + "</td><td>" + who + "</td><td>" + txt + "</td></tr>"
        html = ("<html><head><meta charset='utf-8'><title>Conversa " + name + "</title>"
                "<style>body{font-family:Arial,sans-serif;padding:24px}table{border-collapse:collapse;width:100%}"
                "td,th{border:1px solid #ccc;padding:6px;font-size:13px;vertical-align:top}"
                "th{background:#111;color:#fff;text-align:left}</style></head><body>"
                "<h2>Conversa com " + name + " (" + phone + ")</h2>"
                "<table><tr><th>Data</th><th>De</th><th>Mensagem</th></tr>" + rows_html +
                "</table></body></html>")
        return _QWResponse(content=html, media_type="text/html",
                           headers={"Content-Disposition": "attachment; filename=conversa_" + phone + ".html"})
    buf = _qw_io.StringIO()
    w = _qw_csv.writer(buf)
    w.writerow(["data", "de", "tipo", "mensagem"])
    for m in msgs:
        ts = m["timestamp"].strftime("%Y-%m-%d %H:%M:%S") if m.get("timestamp") else ""
        w.writerow([ts, m.get("sender", ""), m.get("msg_type", ""), m.get("msg_text", "")])
    return _QWResponse(content=buf.getvalue(), media_type="text/csv",
                       headers={"Content-Disposition": "attachment; filename=conversa_" + phone + ".csv"})


# ---- Loop de fundo: envio de mensagens agendadas --------------------
async def scheduled_messages_loop():
    """Quick wins: envia mensagens agendadas cujo horário chegou."""
    from tenant_context import set_current_tenant, reset_current_tenant
    await asyncio.sleep(20)
    while True:
        try:
            due = await db.fetch_due_scheduled_messages(limit=50)
            for m in due:
                token = set_current_tenant(m.get("tenant_id") or DEFAULT_TENANT_ID)
                try:
                    sess = m.get("session_id") or "vendas"
                    evo_id = await evo.send_message(m["phone"], m["text"], sess)
                    if evo_id:
                        await db.save_message(m["phone"], m.get("created_by") or "agendado", m["text"], "text", str(evo_id), 1, sess)
                        await db.mark_scheduled_sent(m["id"], ok=True)
                        await manager.broadcast(f"chat_{m['phone']}")
                    else:
                        await db.mark_scheduled_sent(m["id"], ok=False, error="Evolution nao confirmou envio")
                except Exception as e:
                    await db.mark_scheduled_sent(m["id"], ok=False, error=str(e))
                finally:
                    reset_current_tenant(token)
        except Exception as e:
            logger.error(f"[SCHEDULED] loop erro: {e}")
        await asyncio.sleep(30)




# =====================================================================
# FASE 10 - Automação & IA conversacional (models, helpers, endpoints)
# =====================================================================
import ai_engine
import flow_engine


class AiSettingsRequest(ApiPayload):
    enabled: bool = False
    provider: str = "openai"
    api_base: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    features: Optional[dict] = None


class AiTextRequest(ApiPayload):
    text: str = ""
    tone: str = "formal"


class AiPhoneRequest(ApiPayload):
    phone: str


class AiTranslateRequest(ApiPayload):
    text: str = ""
    target_lang: str = "en"
    source_lang: Optional[str] = None


class KbDocRequest(ApiPayload):
    title: str = ""
    content: str = ""
    source: str = "manual"


class KbQueryRequest(ApiPayload):
    question: str
    phone: Optional[str] = None


class FlowRequest(ApiPayload):
    name: str = ""
    definition: Optional[dict] = None
    trigger: Optional[dict] = None
    active: Optional[bool] = None


class FlowSimRequest(ApiPayload):
    definition: dict
    messages: List[str] = []


class RoutingRuleRequest(ApiPayload):
    name: str = ""
    priority: int = 100
    conditions: Optional[list] = None
    target: str = ""
    skill: str = ""
    strategy: str = "round_robin"
    active: bool = True


class AgentSkillsRequest(ApiPayload):
    username: str
    skills: Optional[list] = None
    regions: Optional[list] = None


def _is_admin(user):
    return bool(user) and user.get("role") == "admin"


async def _ai_config():
    try:
        return await db.get_ai_settings()
    except Exception:
        return {"enabled": False}


async def _load_msgs_for_ai(phone, limit=40):
    try:
        rows = await db.get_conversation_messages(phone)
    except Exception:
        rows = []
    out = []
    for r in rows[-limit:]:
        out.append({"sender": r.get("sender", ""), "phone": phone,
                    "msg_text": r.get("msg_text", ""), "timestamp": r.get("timestamp")})
    return out


# ---------------------------------------------------------- lead scoring
_BUY_WORDS = ["comprar", "quero", "fechar", "pedido", "orcamento", "preco", "valor",
              "pagamento", "pix", "cartao", "boleto", "parcelar", "disponivel", "estoque"]
_URGENCY = ["hoje", "agora", "urgente", "rapido", "ja"]


def score_lead(text, sentiment_score, msg_count):
    low = ai_engine._strip_accents(text or "")
    score = 0
    reasons = []
    hits = [w for w in _BUY_WORDS if w in low]
    if hits:
        score += min(40, 12 * len(hits))
        reasons.append("intencao de compra (" + ", ".join(hits[:3]) + ")")
    urg = [w for w in _URGENCY if w in low]
    if urg:
        score += 15
        reasons.append("urgencia")
    if sentiment_score is not None:
        score += int(max(-15, min(20, sentiment_score * 20)))
        if sentiment_score > 0.15:
            reasons.append("sentimento positivo")
        elif sentiment_score < -0.15:
            reasons.append("sentimento negativo")
    score += min(15, int(msg_count))
    score = max(0, min(100, score))
    band = "quente" if score >= 60 else ("morno" if score >= 30 else "frio")
    return score, band, "; ".join(reasons) or "engajamento inicial"


async def apply_ai_pipeline(phone, session_id, text, user):
    """Sentimento + auto-tag + lead scoring para a mensagem recebida."""
    try:
        config = await _ai_config()
        sent = await ai_engine.analyze_sentiment(text, config)
        lang = ai_engine.detect_language(text)
        try:
            cnt = len(await db.get_conversation_messages(phone))
        except Exception:
            cnt = 1
        score, band, reason = score_lead(text, sent.get("score"), cnt)
        await db.save_conversation_ai(phone, sentiment=sent.get("label"),
                                      sentiment_score=sent.get("score"), language=lang)
        await db.save_lead_score(phone, score, band, reason)
        # auto-tags (idempotentes)
        if sent.get("label") == "negativo":
            await db.apply_tag_by_name(phone, "sentimento-negativo", "#ff4d4d")
        if band == "quente":
            await db.apply_tag_by_name(phone, "lead-quente", "#ff6a00")
        return {"sentiment": sent, "language": lang, "score": score, "band": band}
    except Exception as e:
        logger.warning(f"[AI_PIPELINE] falha: {e}")
        return None


# ------------------------------------------------- distribuição automatica
async def _online_candidates(target, skill):
    """Resolve a lista de atendentes elegíveis (por role/skill/região), online."""
    sys_users = await db.get_all_system_users()
    skills_map = {}
    try:
        skills_map = await db.get_agent_skills()
    except Exception:
        skills_map = {}
    cands = []
    for u in sys_users:
        if not u.get("is_active", 1):
            continue
        role = u.get("role", "")
        if role == "admin":
            continue
        if target and target not in (role, "", "any") and target != "vendas":
            # target pode ser um role especifico
            if target != role:
                continue
        if target == "vendas" and role not in ("vendas_1", "vendas_2"):
            continue
        if skill:
            uskills = [ai_engine._strip_accents(s) for s in skills_map.get(u.get("username"), {}).get("skills", [])]
            if ai_engine._strip_accents(skill) not in uskills:
                continue
        cands.append(u)
    # prioriza online
    online = []
    for u in cands:
        try:
            st = await db.get_seller_status(u["name"])
        except Exception:
            st = "online"
        if st == "online":
            online.append(u)
    return online or cands


async def auto_assign(session_id, target="vendas", skill="", strategy="round_robin"):
    """Escolhe um atendente conforme a estratégia. Retorna o nome ou None."""
    cands = await _online_candidates(target, skill)
    if not cands:
        return None
    names = [u["name"] for u in cands]
    if strategy == "load":
        counts = await db.count_open_by_agent(names)
        names.sort(key=lambda n: counts.get(n, 0))
        return names[0]
    if strategy == "random":
        return random.choice(names)
    bucket = (target or "vendas") + ":" + (skill or "-")
    return await db.next_round_robin(bucket, names)


# ------------------------------------------------------- motor de fluxos
async def run_active_flow(phone, session_id, text, user):
    """Executa o fluxo ativo (se houver). Retorna True se tratou a mensagem."""
    try:
        flow = await db.get_active_flow()
        if not flow or not flow.get("definition", {}).get("nodes"):
            return False
        st = await db.get_flow_state(phone)
        if st and st.get("done") and st.get("flow_id") == flow["id"]:
            return False  # fluxo já concluido para este contato
        if not st or st.get("flow_id") != flow["id"]:
            state = flow_engine.new_state(flow["id"])
        else:
            state = {"flow_id": flow["id"], "node": st.get("node"),
                     "vars": st.get("vars", {}), "retries": {}, "done": False}
        ctx = {"is_business_hours": is_business_hours(), "contact": user or {}}
        new_st, actions = flow_engine.run(flow["definition"], state, text, ctx)
        handed_off = False
        for act in actions:
            if act["type"] == "send" and act.get("text"):
                await send_automated_message(phone, act["text"], session_id, "flow", user, enforce_guard=False)
            elif act["type"] == "tag" and act.get("tag"):
                await db.apply_tag_by_name(phone, act["tag"], "#ffb020")
            elif act["type"] == "handoff":
                target = act.get("target") or "vendas"
                skill = act.get("skill") or ""
                assignee = await auto_assign(session_id, target, skill, "round_robin")
                await db.update_user_stage(phone, "pendente_assumir", assignee or target)
                try:
                    await schedule_sla(phone, assignee or target, session_id)
                except Exception:
                    pass
                handed_off = True
        await db.save_flow_state(phone, flow["id"], new_st.get("node"), new_st.get("vars", {}), new_st.get("done", False))
        return True if (actions or handed_off) else True
    except Exception as e:
        logger.warning(f"[FLOW] falha ao executar fluxo: {e}")
        return False


# =============================== ENDPOINTS ============================
# ---- Configuração de IA ----
@app.get("/api/ai/settings")
async def api_ai_get_settings(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    cfg = await db.get_ai_settings()
    cfg = dict(cfg)
    if cfg.get("api_key"):
        cfg["api_key_set"] = True
        cfg["api_key"] = ""  # nunca devolve a chave
    else:
        cfg["api_key_set"] = False
    return cfg


@app.post("/api/ai/settings")
async def api_ai_save_settings(payload: AiSettingsRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    api_key = payload.api_key
    if not api_key:  # mantém a chave existente se vier vazia
        cur = await db.get_ai_settings()
        api_key = cur.get("api_key", "")
    await db.save_ai_settings(payload.enabled, payload.provider, payload.api_base,
                              api_key, payload.model, payload.features or {})
    return {"status": "ok"}


@app.post("/api/ai/test")
async def api_ai_test(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    cfg = await db.get_ai_settings()
    return await ai_engine.provider_health(cfg)


# ---- Copiloto / análise ----
@app.post("/api/ai/suggest")
async def api_ai_suggest(payload: AiPhoneRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    msgs = await _load_msgs_for_ai(payload.phone)
    contact = await db.get_user(payload.phone)
    return await ai_engine.suggest_reply(msgs, contact, await _ai_config())


@app.post("/api/ai/tone")
async def api_ai_tone(payload: AiTextRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await ai_engine.correct_tone(payload.text, payload.tone, await _ai_config())


@app.post("/api/ai/summary")
async def api_ai_summary(payload: AiPhoneRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    msgs = await _load_msgs_for_ai(payload.phone)
    summary = await ai_engine.summarize(msgs, await _ai_config())
    try:
        await db.save_conversation_ai(payload.phone, summary=summary)
    except Exception:
        pass
    return {"summary": summary}


@app.post("/api/ai/extract")
async def api_ai_extract(payload: AiPhoneRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    msgs = await _load_msgs_for_ai(payload.phone)
    contact = await db.get_user(payload.phone)
    data = await ai_engine.extract_data(msgs, contact, await _ai_config())
    try:
        await db.save_conversation_ai(payload.phone, extracted=data)
    except Exception:
        pass
    return data


@app.post("/api/ai/sentiment")
async def api_ai_sentiment(payload: AiTextRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await ai_engine.analyze_sentiment(payload.text, await _ai_config())


@app.post("/api/ai/translate")
async def api_ai_translate(payload: AiTranslateRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    return await ai_engine.translate(payload.text, payload.target_lang, payload.source_lang, await _ai_config())


@app.get("/api/ai/conversation/{phone}")
async def api_ai_conversation(phone: str, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    ai = await db.get_conversation_ai(phone)
    lead = await db.get_lead_score(phone)
    return {"ai": ai or {}, "lead": lead or {}}


# ---- Base de conhecimento (RAG) ----
@app.get("/api/kb/docs")
async def api_kb_list(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.list_kb_documents()


@app.post("/api/kb/docs")
async def api_kb_add(payload: KbDocRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    if not payload.content.strip():
        return JSONResponse(status_code=400, content={"error": "Conteudo vazio"})
    res = await db.add_kb_document(payload.title, payload.content, payload.source, chunk_fn=ai_engine.chunk_text)
    return {"status": "ok", "id": res.get("id"), "chunks": res.get("chunks")}


@app.delete("/api/kb/docs/{doc_id}")
async def api_kb_delete(doc_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    await db.delete_kb_document(doc_id)
    return {"status": "ok"}


@app.post("/api/kb/query")
async def api_kb_query(payload: KbQueryRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    chunks = await db.get_kb_chunks()
    return await ai_engine.answer_from_kb(payload.question, chunks, await _ai_config())


# ---- Fluxos no-code ----
@app.get("/api/flows")
async def api_flows_list(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.list_flows()


@app.get("/api/flows/{flow_id}")
async def api_flow_get(flow_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    f = await db.get_flow(flow_id)
    if not f:
        return JSONResponse(status_code=404, content={"error": "Nao encontrado"})
    return f


@app.post("/api/flows")
async def api_flow_create(payload: FlowRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    definition = payload.definition or {"start": "", "nodes": {}}
    errs = flow_engine.validate_flow(definition) if definition.get("nodes") else []
    r = await db.create_flow(payload.name or "Novo fluxo", definition, payload.trigger or {}, bool(payload.active))
    return {"status": "ok", "id": r.get("id") if r else None, "warnings": errs}


@app.put("/api/flows/{flow_id}")
async def api_flow_update(flow_id: int, payload: FlowRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    warnings = []
    if payload.definition is not None and payload.definition.get("nodes"):
        warnings = flow_engine.validate_flow(payload.definition)
    await db.update_flow(flow_id, name=payload.name or None, definition=payload.definition,
                         trigger=payload.trigger, active=payload.active)
    return {"status": "ok", "warnings": warnings}


@app.post("/api/flows/{flow_id}/activate")
async def api_flow_activate(flow_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    f = await db.get_flow(flow_id)
    if not f:
        return JSONResponse(status_code=404, content={"error": "Nao encontrado"})
    errs = flow_engine.validate_flow(f.get("definition", {}))
    if errs:
        return JSONResponse(status_code=400, content={"error": "Fluxo invalido", "details": errs})
    await db.set_active_flow(flow_id)
    return {"status": "ok"}


@app.delete("/api/flows/{flow_id}")
async def api_flow_delete(flow_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    await db.delete_flow(flow_id)
    return {"status": "ok"}


@app.post("/api/flows/simulate")
async def api_flow_simulate(payload: FlowSimRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    errs = flow_engine.validate_flow(payload.definition)
    if errs:
        return {"errors": errs, "transcript": []}
    state = flow_engine.new_state(0)
    transcript = []
    ctx = {"is_business_hours": True, "contact": {"name": "Cliente Teste"}}
    msgs = payload.messages or [""]
    for i, m in enumerate(msgs):
        state, actions = flow_engine.run(payload.definition, state, m, ctx)
        if i > 0 or m:
            transcript.append({"from": "cliente", "text": m})
        for a in actions:
            transcript.append({"from": "bot", "type": a["type"],
                               "text": a.get("text", ""), "target": a.get("target", "")})
        if state.get("done"):
            break
    return {"errors": [], "transcript": transcript, "final_vars": state.get("vars", {})}


# ---- Roteamento / skills ----
@app.get("/api/routing/rules")
async def api_routing_list(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.list_routing_rules()


@app.post("/api/routing/rules")
async def api_routing_add(payload: RoutingRuleRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    r = await db.create_routing_rule(payload.name, payload.priority, payload.conditions or [],
                                     payload.target, payload.skill, payload.strategy, payload.active)
    return {"status": "ok", "id": r.get("id") if r else None}


@app.delete("/api/routing/rules/{rule_id}")
async def api_routing_delete(rule_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    await db.delete_routing_rule(rule_id)
    return {"status": "ok"}


@app.get("/api/routing/skills")
async def api_skills_get(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.get_agent_skills()


@app.post("/api/routing/skills")
async def api_skills_set(payload: AgentSkillsRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    await db.set_agent_skills(payload.username, payload.skills or [], payload.regions or [])
    return {"status": "ok"}



# =====================================================================
#  FASE 11 - OMNICHANNEL (caixa unificada, multicanal, campanhas)
# =====================================================================
import channels as ch


# ---- Modelos -----------------------------------------------------------
class ChannelAccountRequest(ApiPayload):
    channel: str
    name: Optional[str] = ""
    config: Optional[Dict] = None
    active: Optional[bool] = True


class OmniSendRequest(ApiPayload):
    conversation_id: int
    text: str = ""
    media_url: Optional[str] = ""


class OmniAssignRequest(ApiPayload):
    conversation_id: int
    assigned_to: str = ""


class OmniStatusRequest(ApiPayload):
    conversation_id: int
    status: str = "open"


class WebchatInboundRequest(ApiPayload):
    session_id: str
    text: str = ""
    name: Optional[str] = ""


class SegmentRequest(ApiPayload):
    name: str = "Segmento"
    rules: Optional[List[Dict]] = None


class CampaignRequest(ApiPayload):
    name: str = "Campanha"
    channel: str = "whatsapp_cloud"
    template: str = ""
    media_url: Optional[str] = ""
    segment_id: Optional[int] = None


# ---- Helpers de ingestao/envio ----------------------------------------
async def _omni_broadcast(channel="", conversation_id=None):
    """Notifica os paineis (best-effort) para atualizar a caixa unificada."""
    try:
        payload = {"type": "omni", "channel": channel, "conversation_id": conversation_id}
        await manager.broadcast(json.dumps(payload, ensure_ascii=True))
        await manager.broadcast("omni_update")
    except Exception:
        pass


async def ingest_omni_message(norm):
    """Grava uma mensagem recebida (dict normalizado de channels.parse_inbound)
    na caixa unificada. O contexto de tenant JA deve estar setado."""
    if not norm:
        return None
    try:
        contact = await db.upsert_omni_contact(
            norm["channel"], norm["contact_ref"], norm.get("contact_name", ""),
            phone=norm["contact_ref"] if norm["channel"] in ("whatsapp_cloud",) else "")
        if not contact:
            return None
        conv = await db.get_or_create_omni_conversation(contact["id"], norm["channel"])
        msg = await db.save_omni_message(
            conv["id"], "in", norm.get("contact_name") or norm["contact_ref"],
            norm.get("text", ""), media_url=norm.get("media_url") or "",
            msg_type=norm.get("media_type", "text"),
            external_id=norm.get("external_id") or "", status="received")
        await _omni_broadcast(norm["channel"], conv["id"])
        return {"conversation_id": conv["id"], "message_id": msg["id"] if msg else None}
    except Exception as e:
        logger.warning(f"[OMNI] ingest falhou: {e}")
        return None


async def send_omni_message(conversation_id, text, sender="", media_url=""):
    """Envia uma mensagem por um canal a partir de uma conversa unificada.
    Retorna o resultado padronizado do canal (com flag stub quando simulado)."""
    conv = await db.get_omni_conversation(conversation_id)
    if not conv:
        return {"ok": False, "error": "conversa nao encontrada"}
    channel = conv["channel"]
    acct = await db.get_channel_account(channel)
    config = (acct or {}).get("config", {})
    to = conv.get("contact_ref", "")
    result = await ch.send(channel, config, to, text, media_url=media_url or None)
    status = "sent" if result.get("ok") else "failed"
    if result.get("stub"):
        status = "simulado"
    await db.save_omni_message(conversation_id, "out", sender or "atendente", text,
                               media_url=media_url or "", external_id=result.get("external_id") or "",
                               status=status)
    await db.mark_conversation_read(conversation_id)
    await _omni_broadcast(channel, conversation_id)
    return result


async def _resolve_tenant_generic(request):
    """Resolve o tenant de um webhook publico pelo host (fallback: padrao)."""
    try:
        tid = await db.resolve_request_tenant_id(request)
        if tid:
            set_current_tenant(tid)
            return tid
    except Exception:
        pass
    set_current_tenant(DEFAULT_TENANT_ID)
    return DEFAULT_TENANT_ID


# ---- Endpoints: canais -------------------------------------------------
@app.get("/api/channels")
async def api_channels_list(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    accounts = await db.list_channel_accounts()
    return {"supported": ch.SUPPORTED, "labels": ch.CHANNEL_LABELS,
            "fields": ch.CONFIG_FIELDS, "accounts": accounts,
            "summary": await db.omni_unread_summary()}


@app.post("/api/channels")
async def api_channels_save(payload: ChannelAccountRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    if payload.channel not in ch.SUPPORTED:
        return JSONResponse(status_code=400, content={"error": "Canal nao suportado"})
    res = await db.upsert_channel_account(payload.channel, payload.name or ch.label(payload.channel),
                                          payload.config or {}, bool(payload.active))
    return {"status": "ok", "result": res}


@app.delete("/api/channels/{channel}")
async def api_channels_delete(channel: str, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    await db.delete_channel_account(channel)
    return {"status": "ok"}


@app.post("/api/channels/{channel}/test")
async def api_channels_test(channel: str, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    acct = await db.get_channel_account(channel)
    if not acct:
        return {"ok": False, "error": "canal nao configurado"}
    config = acct.get("config", {})
    # envio de teste para o proprio numero/id configurado, se houver
    to = config.get("test_to") or config.get("phone_number_id") or "test"
    result = await ch.send(channel, config, to, "Teste de conexao continnumOS")
    await db.set_channel_status(channel, "connected" if result.get("ok") else "error")
    return result


# ---- Endpoints: caixa unificada ---------------------------------------
@app.get("/api/inbox/conversations")
async def api_inbox_conversations(status: str = "", channel: str = "", mine: int = 0,
                                  user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    assigned = user.get("name", "") if mine else ""
    convs = await db.list_omni_conversations(status=status, channel=channel, assigned_to=assigned)
    return {"conversations": convs, "summary": await db.omni_unread_summary()}


@app.get("/api/inbox/messages/{conversation_id}")
async def api_inbox_messages(conversation_id: int, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    conv = await db.get_omni_conversation(conversation_id)
    if not conv:
        return JSONResponse(status_code=404, content={"error": "Nao encontrada"})
    msgs = await db.get_omni_messages(conversation_id)
    await db.mark_conversation_read(conversation_id)
    return {"conversation": conv, "messages": msgs}


@app.post("/api/inbox/send")
async def api_inbox_send(payload: OmniSendRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    if not (payload.text or "").strip() and not (payload.media_url or "").strip():
        return JSONResponse(status_code=400, content={"error": "Mensagem vazia"})
    result = await send_omni_message(payload.conversation_id, payload.text,
                                     sender=user.get("name", "atendente"),
                                     media_url=payload.media_url or "")
    return result


@app.post("/api/inbox/assign")
async def api_inbox_assign(payload: OmniAssignRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.assign_omni_conversation(payload.conversation_id, payload.assigned_to)
    await _omni_broadcast(conversation_id=payload.conversation_id)
    return {"status": "ok"}


@app.post("/api/inbox/status")
async def api_inbox_status(payload: OmniStatusRequest, user: dict = Depends(get_current_user)):
    if not user:
        return JSONResponse(status_code=403, content={"error": "Acesso negado"})
    await db.set_omni_conversation_status(payload.conversation_id, payload.status)
    await _omni_broadcast(conversation_id=payload.conversation_id)
    return {"status": "ok"}


# ---- Webhooks de entrada por canal ------------------------------------
@app.post("/webhook/telegram/{secret}")
async def webhook_telegram(secret: str, request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}
    tid, cfg = await db.find_channel_by_config("telegram", "secret", secret)
    if not tid:
        return JSONResponse(status_code=404, content={"error": "canal nao encontrado"})
    set_current_tenant(tid)
    norm = ch.parse_inbound("telegram", data)
    if norm:
        await ingest_omni_message(norm)
    return {"ok": True}


@app.get("/webhook/cloud")
async def webhook_cloud_verify(request: Request):
    params = dict(request.query_params)
    token = params.get("hub.verify_token", "")
    tid, cfg = await db.find_channel_by_config("whatsapp_cloud", "verify_token", token)
    if tid:
        challenge = ch.verify_meta_challenge(params, token)
        if challenge is not None:
            return Response(content=str(challenge), media_type="text/plain")
    return JSONResponse(status_code=403, content={"error": "verificacao falhou"})


@app.post("/webhook/cloud")
async def webhook_cloud(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}
    # resolve tenant pelo phone_number_id presente no payload
    pnid = None
    try:
        value = data["entry"][0]["changes"][0]["value"]
        pnid = value.get("metadata", {}).get("phone_number_id")
    except Exception:
        pnid = None
    if pnid:
        tid, cfg = await db.find_channel_by_config("whatsapp_cloud", "phone_number_id", str(pnid))
        if tid:
            set_current_tenant(tid)
            norm = ch.parse_inbound("whatsapp_cloud", data)
            if norm:
                await ingest_omni_message(norm)
    return {"ok": True}


@app.get("/webhook/meta/{channel}")
async def webhook_meta_verify(channel: str, request: Request):
    if channel not in ("instagram", "messenger"):
        return JSONResponse(status_code=404, content={"error": "canal invalido"})
    params = dict(request.query_params)
    token = params.get("hub.verify_token", "")
    tid, cfg = await db.find_channel_by_config(channel, "verify_token", token)
    if tid:
        challenge = ch.verify_meta_challenge(params, token)
        if challenge is not None:
            return Response(content=str(challenge), media_type="text/plain")
    return JSONResponse(status_code=403, content={"error": "verificacao falhou"})


@app.post("/webhook/meta/{channel}")
async def webhook_meta(channel: str, request: Request):
    if channel not in ("instagram", "messenger"):
        return JSONResponse(status_code=404, content={"error": "canal invalido"})
    try:
        data = await request.json()
    except Exception:
        return {"ok": True}
    page_id = None
    try:
        page_id = data["entry"][0].get("id")
    except Exception:
        page_id = None
    tid = None
    if page_id:
        tid, cfg = await db.find_channel_by_config(channel, "page_id", str(page_id))
    if tid:
        set_current_tenant(tid)
        norm = ch.parse_inbound(channel, data)
        if norm:
            await ingest_omni_message(norm)
    return {"ok": True}


@app.post("/api/webchat/inbound")
async def webchat_inbound(payload: WebchatInboundRequest, request: Request):
    """Endpoint publico do widget de webchat (resolve tenant pelo host)."""
    await _resolve_tenant_generic(request)
    norm = ch.parse_inbound("webchat", {"session_id": payload.session_id,
                                        "text": payload.text, "name": payload.name})
    res = await ingest_omni_message(norm) if norm else None
    return {"ok": True, "conversation_id": (res or {}).get("conversation_id")}


@app.get("/api/webchat/poll")
async def webchat_poll(session_id: str, request: Request, after: int = 0):
    """O widget consulta novas respostas do atendente (long-poll simples)."""
    await _resolve_tenant_generic(request)
    contact = await db.upsert_omni_contact("webchat", session_id)
    if not contact:
        return {"messages": []}
    conv = await db.get_or_create_omni_conversation(contact["id"], "webchat")
    msgs = await db.get_omni_messages(conv["id"])
    out = [m for m in msgs if m.get("direction") == "out" and int(m.get("id", 0)) > int(after)]
    return {"messages": out, "conversation_id": conv["id"]}


# ---- Endpoints: segmentos ---------------------------------------------
@app.get("/api/segments")
async def api_segments_list(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.list_segments()


@app.post("/api/segments")
async def api_segments_create(payload: SegmentRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    r = await db.create_segment(payload.name, payload.rules or [])
    return {"status": "ok", "id": r.get("id")}


@app.post("/api/segments/preview")
async def api_segments_preview(payload: SegmentRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    recipients = await db.resolve_segment_recipients(payload.rules or [])
    return {"count": len(recipients), "sample": recipients[:20]}


@app.delete("/api/segments/{segment_id}")
async def api_segments_delete(segment_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    await db.delete_segment(segment_id)
    return {"status": "ok"}


# ---- Endpoints: campanhas ---------------------------------------------
async def _run_campaign(campaign_id, tenant_id):
    """Executa o disparo da campanha (background). Faz pacing para nao floodar."""
    set_current_tenant(tenant_id)
    camp = await db.get_campaign(campaign_id)
    if not camp:
        return
    acct = await db.get_channel_account(camp["channel"])
    config = (acct or {}).get("config", {})
    await db.set_campaign_status(campaign_id, "sending")
    pending = await db.list_campaign_recipients(campaign_id, status="pending", limit=10000)
    sent = 0
    failed = 0
    for rec in pending:
        text = ch.render_template(camp.get("template", ""), rec)
        try:
            result = await ch.send(camp["channel"], config, rec["contact_ref"], text,
                                   media_url=camp.get("media_url") or None)
            if result.get("ok"):
                await db.update_recipient_status(rec["id"], "sent")
                sent += 1
            else:
                await db.update_recipient_status(rec["id"], "failed", result.get("error", ""))
                failed += 1
        except Exception as e:
            await db.update_recipient_status(rec["id"], "failed", str(e))
            failed += 1
        await asyncio.sleep(0.2)  # pacing ~5 msg/s
    stats = await db.campaign_stats(campaign_id)
    await db.set_campaign_status(campaign_id, "done", stats)
    logger.info(f"[CAMPAIGN] {campaign_id} concluida: {sent} enviadas, {failed} falhas")


@app.get("/api/campaigns")
async def api_campaigns_list(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.list_campaigns()


@app.post("/api/campaigns")
async def api_campaigns_create(payload: CampaignRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    if payload.channel not in ch.SUPPORTED:
        return JSONResponse(status_code=400, content={"error": "Canal nao suportado"})
    r = await db.create_campaign(payload.name, payload.channel, payload.template,
                                 segment_id=payload.segment_id, media_url=payload.media_url or "")
    cid = r.get("id")
    # popula destinatarios a partir do segmento (se houver)
    if cid and payload.segment_id:
        seg = await db.get_segment(payload.segment_id)
        if seg:
            recipients = await db.resolve_segment_recipients(seg.get("rules", []))
            await db.add_campaign_recipients(cid, recipients)
    return {"status": "ok", "id": cid}


@app.get("/api/campaigns/{campaign_id}")
async def api_campaign_get(campaign_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    camp = await db.get_campaign(campaign_id)
    if not camp:
        return JSONResponse(status_code=404, content={"error": "Nao encontrada"})
    camp["stats"] = await db.campaign_stats(campaign_id)
    return camp


@app.post("/api/campaigns/{campaign_id}/send")
async def api_campaign_send(campaign_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    camp = await db.get_campaign(campaign_id)
    if not camp:
        return JSONResponse(status_code=404, content={"error": "Nao encontrada"})
    stats = await db.campaign_stats(campaign_id)
    if stats.get("total", 0) == 0:
        return JSONResponse(status_code=400, content={"error": "Campanha sem destinatarios"})
    tid = get_current_tenant()
    asyncio.create_task(_run_campaign(campaign_id, tid))
    return {"status": "disparando", "recipients": stats.get("total", 0)}


@app.get("/api/campaigns/{campaign_id}/recipients")
async def api_campaign_recipients(campaign_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.list_campaign_recipients(campaign_id)


@app.delete("/api/campaigns/{campaign_id}")
async def api_campaign_delete(campaign_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    await db.delete_campaign(campaign_id)
    return {"status": "ok"}


# =====================================================================
# FASE 12 - VENDAS & CRM AVANCADO (models, helpers, endpoints)
# Pipeline Kanban, catalogo+carrinho+link de pagamento, cadencias,
# metas/comissoes e historico 360 do contato.
# =====================================================================
import sales_engine as se


class PipelineRequest(ApiPayload):
    name: str = "Pipeline"
    stages: Optional[list] = None


class DealRequest(ApiPayload):
    pipeline_id: Optional[int] = None
    title: str = ""
    contact_phone: str = ""
    contact_name: str = ""
    value: float = 0
    owner: str = ""
    stage: str = "lead"
    probability: Optional[int] = None
    expected_close: Optional[str] = None
    notes: str = ""


class DealUpdateRequest(ApiPayload):
    title: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_name: Optional[str] = None
    value: Optional[float] = None
    owner: Optional[str] = None
    stage: Optional[str] = None
    status: Optional[str] = None
    probability: Optional[int] = None
    expected_close: Optional[str] = None
    notes: Optional[str] = None


class DealMoveRequest(ApiPayload):
    stage: str


class DealEventRequest(ApiPayload):
    type: str = "note"
    detail: str = ""


class ProductRequest(ApiPayload):
    sku: str = ""
    name: str = ""
    description: str = ""
    price: float = 0
    stock: int = 0
    active: bool = True


class CartItemRequest(ApiPayload):
    product_id: int
    qty: int = 1


class CartDiscountRequest(ApiPayload):
    discount: float = 0
    discount_type: str = "value"


class CheckoutRequest(ApiPayload):
    contact_name: str = ""
    provider: str = "link"


class CadenceRequest(ApiPayload):
    name: str = "Cadencia"
    steps: Optional[list] = None
    active: bool = True


class CadenceEnrollRequest(ApiPayload):
    contact_phone: str
    session_id: str = "vendas"


class GoalRequest(ApiPayload):
    owner: str = ""
    period: str = ""
    metric: str = "revenue"
    target: float = 0


class CommissionRuleRequest(ApiPayload):
    name: str = "Regra"
    min_value: float = 0
    rate: float = 0
    active: bool = True


def _sales_token():
    return uuid.uuid4().hex[:20]


def _render_cadence(template, name):
    first = (name or "").split(" ")[0] if name else ""
    out = template or ""
    out = out.replace("[[nome]]", name or "")
    out = out.replace("[[primeiro_nome]]", first)
    return out


# ---- Pipelines --------------------------------------------------------
@app.get("/api/pipelines")
async def api_list_pipelines(user: dict = Depends(get_current_user)):
    await db.ensure_default_pipeline(se.DEFAULT_STAGES)
    return {"pipelines": await db.list_pipelines(), "stage_probability": se.STAGE_PROBABILITY}


@app.post("/api/pipelines")
async def api_create_pipeline(req: PipelineRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    stages = req.stages if req.stages else se.DEFAULT_STAGES
    return await db.create_pipeline(req.name, stages)


# ---- Negocios (deals) -------------------------------------------------
@app.get("/api/deals")
async def api_list_deals(pipeline_id: Optional[int] = None, owner: str = "", status: str = "",
                        user: dict = Depends(get_current_user)):
    pipe = await db.ensure_default_pipeline(se.DEFAULT_STAGES)
    pid = pipeline_id or (pipe.get("id") if pipe else None)
    deals = await db.list_deals(pipeline_id=pid, owner=owner, status=status)
    for d in deals:
        d["health"] = se.deal_health(d)
    return {"deals": deals, "forecast": se.weighted_forecast(deals), "pipeline_id": pid}


@app.post("/api/deals")
async def api_create_deal(req: DealRequest, user: dict = Depends(get_current_user)):
    pipe = await db.ensure_default_pipeline(se.DEFAULT_STAGES)
    pid = req.pipeline_id or (pipe.get("id") if pipe else None)
    owner = req.owner or user.get("username", "")
    return await db.create_deal(pid, req.title, req.contact_phone, req.contact_name,
                                req.value, owner, req.stage, req.probability,
                                req.expected_close or None, req.notes)


@app.get("/api/deals/{deal_id}")
async def api_get_deal(deal_id: int, user: dict = Depends(get_current_user)):
    deal = await db.get_deal(deal_id)
    if not deal:
        return JSONResponse(status_code=404, content={"error": "Negocio nao encontrado"})
    deal["health"] = se.deal_health(deal)
    deal["events"] = await db.get_deal_events(deal_id)
    return deal


@app.put("/api/deals/{deal_id}")
async def api_update_deal(deal_id: int, req: DealUpdateRequest, user: dict = Depends(get_current_user)):
    return await db.update_deal(deal_id, **req.model_dump(exclude_none=True))


@app.post("/api/deals/{deal_id}/move")
async def api_move_deal(deal_id: int, req: DealMoveRequest, user: dict = Depends(get_current_user)):
    res = await db.move_deal(deal_id, req.stage, user.get("username", ""))
    if req.stage == "ganho":
        await db.set_deal_status(deal_id, "won", user.get("username", ""))
    elif req.stage == "perdido":
        await db.set_deal_status(deal_id, "lost", user.get("username", ""))
    return res


@app.post("/api/deals/{deal_id}/event")
async def api_deal_event(deal_id: int, req: DealEventRequest, user: dict = Depends(get_current_user)):
    return await db.add_deal_event(deal_id, req.type, req.detail, user.get("username", ""))


@app.delete("/api/deals/{deal_id}")
async def api_delete_deal(deal_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.delete_deal(deal_id)


# ---- Catalogo ---------------------------------------------------------
@app.get("/api/products")
async def api_list_products(active_only: bool = False, user: dict = Depends(get_current_user)):
    return {"products": await db.list_products(active_only=active_only)}


@app.post("/api/products")
async def api_create_product(req: ProductRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.create_product(req.name, req.price, req.sku, req.description, req.stock, req.active)


@app.put("/api/products/{product_id}")
async def api_update_product(product_id: int, req: ProductRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.update_product(product_id, **req.model_dump())


@app.delete("/api/products/{product_id}")
async def api_delete_product(product_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.delete_product(product_id)


# ---- Carrinho ---------------------------------------------------------
@app.get("/api/cart/{phone}")
async def api_get_cart(phone: str, user: dict = Depends(get_current_user)):
    cart = await db.get_or_create_cart(phone)
    items = await db.get_cart_items(cart["id"])
    totals = se.cart_totals(items, float(cart.get("discount") or 0), cart.get("discount_type") or "value")
    return {"cart": cart, "items": items, "totals": totals}


@app.post("/api/cart/{phone}/items")
async def api_add_cart_item(phone: str, req: CartItemRequest, user: dict = Depends(get_current_user)):
    cart = await db.get_or_create_cart(phone)
    return await db.add_cart_item(cart["id"], req.product_id, req.qty)


@app.delete("/api/cart/items/{item_id}")
async def api_del_cart_item(item_id: int, user: dict = Depends(get_current_user)):
    return await db.remove_cart_item(item_id)


@app.post("/api/cart/{phone}/discount")
async def api_cart_discount(phone: str, req: CartDiscountRequest, user: dict = Depends(get_current_user)):
    cart = await db.get_or_create_cart(phone)
    return await db.set_cart_discount(cart["id"], req.discount, req.discount_type)


@app.post("/api/cart/{phone}/checkout")
async def api_checkout(phone: str, req: CheckoutRequest, user: dict = Depends(get_current_user)):
    cart = await db.get_or_create_cart(phone)
    items = await db.get_cart_items(cart["id"])
    if not items:
        return JSONResponse(status_code=400, content={"error": "Carrinho vazio"})
    totals = se.cart_totals(items, float(cart.get("discount") or 0), cart.get("discount_type") or "value")
    token = _sales_token()
    link = "/pay/" + token
    order_items = [{"name": it["name"], "price": float(it["price"]), "qty": int(it["qty"])} for it in items]
    res = await db.create_order(token, phone, req.contact_name, order_items,
                                totals["subtotal"], totals["discount"], totals["total"],
                                payment_link=link, payment_provider=req.provider,
                                owner=user.get("username", ""))
    await db.close_cart(cart["id"])
    return {"status": "ok", "order_id": res.get("id"), "token": token, "payment_link": link, "total": totals["total"]}


# ---- Pedidos ----------------------------------------------------------
@app.get("/api/orders")
async def api_list_orders(status: str = "", user: dict = Depends(get_current_user)):
    return {"orders": await db.list_orders(status=status)}


@app.get("/pay/{token}", response_class=HTMLResponse)
async def public_pay_page(token: str):
    order = await db.get_order_by_token(token)
    if not order:
        return HTMLResponse("<h1>Pedido nao encontrado</h1>", status_code=404)
    paid = order.get("status") == "paid"
    rows = ""
    for it in (order.get("items") or []):
        rows += "<tr><td>" + str(it.get("name", "")) + "</td><td>" + str(it.get("qty", 1)) + "x</td><td>R$ " + ("%.2f" % float(it.get("price", 0))) + "</td></tr>"
    total_txt = "%.2f" % float(order.get("total") or 0)
    action = ("<div class='paid'>Pagamento confirmado. Obrigado!</div>" if paid else
              "<button id='payBtn' onclick=\"pay()\">Pagar R$ " + total_txt + "</button>")
    html = (
        "<!doctype html><html lang='pt-BR'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Pagamento continnumOS</title><style>"
        "body{margin:0;background:#0d0d0f;color:#f2ede6;font-family:system-ui,Segoe UI,sans-serif;display:flex;min-height:100vh;align-items:center;justify-content:center}"
        ".card{width:min(460px,92vw);background:#141417;border:1px solid #2a2a30;border-radius:16px;padding:26px}"
        "h1{font-size:18px;margin:0 0 4px;color:#ff6a00}.mut{color:#9a9aa0;font-size:13px;margin-bottom:16px}"
        "table{width:100%;border-collapse:collapse;margin-bottom:14px}td{padding:7px 4px;border-bottom:1px solid #22222a;font-size:14px}"
        ".tot{font-size:22px;font-weight:700;margin:10px 0 18px}"
        "button{width:100%;padding:13px;border:0;border-radius:10px;background:#ff6a00;color:#0a0a0a;font-weight:700;font-size:15px;cursor:pointer}"
        ".paid{padding:13px;border-radius:10px;background:#3ddc8422;color:#3ddc84;text-align:center;font-weight:700}"
        "</style></head><body><div class='card'>"
        "<h1>continnumOS</h1><div class='mut'>Pedido " + str(token) + "</div>"
        "<table>" + rows + "</table><div class='tot'>Total: R$ " + total_txt + "</div>"
        + action +
        "<script>async function pay(){var b=document.getElementById('payBtn');b.textContent='Processando...';b.disabled=true;"
        "var r=await fetch('/pay/" + str(token) + "/confirm',{method:'POST'});"
        "if(r.ok){location.reload();}else{b.textContent='Falha, tente novamente';b.disabled=false;}}</script>"
        "</div></body></html>"
    )
    return HTMLResponse(html)


@app.post("/pay/{token}/confirm")
async def public_pay_confirm(token: str):
    # Confirmacao de pagamento. Em producao, e disparada pelo webhook do gateway
    # (services.payments.verify_and_parse). Aqui tambem cobre o link direto.
    res = await db.mark_order_paid(token, external_id="confirm-" + _sales_token())
    if res.get("status") != "ok":
        return JSONResponse(status_code=404, content={"error": "Pedido nao encontrado"})
    tid = res.get("tenant_id") or DEFAULT_TENANT_ID
    token_ctx = set_current_tenant(tid)
    try:
        order = await db.get_order_by_token(token)
        if order and order.get("contact_phone"):
            try:
                await send_automated_message(order["contact_phone"],
                    "Recebemos seu pagamento de R$ %.2f. Obrigado pela compra!" % float(order.get("total") or 0),
                    "vendas", "pagamento_confirmado", enforce_guard=False)
            except Exception:
                pass
        await manager.broadcast("orders_update")
    finally:
        reset_current_tenant(token_ctx)
    return {"status": "ok"}


# ---- Cadencias --------------------------------------------------------
@app.get("/api/cadences")
async def api_list_cadences(user: dict = Depends(get_current_user)):
    return {"cadences": await db.list_cadences()}


@app.post("/api/cadences")
async def api_create_cadence(req: CadenceRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    warns = se.validate_cadence(req.steps or [])
    res = await db.create_cadence(req.name, req.steps or [], req.active)
    res["warnings"] = warns
    return res


@app.put("/api/cadences/{cadence_id}")
async def api_update_cadence(cadence_id: int, req: CadenceRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.update_cadence(cadence_id, name=req.name, steps=req.steps, active=req.active)


@app.delete("/api/cadences/{cadence_id}")
async def api_delete_cadence(cadence_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.delete_cadence(cadence_id)


@app.post("/api/cadences/{cadence_id}/enroll")
async def api_enroll_cadence(cadence_id: int, req: CadenceEnrollRequest, user: dict = Depends(get_current_user)):
    return await db.enroll_cadence(cadence_id, req.contact_phone, req.session_id)


# ---- Metas e comissoes ------------------------------------------------
@app.get("/api/goals")
async def api_list_goals(period: str = "", user: dict = Depends(get_current_user)):
    goals = await db.list_goals(period=period)
    rev = await db.revenue_by_owner(period_prefix=period)
    by_owner = {}
    for r in rev:
        by_owner[r.get("owner") or ""] = float(r.get("revenue") or 0)
    for g in goals:
        achieved = by_owner.get(g.get("owner") or "", 0)
        g["achieved"] = achieved
        g["progress"] = se.goal_progress(g.get("target"), achieved)
    return {"goals": goals}


@app.post("/api/goals")
async def api_create_goal(req: GoalRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.create_goal(req.owner, req.period, req.metric, req.target)


@app.delete("/api/goals/{goal_id}")
async def api_delete_goal(goal_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.delete_goal(goal_id)


@app.get("/api/commissions/rules")
async def api_list_comm_rules(user: dict = Depends(get_current_user)):
    return {"rules": await db.list_commission_rules()}


@app.post("/api/commissions/rules")
async def api_create_comm_rule(req: CommissionRuleRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.create_commission_rule(req.name, req.min_value, req.rate, req.active)


@app.delete("/api/commissions/rules/{rule_id}")
async def api_delete_comm_rule(rule_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.delete_commission_rule(rule_id)


@app.get("/api/commissions/report")
async def api_comm_report(period: str = "", user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    rules = await db.list_commission_rules()
    rev = await db.revenue_by_owner(period_prefix=period)
    report = []
    for r in rev:
        comm = se.commission_for(float(r.get("revenue") or 0), rules)
        report.append({"owner": r.get("owner") or "", "revenue": float(r.get("revenue") or 0),
                       "orders": int(r.get("orders") or 0), "rate": comm["rate"], "commission": comm["commission"]})
    total = round(sum(x["commission"] for x in report), 2)
    return {"period": period, "report": report, "total_commission": total}


# ---- Historico 360 ----------------------------------------------------
@app.get("/api/contact360/{phone}")
async def api_contact_360(phone: str, user: dict = Depends(get_current_user)):
    return await db.contact_360(phone)


# ---- Loop de cadencias (background) -----------------------------------
async def cadence_loop():
    """Fase 12: executa passos de cadencia devidos, em todos os tenants."""
    from tenant_context import set_current_tenant as _sct, reset_current_tenant as _rct
    await asyncio.sleep(25)
    while True:
        try:
            enrolls = await db.list_active_enrollments(limit=200)
            for e in enrolls:
                due, step, finished = se.next_cadence_step(
                    e.get("steps") or [], int(e.get("step_idx") or 0), int(e.get("last_run_ts") or 0))
                if finished:
                    await db.advance_enrollment(e["id"], e["tenant_id"], int(e.get("step_idx") or 0), finished=True)
                    continue
                if not due or not step:
                    continue
                tok = _sct(e.get("tenant_id") or DEFAULT_TENANT_ID)
                try:
                    phone = e.get("contact_phone") or ""
                    name = ""
                    try:
                        u = await db.get_user(phone)
                        name = (u or {}).get("name", "") if u else ""
                    except Exception:
                        name = ""
                    text = _render_cadence(step.get("template", ""), name)
                    sess = e.get("session_id") or "vendas"
                    if text:
                        await send_automated_message(phone, text, sess, "cadencia", enforce_guard=False)
                    nxt = int(e.get("step_idx") or 0) + 1
                    is_last = nxt >= len(e.get("steps") or [])
                    await db.advance_enrollment(e["id"], e["tenant_id"], nxt, finished=is_last)
                except Exception as ex:
                    logger.error("[CADENCE] erro no enrollment %s: %s" % (e.get("id"), ex))
                finally:
                    _rct(tok)
        except Exception as ex:
            logger.error("[CADENCE] loop erro: %s" % ex)
        await asyncio.sleep(60)


# =====================================================================
# FASE 13 - PRODUTIVIDADE DA EQUIPE (models, helpers, endpoints)
# Filas & SLA, tickets, supervisao em tempo real, QA, gamificacao,
# escalas e presenca de atendentes.
# =====================================================================
import team_engine as te


class QueueRequest(ApiPayload):
    name: str = "Fila"
    description: str = ""
    sla_first: int = 5
    sla_resolution: int = 60
    members: Optional[list] = None


class QueueUpdateRequest(ApiPayload):
    name: Optional[str] = None
    description: Optional[str] = None
    sla_first: Optional[int] = None
    sla_resolution: Optional[int] = None
    members: Optional[list] = None
    active: Optional[bool] = None


class TicketRequest(ApiPayload):
    queue_id: Optional[int] = None
    contact_phone: str = ""
    contact_name: str = ""
    subject: str = ""
    priority: str = "normal"
    assignee: str = ""


class TicketAssignRequest(ApiPayload):
    assignee: str


class ScorecardRequest(ApiPayload):
    name: str = "Avaliacao"
    criteria: Optional[list] = None


class QaReviewRequest(ApiPayload):
    scorecard_id: Optional[int] = None
    ticket_id: Optional[int] = None
    agent: str = ""
    answers: Optional[dict] = None
    notes: str = ""


class GamificationAwardRequest(ApiPayload):
    agent: str
    action: str
    ref: str = ""
    points: Optional[int] = None


class GamificationRuleRequest(ApiPayload):
    action: str
    points: int = 0


class PresenceRequest(ApiPayload):
    status: str = "online"


class ShiftRequest(ApiPayload):
    agent: str = ""
    weekday: int = 0
    start: str = "09:00"
    end: str = "18:00"


def _apply_ticket_sla(t):
    """Anexa estados de SLA calculados a um ticket (usa a fila quando houver)."""
    first_target = t.get("sla_first_response_mins") or 5
    res_target = t.get("sla_resolution_mins") or 60
    created = t.get("created_ts")
    first = te.sla_state(created, first_target, done_ts=t.get("first_response_ts"))
    res = te.sla_state(created, res_target, done_ts=t.get("resolved_ts"))
    t["sla_first"] = first
    t["sla_resolution"] = res
    return t


# ---- Filas ------------------------------------------------------------
@app.get("/api/queues")
async def api_list_queues(active_only: bool = False, user: dict = Depends(get_current_user)):
    return {"queues": await db.list_queues(active_only=active_only)}


@app.post("/api/queues")
async def api_create_queue(req: QueueRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.create_queue(req.name, req.description, req.sla_first, req.sla_resolution, req.members or [])


@app.put("/api/queues/{queue_id}")
async def api_update_queue(queue_id: int, req: QueueUpdateRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.update_queue(queue_id, **req.model_dump(exclude_none=True))


@app.delete("/api/queues/{queue_id}")
async def api_delete_queue(queue_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.delete_queue(queue_id)


# ---- Tickets ----------------------------------------------------------
@app.get("/api/tickets")
async def api_list_tickets(status: str = "open", queue_id: Optional[int] = None, assignee: str = "",
                         user: dict = Depends(get_current_user)):
    tickets = await db.list_tickets(status=status, queue_id=queue_id, assignee=assignee)
    queues = {q["id"]: q for q in await db.list_queues()}
    for t in tickets:
        q = queues.get(t.get("queue_id"))
        if q:
            t["sla_first_response_mins"] = q.get("sla_first_response_mins")
            t["sla_resolution_mins"] = q.get("sla_resolution_mins")
        _apply_ticket_sla(t)
    return {"tickets": tickets}


@app.post("/api/tickets")
async def api_create_ticket(req: TicketRequest, user: dict = Depends(get_current_user)):
    return await db.create_ticket(req.queue_id, req.contact_phone, req.subject, req.contact_name, req.priority, req.assignee)


@app.get("/api/tickets/{ticket_id}")
async def api_get_ticket(ticket_id: int, user: dict = Depends(get_current_user)):
    t = await db.get_ticket(ticket_id)
    if not t:
        return JSONResponse(status_code=404, content={"error": "Ticket nao encontrado"})
    if t.get("queue_id"):
        q = await db.get_queue(t["queue_id"])
        if q:
            t["sla_first_response_mins"] = q.get("sla_first_response_mins")
            t["sla_resolution_mins"] = q.get("sla_resolution_mins")
    return _apply_ticket_sla(t)


@app.post("/api/tickets/{ticket_id}/assign")
async def api_assign_ticket(ticket_id: int, req: TicketAssignRequest, user: dict = Depends(get_current_user)):
    return await db.assign_ticket(ticket_id, req.assignee)


@app.post("/api/tickets/{ticket_id}/first-response")
async def api_ticket_first_response(ticket_id: int, user: dict = Depends(get_current_user)):
    return await db.mark_ticket_first_response(ticket_id)


@app.post("/api/tickets/{ticket_id}/resolve")
async def api_resolve_ticket(ticket_id: int, user: dict = Depends(get_current_user)):
    res = await db.resolve_ticket(ticket_id)
    try:
        t = await db.get_ticket(ticket_id)
        agent = (t or {}).get("assignee") or user.get("username", "")
        if agent:
            await _gamify(agent, "conversa_resolvida", "ticket:" + str(ticket_id))
    except Exception:
        pass
    await manager.broadcast("supervision_update")
    return res


# ---- Supervisao em tempo real ----------------------------------------
@app.get("/api/supervision")
async def api_supervision(user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    snap = await db.supervision_snapshot()
    # enriquece presenca com status efetivo (escala + heartbeat)
    try:
        shifts_all = await db.list_shifts()
        by_agent = {}
        for s in shifts_all:
            by_agent.setdefault(s["agent"], []).append(s)
        for p in snap.get("presence", []):
            eff = te.effective_status(p.get("status"), by_agent.get(p.get("agent"), []),
                                      heartbeat_ts=p.get("heartbeat_ts"))
            p["effective"] = eff["status"]
            p["in_shift"] = eff["in_shift"]
    except Exception:
        pass
    return snap


# ---- QA ---------------------------------------------------------------
@app.get("/api/qa/scorecards")
async def api_list_scorecards(user: dict = Depends(get_current_user)):
    return {"scorecards": await db.list_scorecards()}


@app.post("/api/qa/scorecards")
async def api_create_scorecard(req: ScorecardRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.create_scorecard(req.name, req.criteria or [])


@app.delete("/api/qa/scorecards/{scorecard_id}")
async def api_delete_scorecard(scorecard_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.delete_scorecard(scorecard_id)


@app.post("/api/qa/reviews")
async def api_create_qa_review(req: QaReviewRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    criteria = []
    if req.scorecard_id:
        criteria = await db.get_scorecard(req.scorecard_id) or []
    scored = te.qa_score(criteria, req.answers or {})
    return await db.create_qa_review(req.scorecard_id, req.ticket_id, req.agent,
                                     user.get("username", ""), req.answers or {}, scored["score"], req.notes)


@app.get("/api/qa/reviews")
async def api_list_qa_reviews(agent: str = "", user: dict = Depends(get_current_user)):
    return {"reviews": await db.list_qa_reviews(agent=agent)}


@app.get("/api/qa/summary")
async def api_qa_summary(user: dict = Depends(get_current_user)):
    return {"summary": await db.qa_average_by_agent()}


# ---- Gamificacao ------------------------------------------------------
async def _gamify(agent, action, ref=""):
    """Registra um evento de gamificacao usando as regras do tenant."""
    try:
        rules = {r["action"]: r["points"] for r in await db.list_gamification_rules()}
    except Exception:
        rules = {}
    pts = te.points_for(action, rules)
    return await db.add_gamification_event(agent, action, pts, ref)


@app.get("/api/gamification/leaderboard")
async def api_leaderboard(user: dict = Depends(get_current_user)):
    board = await db.gamification_leaderboard()
    for row in board:
        row["level"] = te.gamification_level(row.get("points", 0))
    return {"leaderboard": board}


@app.get("/api/gamification/agent/{agent}")
async def api_gamification_agent(agent: str, user: dict = Depends(get_current_user)):
    board = await db.gamification_leaderboard()
    pts = 0
    for row in board:
        if row.get("agent") == agent:
            pts = row.get("points", 0)
            break
    metrics = await db.gamification_metrics(agent)
    qa = await db.qa_average_by_agent()
    for q in qa:
        if q.get("agent") == agent:
            metrics["qa_avg"] = q.get("avg_score", 0)
    return {"agent": agent, "level": te.gamification_level(pts), "badges": te.award_badges(metrics), "metrics": metrics}


@app.post("/api/gamification/award")
async def api_gamification_award(req: GamificationAwardRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    if req.points is not None:
        return await db.add_gamification_event(req.agent, req.action, req.points, req.ref)
    return await _gamify(req.agent, req.action, req.ref)


@app.get("/api/gamification/rules")
async def api_list_gamification_rules(user: dict = Depends(get_current_user)):
    return {"rules": await db.list_gamification_rules(), "defaults": te.DEFAULT_POINTS}


@app.post("/api/gamification/rules")
async def api_set_gamification_rule(req: GamificationRuleRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.set_gamification_rule(req.action, req.points)


# ---- Escalas e presenca ----------------------------------------------
@app.post("/api/presence/heartbeat")
async def api_presence_heartbeat(req: PresenceRequest, user: dict = Depends(get_current_user)):
    return await db.presence_heartbeat(user.get("username", ""), req.status)


@app.get("/api/presence")
async def api_list_presence(user: dict = Depends(get_current_user)):
    presence = await db.list_presence()
    shifts_all = await db.list_shifts()
    by_agent = {}
    for s in shifts_all:
        by_agent.setdefault(s["agent"], []).append(s)
    for p in presence:
        eff = te.effective_status(p.get("status"), by_agent.get(p.get("agent"), []), heartbeat_ts=p.get("heartbeat_ts"))
        p["effective"] = eff["status"]
        p["in_shift"] = eff["in_shift"]
    return {"presence": presence}


@app.get("/api/shifts")
async def api_list_shifts(agent: str = "", user: dict = Depends(get_current_user)):
    return {"shifts": await db.list_shifts(agent=agent)}


@app.post("/api/shifts")
async def api_add_shift(req: ShiftRequest, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.add_shift(req.agent, req.weekday, req.start, req.end)


@app.delete("/api/shifts/{shift_id}")
async def api_delete_shift(shift_id: int, user: dict = Depends(get_current_user)):
    if not _is_admin(user):
        return JSONResponse(status_code=403, content={"error": "Apenas admin"})
    return await db.delete_shift(shift_id)


# ---- Loop de SLA de tickets (background) ------------------------------
async def team_sla_loop():
    """Fase 13: recalcula o estado de SLA dos tickets abertos e sinaliza risco."""
    from tenant_context import set_current_tenant as _sct, reset_current_tenant as _rct
    await asyncio.sleep(30)
    while True:
        try:
            tickets = await db.list_open_tickets_all_tenants(limit=500)
            changed_tenants = set()
            for t in tickets:
                first_target = t.get("sla_first_response_mins") or 5
                res_target = t.get("sla_resolution_mins") or 60
                created = t.get("created_ts")
                fs = te.sla_state(created, first_target, done_ts=t.get("first_response_ts"))
                rs = te.sla_state(created, res_target, done_ts=t.get("resolved_ts"))
                if fs["state"] != t.get("sla_first_state") or rs["state"] != t.get("sla_resolution_state"):
                    await db.update_ticket_sla_state_global(t["id"], t["tenant_id"], fs["state"], rs["state"])
                    changed_tenants.add(t["tenant_id"])
            for tid in changed_tenants:
                tok = _sct(tid)
                try:
                    await manager.broadcast("supervision_update")
                finally:
                    _rct(tok)
        except Exception as ex:
            logger.error("[TEAM-SLA] loop erro: %s" % ex)
        await asyncio.sleep(45)


if __name__ == "__main__":
    import uvicorn
    # Fase 7: nº de workers vem de WEB_CONCURRENCY (multi-worker por processo).
    uvicorn.run("main:app", host="0.0.0.0", port=8000, workers=settings.WEB_CONCURRENCY)
