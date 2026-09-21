"""
Autocontrol antes de publicar. Si algo está mal, sale con error y el flujo
automático publica los datos anteriores en vez de los nuevos.
Uso: python scripts/selftest.py
"""
import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
path = os.path.join(ROOT, "docs", "data", "latest.json")
errors = []

try:
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
except Exception as e:  # noqa: BLE001
    print(f"FALLO: no se pudo leer latest.json: {e}")
    sys.exit(1)

races = d.get("races", [])
if len(races) != 35:
    errors.append(f"se esperaban 35 carreras y hay {len(races)}")
for r in races:
    p = r.get("p_final")
    if p is None or not 0 <= p <= 1:
        errors.append(f"probabilidad inválida en {r.get('name')}: {p}")
for k in ("senate", "house"):
    p = d.get("control", {}).get(k, {}).get("p_d_control")
    if p is None or not 0 <= p <= 1:
        errors.append(f"probabilidad de control inválida ({k}): {p}")
g = d.get("generic", {}).get("margin")
if g is None or abs(g) > 30:
    errors.append(f"voto genérico fuera de rango: {g}")
if d.get("counts", {}).get("polls_total", 0) < 10:
    errors.append("menos de 10 encuestas: probablemente falló la descarga")
try:
    gen = dt.datetime.fromisoformat(d["generated_at"])
    if (dt.datetime.now(dt.timezone.utc) - gen).total_seconds() > 3 * 3600:
        errors.append("latest.json no se generó en esta ejecución")
except Exception:  # noqa: BLE001
    errors.append("fecha de generación ilegible")

if errors:
    print("FALLO del autocontrol:")
    for e in errors:
        print("  -", e)
    sys.exit(1)
print(f"Autocontrol correcto: {len(races)} carreras, {d['counts']['polls_total']} encuestas, "
      f"datos en vivo: {d.get('live_data')}")
