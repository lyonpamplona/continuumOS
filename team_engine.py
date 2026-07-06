"""Fase 13 - Produtividade da equipe: logica pura.

Sem acesso a banco/rede. Funcoes deterministicas e testaveis:
  * sla_state(...)        -> estado de SLA de um ticket (ok/risco/violado).
  * gamification_level(...) -> nivel e progresso a partir de pontos.
  * award_badges(...)     -> conquistas com base em metricas agregadas.
  * qa_score(...)         -> nota ponderada de um formulario de QA.
  * effective_status(...) -> status efetivo do atendente (escala + manual).
  * pick_agent(...)       -> escolhe atendente menos ocupado (round-robin).
"""

import time

# Estados de SLA e limiar de "risco" (fracao do prazo ja consumida).
SLA_WARNING_RATIO = 0.75

# Faixas de nivel de gamificacao (pontos minimos -> nome).
LEVELS = [
    (0, "Bronze I"), (100, "Bronze II"), (300, "Prata I"), (600, "Prata II"),
    (1000, "Ouro I"), (1800, "Ouro II"), (3000, "Platina"), (5000, "Diamante"),
]

# Pontuacao padrao por tipo de acao (usada quando nao ha regra no banco).
DEFAULT_POINTS = {
    "conversa_resolvida": 10,
    "primeira_resposta_rapida": 5,
    "csat_positivo": 15,
    "venda": 30,
    "sla_cumprido": 8,
    "sla_violado": -10,
}

# Catalogo de conquistas: id -> (nome, metrica, alvo).
BADGES = [
    ("primeiros_passos", "Primeiros passos", "resolved", 1),
    ("maratonista", "Maratonista", "resolved", 100),
    ("velocista", "Velocista", "fast_responses", 50),
    ("querido", "Querido pelos clientes", "csat_positive", 25),
    ("campeao_vendas", "Campeao de vendas", "sales", 20),
    ("impecavel", "Atendimento impecavel", "qa_avg", 90),
]


