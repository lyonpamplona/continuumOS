"""Fase 10 - Motor de IA conversacional do continnumOS.

Camada única de IA usada por copiloto, resumo, extração, sentimento, tradução
e RAG (base de conhecimento). Cada função tenta usar um provedor LLM
compatível com a API OpenAI (configurável por tenant) e, se não houver
provedor/conexão, cai para uma heurística offline determinística. Assim o
sistema é útil mesmo sem chave de IA e melhora automaticamente quando o
tenant configura um provedor.
"""
import json
import logging
import math
import re
import unicodedata

import httpx

logger = logging.getLogger("uvicorn.error")

_HTTP_TIMEOUT = 25.0

# --------------------------------------------------------------------- utils
def _strip_accents(s):
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _tokens(s):
    return re.findall(r"[a-z0-9]+", _strip_accents(s))


WEEKDAYS = None

# ----------------------------------------------------------- provider (LLM)
async def _chat(config, system_prompt, user_prompt, max_tokens=400, temperature=0.4):
    """Chama um endpoint OpenAI-compatível. Retorna texto ou None (fallback)."""
    if not config or not config.get("enabled"):
        return None
    api_key = (config.get("api_key") or "").strip()
    api_base = (config.get("api_base") or "https://api.openai.com/v1").strip().rstrip("/")
    model = (config.get("model") or "gpt-4o-mini").strip()
    if not api_key:
        return None
    url = api_base + "/chat/completions"
    headers = {"Authorization": "Bearer " + api_key, "Content-Type": "application/json"}
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    try:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            r = await client.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                logger.warning(f"[AI] provedor retornou {r.status_code}: {r.text[:200]}")
                return None
            data = r.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            return (choices[0].get("message", {}).get("content") or "").strip()
    except Exception as e:
        logger.warning(f"[AI] falha ao chamar provedor: {e}")
        return None


async def provider_health(config):
    """Testa a conexão com o provedor configurado."""
    if not config or not config.get("enabled"):
        return {"ok": False, "mode": "offline", "detail": "IA desativada (usando heurística)."}
    out = await _chat(config, "Voce e um verificador.", "Responda apenas: OK", max_tokens=5, temperature=0)
    if out is None:
        return {"ok": False, "mode": "offline", "detail": "Sem resposta do provedor; heurística ativa."}
    return {"ok": True, "mode": "llm", "detail": "Provedor respondeu: " + out[:40]}


# --------------------------------------------------------------- sentimento
_POS = set(_tokens(
    "otimo excelente perfeito bom boa gostei amei maravilhoso adorei satisfeito "
    "obrigado obrigada agradecido feliz top show recomendo rapido eficiente "
    "resolvido ajudou legal massa parabens sucesso positivo aprovado sim"
))
_NEG = set(_tokens(
    "pessimo ruim horrivel odiei detestei insatisfeito demora demorado lento "
    "caro problema reclamacao reclamar cancelar cancelamento raiva irritado "
    "chateado decepcionado nao errado erro falha pior nunca absurdo enganado "
    "golpe descaso pesimo demais atrasado quebrado defeito"
))
_INTENS = set(_tokens("muito super extremamente demais bastante totalmente completamente"))


def _heur_sentiment(text):
    toks = _tokens(text)
    if not toks:
        return {"label": "neutro", "score": 0.0}
    pos = sum(1 for t in toks if t in _POS)
    neg = sum(1 for t in toks if t in _NEG)
    boost = 1 + 0.3 * sum(1 for t in toks if t in _INTENS)
    raw = (pos - neg) * boost
    if "nao" in toks and pos > neg:
        raw -= pos
    score = max(-1.0, min(1.0, raw / max(3.0, len(toks) ** 0.5)))
    label = "positivo" if score > 0.15 else ("negativo" if score < -0.15 else "neutro")
    return {"label": label, "score": round(score, 3)}


