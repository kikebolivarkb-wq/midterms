"""
Lee las tablas de las páginas de Wikipedia de cada carrera:
  - "Poll source ..."            -> encuestas individuales (con partidismo "(R)"/"(D)")
  - "Source of poll aggregation" -> promedios de otros agregadores
  - "Source | Ranking | As of"   -> calificaciones de pronosticadores
y la tabla general de predicciones de la página del Senado.

Wikipedia cambia a veces el formato; por eso todo va en try/except y cada
tabla se identifica por sus encabezados, no por su posición.
"""
import datetime as dt
import re
from io import StringIO

import pandas as pd

MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july",
                                      "august", "september", "october", "november", "december"], 1)}
RATING_POINTS = {"safe": 15.0, "solid": 15.0, "likely": 8.0, "lean": 4.0, "tilt": 1.5, "tossup": 0.0,
                 "toss-up": 0.0, "toss up": 0.0}


def _clean(s):
    s = "" if s is None or (isinstance(s, float) and pd.isna(s)) else str(s)
    s = re.sub(r"\[[^\]]*\]", "", s)          # notas [a], [12]
    return re.sub(r"\s+", " ", s).strip()


def _tables(html):
    try:
        return pd.read_html(StringIO(html))
    except Exception:  # noqa: BLE001
        return []


def _flat_cols(df):
    cols = []
    for c in df.columns:
        if isinstance(c, tuple):
            parts = [_clean(x) for x in c if _clean(x) and not _clean(x).startswith("Unnamed")]
            # conservar la parte más informativa (la última suele ser el nombre)
            cols.append(parts[-1] if parts else "")
        else:
            cols.append(_clean(c))
    df = df.copy()
    df.columns = cols
    return df


def _pct(x):
    s = _clean(x).replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_end_date(text, default_year=None):
    """'September 10–12, 2026' -> 2026-09-12; 'June 27 – July 1, 2026' -> 2026-07-01."""
    t = _clean(text).replace("—", "–").replace("-", "–")
    year = re.findall(r"(20\d\d)", t)
    year = int(year[-1]) if year else default_year
    if not year:
        return None
    t = re.sub(r",?\s*20\d\d", "", t)
    last = t.split("–")[-1].strip()
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2})", last)
    if m and m.group(1).lower() in MONTHS:
        month, day = MONTHS[m.group(1).lower()], int(m.group(2))
    else:
        d = re.match(r"(\d{1,2})", last)
        first = re.match(r"([A-Za-z]+)", t.strip())
        if not (d and first and first.group(1).lower() in MONTHS):
            return None
        month, day = MONTHS[first.group(1).lower()], int(d.group(1))
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_sample(text):
    t = _clean(text)
    pop = None
    m = re.search(r"\((LV|RV|A|V)\)", t, re.I)
    if m:
        pop = m.group(1).lower()
    n = re.search(r"([\d,]{2,})", t)
    return (int(n.group(1).replace(",", "")) if n else None), pop


def parse_pollster(text):
    """'InsiderAdvantage (R)' -> ('InsiderAdvantage', 'REP'). Encuestas bipartidistas -> None."""
    t = _clean(text)
    tags = re.findall(r"\((D|R|I)\)", t)
    name = re.sub(r"\s*\((D|R|I)\)", "", t).strip()
    partisan = None
    if tags and len(set(tags)) == 1:
        partisan = {"D": "DEM", "R": "REP", "I": "IND"}[tags[0]]
    return name, partisan


def _name_hit(col, name):
    if not name:
        return False
    last = name.split()[-1].lower()
    return last in col.lower()


def find_poll_table(tables, cands):
    """Primera tabla de encuestas que enfrenta a los candidatos principales."""
    main = [c for c in (cands.get("D"), cands.get("R"), cands.get("I")) if c]
    for raw in tables:
        df = _flat_cols(raw)
        cols = list(df.columns)
        if not cols or "poll source" not in cols[0].lower():
            continue
        hits = [n for n in main if any(_name_hit(c, n) for c in cols[1:])]
        if cands.get("R") and any(_name_hit(c, cands["R"]) for c in cols) and len(hits) >= 2:
            return df
    return None


def parse_polls(html, cands, race_id):
    out = []
    tables = _tables(html)
    df = find_poll_table(tables, cands)
    if df is None:
        return out, tables
    cols = list(df.columns)
    date_col = next((c for c in cols if "date" in c.lower()), None)
    size_col = next((c for c in cols if "sample" in c.lower()), None)
    cand_cols = {}
    for party in ("D", "R", "I"):
        name = cands.get(party)
        col = next((c for c in cols if _name_hit(c, name)), None) if name else None
        if col:
            cand_cols[party] = (name, col)
    for _, row in df.iterrows():
        src = _clean(row[cols[0]])
        if not src or src.lower().startswith(("poll source", "aggregate")):
            continue
        end = parse_end_date(row[date_col]) if date_col else None
        if not end:
            continue
        pollster, partisan = parse_pollster(src)
        n, pop = parse_sample(row[size_col]) if size_col else (None, None)
        answers = []
        for party, (name, col) in cand_cols.items():
            v = _pct(row[col])
            if v is not None:
                answers.append({"choice": name, "pct": v, "party": party})
        if len(answers) < 2:
            continue
        out.append({"race": race_id, "pollster": pollster, "end_date": end, "start_date": end,
                    "sample_size": n, "population": pop or "lv", "partisan": partisan,
                    "internal": False, "url": "", "answers": answers, "source": "wikipedia"})
    return out, tables


