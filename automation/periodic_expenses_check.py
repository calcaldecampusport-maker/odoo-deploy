#!/usr/bin/env python3
"""
Análisis trimestral de gastos/ingresos periódicos.

Detecta automáticamente patrones recurrentes en facturas y nóminas (sin lista
predefinida — aprende del histórico) y reporta cuáles faltan respecto a su
cadencia esperada.

Cron: 10 de enero, abril, julio y octubre a las 08:00 (4 veces al año).
La salida JSON la lee `dudas_xlsx_publish.py` y la añade como hoja extra
"Gastos_periodicos_pendientes" en el xlsx diario.

Algoritmo:
1. Agrupa facturas por (partner, cuenta, signo) en los últimos 18 meses.
2. Si la misma combinación tiene >=3 ocurrencias y la variación de gaps es
   <35% del avg_gap → es un patrón periódico.
3. Calcula `expected_next = last_date + avg_gap`. Si `today > expected_next + 50%`
   del avg_gap → marcado como MISSING.
4. También trata nóminas (entries con ref like "Nomina%") como un grupo aparte.

Salida: /tmp/periodic/<vat>_periodic.json
"""
# === pipeline isolation guard (auto-injected) ===
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
try:
    import companies as _comp_guard
    if getattr(_comp_guard, "PIPELINE_NAME", None) != 'cararjfam':
        raise RuntimeError(
            f"PIPELINE_MISMATCH: script {__file__} expected pipeline='cararjfam' "
            f"but loaded companies.PIPELINE_NAME={getattr(_comp_guard, 'PIPELINE_NAME', None)!r}"
        )
except ImportError:
    pass  # script sin dependencia de companies.py (e.g. drive_ops)
# === end isolation guard ===

import json
import logging
import statistics
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"
OUT_DIR = Path("/tmp/periodic")
LOOKBACK_MONTHS = 18
MIN_OCCURRENCES = 3
GAP_CV_MAX = 0.35
GRACE_PCT = 0.5

sys.path.insert(0, ODOO_PATH)
sys.path.insert(0, _HERE)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402
import companies as comp  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("periodic")


def _classify_freq(avg_gap_days: float) -> str:
    if avg_gap_days < 45: return "mensual"
    if 75 < avg_gap_days < 110: return "trimestral"
    if 160 < avg_gap_days < 200: return "semestral"
    if 330 < avg_gap_days < 400: return "anual"
    return f"cada_{int(avg_gap_days)}d"


