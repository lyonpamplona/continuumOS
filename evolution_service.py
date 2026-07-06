"""Cliente assíncrono para comunicação com a Evolution API.

As funções deste módulo encapsulam envio de texto, mídia, reação, busca de
foto de perfil, download de mídia recebida e sincronização básica de histórico.
"""

import re
import httpx
from loguru import logger
from config import settings

EVO_URL = settings.EVO_API_URL
API_KEY = settings.EVO_API_KEY

HEADERS = {
    "apikey": API_KEY,
    "Content-Type": "application/json"
}

client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), headers=HEADERS)


def normalize_number(chat_id: str) -> str:
    """Remove sufixos JID e caracteres não numéricos para endpoints que exigem número."""
    return re.sub(r"\D", "", chat_id or "")

def response_excerpt(response, limit: int = 1000) -> str:
    """Retorna um trecho seguro do corpo da Evolution para diagnóstico de erro."""
    return response.text[:limit] if getattr(response, "text", None) else ""

def evolution_result(ok: bool, response=None, error: str = ""):
    """Padroniza o retorno interno das chamadas Evolution com diagnóstico seguro."""
    if response is None:
        return {"ok": ok, "status_code": None, "body": "", "error": error, "data": None}
    body = response.text[:500] if getattr(response, "text", None) else ""
    data = None
    try:
        data = response.json()
    except Exception:
        pass
    return {"ok": ok, "status_code": response.status_code, "body": body, "error": error, "data": data}

def extract_message_id(data):
    """Extrai o ID remoto de respostas Evolution com formatos diferentes."""
    if isinstance(data, dict):
        key = data.get("key")
        if isinstance(key, dict) and key.get("id"):
            return key.get("id")
        for value in data.values():
            found = extract_message_id(value)
            if found:
                return found
    if isinstance(data, list):
        for item in data:
            found = extract_message_id(item)
            if found:
                return found
    return None

