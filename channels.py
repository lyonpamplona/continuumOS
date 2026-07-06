"""Fase 11 - Omnichannel: adaptadores de canais.

Cada canal implementa duas responsabilidades:
  * send(...)          -> envia uma mensagem pelo canal (HTTP real via httpx).
  * parse_inbound(...) -> normaliza um payload de webhook em um dict unificado.

Quando nao ha credenciais/config validas, o envio cai em modo 'stub' (simulado)
para o sistema continuar funcional em desenvolvimento e sem credenciais reais.
O dict unificado de entrada tem o formato:
    {
      channel, external_id, contact_ref, contact_name,
      text, media_url, media_type, timestamp, raw
    }
"""

import re
import time
import logging

try:
    import httpx
except Exception:  # pragma: no cover - httpx sempre presente em producao
    httpx = None

logger = logging.getLogger("continnumos.channels")

# Canais adicionais oferecidos na Fase 11 (o WhatsApp via Evolution ja existe).
SUPPORTED = ["whatsapp_cloud", "telegram", "instagram", "messenger", "email", "webchat"]

CHANNEL_LABELS = {
    "whatsapp_cloud": "WhatsApp Cloud API",
    "telegram": "Telegram",
    "instagram": "Instagram Direct",
    "messenger": "Facebook Messenger",
    "email": "E-mail",
    "webchat": "Webchat",
}

# Campos de configuracao esperados por canal (para a UI de conexao).
CONFIG_FIELDS = {
    "whatsapp_cloud": ["phone_number_id", "access_token", "verify_token", "app_secret"],
    "telegram": ["bot_token", "secret"],
    "instagram": ["page_id", "access_token", "verify_token", "app_secret"],
    "messenger": ["page_id", "access_token", "verify_token", "app_secret"],
    "email": ["smtp_host", "smtp_port", "username", "password", "from_addr"],
    "webchat": ["widget_color", "welcome"],
}

_TIMEOUT = 30.0


def label(channel):
    return CHANNEL_LABELS.get(channel, channel)


def _result(ok, channel, external_id=None, response=None, error="", stub=False):
    """Retorno padronizado de qualquer envio de canal."""
    return {
        "ok": ok,
        "channel": channel,
        "external_id": external_id,
        "response": response,
        "error": error,
        "stub": stub,
    }


def _has(config, *keys):
    """True se todas as chaves existem e sao nao-vazias na config."""
    if not isinstance(config, dict):
        return False
    for k in keys:
        if not str(config.get(k, "") or "").strip():
            return False
    return True


async def _post_json(url, payload, headers=None):
    """POST JSON generico. Levanta em erro HTTP; o chamador captura."""
    if httpx is None:
        raise RuntimeError("httpx indisponivel")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
        r = await c.post(url, json=payload, headers=headers or {})
        r.raise_for_status()
        try:
            return r.json()
        except Exception:
            return {"raw": r.text[:500]}


# =====================================================================
#  ENVIO (send) por canal
# =====================================================================
async def _send_whatsapp_cloud(config, to, text, media_url=None):
    if not _has(config, "phone_number_id", "access_token"):
        return _result(True, "whatsapp_cloud", external_id="stub-" + str(int(time.time())),
                       stub=True, error="sem credenciais - envio simulado")
    pnid = config["phone_number_id"]
    url = "https://graph.facebook.com/v19.0/" + str(pnid) + "/messages"
    headers = {"Authorization": "Bearer " + config["access_token"],
               "Content-Type": "application/json"}
    if media_url:
        body = {"messaging_product": "whatsapp", "to": to, "type": "image",
                "image": {"link": media_url, "caption": text or ""}}
    else:
        body = {"messaging_product": "whatsapp", "to": to, "type": "text",
                "text": {"body": text or ""}}
    try:
        data = await _post_json(url, body, headers)
        ext = None
        msgs = data.get("messages") if isinstance(data, dict) else None
        if isinstance(msgs, list) and msgs:
            ext = msgs[0].get("id")
        return _result(True, "whatsapp_cloud", external_id=ext, response=data)
    except Exception as e:
        logger.warning("[cloud] envio falhou: %s", e)
        return _result(False, "whatsapp_cloud", error=str(e))


