"""Fase 12 - Vendas & CRM avancado: logica pura de vendas.

Funcoes sem acesso a banco/rede (testaveis e deterministicas):
  * cart_totals(...)      -> subtotal, desconto e total de um carrinho.
  * commission_for(...)   -> comissao de uma venda conforme regras.
  * next_cadence_step(...) -> proximo passo devido de uma cadencia.
  * deal_health(...)      -> saude/aging de um negocio no pipeline.
  * goal_progress(...)    -> progresso de uma meta (0..1) e status.
"""

import time

# Estagios padrao de um pipeline de vendas (Kanban).
DEFAULT_STAGES = [
    {"id": "lead", "name": "Lead", "color": "#9a9aa0"},
    {"id": "qualificado", "name": "Qualificado", "color": "#2aabee"},
    {"id": "proposta", "name": "Proposta", "color": "#ffb020"},
    {"id": "negociacao", "name": "Negociacao", "color": "#ff6a00"},
    {"id": "ganho", "name": "Ganho", "color": "#3ddc84"},
    {"id": "perdido", "name": "Perdido", "color": "#ff4d4d"},
]

# Probabilidade default por estagio (para previsao ponderada de receita).
STAGE_PROBABILITY = {
    "lead": 10, "qualificado": 30, "proposta": 55,
    "negociacao": 75, "ganho": 100, "perdido": 0,
}


def _num(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return float(default)


def cart_totals(items, discount=0.0, discount_type="value"):
    """Calcula os totais de um carrinho.

    items: lista de dicts com 'price' e 'qty'.
    discount_type: 'value' (R$ fixo) ou 'percent' (% sobre o subtotal).
    Retorna dict com subtotal, discount, total e quantidade de itens.
    """
    subtotal = 0.0
    count = 0
    for it in (items or []):
        price = _num(it.get("price"))
        qty = _num(it.get("qty"), 1)
        subtotal += price * qty
        count += int(qty)
    disc = _num(discount)
    if discount_type == "percent":
        disc = subtotal * (disc / 100.0)
    disc = max(0.0, min(disc, subtotal))
    total = round(subtotal - disc, 2)
    return {
        "subtotal": round(subtotal, 2),
        "discount": round(disc, 2),
        "total": total,
        "item_count": count,
    }


def commission_for(amount, rules, owner_rate=None):
    """Calcula a comissao de uma venda.

    rules: lista de dicts {min_value, rate} (rate em %). Usa a maior faixa
    cujo min_value <= amount. owner_rate (se informado) sobrepoe as faixas.
    Retorna dict com rate aplicada e valor da comissao.
    """
    amount = _num(amount)
    rate = _num(owner_rate) if owner_rate is not None else 0.0
    if owner_rate is None:
        best_min = -1.0
        for r in (rules or []):
            if not r.get("active", True):
                continue
            mv = _num(r.get("min_value"))
            if amount >= mv and mv >= best_min:
                best_min = mv
                rate = _num(r.get("rate"))
    value = round(amount * (rate / 100.0), 2)
    return {"rate": rate, "commission": value, "amount": round(amount, 2)}


def next_cadence_step(steps, step_idx, last_run_ts, now_ts=None):
    """Decide se o proximo passo de uma cadencia esta devido.

    steps: lista de dicts {delay_hours, channel, template}.
    step_idx: indice do proximo passo a executar (0-based).
    last_run_ts: epoch do ultimo passo executado (0 se nunca).
    Retorna (due: bool, step: dict|None, finished: bool).
    """
    now = now_ts if now_ts is not None else int(time.time())
    steps = steps or []
    if step_idx >= len(steps):
        return False, None, True
    step = steps[step_idx]
    delay_h = _num(step.get("delay_hours"), 0)
    base = last_run_ts or 0
    due_at = base + int(delay_h * 3600)
    if now >= due_at:
        return True, step, False
    return False, step, False


def deal_health(deal, now_ts=None):
    """Avalia a saude de um negocio: dias parado e sinal (ok/atencao/risco)."""
    now = now_ts if now_ts is not None else int(time.time())
    updated = _num(deal.get("updated_ts"), now)
    days_idle = max(0, int((now - updated) / 86400))
    if days_idle >= 14:
        signal = "risco"
    elif days_idle >= 7:
        signal = "atencao"
    else:
        signal = "ok"
    return {"days_idle": days_idle, "signal": signal}


def weighted_forecast(deals):
    """Previsao ponderada de receita: soma(value * probabilidade/100) de abertos."""
    total = 0.0
    for d in (deals or []):
        if d.get("status") not in ("open", None, ""):
            continue
        prob = d.get("probability")
        if prob is None:
            prob = STAGE_PROBABILITY.get(d.get("stage", ""), 0)
        total += _num(d.get("value")) * (_num(prob) / 100.0)
    return round(total, 2)


def goal_progress(target, achieved):
    """Progresso de uma meta. Retorna ratio (0..1+), pct e status."""
    target = _num(target)
    achieved = _num(achieved)
    if target <= 0:
        return {"ratio": 0.0, "pct": 0, "status": "sem meta"}
    ratio = achieved / target
    pct = int(round(ratio * 100))
    if ratio >= 1.0:
        status = "batida"
    elif ratio >= 0.7:
        status = "no caminho"
    else:
        status = "abaixo"
    return {"ratio": round(ratio, 3), "pct": pct, "status": status}


def validate_cadence(steps):
    """Confere uma cadencia antes de salvar. Retorna lista de avisos."""
    warns = []
    if not steps:
        warns.append("cadencia sem passos")
        return warns
    for i, s in enumerate(steps):
        if not str(s.get("template", "")).strip():
            warns.append("passo " + str(i + 1) + " sem mensagem")
        if _num(s.get("delay_hours"), -1) < 0:
            warns.append("passo " + str(i + 1) + " com atraso invalido")
    return warns


if __name__ == "__main__":
    # autoteste rapido (sem dependencias externas)
    t = cart_totals([{"price": 100, "qty": 2}, {"price": 50, "qty": 1}], 10, "percent")
    assert t["subtotal"] == 250.0 and t["total"] == 225.0, t
    c = commission_for(1000, [{"min_value": 0, "rate": 3, "active": True},
                              {"min_value": 500, "rate": 5, "active": True}])
    assert c["rate"] == 5 and c["commission"] == 50.0, c
    due, step, fin = next_cadence_step([{"delay_hours": 0, "template": "oi"}], 0, 0)
    assert due and not fin, (due, fin)
    g = goal_progress(1000, 750)
    assert g["status"] == "no caminho" and g["pct"] == 75, g
    print("sales_engine OK")
