"""
Modelo del Radar Electoral. Todo es transparente y configurable en config/.

Pasos:
 1. Normalizar cada encuesta a un margen (candidato no republicano - republicano).
 2. Ponderar: calidad de la encuestadora, tamaño de muestra, antigüedad,
    tipo de población (votantes probables > registrados > adultos),
    patrocinio partidista (vale la mitad) y cuántas veces repite la firma.
 3. Corregir el "efecto casa": cuánto se desvía cada firma, en promedio,
    del consenso en todas las carreras que encuestó. Se resta ese sesgo.
 4. Mezclar el promedio de encuestas con una base estructural
    (cómo votó el estado en 2024 + el ambiente nacional del voto genérico).
    Con muchas encuestas manda el promedio; sin encuestas manda la base.
 5. Simular 20.000 elecciones con un error nacional compartido (si las
    encuestas fallan, suelen fallar todas hacia el mismo lado).
"""
import datetime as dt
import math
import random
import statistics

NON_R_PARTIES = ("D", "I")


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_ppf(p):
    p = min(max(p, 1e-6), 1 - 1e-6)
    lo, hi = -10.0, 10.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if norm_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ------------------------------------------------------------ calidad
def pollster_quality(name, ratings, pr=None):
    """Devuelve (etiqueta, peso, info). Primero datos de precisión histórica, luego lista manual."""
    hit = pr.lookup(name) if pr else None
    if hit and hit.get("quality"):
        label = f"{hit['origin']} {hit['grade_label']}"
        return label, hit["quality"], hit
    n = (name or "").lower()
    tiers = ratings["tiers"]
    lists = ratings["pollsters"]
    for tier in ("excluida", "B", "C", "A"):  # lo específico antes que "university"/"college"
        for key in lists.get(tier, []):
            if key in n:
                return f"Manual {tier}", tiers[tier], hit
    return "Sin calificación", tiers["desconocida"], hit


# ------------------------------------------------------------ partidos
def party_of(choice, cands):
    c = (choice or "").strip().lower()
    if c in ("dem", "democrat", "democratic", "democrats", "d"):
        return "D"
    if c in ("rep", "republican", "republicans", "gop", "r"):
        return "R"
    # nombre completo primero, apellido después (solo si no es ambiguo)
    for party, name in (cands or {}).items():
        if name and name.lower() == c:
            return party
    hits = []
    for party, name in (cands or {}).items():
        if not name:
            continue
        last = name.split()[-1].lower()
        if last in c.split() or last == c:
            hits.append(party)
    return hits[0] if len(hits) == 1 else None


def poll_margin(poll, cands, warnings):
    """Margen desde el lado no republicano (D, o I si no hay D). None si no se puede."""
    if "margin" in poll and poll["margin"] is not None:
        return float(poll["margin"]), "D"
    by_party = {}
    for a in poll.get("answers", []):
        party = a.get("party")
        party = {"DEM": "D", "REP": "R", "IND": "I"}.get(str(party).upper(), party) if party else None
        party = party or party_of(a.get("choice"), cands)
        if party is None:
            if a.get("choice") and a.get("pct", 0) and a["pct"] >= 10:
                warnings.add(f"Nombre sin partido asignado: '{a['choice']}' ({poll.get('race')})")
            continue
        if a.get("pct") is None:
            continue
        by_party[party] = max(by_party.get(party, 0), float(a["pct"]))
    if "R" not in by_party:
        return None, None
    non_r = [k for k in ("D", "I") if k in by_party]
    if not non_r:
        return None, None
    side = max(non_r, key=lambda k: by_party[k])
    return by_party[side] - by_party["R"], side