async def _send_telegram(config, to, text, media_url=None):
    if not _has(config, "bot_token"):
        return _result(True, "telegram", external_id="stub-" + str(int(time.time())),
                       stub=True, error="sem token - envio simulado")
    token = config["bot_token"]
    if media_url:
        url = "https://api.telegram.org/bot" + token + "/sendPhoto"
        body = {"chat_id": to, "photo": media_url, "caption": text or ""}
    else:
        url = "https://api.telegram.org/bot" + token + "/sendMessage"
        body = {"chat_id": to, "text": text or ""}
    try:
        data = await _post_json(url, body)
        ext = None
        if isinstance(data, dict) and isinstance(data.get("result"), dict):
            ext = str(data["result"].get("message_id", ""))
        return _result(True, "telegram", external_id=ext, response=data)
    except Exception as e:
        logger.warning("[telegram] envio falhou: %s", e)
        return _result(False, "telegram", error=str(e))


async def _send_meta_dm(channel, config, to, text, media_url=None):
    """Instagram Direct / Messenger usam a mesma Graph Send API."""
    if not _has(config, "page_id", "access_token"):
        return _result(True, channel, external_id="stub-" + str(int(time.time())),
                       stub=True, error="sem credenciais - envio simulado")
    url = "https://graph.facebook.com/v19.0/" + str(config["page_id"]) + "/messages"
    params_token = config["access_token"]
    if media_url:
        message = {"attachment": {"type": "image",
                                  "payload": {"url": media_url, "is_reusable": False}}}
    else:
        message = {"text": text or ""}
    body = {"recipient": {"id": to}, "message": message,
            "messaging_type": "RESPONSE", "access_token": params_token}
    try:
        data = await _post_json(url, body)
        ext = data.get("message_id") if isinstance(data, dict) else None
        return _result(True, channel, external_id=ext, response=data)
    except Exception as e:
        logger.warning("[%s] envio falhou: %s", channel, e)
        return _result(False, channel, error=str(e))


