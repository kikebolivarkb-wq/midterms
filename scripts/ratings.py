"""
Calidad de encuestadoras basada en datos de precisión histórica.

Prioridad:
 1. Silver Bulletin (si guardaste config/external/silver_bulletin_pollster_stats.xlsx)
 2. FiveThirtyEight (archivo público; se descarga en cada actualización y hay copia local)
 3. Lista manual de config/pollsters.json
La pertenencia a AAPOR Transparency Initiative o Roper Center da un pequeño plus.
"""
import csv
import io
import os
import re

STOP = {"the", "of", "and", "inc", "llc", "group", "research", "polling", "poll", "polls", "strategies",
        "associates", "co", "company", "insights", "analytics", "university", "college",
        "institute", "survey", "surveys", "&", "for", "news", "partners", "reports", "law", "school"}


def tokens(name):
    n = re.sub(r"[^a-z0-9/ ]", " ", (name or "").lower())
    toks = [t for t in re.split(r"[\s/]+", n) if t and t not in STOP]
    return toks


def _key(name):
    t = tokens(name)
    return " ".join(t)


class PollsterRatings:
    def __init__(self):
        self.rows = {}      # clave -> dict
        self.source = None

    def load_538(self, text):
        rd = csv.DictReader(io.StringIO(text))
        n = 0
        for r in rd:
            grade = r.get("numeric_grade")
            try:
                grade = float(grade) if grade not in ("", None) else None
            except ValueError:
                grade = None
            if r.get("inactive", "").upper() == "TRUE" and grade is None:
                continue
            aapor = r.get("aapor_roper", "").upper() == "TRUE"
            try:
                bias = float(r.get("bias_ppm") or 0)
            except ValueError:
                bias = 0.0
            q = None
            if grade is not None:
                q = 0.35 + 0.65 * grade / 3.0 + (0.05 if aapor else 0)
            self.rows[_key(r["pollster"])] = {"name": r["pollster"], "quality": q, "grade": grade,
                                             "grade_label": f"{grade:.1f}/3" if grade is not None else "s/n",
                                             "aapor_roper": aapor, "hist_bias": bias, "origin": "538",
                                             "bias_label": f"{bias:+.1f}"}
            n += 1
        self.source = self.source or "538"
        return n

    def load_silver(self, path):
        """Lee el XLSX de Silver Bulletin si existe. Busca columnas por nombre."""
        try:
            import pandas as pd
            df = pd.read_excel(path)
        except Exception as e:  # noqa: BLE001
            print(f"  [aviso] no se pudo leer {path}: {e}")
            return 0
        cols = {c.lower(): c for c in df.columns}
        name_c = next((cols[c] for c in cols if "pollster" in c), None)
        pm_c = next((cols[c] for c in cols if "predictive" in c or "plus-minus" in c or "plus minus" in c), None)
        grade_c = next((cols[c] for c in cols if c == "grade" or "letter" in c), None)
        aapor_c = next((cols[c] for c in cols if "aapor" in c or "roper" in c), None)
        bias_c = next((cols[c] for c in cols if "bias" in c), None)
        if not (name_c and pm_c):
            print("  [aviso] el XLSX de Silver Bulletin no tiene las columnas esperadas")
            return 0
        n = 0
        for _, r in df.iterrows():
            try:
                ppm = float(r[pm_c])
            except (TypeError, ValueError):
                continue
            q = max(0.3, min(1.1, 0.9 - 0.25 * ppm))
            bias = 0.0
            if bias_c is not None:
                m = re.match(r"([DR])\s*\+\s*([\d.]+)", str(r[bias_c]))
                if m:
                    bias = float(m.group(2)) * (1 if m.group(1) == "D" else -1)
            self.rows[_key(r[name_c])] = {"name": str(r[name_c]), "quality": q,
                                         "grade": None, "grade_label": str(r[grade_c]) if grade_c else f"{ppm:+.1f}",
                                         "aapor_roper": bool(aapor_c and str(r[aapor_c]).strip().lower() in ("yes", "true", "1", "x")),
                                         "hist_bias": bias, "origin": "Silver Bulletin",
                                         "bias_label": ("D" if bias > 0 else "R") + f" +{abs(bias):.1f}" if bias else "0"}
            n += 1
        self.source = "Silver Bulletin"
        return n

    def _lookup_one(self, name):
        k = _key(name)
        if k in self.rows:
            return self.rows[k]
        tk = set(tokens(name))
        if not tk:
            return None
        best, score = None, 0.0
        for key, row in self.rows.items():
            rk = set(key.split())
            if not rk or not (tk <= rk or rk <= tk):
                continue
            s = len(tk & rk) / len(rk | tk)
            if s > score:
                best, score = row, s
        return best if score > 0.5 else None

    def lookup(self, name):
        """Busca por palabras clave; en encuestas conjuntas ('A/B') usa la firma mejor calificada."""
        if not self.rows:
            return None
        hit = self._lookup_one(name)
        if hit:
            return hit
        parts = [p for p in re.split(r"/|\band\b", name or "") if p.strip()]
        found = [self._lookup_one(p) for p in parts] if len(parts) > 1 else []
        found = [f for f in found if f and f.get("quality")]
        return max(found, key=lambda f: f["quality"]) if found else None


def load_all(root, fresh_538_text=None):
    pr = PollsterRatings()
    text = fresh_538_text
    local = os.path.join(root, "config", "external", "538_pollster_ratings.csv")
    if not text and os.path.exists(local):
        with open(local, encoding="utf-8-sig") as f:
            text = f.read()
    if text:
        pr.load_538(text)
    silver = os.path.join(root, "config", "external", "silver_bulletin_pollster_stats.xlsx")
    if os.path.exists(silver):
        pr.load_silver(silver)
    return pr