# ------------------------------------------------------------ ponderación
def weight_poll(p, cfg, ratings, today, half_life, pr=None):
    pc = cfg["polls"]
    try:
        end = dt.date.fromisoformat(p["end_date"][:10])
    except Exception:  # noqa: BLE001
        return 0, {}
    age = (today - end).days
    if age < 0 or age > pc["max_age_days"]:
        return 0, {}
    if p.get("internal") and pc["exclude_internal"]:
        return 0, {}
    tier, q, info = pollster_quality(p.get("pollster"), ratings, pr)
    try:
        n = float(p.get("sample_size") or pc["reference_sample"])
    except (TypeError, ValueError):
        n = pc["reference_sample"]
    size = math.sqrt(min(n, pc["max_sample"]) / pc["reference_sample"])
    rec = 0.5 ** (age / half_life)
    pop = pc["population_weight"].get((p.get("population") or "rv").lower(), 0.8)
    part = pc["partisan_weight"] if p.get("partisan") else 1.0
    ver = 1.0 if p.get("verified") in (True, None) else pc.get("unverified_weight", 0.9)
    w = q * size * rec * pop * part * ver
    return w, {"tier": tier, "age": age, "quality": round(q, 3),
               "aapor_roper": bool(info and info.get("aapor_roper")),
               "hist_bias": info.get("bias_label") if info else None}


def compute_house_effects(polls_by_race, shrink):
    """Desviación media de cada encuestadora respecto al consenso de cada carrera."""
    resid = {}
    for race, polls in polls_by_race.items():
        tot = sum(p["w"] for p in polls)
        if tot <= 0 or len(polls) < 3:
            continue
        avg = sum(p["w"] * p["adj"] for p in polls) / tot
        for p in polls:
            resid.setdefault(p["pollster_key"], []).append(p["adj"] + p["house"] - avg)
    effects = {}
    for k, r in resid.items():
        m = statistics.mean(r)
        effects[k] = m * len(r) / (len(r) + shrink)
    return effects


def pollster_key(name):
    n = (name or "").lower()
    for token in ("/", "(", ","):
        n = n.split(token)[0]
    return n.strip()


def build_averages(polls, cfg, ratings, today, pr=None):
    """polls: lista de encuestas normalizadas con 'race','margin','side'.
    Devuelve promedios por carrera y la tabla de encuestadoras."""
    by_race = {}
    for p in polls:
        hl = cfg["polls"]["half_life_days_generic"] if p["race"] == "generic" else cfg["polls"]["half_life_days_race"]
        w, meta = weight_poll(p, cfg, ratings, today, hl, pr)
        if w <= 0:
            continue
        p.update(meta)
        p["w_raw"] = w
        p["pollster_key"] = pollster_key(p.get("pollster"))
        p["house"] = 0.0
        p["adj"] = p["margin"]
        by_race.setdefault(p["race"], []).append(p)

    # penalizar que una misma firma inunde una carrera
    for race, ps in by_race.items():
        counts = {}
        for p in ps:
            counts[p["pollster_key"]] = counts.get(p["pollster_key"], 0) + 1
        for p in ps:
            p["w"] = p["w_raw"] / math.sqrt(counts[p["pollster_key"]])

    effects = {}
    for _ in range(3):
        effects = compute_house_effects(by_race, cfg["polls"]["house_effect_shrink"])
        for ps in by_race.values():
            for p in ps:
                p["house"] = effects.get(p["pollster_key"], 0.0)
                p["adj"] = p["margin"] - p["house"]

    averages = {}
    for race, ps in by_race.items():
        tot = sum(p["w"] for p in ps)
        avg = sum(p["w"] * p["adj"] for p in ps) / tot
        raw = sum(p["w"] * p["margin"] for p in ps) / tot
        n_eff = tot ** 2 / sum(p["w"] ** 2 for p in ps)
        averages[race] = {
            "avg": round(avg, 2), "raw_avg": round(raw, 2), "strength": round(tot, 3),
            "n_eff": round(n_eff, 2), "n_polls": len(ps),
            "side": max(set(p["side"] for p in ps), key=[p["side"] for p in ps].count),
            "polls": sorted([{
                "pollster": p.get("pollster"), "end_date": p["end_date"], "n": p.get("sample_size"),
                "population": p.get("population"), "margin": round(p["margin"], 1),
                "adjusted": round(p["adj"], 1), "weight": round(p["w"], 3), "tier": p["tier"],
                "partisan": p.get("partisan"), "url": p.get("url"), "answers": p.get("answers"),
                "sources": p.get("sources", []), "verified": p.get("verified"), "side": p["side"],
            } for p in ps], key=lambda x: x["end_date"], reverse=True),
        }

    table = {}
    for ps in by_race.values():
        for p in ps:
            t = table.setdefault(p["pollster_key"], {"name": p.get("pollster"), "tier": p["tier"],
                                                    "quality": p["quality"], "aapor": p.get("aapor_roper"),
                                                    "hist_bias": p.get("hist_bias"), "polls": 0, "races": set()})
            t["polls"] += 1
            t["races"].add(p["race"])
    pollsters = sorted([{
        "name": v["name"], "tier": v["tier"], "quality": v["quality"], "aapor_roper": v["aapor"],
        "hist_bias": v["hist_bias"], "polls": v["polls"], "races": len(v["races"]),
        "house_effect": round(effects.get(k, 0.0), 2),
    } for k, v in table.items()], key=lambda x: -x["polls"])
    return averages, pollsters