def analyze_company(env, company_id: int) -> dict:
    today = date.today()
    cutoff = today - timedelta(days=30 * LOOKBACK_MONTHS)

    groups = defaultdict(list)

    # Facturas (gastos e ingresos)
    invs = env["account.move"].search([
        ("company_id", "=", company_id),
        ("state", "=", "posted"),
        ("move_type", "in", ["in_invoice", "out_invoice", "in_refund", "out_refund"]),
        ("invoice_date", ">=", cutoff),
    ])
    for m in invs:
        if not m.invoice_line_ids:
            continue
        main_acc = m.invoice_line_ids[0].account_id
        if not main_acc:
            continue
        sign = "ingreso" if m.move_type in ("out_invoice", "out_refund") else "gasto"
        key = ("partner", m.partner_id.id, main_acc.code, sign)
        groups[key].append({"id": m.id, "name": m.name, "date": m.invoice_date,
                            "amount": float(m.amount_total),
                            "partner_name": m.partner_id.name or "?"})

    # Nóminas (entries con ref Nomina)
    nominas = env["account.move"].search([
        ("company_id", "=", company_id),
        ("state", "=", "posted"),
        ("move_type", "=", "entry"),
        ("ref", "=ilike", "Nomina%"),
        ("date", ">=", cutoff),
    ])
    for m in nominas:
        key = ("nomina", "*", "640000", "gasto")
        groups[key].append({"id": m.id, "name": m.ref or m.name, "date": m.date,
                            "amount": float(m.amount_total),
                            "partner_name": "Nóminas (asiento)"})

    # Pagos TGSS / IRPF / impuestos via ref pattern (entries)
    impuestos = env["account.move"].search([
        ("company_id", "=", company_id),
        ("state", "=", "posted"),
        ("move_type", "=", "entry"),
        "|", "|", "|",
            ("ref", "ilike", "TGSS"),
            ("ref", "ilike", "IRPF"),
            ("ref", "ilike", "mod 111"),
            ("ref", "ilike", "mod 303"),
        ("date", ">=", cutoff),
    ])
    for m in impuestos:
        # Categorize by which keyword matches
        ref_upper = (m.ref or "").upper()
        if "TGSS" in ref_upper: cat = "TGSS"
        elif "IRPF" in ref_upper or "MOD 111" in ref_upper: cat = "IRPF"
        elif "MOD 303" in ref_upper: cat = "IVA"
        else: cat = "IMPUESTO"
        key = ("impuesto", cat, "*", "gasto")
        groups[key].append({"id": m.id, "name": m.ref or m.name, "date": m.date,
                            "amount": float(m.amount_total),
                            "partner_name": cat})

    # Detect periodic + missing
    periodic_patterns = []
    for key, items in groups.items():
        if len(items) < MIN_OCCURRENCES:
            continue
        items_sorted = sorted(items, key=lambda x: x["date"])
        gaps = [(items_sorted[i+1]["date"] - items_sorted[i]["date"]).days
                for i in range(len(items_sorted) - 1)]
        if not gaps:
            continue
        avg = statistics.mean(gaps)
        std = statistics.stdev(gaps) if len(gaps) > 1 else 0
        cv = std / avg if avg > 0 else 0
        if cv > GAP_CV_MAX:
            continue
        last = items_sorted[-1]
        expected_next = last["date"] + timedelta(days=int(round(avg)))
        days_late = (today - expected_next).days
        is_missing = days_late > avg * GRACE_PCT

        # avg_amount over last 3 to be tolerant of small variations
        avg_amount = statistics.mean(it["amount"] for it in items_sorted[-3:])
        pattern_type = key[0]
        if pattern_type == "partner":
            label = last["partner_name"]
        elif pattern_type == "nomina":
            label = "Nóminas mensuales"
        else:
            label = f"Pago {key[1]}"

        periodic_patterns.append({
            "tipo": pattern_type,
            "label": label,
            "account_code": key[2] if key[2] != "*" else "",
            "signo": key[3],
            "frecuencia": _classify_freq(avg),
            "avg_gap_dias": round(avg, 1),
            "regularidad_cv": round(cv, 2),
            "ocurrencias": len(items_sorted),
            "ultima_fecha": str(last["date"]),
            "ultimo_importe": round(last["amount"], 2),
            "importe_medio": round(avg_amount, 2),
            "esperada_proxima": str(expected_next),
            "dias_de_retraso": days_late,
            "falta": is_missing,
        })

    missing = [p for p in periodic_patterns if p["falta"]]
    # Sort missing by days late descending (more urgent first)
    missing.sort(key=lambda x: -x["dias_de_retraso"])
    periodic_patterns.sort(key=lambda x: (not x["falta"], -x["dias_de_retraso"]))

    return {
        "company_id": company_id,
        "today": str(today),
        "patterns": periodic_patterns,
        "missing": missing,
        "summary": {
            "total_periodicos_detectados": len(periodic_patterns),
            "faltantes": len(missing),
        },
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    out_summary = []
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        for cfg in comp.COMPANIES:
            cid = cfg["odoo_company_id"]
            log.info(f"=== {cfg['name']} ===")
            result = analyze_company(env, cid)
            result["company"] = cfg["name"]
            result["vat"] = cfg["vat"]
            target = OUT_DIR / f"{cfg['vat']}_periodic.json"
            target.write_text(json.dumps(result, ensure_ascii=False, default=str, indent=2))
            s = result["summary"]
            log.info(f"  detectados={s['total_periodicos_detectados']} faltantes={s['faltantes']} -> {target}")
            out_summary.append({
                "company": cfg["name"],
                "patterns": s["total_periodicos_detectados"],
                "missing": s["faltantes"],
            })
    print(json.dumps(out_summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
