#!/usr/bin/env python3
"""
Collects current dudas from Odoo and writes one JSON per company to
/tmp/dudas_austral/<vat>_dudas.json. Pairs with dudas_xlsx_publish.py which uploads
to Drive (split because of google-libs/pyOpenSSL conflict in Odoo venv).
"""
# === pipeline isolation guard (auto-injected) ===
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
try:
    import companies as _comp_guard
    if getattr(_comp_guard, "PIPELINE_NAME", None) != 'austral':
        raise RuntimeError(
            f"PIPELINE_MISMATCH: script {__file__} expected pipeline='austral' "
            f"but loaded companies.PIPELINE_NAME={getattr(_comp_guard, 'PIPELINE_NAME', None)!r}"
        )
except ImportError:
    pass  # script sin dependencia de companies.py (e.g. drive_ops)
# === end isolation guard ===

import json
import logging
import re
import sys
from datetime import date
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam_test"
OUTPUT_DIR = Path("/tmp/dudas_austral")

sys.path.insert(0, ODOO_PATH)
sys.path.insert(0, _HERE)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

import companies as comp  # noqa: E402
import bank_matcher  # noqa: E402

log = logging.getLogger("dudas_collect")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _confidence(narr: str):
    if not narr:
        return None
    m = re.search(r"confianza[:\s]+([0-9]+\.?[0-9]*)", narr.lower())
    if not m:
        return None
    try: return float(m.group(1))
    except ValueError: return None


def _invoice_dudas(env, cid: int) -> list[dict]:
    drafts = env["account.move"].search([
        ("company_id", "=", cid),
        ("move_type", "in", ["in_invoice", "in_refund"]),
        ("state", "=", "draft"),
    ])
    out = []
    for m in drafts:
        conf = _confidence(m.narration or "")
        reasons = []
        if conf is not None and conf < 0.9: reasons.append(f"confianza {conf:.2f}<0.9")
        if m.amount_total < 0: reasons.append("importe negativo (rectificativa?)")
        if not m.invoice_line_ids: reasons.append("sin lineas")
        if not reasons: reasons.append("revision general")
        codes = ",".join(sorted({l.account_id.code for l in m.invoice_line_ids if l.account_id}))
        out.append({
            "tipo": "factura", "id_odoo": m.id,
            "ref_o_concepto": m.ref or "",
            "fecha": str(m.invoice_date) if m.invoice_date else "",
            "importe": round(m.amount_total, 2),
            "descripcion_corta": (m.partner_id.name or "")[:80],
            "motivo_duda": "; ".join(reasons),
            "sugerencia_actual": f"cuenta lineas: {codes}" if codes else "",
            "estado_actual": m.state,
            "notas": (m.narration or "").replace("\n", " ").replace("\r", " ")[:300],
        })
    return out


def _bank_dudas(env, cid: int) -> list[dict]:
    out = []
    for r in bank_matcher.propose_for_company(env, cid):
        proposals = r["proposals"] or []

        # Skip if any proposal is clearly "resolvable":
        if any(p.get("score", 0) >= 90 for p in proposals):
            continue
        if any(p.get("kind") == "rule" and p.get("score", 0) >= 85 for p in proposals):
            continue
        if any(p.get("kind") != "rule" and p.get("score", 0) >= 75 for p in proposals):
            continue

        non_rule = [x for x in proposals if x.get("kind") != "rule"]
        top = non_rule[0] if non_rule else None
        sugg = "sin candidatos" if not top else f"{top.get('score',0)}% {top.get('rule_name') or top.get('move_name','?')} {top.get('partner','')}"
        reason = "multiples candidatos" if len(non_rule) > 1 else ("sin propuesta" if not top else "candidato unico bajo umbral")
        out.append({
            "tipo": "banco", "id_odoo": r["line_id"],
            "ref_o_concepto": (r["concept"] or "")[:100],
            "fecha": r["date"], "importe": r["amount"],
            "descripcion_corta": r["journal"],
            "motivo_duda": reason, "sugerencia_actual": sugg,
            "estado_actual": "no_conciliado", "notas": "",
        })
    rule_model_present = bool(env["ir.model"].search([("model", "=", "learned.rule")], limit=1))
    for r in bank_matcher.find_near_matches_for_company(env, cid):
        n = r["near"]
        # Already filtered by partner-similarity in matcher, but double-skip if
        # the bank line has a strong learned rule (will be auto-categorised)
        if rule_model_present:
            bl = env["account.bank.statement.line"].browse(r["bank_line_id"])
            rule = env["learned.rule"].find_match(bl.payment_ref or "", "bank", cid)
            if rule and (rule.confidence or 0) >= 0.85:
                continue
        out.append({
            "tipo": "banco_descuadre", "id_odoo": r["bank_line_id"],
            "ref_o_concepto": (r["bank_concept"] or "")[:100],
            "fecha": r["bank_date"], "importe": r["bank_amount"],
            "descripcion_corta": f"{n['aml_account']} {n['aml_partner']}"[:80],
            "motivo_duda": f"diferencia {n['diff']:+.2f}€ con apunte abierto",
            "sugerencia_actual": f"apunte {n['aml_move_name']} = {n['aml_amount']:.2f}",
            "estado_actual": "descuadrado",
            "notas": (n['aml_label'] or "")[:300],
        })
    return out


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    today = date.today().isoformat()
    out_summary = []

    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        for cfg in comp.COMPANIES:
            cid = cfg["odoo_company_id"]
            rows = _invoice_dudas(env, cid) + _bank_dudas(env, cid)
            for r in rows:
                r["empresa"] = cfg["name"]
            payload = {
                "company": cfg["name"],
                "vat": cfg["vat"],
                "pending_folder": cfg.get("pending_folder"),
                "today": today,
                "rows": rows,
            }
            target = OUTPUT_DIR / f"{cfg['vat']}_dudas.json"
            target.write_text(json.dumps(payload, ensure_ascii=False, default=str))
            out_summary.append({"company": cfg["name"], "rows": len(rows), "path": str(target)})
            log.info(f"[{cfg['name']}] {len(rows)} rows -> {target}")

    print(json.dumps(out_summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