async def analyze_sentiment(text, config=None):
    out = await _chat(
        config,
        "Voce classifica o sentimento de mensagens de clientes. Responda em JSON "
        "com as chaves label (positivo, neutro ou negativo) e score (-1 a 1).",
        "Mensagem: " + (text or "")[:1500],
        max_tokens=40, temperature=0,
    )
    if out:
        try:
            m = re.search(r"\{.*\}", out, re.S)
            if m:
                d = json.loads(m.group(0))
                lab = str(d.get("label", "neutro")).lower()
                if lab in ("positivo", "neutro", "negativo"):
                    return {"label": lab, "score": float(d.get("score", 0) or 0)}
        except Exception:
            pass
    return _heur_sentiment(text)


# ------------------------------------------------------------------ idioma
_LANG_HINTS = {
    "en": _tokens("the you and hello please thanks order how are what when where price"),
    "es": _tokens("el la usted hola gracias por favor pedido como donde cuando precio"),
    "pt": _tokens("voce ola obrigado por favor pedido como onde quando preco nao sim"),
}


def detect_language(text):
    toks = set(_tokens(text))
    if not toks:
        return "pt"
    best, best_score = "pt", 0
    for lang, words in _LANG_HINTS.items():
        sc = sum(1 for w in words if w in toks)
        if sc > best_score:
            best, best_score = lang, sc
    return best


# --------------------------------------------------------------- tradução
_MINI_DICT = {
    ("pt", "en"): {
        "ola": "hello", "obrigado": "thank you", "obrigada": "thank you",
        "bom dia": "good morning", "boa tarde": "good afternoon",
        "boa noite": "good evening", "pedido": "order", "preco": "price",
        "entrega": "delivery", "pagamento": "payment", "sim": "yes", "nao": "no",
    },
    ("pt", "es"): {
        "ola": "hola", "obrigado": "gracias", "obrigada": "gracias",
        "bom dia": "buenos dias", "boa tarde": "buenas tardes",
        "boa noite": "buenas noches", "pedido": "pedido", "preco": "precio",
        "entrega": "entrega", "pagamento": "pago", "sim": "si", "nao": "no",
    },
}


async def translate(text, target_lang="en", source_lang=None, config=None):
    text = text or ""
    if not text.strip():
        return {"text": "", "engine": "noop"}
    src = source_lang or detect_language(text)
    if src == target_lang:
        return {"text": text, "engine": "noop"}
    out = await _chat(
        config,
        "Voce e um tradutor profissional. Traduza mantendo o tom. Responda apenas a traducao.",
        "Traduza de " + src + " para " + target_lang + ":\n" + text[:2000],
        max_tokens=600, temperature=0.2,
    )
    if out:
        return {"text": out, "engine": "llm"}
    # Fallback offline: dicionário mínimo palavra a palavra (limitado).
    d = _MINI_DICT.get((src, target_lang))
    if not d:
        return {"text": text, "engine": "passthrough",
                "note": "Traducao automatica requer provedor de IA configurado."}
    low = text.lower()
    for k, v in sorted(d.items(), key=lambda kv: -len(kv[0])):
        low = re.sub(r"\b" + re.escape(k) + r"\b", v, low)
    return {"text": low, "engine": "dict",
            "note": "Traducao aproximada (offline). Configure um provedor de IA para traducao completa."}


# --------------------------------------------------- resumo + extração
def _format_transcript(messages, limit=40):
    lines = []
    for m in messages[-limit:]:
        who = m.get("sender", "")
        role = "Cliente" if who and who == m.get("phone") else ("Bot" if who == "bot" else "Atendente")
        txt = (m.get("msg_text") or "").strip()
        if txt and not txt.startswith("["):
            lines.append(role + ": " + txt)
    return "\n".join(lines)


def _heur_summary(messages):
    texts = [(m.get("msg_text") or "").strip() for m in messages if (m.get("msg_text") or "").strip()]
    texts = [t for t in texts if not t.startswith("[")]
    if not texts:
        return "Conversa sem mensagens de texto para resumir."
    total = len(texts)
    first = texts[0][:160]
    last = texts[-1][:160]
    return ("Conversa com " + str(total) + " mensagens. "
            "Inicio: \"" + first + "\". Ultima: \"" + last + "\".")


