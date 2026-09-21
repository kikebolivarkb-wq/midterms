"""
Actualizador del Radar Electoral 2026 (versión multifuente).

Uso:  python scripts/update.py            (datos en vivo)
      python scripts/update.py --offline  (solo datos semilla, para probar)

Fuentes: VoteHub, Wikipedia (encuestas, promedios de agregadores y calificaciones
de pronosticadores), FiveThirtyEight / Silver Bulletin (calidad de encuestadoras),
Polymarket, Kalshi, PredictIt, OpenFEC, GDELT y Google News.
Escribe docs/data/latest.json y agrega un punto a docs/data/history.json.
"""
import argparse
import datetime as dt
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
import model  # noqa: E402
import ratings as ratings_mod  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG_DIR = os.path.join(ROOT, "config")
OUT_DIR = os.path.join(ROOT, "docs", "data")

RATER_NAMES = {
    "cook": "Cook Political Report", "ie": "Inside Elections", "sabato": "Sabato's Crystal Ball",
    "wh": "Race to the WH", "econ": "The Economist", "rcp": "RealClearPolitics", "ddhq": "Decision Desk HQ",
    "fox": "Fox News", "fpo": "FiftyPlusOne", "st": "Split Ticket", "silver": "Silver Bulletin",
}


def load(name):
    with open(os.path.join(CFG_DIR, name), encoding="utf-8") as f:
        return json.load(f)


def rater_name(src):
    s = (src or "").lower()
    for k, v in RATER_NAMES.items():
        if s.startswith(k) or k in s.replace("'", "").replace(" ", "") or v.lower() in s:
            return v
    return src


def dedupe(polls):
    """Une encuestas repetidas entre fuentes. Si coinciden en carrera, encuestadora
    y fecha (±3 días) y resultado (±1 punto), queda una sola marcada como verificada."""
    out = []
    for p in sorted(polls, key=lambda x: 0 if x.get("source") == "votehub" else 1):
        key = model.pollster_key(p.get("pollster")).split(" ")[0]
        try:
            end = dt.date.fromisoformat(p["end_date"][:10])
        except Exception:  # noqa: BLE001
            continue
        match = None
        for q in out:
            if q["race"] != p["race"] or model.pollster_key(q.get("pollster")).split(" ")[0] != key:
                continue
            if abs((dt.date.fromisoformat(q["end_date"][:10]) - end).days) > 3:
                continue
            if abs(q["margin"] - p["margin"]) > 1.01:
                continue
            match = q
            break
        if match:
            src = p.get("source", "?")
            if src not in match["sources"]:
                match["sources"].append(src)
            match["verified"] = len(match["sources"]) >= 2
            if not match.get("url") and p.get("url"):
                match["url"] = p["url"]
        else:
            q = dict(p)
            q["sources"] = [p.get("source", "?")]
            out.append(q)
    return out


def party_from_label(label, cands):
    low = label.lower()
    if "(d)" in low or "democrat" in low:
        return "D"
    if "(r)" in low or "republican" in low:
        return "R"
    if "(i)" in low or "independent" in low:
        return "I"
    return model.party_of(label.split("(")[0].strip(), cands)