def _num(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return float(default)


def sla_state(created_ts, target_minutes, done_ts=None, now_ts=None):
    """Calcula o estado de SLA de um marco (primeira resposta ou resolucao).

    created_ts: epoch de abertura. target_minutes: prazo em minutos.
    done_ts: epoch de conclusao (se ja concluido). Retorna dict com
    state (ok|risco|violado|cumprido), remaining_seconds e elapsed_seconds.
    """
    now = now_ts if now_ts is not None else int(time.time())
    created = int(created_ts or now)
    target_s = int(_num(target_minutes) * 60)
    deadline = created + target_s
    if done_ts:
        done = int(done_ts)
        state = "cumprido" if done <= deadline else "violado"
        return {"state": state, "elapsed_seconds": max(0, done - created),
                "remaining_seconds": deadline - done, "deadline": deadline}
    remaining = deadline - now
    elapsed = now - created
    if target_s <= 0:
        ratio = 0.0
    else:
        ratio = elapsed / float(target_s)
    if remaining < 0:
        state = "violado"
    elif ratio >= SLA_WARNING_RATIO:
        state = "risco"
    else:
        state = "ok"
    return {"state": state, "elapsed_seconds": max(0, elapsed),
            "remaining_seconds": remaining, "deadline": deadline}


def points_for(action, rules=None):
    """Pontos de uma acao. rules: dict {action: points} sobrepoe o padrao."""
    if rules and action in rules:
        return int(_num(rules[action]))
    return int(DEFAULT_POINTS.get(action, 0))


def gamification_level(points):
    """Nivel atual, proximo nivel e progresso (0..1) a partir dos pontos."""
    points = int(_num(points))
    current = LEVELS[0]
    nxt = None
    for i, lv in enumerate(LEVELS):
        if points >= lv[0]:
            current = lv
            nxt = LEVELS[i + 1] if i + 1 < len(LEVELS) else None
    if nxt is None:
        return {"level": current[1], "points": points, "next": None, "progress": 1.0, "to_next": 0}
    span = nxt[0] - current[0]
    prog = (points - current[0]) / float(span) if span > 0 else 1.0
    return {"level": current[1], "points": points, "next": nxt[1],
            "progress": round(prog, 3), "to_next": nxt[0] - points}


def award_badges(metrics):
    """Retorna a lista de conquistas alcancadas dado um dict de metricas."""
    earned = []
    for bid, name, metric, target in BADGES:
        if _num(metrics.get(metric)) >= target:
            earned.append({"id": bid, "name": name, "metric": metric, "target": target})
    return earned


def qa_score(criteria, answers):
    """Nota ponderada de um formulario de QA.

    criteria: lista de dicts {id, label, weight, max}.
    answers: dict {criterion_id: valor}.
    Retorna dict com score (0..100), obtained, total e breakdown.
    """
    total_w = 0.0
    obtained = 0.0
    breakdown = []
    for c in (criteria or []):
        w = _num(c.get("weight"), 1)
        mx = _num(c.get("max"), 5)
        val = _num((answers or {}).get(c.get("id")), 0)
        val = max(0.0, min(val, mx))
        pct = (val / mx) if mx > 0 else 0.0
        obtained += pct * w
        total_w += w
        breakdown.append({"id": c.get("id"), "label": c.get("label"), "value": val, "max": mx, "weight": w})
    score = int(round((obtained / total_w) * 100)) if total_w > 0 else 0
    return {"score": score, "obtained": round(obtained, 2), "total_weight": round(total_w, 2), "breakdown": breakdown}


def _parse_hhmm(s):
    try:
        h, m = str(s).split(":")
        return int(h) * 60 + int(m)
    except Exception:
        return None


def effective_status(manual_status, shifts, now_dt=None, heartbeat_ts=None, now_ts=None):
    """Determina o status efetivo do atendente.

    manual_status: status definido manualmente (online/ausente/offline/pausa).
    shifts: lista de dicts {weekday(0=seg..6=dom), start, end} em HH:MM.
    heartbeat_ts: ultimo sinal de presenca (epoch). Sem heartbeat recente
    (>150s), o atendente e considerado offline mesmo se marcado online.
    Retorna dict com status e in_shift.
    """
    import datetime as _dt
    now = now_dt or _dt.datetime.now()
    now_epoch = now_ts if now_ts is not None else int(time.time())
    weekday = now.weekday()
    minutes = now.hour * 60 + now.minute
    in_shift = False
    for sh in (shifts or []):
        if int(sh.get("weekday", -1)) != weekday:
            continue
        st = _parse_hhmm(sh.get("start"))
        en = _parse_hhmm(sh.get("end"))
        if st is None or en is None:
            continue
        if st <= minutes <= en:
            in_shift = True
            break
    status = manual_status or "offline"
    if heartbeat_ts is not None and (now_epoch - int(heartbeat_ts)) > 150:
        status = "offline"
    if status == "online" and shifts and not in_shift:
        status = "fora_de_escala"
    return {"status": status, "in_shift": in_shift}


def pick_agent(agents):
    """Escolhe o atendente online com menos conversas ativas (balanceamento).

    agents: lista de dicts {name, status, active_load}. Retorna nome ou None.
    """
    online = [a for a in (agents or []) if a.get("status") == "online"]
    if not online:
        return None
    online.sort(key=lambda a: _num(a.get("active_load"), 0))
    return online[0].get("name")


if __name__ == "__main__":
    s = sla_state(1000, 5, now_ts=1000 + 60)
    assert s["state"] == "ok", s
    s = sla_state(1000, 5, now_ts=1000 + 240)
    assert s["state"] == "risco", s
    s = sla_state(1000, 5, now_ts=1000 + 400)
    assert s["state"] == "violado", s
    lv = gamification_level(700)
    assert lv["level"] == "Prata II" and lv["next"] == "Ouro I", lv
    q = qa_score([{"id": "a", "weight": 2, "max": 5}, {"id": "b", "weight": 1, "max": 5}], {"a": 5, "b": 0})
    assert q["score"] == 67, q
    b = award_badges({"resolved": 120, "sales": 5})
    assert any(x["id"] == "maratonista" for x in b), b
    print("team_engine OK")