def parse_aggregates(tables, cands):
    """Promedios de otros agregadores para el enfrentamiento principal."""
    out = []
    for raw in tables:
        df = _flat_cols(raw)
        cols = list(df.columns)
        if not cols or "aggregation" not in cols[0].lower():
            continue
        if cands and not all(any(_name_hit(c, n) for c in cols) for n in
                              [x for x in (cands.get("D") or cands.get("I"), cands.get("R")) if x]):
            continue
        margin_col = next((c for c in cols if c.lower().startswith("margin")), None)
        upd_col = next((c for c in cols if "updated" in c.lower()), None)
        for _, row in df.iterrows():
            src = _clean(row[cols[0]])
            if not src or src.lower() == "average":
                continue
            m = _clean(row[margin_col]) if margin_col else ""
            mm = re.search(r"([A-Za-z\-\.' ]+)\s*\+\s*([\d.]+)", m)
            if not mm:
                continue
            who, val = mm.group(1).strip(), float(mm.group(2))
            sign = -1 if (cands.get("R") and _name_hit(who, cands["R"])) or who.lower().startswith("republican") else 1
            out.append({"source": src, "margin": sign * val, "updated": _clean(row[upd_col]) if upd_col else ""})
        if out:
            break
    return out


def parse_race_ratings(tables):
    out = []
    for raw in tables:
        df = _flat_cols(raw)
        cols = [c.lower() for c in df.columns]
        if len(cols) >= 2 and cols[0] == "source" and "ranking" in cols[1]:
            for _, row in df.iterrows():
                src, rating = _clean(row.iloc[0]), _clean(row.iloc[1])
                pts = rating_to_margin(rating)
                if src and pts is not None:
                    out.append({"source": src, "rating": rating, "as_of": _clean(row.iloc[2]) if len(row) > 2 else "",
                                "points": pts})
            break
    return out


def rating_to_margin(rating):
    r = _clean(rating).lower().replace("(flip)", "").strip()
    if not r:
        return None
    for key, pts in RATING_POINTS.items():
        if r.startswith(key):
            if pts == 0:
                return 0.0
            side = r.split()[-1]
            if side in ("d", "dfl", "i"):
                return pts
            if side == "r":
                return -pts
    return None


def parse_overview_ratings(html):
    """Tabla grande de la página del Senado: estado x pronosticador."""
    result = {}
    for raw in _tables(html):
        df = _flat_cols(raw)
        cols = list(df.columns)
        low = [c.lower() for c in cols]
        if not low or low[0] not in ("state", "constituency") or not any(c.startswith("cook") for c in low):
            continue
        rater_cols = [c for c in cols[4:] if c]
        short = {c: re.sub(r"\s+[A-Z][a-z]{2,}\.?\s+\d{1,2},\s+20\d\d$", "", c).strip() for c in rater_cols}
        for _, row in df.iterrows():
            state = _clean(row[cols[0]]).replace("(special)", "").strip()
            if not state or state.lower() == "overall":
                continue
            items = []
            for c in rater_cols:
                pts = rating_to_margin(row[c])
                if pts is not None:
                    items.append({"source": short[c], "rating": _clean(row[c]), "points": pts,
                                  "as_of": c[len(short[c]):].strip()})
            if items:
                result[state] = items
        if result:
            break
    return result


def parse_generic_aggregates(html):
    """Tabla de promedios del voto genérico (Republicanos / Demócratas)."""
    for raw in _tables(html):
        df = _flat_cols(raw)
        cols = list(df.columns)
        if not cols or "aggregation" not in cols[0].lower():
            continue
        if not any("democrat" in c.lower() for c in cols):
            continue
        out = []
        margin_col = next((c for c in cols if c.lower().startswith("margin")), None)
        upd_col = next((c for c in cols if "updated" in c.lower()), None)
        for _, row in df.iterrows():
            src = _clean(row[cols[0]])
            if not src or src.lower() == "average" or not margin_col:
                continue
            mm = re.search(r"(Democrats|Republicans)\s*\+\s*([\d.]+)", _clean(row[margin_col]))
            if mm:
                val = float(mm.group(2)) * (1 if mm.group(1) == "Democrats" else -1)
                out.append({"source": src, "margin": val, "updated": _clean(row[upd_col]) if upd_col else ""})
        return out
    return []
