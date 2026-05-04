#!/usr/bin/env python3
"""
Generate dudas_para_revisar.csv with all items requiring human decision.

Sources:
  - draft invoices (confidence < 0.9) — possibly wrong account/partner/IVA
  - bank statement lines unreconciled with no high-confidence rule match
  - invoices that auto-posting failed for (e.g. negative totals)

Output: per-company CSV uploaded to "Dudas" subfolder of each Pendientes folder.

Each row format (user fills column "tu_decision"):
  empresa, tipo, ref, fecha, importe, descripcion_corta, motivo_duda,
  sugerencia_actual, tu_decision, notas
"""
import argparse
import csv
import io
import json
import logging
import re
import sys
import tempfile
from datetime import date
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"

sys.path.insert(0, ODOO_PATH)
sys.path.insert(0, "/opt/automation")
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

import companies as comp  # noqa: E402
import bank_matcher  # noqa: E402

log = logging.getLogger("dudas")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CSV_HEADER = [
    "empresa", "tipo", "id_odoo", "ref_o_concepto", "fecha",
    "importe", "descripcion_corta", "motivo_duda",
    "sugerencia_actual", "tu_decision", "notas",
]


def _confidence_from_narration(narr: str) -> float | None:
    if not narr:
        return None
    m = re.search(r"confianza[:\s]+([0-9]+\.?[0-9]*)", narr.lower())
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def collect_doubts_for_company(env, cfg: dict) -> list[dict]:
    cid = cfg["odoo_company_id"]
    doubts = []

    # 1. Draft invoices (auto-post threshold not met OR auto-post failed)
    drafts = env["account.move"].search([
        ("company_id", "=", cid),
        ("move_type", "in", ["in_invoice", "in_refund"]),
        ("state", "=", "draft"),
    ])
    for m in drafts:
        conf = _confidence_from_narration(m.narration or "")
        reason_bits = []
        if conf is not None and conf < 0.9:
            reason_bits.append(f"confianza extraccion {conf:.2f}<0.9")
        if m.amount_total < 0:
            reason_bits.append("importe negativo (puede ser factura rectificativa)")
        # detect VAT mismatch
        if m.partner_id and m.partner_id.vat:
            from urllib.parse import unquote
            pass  # no extra check for now
        if not m.invoice_line_ids:
            reason_bits.append("sin lineas")
        if not reason_bits:
            reason_bits.append("revision general")
        sugg = f"cuenta lineas: {','.join(set(l.account_id.code for l in m.invoice_line_ids if l.account_id))}"
        doubts.append({
            "tipo": "factura",
            "id_odoo": m.id,
            "ref_o_concepto": m.ref or "",
            "fecha": str(m.invoice_date) if m.invoice_date else "",
            "importe": round(m.amount_total, 2),
            "descripcion_corta": (m.partner_id.name or "")[:60],
            "motivo_duda": "; ".join(reason_bits),
            "sugerencia_actual": sugg,
            "tu_decision": "",
            "notas": (m.narration or "").replace("\n", " ")[:200],
        })

    # 2. Unreconciled bank lines without strong proposals
    proposals = bank_matcher.propose_for_company(env, cid)
    for p in proposals:
        top = p["proposals"][0] if p["proposals"] else None
        if top and top.get("score", 0) >= 90 and top.get("kind") == "rule":
            continue  # rule with high score => good, skip
        if top and top.get("score", 0) >= 95:
            continue  # heuristic 95%+ => good, skip
        sugg = "sin candidatos" if not top else f"{top.get('score',0)}% {top.get('rule_name') or top.get('move_name','?')}"
        reason = "rule baja" if (top and top.get("kind") == "rule") else \
                 ("multiples candidatos" if len(p["proposals"]) > 1 else
                  ("sin propuesta" if not top else "candidato unico bajo umbral"))
        doubts.append({
            "tipo": "banco",
            "id_odoo": p["line_id"],
            "ref_o_concepto": (p["concept"] or "")[:80],
            "fecha": p["date"],
            "importe": p["amount"],
            "descripcion_corta": p["journal"],
            "motivo_duda": reason,
            "sugerencia_actual": sugg,
            "tu_decision": "",
            "notas": "",
        })

    return doubts


def write_csv(rows: list[dict]) -> bytes:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_HEADER, delimiter=";", extrasaction="ignore",
                            quoting=csv.QUOTE_MINIMAL)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 right


def write_to_drive(svc, folder_id: str, filename: str, content: bytes) -> str:
    """Workaround for SA storage quota: upload via odoo's ir.attachment? No.
    Better: write to /tmp and tell user the path. Or store in Odoo as ir.attachment.
    Here we save locally and return the path."""
    out = Path("/tmp/dudas") / filename
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(content)
    return str(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--upload", action="store_true", help="Try to upload to Drive (may fail with SA quota)")
    args = p.parse_args()

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)

    today = date.today().isoformat()
    overall = {}
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        for cfg in comp.COMPANIES:
            doubts = collect_doubts_for_company(env, cfg)
            for r in doubts:
                r["empresa"] = cfg["name"]
            content = write_csv(doubts)
            fname = f"dudas_{cfg['vat']}_{today}.csv"
            path = write_to_drive(None, cfg["queue_folder"], fname, content)
            overall[cfg["name"]] = {
                "count": len(doubts),
                "path": path,
                "filename": fname,
            }
            log.info(f"[{cfg['name']}] {len(doubts)} doubts -> {path}")

    print(json.dumps(overall, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