def market_prob(outcomes, cands, side):
    probs = {}
    for name, pr in outcomes or []:
        party = party_from_label(name, cands)
        if party:
            probs[party] = max(probs.get(party, 0), pr)
    a, b = probs.get(side), probs.get("R")
    if a is None and side == "D" and "I" in probs:
        a = probs["I"]
    if a is None or b is None or a + b <= 0:
        return None
    return a / (a + b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--today", default=None, help="AAAA-MM-DD, para pruebas")
    args = ap.parse_args()

    cfg = load("config.json")
    manual_ratings = load("pollsters.json")
    races = load("senate_races.json")["races"]
    manual = load("manual_polls.json").get("polls", [])
    seed = load("seed_data.json")
    registry = load("sources.json")

    today = dt.date.fromisoformat(args.today) if args.today else dt.date.today()
    election = dt.date.fromisoformat(cfg["election_date"])
    days_left = max((election - today).days, 0)
    since = (today - dt.timedelta(days=cfg["polls"]["max_age_days"])).isoformat()
    warnings = set()
    status = {}
    race_by_id = {r["id"]: r for r in races}
    race_by_name = {r["name"]: r for r in races}

    # ------------------------------------------------ 0. calidad de encuestadoras
    fresh_538 = None
    if not args.offline:
        import sources
        print("FiveThirtyEight: calificaciones de encuestadoras…")
        fresh_538 = sources.fivethirtyeight_ratings_csv()
        if fresh_538 and fresh_538.startswith("pollster,"):
            with open(os.path.join(CFG_DIR, "external", "538_pollster_ratings.csv"), "w", encoding="utf-8") as f:
                f.write(fresh_538)
        else:
            fresh_538 = None
    pr = ratings_mod.load_all(ROOT, fresh_538)
    status["538"] = {"ok": bool(pr.rows), "records": len(pr.rows), "detail": "descargado hoy" if fresh_538 else "copia local"}
    status["silver"] = {"ok": pr.source == "Silver Bulletin",
                        "detail": "archivo cargado" if pr.source == "Silver Bulletin" else "sin archivo (opcional)"}
    status["aapor"] = {"ok": bool(pr.rows), "records": sum(1 for r in pr.rows.values() if r["aapor_roper"])}
    status["roper"] = status["aapor"]

    # ------------------------------------------------ 1. encuestas
    raw, house_districts = [], []
    wiki = {"aggregates": {}, "ratings": {}, "generic_aggregates": [], "overview": {}}
    live = False
    if not args.offline:
        import sources
        import wiki_parse
        print("VoteHub…")
        gen = sources.votehub_polls("generic-ballot", "2026", since)
        if gen is not None:
            live = True
            for p in gen:
                p["race"], p["source"] = "generic", "votehub"
            raw += gen
        sen = sources.votehub_polls("us-senator", None, since) or []
        subj = {r["votehub_subject"].lower(): r["id"] for r in races}
        for p in sen:
            rid = subj.get((p.get("subject") or "").lower())
            if rid:
                p["race"], p["source"] = rid, "votehub"
                raw.append(p)
        house_districts = sources.votehub_polls("us-representative", None, since) or []
        status["votehub"] = {"ok": live, "records": len(gen or []) + len(sen) + len(house_districts)}

        print("Wikipedia: página general del Senado…")
        n_wiki = 0
        html = sources.wikipedia_html("2026 United States Senate elections")
        if html:
            wiki["overview"] = wiki_parse.parse_overview_ratings(html)
            wiki["generic_aggregates"] = wiki_parse.parse_generic_aggregates(html)
        html_h = sources.wikipedia_html("2026 United States House of Representatives elections")
        if html_h and not wiki["generic_aggregates"]:
            wiki["generic_aggregates"] = wiki_parse.parse_generic_aggregates(html_h)
        print("Wikipedia: 35 páginas de carreras…")
        for r in races:
            html = sources.wikipedia_html(r["wikipedia_page"])
            time.sleep(0.5)
            if not html:
                continue
            try:
                polls, tables = wiki_parse.parse_polls(html, r["candidates"], r["id"])
                polls = [p for p in polls if p["end_date"] >= since]
                raw += polls
                n_wiki += len(polls)
                wiki["aggregates"][r["id"]] = wiki_parse.parse_aggregates(tables, r["candidates"])
                wiki["ratings"][r["id"]] = wiki_parse.parse_race_ratings(tables)
            except Exception as e:  # noqa: BLE001
                warnings.add(f"No se pudo leer la página de Wikipedia de {r['name']}: {str(e)[:80]}")
        status["wikipedia"] = {"ok": n_wiki > 0, "records": n_wiki}
        if html and n_wiki == 0:
            warnings.add("Wikipedia respondió pero no se reconocieron tablas de encuestas; revisa si cambió el formato.")
    # Respaldo: si una fuente falla hoy, se usa lo último bueno que se descargó de ELLA
    # (caché), nunca los datos semilla si ya hubo una descarga real antes.
    cache_path = os.path.join(OUT_DIR, "cache.json")
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except Exception:  # noqa: BLE001
            cache = {}
    vh_ok = status.get("votehub", {}).get("ok", False)
    wk_ok = status.get("wikipedia", {}).get("ok", False)
    if not args.offline:
        if vh_ok:
            cache["votehub"] = {"date": today.isoformat(), "polls": [p for p in raw if p.get("source") == "votehub"],
                                "house": house_districts}
        elif cache.get("votehub"):
            raw += cache["votehub"]["polls"]
            house_districts = cache["votehub"].get("house", [])
            status["votehub"] = {"ok": False, "fallback": "cache", "detail": f"falló hoy; se usa la descarga del {cache['votehub']['date']}"}
            warnings.add(f"VoteHub no respondió; se usan sus datos del {cache['votehub']['date']}.")
        if wk_ok:
            cache["wikipedia"] = {"date": today.isoformat(), "polls": [p for p in raw if p.get("source") == "wikipedia"],
                                  "wiki": wiki}
        elif cache.get("wikipedia"):
            raw += cache["wikipedia"]["polls"]
            wiki = cache["wikipedia"]["wiki"]
            status["wikipedia"] = {"ok": False, "fallback": "cache", "detail": f"falló hoy; se usa la descarga del {cache['wikipedia']['date']}"}
            warnings.add(f"Wikipedia no respondió; se usan sus datos del {cache['wikipedia']['date']}.")
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    live = vh_ok or wk_ok
    if not raw:
        print("Usando datos semilla (no hay descargas previas).")
        raw = [dict(p) for p in seed["polls"]]
        wiki["overview"] = seed["expert_ratings"]
        wiki["aggregates"] = seed["aggregates"]
        wiki["generic_aggregates"] = seed["generic_aggregates"]
        status["votehub"] = {"ok": False, "fallback": "seed"}
        status["wikipedia"] = {"ok": False, "fallback": "seed"}

    for p in manual:
        if p.get("enabled", True):
            q = dict(p)
            q.setdefault("pollster", "Encuesta manual")
            q["source"] = "manual"
            raw.append(q)

    normalized = []
    for p in raw:
        rid = p.get("race")
        cands = {"D": "Dem", "R": "Rep"} if rid == "generic" else race_by_id.get(rid, {}).get("candidates", {})
        m, side = model.poll_margin(p, cands, warnings)
        if m is None:
            continue
        q = dict(p)
        q["margin"], q["side"] = m, side
        normalized.append(q)
    merged = dedupe(normalized)
    wiki_on = status.get("wikipedia", {}).get("ok") or status.get("wikipedia", {}).get("fallback")
    for p in merged:
        if not wiki_on or p.get("source") == "manual" or p["race"] == "generic":
            p["verified"] = None if not p.get("verified") else True
        else:
            p.setdefault("verified", False)
    n_verified = sum(1 for p in merged if p.get("verified"))

    averages, pollster_table = model.build_averages(merged, cfg, manual_ratings, today, pr)
    generic = averages.get("generic")
    generic_margin = generic["avg"] if generic else 6.0
    if not generic:
        warnings.add("Sin encuestas de voto genérico: se usa D+6 como supuesto.")

    # ------------------------------------------------ 2. pronosticadores
    experts = {}
    overview = wiki.get("overview") or seed.get("expert_ratings", {})
    for r in races:
        items = wiki["ratings"].get(r["id"]) or overview.get(r["name"]) or []
        items = [dict(i, source=rater_name(i["source"])) for i in items]
        if items:
            experts[r["id"]] = {"mean": statistics.mean(i["points"] for i in items), "items": items}
    status["experts"] = {"ok": bool(experts), "records": sum(len(e["items"]) for e in experts.values())}

    # ------------------------------------------------ 3. mercados
    markets = {}
    senate_market = house_market = None
    if not args.offline:
        import sources
        print("Mercados: Polymarket, Kalshi, PredictIt…")
        ok = {"polymarket": 0, "kalshi": 0, "predictit": 0}
        for r in races:
            side = "I" if (r["candidates"].get("I") and not r["candidates"].get("D")) else "D"
            got = {}
            ev = sources.polymarket_event(r["polymarket_slug"])
            if ev and ev["outcomes"]:
                p = market_prob(ev["outcomes"], r["candidates"], side)
                if p is not None:
                    got["Polymarket"] = {"p": p, "url": ev["url"]}
                    ok["polymarket"] += 1
            ev = sources.kalshi_series(r["kalshi_series"])
            if ev and ev["outcomes"]:
                p = market_prob(ev["outcomes"], r["candidates"], side)
                if p is not None:
                    got["Kalshi"] = {"p": p, "url": ev["url"]}
                    ok["kalshi"] += 1
            ev = sources.predictit_find(r["predictit_keywords"])
            if ev and ev["outcomes"]:
                p = market_prob(ev["outcomes"], r["candidates"], side)
                if p is not None:
                    got["PredictIt"] = {"p": p, "url": ev["url"]}
                    ok["predictit"] += 1
            if got:
                markets[r["id"]] = got
            time.sleep(0.3)
        for slug, key in (("which-party-will-win-the-senate-in-2026", "senate"),
                          ("which-party-will-win-the-house-in-2026", "house")):
            ev = sources.polymarket_event(slug)
            if ev and ev["outcomes"]:
                d = next((p for n, p in ev["outcomes"] if "democrat" in n.lower()), None)
                if d is not None:
                    if key == "senate":
                        senate_market = d / 100
                    else:
                        house_market = d / 100
        for k, v in ok.items():
            status[k] = {"ok": v > 0, "records": v}
    else:
        for rid, ex in seed["markets"]["races"].items():
            side = "I" if race_by_id[rid]["candidates"].get("I") and not race_by_id[rid]["candidates"].get("D") else "D"
            markets[rid] = {}
            for exch, probs in ex.items():
                a, b = probs.get(side), probs.get("R")
                if a is not None and b:
                    markets[rid][exch] = {"p": a / (a + b), "url": None}
        senate_market = seed["markets"]["senate_control"]["D"] / 100
        for k in ("polymarket", "kalshi", "predictit"):
            status[k] = {"ok": False, "fallback": "seed"}

    # ------------------------------------------------ 4. medios y dinero
    sentiment, headlines, money = {}, {}, {}
    if not args.offline:
        import sources
        print("GDELT, Google News y FEC (tarda unos minutos por los límites de uso)…")
        pause = cfg["sentiment"]["seconds_between_calls"]
        fec_key = os.environ.get(cfg["fec"]["api_key_env"], "DEMO_KEY")
        for r in races:
            avg = averages.get(r["id"])
            base = avg["avg"] if avg else (r["pres_2024_margin"] - cfg["national_pres_2024_margin"] + generic_margin)
            if abs(base) > cfg["sentiment"]["only_if_margin_under"]:
                continue
            c = r["candidates"]
            non_r = c.get("D") or c.get("I")
            if cfg["fec"]["enabled"]:
                money[r["id"]] = sources.fec_senate(r["state"], fec_key)[:4]
            if cfg["sentiment"]["enabled"] and non_r and c.get("R"):
                t_nr = sources.gdelt_tone(f'"{non_r}"', cfg["sentiment"]["gdelt_timespan"])
                time.sleep(pause)
                t_r = sources.gdelt_tone(f'"{c["R"]}"', cfg["sentiment"]["gdelt_timespan"])
                time.sleep(pause)
                if t_nr["tone"] is not None and t_r["tone"] is not None:
                    diff = t_nr["tone"] - t_r["tone"]
                    pts = max(-cfg["blend"]["sentiment_max_points"],
                              min(cfg["blend"]["sentiment_max_points"], 0.25 * diff))
                    sentiment[r["id"]] = {"tone_non_r": t_nr["tone"], "tone_r": t_r["tone"],
                                          "articles_non_r": t_nr["articles"], "articles_r": t_r["articles"],
                                          "tone_diff": round(diff, 2), "points": round(pts, 2)}
                headlines[r["id"]] = sources.google_news(f'{r["name"]} Senate {non_r} {c["R"]}', 5)
        status["gdelt"] = {"ok": bool(sentiment), "records": len(sentiment)}
        status["google_news"] = {"ok": any(headlines.values()), "records": sum(len(v) for v in headlines.values())}
        status["fec"] = {"ok": any(money.values()), "records": sum(len(v) for v in money.values())}

    # ------------------------------------------------ 5. estimación por carrera
    out_races = []
    for r in races:
        avg = averages.get(r["id"])
        side = avg["side"] if avg else ("I" if (r["candidates"].get("I") and not r["candidates"].get("D")) else "D")
        s_pts = sentiment.get(r["id"], {}).get("points", 0.0)
        exp = experts.get(r["id"])
        est = model.race_estimate(r, avg, generic_margin, cfg, s_pts, days_left,
                                  exp["mean"] if exp else None, side)
        mk = markets.get(r["id"], {})
        p_market = statistics.mean(v["p"] for v in mk.values()) if mk else None
        p_final = model.blend_prob(est["p_model"], p_market, cfg)
        aggs = wiki["aggregates"].get(r["id"]) or []
        if avg and aggs:
            med = statistics.median(a["margin"] for a in aggs)
            if abs(med - avg["avg"]) > cfg["qa"]["aggregator_gap_warning"]:
                warnings.add(f"{r['name']}: el promedio propio ({avg['avg']:+.1f}) se aleja más de "
                             f"{cfg['qa']['aggregator_gap_warning']:.0f} puntos de la mediana de otros agregadores ({med:+.1f}).")
        out_races.append({
            "id": r["id"], "state": r["state"], "name": r["name"], "special": r["special"], "region": r["region"],
            "seat_held_by": r["seat_held_by"], "candidates": r["candidates"], "side": side,
            "pres_2024_margin": r["pres_2024_margin"], "est": est,
            "markets": {k: round(v["p"], 4) for k, v in mk.items()},
            "market_urls": {k: v["url"] for k, v in mk.items() if v.get("url")},
            "p_market": None if p_market is None else round(p_market, 4),
            "p_final": p_final, "sentiment": sentiment.get(r["id"]),
            "headlines": headlines.get(r["id"], []), "money": money.get(r["id"], []),
            "experts": exp["items"] if exp else [], "aggregates": aggs,
            "polls": (avg or {}).get("polls", [])[:20], "n_polls": (avg or {}).get("n_polls", 0),
            "wikipedia": "https://en.wikipedia.org/wiki/" + r["wikipedia_page"].replace(" ", "_"),
        })

    sim = model.simulate(out_races, generic_margin, cfg, days_left, house_market)
    for r in out_races:
        r["p_final"] = round(r["p_final"], 4)
        r["p_sim"] = round(r["p_sim"], 4)
        r["est"]["p_model"] = round(r["est"]["p_model"], 4)
        m = r["est"]["mean"]
        who = "R" if m < 0 else r["side"]
        r["rating"] = ("Empate técnico" if abs(m) < 2 else
                       ("Inclinado " if abs(m) < 5 else "Probable " if abs(m) < 10 else "Seguro ") + who)

    # distritos de la Cámara con encuestas
    districts, by_subj = [], {}
    for p in house_districts:
        by_subj.setdefault(p.get("subject"), []).append(p)
    for subj, ps in by_subj.items():
        ps = sorted(ps, key=lambda x: x.get("end_date", ""), reverse=True)[:5]
        tot = {}
        for p in ps:
            for a in p.get("answers", []):
                if a.get("pct") is not None:
                    tot.setdefault(a["choice"], []).append(a["pct"])
        avgc = sorted(((k, round(sum(v) / len(v), 1)) for k, v in tot.items()), key=lambda x: -x[1])[:3]
        if len(avgc) >= 2:
            districts.append({"district": (subj or "").replace("2026 ", ""), "leader": avgc[0][0],
                              "lead": round(avgc[0][1] - avgc[1][1], 1), "candidates": avgc,
                              "polls": len(ps), "last": ps[0].get("end_date")})
    districts.sort(key=lambda x: x["lead"])

    # registro de fuentes con su estado
    for g in registry["groups"]:
        for it in g["items"]:
            st = status.get(it["key"])
            if st is None and g["id"] == "experts":
                st = {"ok": status["experts"]["ok"], "detail": "calificación leída vía Wikipedia" if status["experts"]["ok"] else "sin datos hoy"}
            elif st is None and g["id"] == "aggregators":
                has = bool(wiki["generic_aggregates"]) or any(wiki["aggregates"].values())
                st = {"ok": has, "detail": "promedio leído vía Wikipedia" if has else "sin datos hoy"}
            elif st is None and it["key"] == "results2024":
                st = {"ok": True, "detail": "cargado en config/senate_races.json"}
            elif st is None and it["key"] == "manual":
                n_man = sum(1 for m in manual if m.get("enabled", True))
                st = {"ok": n_man > 0, "detail": f"{n_man} encuestas activas"}
            it["status"] = st or {"ok": False, "detail": "no consultada"}

    result = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
        "as_of": today.isoformat(), "days_left": days_left, "live_data": live,
        "generic": {"margin": round(generic_margin, 2), "raw_margin": generic["raw_avg"] if generic else None,
                    "n_polls": generic["n_polls"] if generic else 0,
                    "polls": generic["polls"][:25] if generic else [],
                    "aggregates": wiki["generic_aggregates"]},
        "control": sim, "markets": {"senate_d": senate_market, "house_d": house_market},
        "races": sorted(out_races, key=lambda r: abs(r["est"]["mean"])),
        "house_districts": districts[:40],
        "pollsters": pollster_table,
        "ratings_source": pr.source,
        "counts": {"polls_total": len(merged), "polls_verified": n_verified,
                   "polls_raw": len(normalized), "experts": status["experts"].get("records", 0)},
        "source_registry": registry["groups"],
        "warnings": sorted(warnings),
        "config": {"blend": cfg["blend"], "prior": cfg["prior"],
                   "house": {k: v for k, v in cfg["house"].items() if not k.startswith("_")},
                   "senate": {k: v for k, v in cfg["senate"].items() if not k.startswith("_")}},
        "context": seed.get("context", {}),
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    hist_path = os.path.join(OUT_DIR, "history.json")
    history = []
    if os.path.exists(hist_path):
        with open(hist_path, encoding="utf-8") as f:
            history = json.load(f)
    point = {"date": today.isoformat(), "generic": result["generic"]["margin"],
             "senate_d": sim["senate"]["p_d_control"], "house_d": sim["house"]["p_d_control"],
             "senate_seats": sim["senate"]["d_seats_median"], "house_seats": sim["house"]["d_seats_median"]}
    history = [h for h in history if h["date"] != point["date"]] + [point]
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(history[-400:], f, ensure_ascii=False, indent=1)

    s, h = sim["senate"], sim["house"]
    print(f"\nEncuestas: {len(merged)} únicas ({n_verified} verificadas en dos fuentes)")
    print(f"Voto genérico D{generic_margin:+.1f}")
    print(f"Senado: P(D)={s['p_d_control']:.0%} P(R)={s['p_r_control']:.0%} mediana D={s['d_seats_median']}")
    print(f"Cámara: P(D)={h['p_d_control']:.0%} mediana D={h['d_seats_median']}")
    if warnings:
        print("Avisos:", *sorted(warnings), sep="\n  - ")


if __name__ == "__main__":
    main()
