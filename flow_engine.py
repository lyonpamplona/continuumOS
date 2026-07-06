"""Fase 10 - Motor de fluxos no-code (chatbot visual) do continnumOS.

Executa uma definição de fluxo (JSON, montada pelo construtor visual) como uma
máquina de estados por contato. É puro (sem I/O): recebe o fluxo, o estado atual
e o texto do cliente, e devolve o novo estado + uma lista de ações para o main.py
executar (enviar mensagem, aplicar tag, coletar variavel, transferir, encerrar).

Esquema do fluxo:
{
  "start": "n1",
  "nodes": {
    "n1": {"type": "message", "text": "Ola {nome}!", "next": "n2"},
    "n2": {"type": "menu", "text": "1) Vendas 2) Suporte",
            "options": [{"match": ["1","vendas"], "next": "n3"},
                        {"match": ["2","suporte"], "next": "n4"}],
            "fallback": "n2", "max_retries": 2, "on_fail": "n_handoff"},
    "n3": {"type": "collect", "text": "Qual seu nome?", "var": "nome", "next": "n5"},
    "n5": {"type": "condition", "var": "nome", "op": "contains",
            "value": "joao", "then": "n6", "else": "n7"},
    "n6": {"type": "business_hours", "open": "n8", "closed": "n9"},
    "n_handoff": {"type": "handoff", "target": "vendas", "text": "Um momento..."},
    "n_tag": {"type": "set_tag", "tag": "lead-quente", "next": "n_end"},
    "n_end": {"type": "end"}
  }
}
"""
import re
import unicodedata

MAX_STEPS = 25  # trava anti-loop por execução


def _norm(s):
    if not s:
        return ""
    nfkd = unicodedata.normalize("NFKD", str(s))
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower().strip()


def render_vars(text, variables):
    """Substitui {chave} pelos valores coletados (sem depender de f-strings)."""
    if not text:
        return ""
    out = str(text)
    for k, v in (variables or {}).items():
        out = out.replace("{" + str(k) + "}", str(v))
    return out


def _match_option(text, options):
    low = _norm(text)
    for idx, opt in enumerate(options or []):
        for m in opt.get("match", []):
            mm = _norm(m)
            if mm and (low == mm or mm in low):
                return opt
        # aceita o número da opção (1-based) como atalho implícito
        if low == str(idx + 1):
            return opt
    return None


def _check_condition(node, variables, text):
    op = node.get("op", "contains")
    var = node.get("var")
    value = _norm(node.get("value", ""))
    subject = _norm(variables.get(var, "")) if var else _norm(text)
    if op == "contains":
        return value in subject
    if op == "equals":
        return subject == value
    if op == "not_empty":
        return bool(subject)
    if op == "is_empty":
        return not subject
    if op == "regex":
        try:
            return bool(re.search(node.get("value", ""), subject))
        except re.error:
            return False
    return False


def new_state(flow_id):
    return {"flow_id": flow_id, "node": None, "vars": {}, "retries": {}, "done": False}