async def summarize(messages, config=None):
    transcript = _format_transcript(messages)
    if not transcript:
        return _heur_summary(messages)
    out = await _chat(
        config,
        "Voce resume conversas de atendimento em portugues, em ate 4 linhas objetivas, "
        "destacando o pedido do cliente, o status e o proximo passo.",
        "Resuma a conversa:\n" + transcript[:4000],
        max_tokens=250, temperature=0.3,
    )
    return out or _heur_summary(messages)


_ORDER_RE = re.compile(r"(?:pedido|order|nota|of|nf|protocolo)[\s:#no\.]*([0-9]{3,})", re.I)
_MONEY_RE = re.compile(r"r\$\s?([0-9][0-9\.\,]*)", re.I)
_EMAIL_RE = re.compile(r"[a-z0-9\.\_\%\+\-]+@[a-z0-9\.\-]+\.[a-z]{2,}", re.I)
_CPF_RE = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}\-?\d{2}\b")
_OBJ_WORDS = _tokens("caro preco desconto concorrente pensar depois duvida receio prazo frete garantia")


def _heur_extract(messages, contact=None):
    joined = " ".join((m.get("msg_text") or "") for m in messages)
    order = None
    mo = _ORDER_RE.search(joined)
    if mo:
        order = mo.group(1)
    money = None
    mm = _MONEY_RE.search(joined)
    if mm:
        money = "R$ " + mm.group(1)
    email = None
    me = _EMAIL_RE.search(joined)
    if me:
        email = me.group(0)
    cpf = None
    mc = _CPF_RE.search(joined)
    if mc:
        cpf = mc.group(0)
    toks = set(_tokens(joined))
    objection = ", ".join(sorted(w for w in _OBJ_WORDS if w in toks)) or None
    name = (contact or {}).get("name") or None
    return {"name": name, "order": order, "amount": money, "email": email,
            "cpf": cpf, "objection": objection}


async def extract_data(messages, contact=None, config=None):
    transcript = _format_transcript(messages)
    if transcript:
        out = await _chat(
            config,
            "Extraia dados estruturados da conversa. Responda em JSON com as chaves: "
            "name, order, amount, email, cpf, objection. Use null quando nao houver.",
            transcript[:4000],
            max_tokens=200, temperature=0,
        )
        if out:
            try:
                m = re.search(r"\{.*\}", out, re.S)
                if m:
                    d = json.loads(m.group(0))
                    base = _heur_extract(messages, contact)
                    for k in list(base.keys()):
                        v = d.get(k)
                        if v not in (None, "", "null"):
                            base[k] = v
                    return base
            except Exception:
                pass
    return _heur_extract(messages, contact)


# ------------------------------------------------ copiloto (sugerir/tom)
def _heur_suggest(messages, contact):
    last_client = ""
    for m in reversed(messages):
        if (m.get("sender") == m.get("phone")) and (m.get("msg_text") or "").strip():
            last_client = m["msg_text"].strip()
            break
    name = ((contact or {}).get("name") or "").split(" ")[0]
    hi = ("Ola " + name if name else "Ola") + "! "
    low = _strip_accents(last_client)
    if any(w in low for w in ["preco", "valor", "quanto", "orcamento"]):
        return hi + "Posso te passar os valores agora. Sobre qual produto/servico voce quer o orcamento?"
    if any(w in low for w in ["entrega", "prazo", "frete"]):
        return hi + "O prazo de entrega depende do seu CEP. Pode me informar para eu calcular certinho?"
    if any(w in low for w in ["pagamento", "boleto", "pix", "cartao", "parcel"]):
        return hi + "Trabalhamos com Pix, boleto e cartao (com parcelamento). Qual forma prefere?"
    if any(w in low for w in ["reclama", "problema", "defeito", "errado", "ruim"]):
        return hi + "Sinto muito pelo transtorno. Vou verificar isso com prioridade e ja te retorno com uma solucao."
    if any(w in low for w in ["obrigad", "valeu", "otimo", "perfeito"]):
        return "Fico feliz em ajudar! Precisando de mais alguma coisa, estou por aqui. "
    return hi + "Recebi sua mensagem e ja estou verificando. Pode me dar mais detalhes para eu te ajudar melhor?"