async def _send_email(config, to, text, subject="Mensagem", media_url=None):
    """Envio de e-mail via SMTP (bloqueante -> roda em thread pelo chamador).
    Sem SMTP configurado, retorna stub."""
    if not _has(config, "smtp_host", "from_addr"):
        return _result(True, "email", external_id="stub-" + str(int(time.time())),
                       stub=True, error="sem SMTP - envio simulado")
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(text or "", "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = config["from_addr"]
        msg["To"] = to
        port = int(config.get("smtp_port", 587) or 587)
        server = smtplib.SMTP(config["smtp_host"], port, timeout=20)
        try:
            server.starttls()
        except Exception:
            pass
        if _has(config, "username", "password"):
            server.login(config["username"], config["password"])
        server.sendmail(config["from_addr"], [to], msg.as_string())
        server.quit()
        return _result(True, "email", external_id="smtp-" + str(int(time.time())))
    except Exception as e:
        logger.warning("[email] envio falhou: %s", e)
        return _result(False, "email", error=str(e))


async def send(channel, config, to, text, media_url=None, subject="Mensagem"):
    """Despachante de envio. Retorna o dict padronizado de _result.

    'webchat' nao envia por HTTP externo: a resposta e entregue quando o
    widget faz polling em /api/webchat/poll, entao aqui apenas confirmamos.
    """
    config = config or {}
    if channel == "whatsapp_cloud":
        return await _send_whatsapp_cloud(config, to, text, media_url)
    if channel == "telegram":
        return await _send_telegram(config, to, text, media_url)
    if channel in ("instagram", "messenger"):
        return await _send_meta_dm(channel, config, to, text, media_url)
    if channel == "email":
        return await _send_email(config, to, text, subject, media_url)
    if channel == "webchat":
        return _result(True, "webchat", external_id="web-" + str(int(time.time())))
    return _result(False, channel, error="canal nao suportado")


# =====================================================================
#  ENTRADA (parse_inbound) por canal
# =====================================================================
def _norm(channel, external_id, contact_ref, contact_name, text,
          media_url=None, media_type="text", timestamp=None, raw=None):
    return {
        "channel": channel,
        "external_id": str(external_id) if external_id is not None else None,
        "contact_ref": str(contact_ref) if contact_ref is not None else "",
        "contact_name": contact_name or "",
        "text": text or "",
        "media_url": media_url,
        "media_type": media_type,
        "timestamp": timestamp or int(time.time()),
        "raw": raw,
    }


def _parse_telegram(payload):
    msg = payload.get("message") or payload.get("edited_message") or {}
    if not msg:
        return None
    chat = msg.get("chat", {})
    frm = msg.get("from", {})
    name = (str(frm.get("first_name", "")) + " " + str(frm.get("last_name", ""))).strip()
    name = name or frm.get("username", "") or str(chat.get("id", ""))
    text = msg.get("text") or msg.get("caption") or ""
    media_type = "text"
    if msg.get("photo"):
        media_type = "image"
    elif msg.get("document"):
        media_type = "document"
    return _norm("telegram", msg.get("message_id"), chat.get("id"), name, text,
                 media_type=media_type, raw=payload)


def _parse_whatsapp_cloud(payload):
    try:
        entry = (payload.get("entry") or [])[0]
        change = (entry.get("changes") or [])[0]
        value = change.get("value", {})
        messages = value.get("messages") or []
        if not messages:
            return None
        m = messages[0]
        contacts = value.get("contacts") or []
        name = ""
        if contacts:
            name = contacts[0].get("profile", {}).get("name", "")
        text = ""
        media_type = "text"
        media_url = None
        if m.get("type") == "text":
            text = m.get("text", {}).get("body", "")
        elif m.get("type") == "image":
            media_type = "image"
            text = m.get("image", {}).get("caption", "")
            media_url = m.get("image", {}).get("id")
        return _norm("whatsapp_cloud", m.get("id"), m.get("from"), name, text,
                     media_url=media_url, media_type=media_type, raw=payload)
    except Exception as e:
        logger.warning("[cloud] parse falhou: %s", e)
        return None


def _parse_meta(channel, payload):
    try:
        entry = (payload.get("entry") or [])[0]
        messaging = (entry.get("messaging") or [])[0]
        sender = messaging.get("sender", {}).get("id")
        msg = messaging.get("message", {})
        if not msg or msg.get("is_echo"):
            return None
        text = msg.get("text", "")
        media_type = "text"
        media_url = None
        atts = msg.get("attachments") or []
        if atts:
            media_type = atts[0].get("type", "file")
            media_url = atts[0].get("payload", {}).get("url")
        return _norm(channel, msg.get("mid"), sender, "", text,
                     media_url=media_url, media_type=media_type, raw=payload)
    except Exception as e:
        logger.warning("[%s] parse falhou: %s", channel, e)
        return None


def _parse_webchat(payload):
    ref = payload.get("session_id") or payload.get("contact_ref") or ""
    if not ref:
        return None
    return _norm("webchat", payload.get("external_id") or ("web-" + str(int(time.time()))),
                 ref, payload.get("name", ""), payload.get("text", ""), raw=payload)


def parse_inbound(channel, payload):
    """Normaliza o payload de webhook do canal. Retorna dict unificado ou None."""
    if channel == "telegram":
        return _parse_telegram(payload)
    if channel == "whatsapp_cloud":
        return _parse_whatsapp_cloud(payload)
    if channel in ("instagram", "messenger"):
        return _parse_meta(channel, payload)
    if channel == "webchat":
        return _parse_webchat(payload)
    return None


# =====================================================================
#  Templates de campanha (HSM) e utilidades
# =====================================================================
def render_template(template, contact):
    """Renderiza um template de campanha com variaveis do contato.

    Suporta marcadores no formato [[nome]] (evita chaves duplas por robustez
    de gravacao). Ex.: 'Ola [[nome]]' -> 'Ola Maria'.
    """
    text = template or ""
    contact = contact or {}
    mapping = {
        "nome": contact.get("name") or contact.get("contact_name") or "cliente",
        "primeiro_nome": (str(contact.get("name") or "cliente").split() or ["cliente"])[0],
    }
    for k, v in mapping.items():
        text = text.replace("[[" + k + "]]", str(v))
    return text


def verify_meta_challenge(params, expected_token):
    """Responde ao handshake de verificacao (GET) da Meta/WhatsApp Cloud.
    Retorna o hub.challenge se o verify_token bater, senao None."""
    mode = params.get("hub.mode")
    token = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token and token == expected_token:
        return challenge
    return None