def extract_contact_records(data):
    """Normaliza formatos de resposta da Evolution para uma lista de contatos."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("records", "contacts", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    nested_contacts = data.get("contacts")
    if isinstance(nested_contacts, dict):
        records = nested_contacts.get("records")
        if isinstance(records, list):
            return records
    return []

def normalize_contact_record(record: dict):
    """Extrai JID e nome de um contato salvo, ignorando grupos e registros inválidos."""
    if not isinstance(record, dict):
        return None
    remote_jid = record.get("remoteJid") or record.get("id")
    if not remote_jid or record.get("isGroup") or "@g.us" in remote_jid:
        return None
    if record.get("isSaved") is False:
        return None
    if "@s.whatsapp.net" not in remote_jid:
        return None
    push_name = (record.get("pushName") or record.get("name") or "").strip()
    return {"phone": remote_jid, "name": push_name}

async def fetch_contacts(session: str = "vendas"):
    """Busca contatos salvos da instância Evolution e devolve contatos normalizados."""
    url = f"{EVO_URL}/chat/findContacts/{session}"
    try:
        response = await client.post(url, json={})
        if response.status_code not in [200, 201]:
            logger.error(f"Erro Evolution ao buscar contatos: status={response.status_code} body={response_excerpt(response)} session={session}")
            return evolution_result(False, response)
        records = extract_contact_records(response.json())
        contacts = []
        ignored = 0
        for record in records:
            contact = normalize_contact_record(record)
            if contact:
                contacts.append(contact)
            else:
                ignored += 1
        result = evolution_result(True, response)
        result["contacts"] = contacts
        result["total_records"] = len(records)
        result["ignored_records"] = ignored
        return result
    except Exception as e:
        logger.error(f"Erro ao buscar contatos pela Evolution: {e}")
        return evolution_result(False, error=str(e))

async def send_message(chat_id: str, text: str, session: str = "vendas", quoted_id: str = None):
    """Envia texto e retorna ID remoto somente quando a Evolution confirma."""
    url = f"{EVO_URL}/message/sendText/{session}"
    number = normalize_number(chat_id)
    if not number:
        logger.error(f"Envio de texto cancelado: numero invalido ({chat_id}).")
        return None
    payload = {"number": number, "text": text}
    if quoted_id:
        payload["quoted"] = {"key": {"id": quoted_id}}
    try:
        response = await client.post(url, json=payload)
        if response.status_code not in [200, 201]:
            logger.error(f"Erro Evolution ao enviar texto: status={response.status_code} body={response_excerpt(response)} number={number} session={session}")
            return None
        message_id = extract_message_id(response.json())
        if not message_id:
            logger.error(f"Evolution enviou texto sem ID remoto: status={response.status_code} body={response.text[:300]}")
        return message_id
    except Exception as e:
        logger.error(f"Erro ao enviar texto pela Evolution: {e}")
        return None

async def send_reaction(chat_id: str, message_id: str, emoji: str, session: str = "vendas", from_me: bool = False):
    """Envia uma reação com emoji para uma mensagem do WhatsApp."""
    url = f"{EVO_URL}/message/sendReaction/{session}"
    payload = {
        "key": {
            "remoteJid": chat_id,
            "fromMe": from_me,
            "id": message_id
        },
        "reaction": emoji
    }
    try:
        response = await client.post(url, json=payload)
        return response.status_code in [200, 201]
    except Exception:
        return False

async def edit_message(chat_id: str, message_id: str, new_text: str, session: str = "vendas"):
    """Edita uma mensagem enviada pela instância dentro da janela aceita pelo WhatsApp."""
    url = f"{EVO_URL}/chat/updateMessage/{session}"
    payload = {
        "number": normalize_number(chat_id),
        "text": new_text,
        "key": {
            "remoteJid": chat_id,
            "fromMe": True,
            "id": message_id
        }
    }
    try:
        response = await client.post(url, json=payload)
        if response.status_code == 405:
            response = await client.put(url, json=payload)
        ok = response.status_code in [200, 201, 204]
        if not ok:
            logger.error(f"Erro Evolution ao editar mensagem: {response.status_code} - {response.text}")
        return evolution_result(ok, response)
    except Exception as e:
        logger.error(f"Exceção ao editar mensagem na Evolution: {e}")
        return evolution_result(False, error=str(e))

async def delete_message_for_everyone(chat_id: str, message_id: str, session: str = "vendas"):
    """Apaga para todos uma mensagem enviada pela instância dentro da janela segura."""
    url = f"{EVO_URL}/chat/deleteMessageForEveryone/{session}"
    payload = {
        "id": message_id,
        "remoteJid": chat_id,
        "fromMe": True,
        "participant": chat_id
    }
    try:
        response = await client.request("DELETE", url, json=payload)
        ok = response.status_code in [200, 201, 204]
        if not ok:
            logger.error(f"Erro Evolution ao apagar mensagem: {response.status_code} - {response.text}")
        return evolution_result(ok, response)
    except Exception as e:
        logger.error(f"Exceção ao apagar mensagem na Evolution: {e}")
        return evolution_result(False, error=str(e))

async def get_profile_pic(chat_id: str, session: str = "vendas"):
    """Consulta a URL da foto de perfil de um contato."""
    url = f"{EVO_URL}/chat/fetchProfilePictureUrl/{session}"
    payload = {"number": chat_id}
    try:
        response = await client.post(url, json=payload)
        return response.json().get("profilePictureUrl")
    except Exception:
        return None

# --------------------------------------------------------------------------
# Fase 2 — Provisionamento de instâncias por tenant
# --------------------------------------------------------------------------

async def get_connection_state(instance: str = "vendas"):
    """Retorna a resposta bruta de connectionState (ou None em falha de rede)."""
    url = f"{EVO_URL}/instance/connectionState/{instance}"
    try:
        return await client.get(url)
    except Exception as e:
        logger.error(f"Erro ao consultar estado da instância {instance}: {e}")
        return None


async def create_instance(instance: str, token: str = None):
    """Provisiona uma instância Evolution para um tenant.

    Idempotente para o chamador: status 403/409 ("já existe") é tratado como sucesso.
    """
    url = f"{EVO_URL}/instance/create"
    payload = {
        "instanceName": instance,
        "token": token or API_KEY,
        "qrcode": True,
        "integration": "WHATSAPP-BAILEYS",
    }
    try:
        res = await client.post(url, json=payload)
        already = res.status_code in [403, 409]
        ok = res.status_code in [200, 201] or already
        if not ok:
            logger.error(
                f"Erro Evolution ao criar instância {instance}: "
                f"status={res.status_code} body={response_excerpt(res)}"
            )
        result = evolution_result(ok, res)
        result["already_exists"] = already
        return result
    except Exception as e:
        logger.error(f"Exceção ao criar instância {instance}: {e}")
        return evolution_result(False, error=str(e))


async def ensure_instance(instance: str, token: str = None) -> bool:
    """Garante que a instância exista na Evolution; cria automaticamente se faltar."""
    state = await get_connection_state(instance)
    if state is not None and state.status_code not in [400, 404]:
        return True
    result = await create_instance(instance, token=token)
    return bool(result.get("ok"))


async def logout_instance(instance: str = "vendas"):
    """Desconecta uma instância Evolution."""
    url = f"{EVO_URL}/instance/logout/{instance}"
    try:
        res = await client.delete(url)
        return evolution_result(res.status_code in [200, 201, 204], res)
    except Exception as e:
        logger.error(f"Erro ao deslogar instância {instance}: {e}")
        return evolution_result(False, error=str(e))

async def download_media(full_message_payload: dict, session_id: str) -> str:
    """Baixa a mídia de uma mensagem recebida e retorna o conteúdo em base64."""
    if not full_message_payload:
        return None
    url = f"{settings.EVO_API_URL}/chat/getBase64FromMediaMessage/{session_id}"
    payload = {"message": full_message_payload}
    try:
        headers = {"apikey": settings.EVO_API_KEY, "Content-Type": "application/json"}
        response = await client.post(url, json=payload, headers=headers)
        if response.status_code in [200, 201]:
            data = response.json()
            base64_data = data.get('base64')
            if base64_data:
                 return base64_data.split("base64,")[1] if "base64," in base64_data else base64_data
    except Exception:
        pass
    return None

async def send_media(chat_id: str, file_data_b64: str, media_type: str, mimetype: str, filename: str, caption: str = "", session: str = "vendas", quoted_id: str = None):
    """Envia midia em base64 e retorna ID remoto somente quando confirmado."""
    url = f"{EVO_URL}/message/sendMedia/{session}"
    number = normalize_number(chat_id)
    if not number:
        logger.error(f"Envio de mídia cancelado: numero invalido ({chat_id}).")
        return None
    base64_limpo = file_data_b64.split("base64,")[1] if "base64," in file_data_b64 else file_data_b64
    safe_media_type = media_type if media_type in ['image', 'video', 'document', 'audio'] else 'document'
    payload = {
        "number": number,
        "mediatype": safe_media_type,
        "mimetype": mimetype or "application/octet-stream",
        "media": base64_limpo,
        "fileName": filename
    }
    if caption: 
        payload["caption"] = caption
    if quoted_id:
        payload["quoted"] = {"key": {"id": quoted_id}}
    try:
        response = await client.post(url, json=payload)
        if response.status_code == 400:
            retry_payload = {**payload, "media": f"data:{payload['mimetype']};base64,{base64_limpo}"}
            retry_response = await client.post(url, json=retry_payload)
            if retry_response.status_code in [200, 201]:
                response = retry_response
            else:
                logger.error(
                    "Erro Evolution ao enviar mídia com retry data-uri: "
                    f"status={retry_response.status_code} body={response_excerpt(retry_response)} "
                    f"number={number} session={session} mediatype={safe_media_type} "
                    f"mimetype={payload['mimetype']} fileName={filename}"
                )
        if response.status_code not in [200, 201]:
            logger.error(
                "Erro Evolution ao enviar mídia: "
                f"status={response.status_code} body={response_excerpt(response)} "
                f"number={number} session={session} mediatype={safe_media_type} "
                f"mimetype={payload['mimetype']} fileName={filename}"
            )
            return None
        message_id = extract_message_id(response.json())
        if not message_id:
            logger.error(f"Evolution enviou midia sem ID remoto: status={response.status_code} body={response.text[:300]}")
        return message_id
    except Exception as e:
        logger.error(f"Erro ao enviar mídia pela Evolution: {e}")
        return None
    
async def fetch_history(chat_id: str, count: int = 30, session: str = "vendas"):
    """Busca mensagens recentes de um contato na base da Evolution API."""
    url = f"{EVO_URL}/chat/findMessages/{session}"
    payload = {"where": {"remoteJid": chat_id}, "take": count}
    try:
        response = await client.post(url, json=payload)
        if response.status_code in [200, 201]:
            data = response.json()
            records = data.get("messages", {}).get("records", []) if "messages" in data else data
            return records if isinstance(records, list) else []
        return []
    except Exception:
        return []

async def send_status(session: str, status_type: str, content: str, jid_list: list, background_color: str = "#FF6600"):
    """Publica um status do WhatsApp para uma lista de contatos.

    Fase 3: `background_color` vem da marca do tenant (fallback laranja continnumOS).
    """
    url = f"{EVO_URL}/message/sendStatus/{session}"
    background_color = background_color or "#FF6600"
    base_payload = {
        "type": status_type,
        "content": content,
        "caption": "",
        "backgroundColor": background_color,
        "font": 1,
        "allContacts": False,
        "statusJidList": jid_list,
    }
    if status_type == "text":
        base_payload["backgroundColor"] = background_color
        base_payload["font"] = 1

    content_candidates = [content]
    if status_type == "image" and isinstance(content, str) and content.startswith("data:") and "," in content:
        raw_base64 = content.split(",", 1)[1].strip()
        if raw_base64:
            content_candidates.append(raw_base64)

    attempts = []
    seen_attempt_keys = set()
    for content_variant in content_candidates:
        for payload_style in ("flat", "wrapped"):
            payload = dict(base_payload)
            payload["content"] = content_variant
            outbound = payload if payload_style == "flat" else {"statusMessage": payload}
            key = (payload_style, "data_uri" if str(content_variant).startswith("data:") else "raw_base64_or_text")
            if key in seen_attempt_keys:
                continue
            seen_attempt_keys.add(key)
            attempts.append((payload_style, key[1], outbound))

    last_error = ""
    for index, (payload_style, content_variant_name, outbound_payload) in enumerate(attempts, start=1):
        safe_payload = {
            "payload_style": payload_style,
            "content_variant": content_variant_name,
            "type": status_type,
            "content": f"<{status_type}:{len(content)} chars>",
            "caption": "",
            "backgroundColor": "#FF6600",
            "font": 1,
            "allContacts": False,
            "statusJidList": f"{len(jid_list)} contatos",
        }
        try:
            logger.info(
                f"[EVO_STATUS_ATTEMPT] session={session} attempt={index}/{len(attempts)} "
                f"style={payload_style} content_variant={content_variant_name} targets={len(jid_list)}"
            )
            response = await client.post(url, json=outbound_payload)
            if response.status_code in [200, 201]:
                logger.info(
                    f"[EVO_STATUS_SUCCESS] session={session} attempt={index}/{len(attempts)} "
                    f"style={payload_style} content_variant={content_variant_name} status={response.status_code}"
                )
                result = evolution_result(True, response)
                result["attempt"] = index
                result["payload_style"] = payload_style
                result["content_variant"] = content_variant_name
                return result

            excerpt = response_excerpt(response)
            logger.error(
                "Erro Evolution ao postar status: "
                f"session={session} attempt={index}/{len(attempts)} style={payload_style} "
                f"content_variant={content_variant_name} status={response.status_code} "
                f"body={excerpt} payload={safe_payload}"
            )
            last_error = excerpt or f"http_{response.status_code}"
        except Exception as exc:
            error_class = exc.__class__.__name__
            error_text = str(exc) or repr(exc)
            logger.error(
                "Exceção ao postar status na Evolution: "
                f"session={session} attempt={index}/{len(attempts)} style={payload_style} "
                f"content_variant={content_variant_name} error_type={error_class} error={error_text} "
                f"payload={safe_payload}"
            )
            last_error = f"{error_class}: {error_text}"

    result = evolution_result(False, error=last_error or "falha_sem_detalhe")
    result["attempt"] = len(attempts)
    result["payload_style"] = "n/a"
    result["content_variant"] = "n/a"
    return result