# ------------------------------------------------------------ carreras
def race_estimate(race, avg, generic, cfg, sentiment_pts, days_left, expert=None, side="D"):
    pc = cfg["polls"]
    lean = race["pres_2024_margin"] - cfg["national_pres_2024_margin"]
    prior = lean + generic
    if race.get("incumbent_running"):
        prior += pc["incumbency_bonus"] * (1 if race["seat_held_by"] == "D" else -1)
    # Independiente sin demócrata (Nebraska, Idaho...): parte de la base republicana con descuento
    if side == "I":
        prior -= 3.0
    structural = prior
    if expert:
        w_exp = cfg["prior"]["expert_weight"]
        prior = (1 - w_exp) * structural + w_exp * expert

    if avg:
        alpha = avg["strength"] / (avg["strength"] + pc["prior_strength"])
        poll_mean = avg["avg"]
        n_eff = avg["n_eff"]
    else:
        alpha, poll_mean, n_eff = 0.0, None, 0.0
    mean = alpha * poll_mean + (1 - alpha) * prior if poll_mean is not None else prior
    mean += sentiment_pts

    u = cfg["uncertainty"]
    race_sd = u["race_sd_base"] + u["race_sd_poll_scarcity"] / (1 + n_eff)
    nat_sd = u["national_sd_base"] + u["national_sd_per_day"] * days_left
    total_sd = math.sqrt(race_sd ** 2 + nat_sd ** 2)
    p_model = norm_cdf(mean / total_sd)
    return {
        "prior": round(prior, 2), "structural": round(structural, 2),
        "expert": None if expert is None else round(expert, 2), "poll_avg": None if poll_mean is None else round(poll_mean, 2),
        "poll_weight": round(alpha, 2), "mean": round(mean, 2), "race_sd": round(race_sd, 2),
        "nat_sd": round(nat_sd, 2), "total_sd": round(total_sd, 2), "p_model": p_model,
    }


def blend_prob(p_model, p_market, cfg):
    b = cfg["blend"]
    if p_market is None or b["markets"] <= 0:
        return p_model
    wm, wk = b["model"], b["markets"]
    return (wm * p_model + wk * p_market) / (wm + wk)


# ------------------------------------------------------------ simulación
def _t(rng, df):
    """Error con colas gruesas (t de Student), escalado a varianza 1."""
    if not df or df > 100:
        return rng.gauss(0, 1)
    chi2 = rng.gammavariate(df / 2, 2)
    return rng.gauss(0, 1) / math.sqrt(chi2 / df) * math.sqrt((df - 2) / df)