async def suggest_reply(messages, contact=None, config=None, extra=""):
    transcript = _format_transcript(messages)
    if transcript:
        prompt = "Sugira UMA resposta curta, cordial e objetiva que o atendente pode enviar agora."
        if extra:
            prompt += " Contexto extra: " + extra
        out = await _chat(
            config,
            "Voce e um copiloto de vendas por WhatsApp. Escreva em portugues do Brasil, "
            "tom profissional e proximo, sem inventar dados que nao estao na conversa.",
            prompt + "\n\nConversa:\n" + transcript[:3500],
            max_tokens=200, temperature=0.6,
        )
        if out:
            return {"text": out, "engine": "llm"}
    return {"text": _heur_suggest(messages, contact), "engine": "heuristic"}


def _heur_tone(text, tone):
    t = (text or "").strip()
    if not t:
        return t
    if tone == "formal":
        t = re.sub(r"\bvc\b", "voce", t, flags=re.I)
        t = re.sub(r"\bpq\b", "porque", t, flags=re.I)
        t = re.sub(r"\bvlw\b", "obrigado", t, flags=re.I)
        if not t[0].isupper():
            t = t[0].upper() + t[1:]
        if t[-1] not in ".!?":
            t += "."
    elif tone == "amigavel":
        if not re.search(r"[!😊]", t):
            t = t.rstrip(".") + "! "
    elif tone == "conciso":
        t = re.sub(r"\s+", " ", t).strip()
        t = t.split(". ")[0].strip()
        if t and t[-1] not in ".!?":
            t += "."
    return t


async def correct_tone(text, tone="formal", config=None):
    out = await _chat(
        config,
        "Voce reescreve mensagens de atendimento ajustando o tom pedido "
        "(formal, amigavel ou conciso), mantendo o significado. Responda apenas a mensagem.",
        "Tom desejado: " + tone + "\nMensagem: " + (text or "")[:1500],
        max_tokens=250, temperature=0.4,
    )
    if out:
        return {"text": out, "engine": "llm"}
    return {"text": _heur_tone(text, tone), "engine": "heuristic"}


# --------------------------------------------------------- RAG (base conhec.)
def rank_chunks(question, chunks, top_k=4):
    """Ranqueia trechos por TF-IDF simples (offline, sem dependências)."""
    q = set(_tokens(question))
    if not q or not chunks:
        return []
    docs = [set(_tokens(c.get("content", ""))) for c in chunks]
    n = len(docs)
    df = {}
    for d in docs:
        for term in d:
            df[term] = df.get(term, 0) + 1
    scored = []
    for i, d in enumerate(docs):
        score = 0.0
        for term in q:
            if term in d:
                idf = math.log((n + 1) / (1 + df.get(term, 0))) + 1
                score += idf
        if score > 0:
            scored.append((score, chunks[i]))
    scored.sort(key=lambda x: -x[0])
    return [c for _s, c in scored[:top_k]]


async def answer_from_kb(question, chunks, config=None):
    top = rank_chunks(question, chunks)
    if not top:
        return {"text": "", "found": False, "sources": []}
    context = "\n\n".join("[" + str(i + 1) + "] " + (c.get("content") or "")[:800] for i, c in enumerate(top))
    sources = list({c.get("title", "") for c in top if c.get("title")})
    out = await _chat(
        config,
        "Voce responde perguntas usando SOMENTE o contexto fornecido da base de "
        "conhecimento do cliente. Se a resposta nao estiver no contexto, diga que nao "
        "encontrou. Responda em portugues, citando os numeros [n] das fontes usadas.",
        "Pergunta: " + question + "\n\nContexto:\n" + context,
        max_tokens=350, temperature=0.2,
    )
    if out:
        return {"text": out, "found": True, "sources": sources, "engine": "llm"}
    # Fallback offline: devolve os melhores trechos como resposta extrativa.
    extract = top[0].get("content", "")[:600]
    return {"text": extract, "found": True, "sources": sources, "engine": "extractive"}


def chunk_text(text, size=900, overlap=150):
    """Divide um documento em trechos sobrepostos para indexação RAG."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks
