"""
Clientes de las fuentes de datos. Todas son gratuitas y no requieren clave.

- VoteHub (https://votehub.com/polls/api/): encuestas. Licencia CC BY 4.0,
  hay que citar a VoteHub como fuente (el panel ya lo hace).
- Polymarket (gamma-api): probabilidades de mercados de predicción.
- GDELT DOC 2.0: tono y volumen de cobertura en medios, incluidos locales.
- Google News RSS: titulares recientes por carrera.

Cada función falla "suave": si una fuente se cae, devuelve None o lista
vacía y el resto del sistema sigue funcionando.
"""
import json
import time
import urllib.parse
import xml.etree.ElementTree as ET

import requests

import os
_REPO = os.environ.get("GITHUB_REPOSITORY", "local")
UA = {"User-Agent": f"RadarElectoral/2.0 (https://github.com/{_REPO}; proyecto personal no comercial)"}
TIMEOUT = 30


_FAILS = {}  # fallos seguidos por servidor: si uno se cae, dejamos de insistir


def _get(url, params=None, retries=3, as_json=True):
    host = urllib.parse.urlparse(url).netloc
    if _FAILS.get(host, 0) >= 3:
        return None
    for attempt in range(retries):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            r.raise_for_status()
            _FAILS[host] = 0
            return r.json() if as_json else r.text
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                _FAILS[host] = _FAILS.get(host, 0) + 1
                print(f"  [aviso] {url} falló: {str(e)[:120]}")
                return None
            time.sleep(3 * (attempt + 1))
    return None


# ---------------------------------------------------------------- VoteHub
VOTEHUB = "https://api.votehub.com"


def votehub_polls(poll_type, subject=None, from_date=None):
    params = {"poll_type": poll_type}
    if subject:
        params["subject"] = subject
    if from_date:
        params["from_date"] = from_date
    data = _get(f"{VOTEHUB}/polls", params)
    if data is None:
        return None
    polls = data.get("polls", []) if isinstance(data, dict) else data
    if subject:  # el filtro del API no siempre es exacto
        polls = [p for p in polls if (p.get("subject") or "").strip().lower() == subject.lower()]
    return polls


def votehub_subjects():
    return _get(f"{VOTEHUB}/subjects") or []


# ---------------------------------------------------------------- Polymarket
GAMMA = "https://gamma-api.polymarket.com"


def polymarket_event(slug):
    """Devuelve [(nombre, probabilidad_pct)] para un evento de Polymarket."""
    data = _get(f"{GAMMA}/events", {"slug": slug})
    if not data:
        return None
    event = data[0] if isinstance(data, list) else data
    out = []
    for m in event.get("markets", []) or []:
        if m.get("closed") and not m.get("active", True):
            continue
        name = m.get("groupItemTitle") or m.get("question") or ""
        try:
            prices = m.get("outcomePrices")
            prices = json.loads(prices) if isinstance(prices, str) else prices
            yes = float(prices[0]) * 100
        except Exception:  # noqa: BLE001
            continue
        if name and yes > 0.05:
            out.append((name, round(yes, 1)))
    return {"title": event.get("title"), "url": f"https://polymarket.com/event/{slug}", "outcomes": out}


# ---------------------------------------------------------------- GDELT
GDELT = "https://api.gdeltproject.org/api/v2/doc/doc"


def gdelt_tone(query, timespan="14d"):
    """Tono medio (-10 a +10 aprox.) y volumen de artículos sobre `query`
    en medios de EE. UU. durante el periodo indicado."""
    q = f'{query} sourcecountry:US'
    tone = _get(GDELT, {"query": q, "mode": "timelinetone", "timespan": timespan, "format": "json"})
    vol = _get(GDELT, {"query": q, "mode": "timelinevolraw", "timespan": timespan, "format": "json"})
    result = {"tone": None, "articles": None}
    try:
        series = tone["timeline"][0]["data"]
        vals = [pt["value"] for pt in series if pt.get("value") is not None]
        if vals:
            result["tone"] = round(sum(vals) / len(vals), 2)
    except Exception:  # noqa: BLE001
        pass
    try:
        series = vol["timeline"][0]["data"]
        result["articles"] = int(sum(pt.get("value", 0) for pt in series))
    except Exception:  # noqa: BLE001
        pass
    return result


# ---------------------------------------------------------------- Google News
def google_news(query, limit=5):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f"{query} when:7d", "hl": "en-US", "gl": "US", "ceid": "US:en"})
    text = _get(url, as_json=False)
    if not text:
        return []
    items = []
    try:
        root = ET.fromstring(text)
        for it in root.iter("item"):
            items.append({
                "title": (it.findtext("title") or "").strip(),
                "url": it.findtext("link"),
                "source": (it.find("source").text if it.find("source") is not None else ""),
                "date": it.findtext("pubDate"),
            })
            if len(items) >= limit:
                break
    except ET.ParseError:
        pass
    return items


# ---------------------------------------------------------------- Wikipedia
WIKI_API = "https://en.wikipedia.org/w/api.php"


def wikipedia_html(page):
    """HTML renderizado de una página por la API oficial de MediaWiki."""
    data = _get(WIKI_API, {"action": "parse", "page": page, "prop": "text", "format": "json",
                           "formatversion": 2, "redirects": 1})
    try:
        return data["parse"]["text"]
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- Kalshi
KALSHI = "https://external-api.kalshi.com/trade-api/v2"


def kalshi_series(series_ticker):
    """[(nombre, probabilidad_pct)] de los mercados abiertos de una serie de Kalshi."""
    data = _get(f"{KALSHI}/events", {"series_ticker": series_ticker, "with_nested_markets": "true",
                                     "status": "open"})
    if not data or not data.get("events"):
        return None
    ev = data["events"][0]
    out = []
    for m in ev.get("markets", []) or []:
        name = m.get("yes_sub_title") or m.get("subtitle") or m.get("title") or ""
        price = None
        for k in ("last_price_dollars", "yes_bid_dollars"):
            if m.get(k) not in (None, ""):
                try:
                    price = float(m[k]) * 100
                    break
                except (TypeError, ValueError):
                    pass
        if price is None:
            for k in ("last_price", "yes_bid"):
                if m.get(k) not in (None, 0):
                    price = float(m[k])
                    break
        if name and price:
            out.append((name, round(price, 1)))
    return {"title": ev.get("title"), "url": f"https://kalshi.com/markets/{series_ticker.lower()}",
            "outcomes": out}


# ---------------------------------------------------------------- PredictIt
_PREDICTIT_CACHE = {}


def predictit_all():
    if "all" not in _PREDICTIT_CACHE:
        _PREDICTIT_CACHE["all"] = _get("https://www.predictit.org/api/marketdata/all/") or {}
    return _PREDICTIT_CACHE["all"].get("markets", [])


def predictit_find(keywords):
    """Primer mercado cuyo nombre contiene todas las palabras clave."""
    for m in predictit_all():
        name = (m.get("name") or "") + " " + (m.get("shortName") or "")
        if all(k.lower() in name.lower() for k in keywords) and "2026" in name:
            out = []
            for c in m.get("contracts", []):
                p = c.get("lastTradePrice") or c.get("bestBuyYesCost")
                if p:
                    out.append((c.get("name") or c.get("shortName") or "", round(float(p) * 100, 1)))
            return {"title": m.get("name"), "url": m.get("url"), "outcomes": out}
    return None


# ---------------------------------------------------------------- FEC
def fec_senate(state, api_key="DEMO_KEY", cycle=2026):
    data = _get("https://api.open.fec.gov/v1/elections/",
                {"cycle": cycle, "office": "senate", "state": state, "api_key": api_key,
                 "per_page": 20, "sort": "-total_receipts"})
    if not data:
        return []
    out = []
    for c in data.get("results", []):
        out.append({"name": c.get("candidate_name"), "party": (c.get("party_full") or "")[:3].upper(),
                    "receipts": c.get("total_receipts"), "cash": c.get("cash_on_hand_end_period"),
                    "through": c.get("coverage_end_date")})
    return out


# ---------------------------------------------------------------- 538
def fivethirtyeight_ratings_csv():
    return _get("https://raw.githubusercontent.com/fivethirtyeight/data/master/pollster-ratings/"
                "pollster-ratings-combined.csv", as_json=False)