def simulate(races, generic_mean, cfg, days_left, p_house_market=None, seed=2026):
    u = cfg["uncertainty"]
    sims = u["simulations"]
    df = u.get("tail_df", 5)
    reg_sd = u.get("regional_sd", 1.5)
    nat_sd = u["national_sd_base"] + u["national_sd_per_day"] * days_left
    rng = random.Random(seed)
    sen, h = cfg["senate"], cfg["house"]
    regions = sorted({r.get("region", "Otra") for r in races})

    # media efectiva por carrera para que P(gana) = probabilidad final combinada
    for r in races:
        tot = math.sqrt(r["est"]["total_sd"] ** 2 + reg_sd ** 2)
        r["_mu"] = norm_ppf(r["p_final"]) * tot

    senate_d, senate_i, house_d = [], [], []
    d_ctrl = r_ctrl = 0
    win_counts = {r["id"]: 0 for r in races}
    for _ in range(sims):
        e_nat = nat_sd * _t(rng, df)
        e_reg = {g: reg_sd * rng.gauss(0, 1) for g in regions}
        d = sen["d_not_up"]
        i = 0
        for r in races:
            m = r["_mu"] + e_nat + e_reg[r.get("region", "Otra")] + r["est"]["race_sd"] * _t(rng, df)
            if m > 0:
                win_counts[r["id"]] += 1
                if r["side"] == "I":
                    i += 1
                else:
                    d += 1
        rr = 100 - d - i
        senate_d.append(d)
        senate_i.append(i)
        if d >= sen["majority"]:
            d_ctrl += 1
        elif rr >= 50:
            r_ctrl += 1
        seats = h["majority"] + h["seats_per_point"] * (generic_mean + e_nat - h["tipping_point_generic_margin"]) \
            + h["seat_noise_sd"] * _t(rng, df)
        house_d.append(int(round(min(max(seats, 150), 290))))

    p_house_model = sum(1 for s in house_d if s >= h["majority"]) / sims
    p_house = blend_prob(p_house_model, p_house_market, cfg)
    sd_seats = statistics.pstdev(house_d) or 1
    shift = (norm_ppf(p_house) - norm_ppf(p_house_model)) * sd_seats if 0 < p_house_model < 1 else 0
    house_d = [int(round(s + shift)) for s in house_d]

    def hist(values, lo, hi):
        out = {}
        for v in values:
            v = min(max(v, lo), hi)
            out[v] = out.get(v, 0) + 1
        return [{"seats": k, "pct": round(100 * out.get(k, 0) / sims, 3)} for k in range(lo, hi + 1)]

    def pct(values, q):
        s = sorted(values)
        return s[int(q * (len(s) - 1))]

    for r in races:
        r["p_sim"] = win_counts[r["id"]] / sims
        del r["_mu"]

    return {
        "senate": {
            "p_d_control": round(d_ctrl / sims, 4),
            "p_r_control": round(r_ctrl / sims, 4),
            "p_depends_on_independent": round(1 - (d_ctrl + r_ctrl) / sims, 4),
            "d_seats_median": int(statistics.median(senate_d)),
            "d_seats_mean": round(statistics.mean(senate_d), 1),
            "i_seats_mean": round(statistics.mean(senate_i), 2),
            "d_seats_80": [pct(senate_d, 0.1), pct(senate_d, 0.9)],
            "histogram": hist(senate_d, 40, 60),
        },
        "house": {
            "p_d_control": round(p_house, 4),
            "p_d_control_model_only": round(p_house_model, 4),
            "d_seats_median": int(statistics.median(house_d)),
            "d_seats_80": [pct(house_d, 0.1), pct(house_d, 0.9)],
            "histogram": hist(house_d, 160, 290),
        },
        "national_sd": round(nat_sd, 2),
        "regional_sd": reg_sd,
        "tail_df": df,
        "simulations": sims,
    }