def run(flow, state, text, ctx=None):
    """Avança o fluxo. Retorna (novo_estado, acoes).

    ctx: dict opcional com contexto (ex.: {"is_business_hours": bool, "contact": {...}}).
    acoes: lista de dicts {type: send|tag|handoff|collect_done|end, ...}.
    """
    ctx = ctx or {}
    actions = []
    nodes = (flow or {}).get("nodes", {})
    if not nodes:
        return state, actions

    state = dict(state or {})
    state.setdefault("vars", {})
    state.setdefault("retries", {})
    variables = dict(state["vars"])
    if ctx.get("contact"):
        # disponibiliza nome do contato como variavel padrao
        variables.setdefault("nome", (ctx["contact"].get("name") or ""))

    current = state.get("node")

    # 1) Se estamos aguardando input (menu/collect), processa a resposta.
    if current and current in nodes:
        node = nodes[current]
        ntype = node.get("type")
        if ntype == "menu":
            opt = _match_option(text, node.get("options"))
            if opt is None:
                rk = current
                state["retries"][rk] = state["retries"].get(rk, 0) + 1
                if state["retries"][rk] > int(node.get("max_retries", 2)):
                    nxt = node.get("on_fail") or node.get("fallback")
                    current = nxt
                else:
                    fb = node.get("fallback")
                    if fb and fb in nodes and fb != current:
                        current = fb
                    else:
                        actions.append({"type": "send", "text": render_vars(node.get("retry_text") or node.get("text", ""), variables)})
                        state["vars"] = variables
                        return state, actions
            else:
                state["retries"][current] = 0
                current = opt.get("next")
        elif ntype == "collect":
            variables[node.get("var", "resposta")] = (text or "").strip()
            actions.append({"type": "collect_done", "var": node.get("var"), "value": (text or "").strip()})
            current = node.get("next")
    elif not current:
        current = flow.get("start")

    # 2) Avança por nós automaticos ate parar em um nó de espera ou terminar.
    steps = 0
    while current and current in nodes and steps < MAX_STEPS:
        steps += 1
        node = nodes[current]
        ntype = node.get("type")
        if ntype == "message":
            actions.append({"type": "send", "text": render_vars(node.get("text", ""), variables)})
            current = node.get("next")
            continue
        if ntype == "set_tag":
            actions.append({"type": "tag", "tag": node.get("tag", "")})
            current = node.get("next")
            continue
        if ntype == "condition":
            current = node.get("then") if _check_condition(node, variables, text) else node.get("else")
            continue
        if ntype == "business_hours":
            current = node.get("open") if ctx.get("is_business_hours", True) else node.get("closed")
            continue
        if ntype == "menu":
            actions.append({"type": "send", "text": render_vars(node.get("text", ""), variables)})
            state["node"] = current
            state["vars"] = variables
            return state, actions
        if ntype == "collect":
            actions.append({"type": "send", "text": render_vars(node.get("text", ""), variables)})
            state["node"] = current
            state["vars"] = variables
            return state, actions
        if ntype == "handoff":
            if node.get("text"):
                actions.append({"type": "send", "text": render_vars(node.get("text", ""), variables)})
            actions.append({"type": "handoff", "target": node.get("target", ""),
                            "skill": node.get("skill", ""), "vars": variables})
            state["done"] = True
            state["node"] = None
            state["vars"] = variables
            return state, actions
        if ntype == "end":
            actions.append({"type": "end", "vars": variables})
            state["done"] = True
            state["node"] = None
            state["vars"] = variables
            return state, actions
        # tipo desconhecido: encerra por segurança
        break

    state["node"] = current if (current in nodes) else None
    state["vars"] = variables
    if not state["node"]:
        state["done"] = True
    return state, actions


def validate_flow(flow):
    """Valida a estrutura do fluxo. Retorna lista de erros (vazia = ok)."""
    errors = []
    if not isinstance(flow, dict):
        return ["Fluxo deve ser um objeto JSON."]
    nodes = flow.get("nodes")
    if not isinstance(nodes, dict) or not nodes:
        return ["Fluxo precisa de ao menos um no em 'nodes'."]
    start = flow.get("start")
    if start not in nodes:
        errors.append("'start' deve apontar para um no existente.")
    valid_types = {"message", "menu", "condition", "collect", "business_hours", "set_tag", "handoff", "end"}

    def ref(nid, label):
        if nid and nid not in nodes:
            errors.append("No '" + str(label) + "' aponta para no inexistente: " + str(nid))

    for nid, node in nodes.items():
        t = node.get("type")
        if t not in valid_types:
            errors.append("No '" + str(nid) + "' tem tipo invalido: " + str(t))
            continue
        if t in ("message", "collect", "set_tag"):
            ref(node.get("next"), nid)
        if t == "collect" and not node.get("var"):
            errors.append("No collect '" + str(nid) + "' precisa de 'var'.")
        if t == "menu":
            for opt in node.get("options", []):
                ref(opt.get("next"), nid)
            ref(node.get("fallback"), nid)
            ref(node.get("on_fail"), nid)
        if t == "condition":
            ref(node.get("then"), nid)
            ref(node.get("else"), nid)
        if t == "business_hours":
            ref(node.get("open"), nid)
            ref(node.get("closed"), nid)
    return errors
