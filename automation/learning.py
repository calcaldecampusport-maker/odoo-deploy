#!/usr/bin/env python3
"""
Learning module — two modes:

  --mode active   : reads aprendizajes.csv from each company's Pendientes folder
                    and imports each row as a learned.rule record (source='active').
                    The file is then moved to a 'Aprendizajes_aplicados' subfolder.

  --mode passive  : scans recent invoice lines whose account is NOT the default
                    expense account; learns "description fragment -> account"
                    rules with source='passive'.

Default with no flag = both.
"""
import argparse
import csv
import io
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"
DEFAULT_EXPENSE_CODE = "600000"

sys.path.insert(0, ODOO_PATH)
sys.path.insert(0, "/opt/automation")
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

import companies as comp  # noqa: E402

log = logging.getLogger("learning")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

CSV_FIELD_NORMALIZATIONS = {
    "nombre": "name", "name": "name",
    "patron": "pattern", "patrón": "pattern", "pattern": "pattern",
    "tipo": "rule_type", "type": "rule_type",
    "cuenta_codigo": "account_code", "cuenta_código": "account_code",
    "cuenta": "account_code", "account": "account_code",
    "partner_nombre": "partner_name", "partner": "partner_name",
    "tax_nombre": "tax_name", "tax": "tax_name", "iva": "tax_name",
    "confianza": "confidence", "confidence": "confidence",
    "notas": "notes", "notes": "notes",
}


def _normalize_row(row: dict) -> dict:
    out = {}
    for k, v in row.items():
        if k is None:
            continue
        nk = CSV_FIELD_NORMALIZATIONS.get(k.strip().lower())
        if nk:
            out[nk] = (v or "").strip() if isinstance(v, str) else v
    return out


def _resolve_rule_type(s: str) -> str:
    s = (s or "").strip().lower()
    if s in ("bank", "banco", "extracto"):
        return "bank"
    if s in ("invoice", "factura"):
        return "invoice"
    return "bank"


def _import_csv_rows(env, raw: bytes, company_id: int) -> dict:
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")

    sample = text[:1024]
    delimiter = ";"
    if sample.count(",") > sample.count(";") and sample.count(",") > 0:
        delimiter = ","

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    created = 0
    updated = 0
    skipped = 0
    errors = []

    for raw_row in reader:
        row = _normalize_row(raw_row)
        if not row.get("pattern"):
            skipped += 1
            continue
        rule_type = _resolve_rule_type(row.get("rule_type"))
        name = row.get("name") or row["pattern"][:60]

        vals = {
            "name": name,
            "pattern": row["pattern"],
            "rule_type": rule_type,
            "company_id": company_id,
            "source": "active",
            "notes": row.get("notes") or "",
        }
        if row.get("confidence"):
            try:
                vals["confidence"] = float(row["confidence"])
            except ValueError:
                pass
        if row.get("account_code"):
            acc = env["account.account"].search([
                ("code", "=", row["account_code"]),
                ("company_id", "=", company_id),
            ], limit=1)
            if acc:
                vals["account_id"] = acc.id
            else:
                errors.append(f"cuenta {row['account_code']!r} no encontrada para company {company_id}")
        if row.get("partner_name"):
            partner = env["res.partner"].search([("name", "ilike", row["partner_name"])], limit=1)
            if partner:
                vals["partner_id"] = partner.id
        if row.get("tax_name"):
            tax = env["account.tax"].search([
                ("name", "=", row["tax_name"]),
                ("company_id", "=", company_id),
            ], limit=1)
            if tax:
                vals["tax_id"] = tax.id

        existing = env["learned.rule"].search([
            ("pattern", "=", row["pattern"]),
            ("rule_type", "=", rule_type),
            ("company_id", "=", company_id),
        ], limit=1)
        if existing:
            existing.write(vals)
            updated += 1
        else:
            env["learned.rule"].create(vals)
            created += 1

    return {"created": created, "updated": updated, "skipped": skipped, "errors": errors}


def run_active(env, staging_dir: Path) -> list[dict]:
    """Read CSVs staged by learning_drive.py and import them as learned.rule rows."""
    results = []
    if not staging_dir.exists():
        log.info(f"no staging dir at {staging_dir}, skipping active mode")
        return results

    for cfg in comp.COMPANIES:
        company_dir = staging_dir / cfg["vat"]
        if not company_dir.exists():
            continue
        for f in sorted(company_dir.iterdir()):
            if not f.is_file():
                continue
            try:
                raw = f.read_bytes()
                stats = _import_csv_rows(env, raw, cfg["odoo_company_id"])
                stats["company"] = cfg["name"]
                stats["file"] = f.name
                results.append(stats)
                log.info(f"[{cfg['name']}] {f.name} -> created={stats['created']} updated={stats['updated']} skipped={stats['skipped']}")
                f.unlink()
            except Exception as e:
                log.exception(f"[{cfg['name']}] failed to import {f.name}: {e}")
                results.append({"company": cfg["name"], "file": f.name, "error": str(e)})
    return results


def run_passive(env, days: int = 7) -> list[dict]:
    """Scan invoice lines posted in the last N days. For each line whose
    account is NOT the default 600000 and has a non-empty description, create
    a passive learned.rule (or bump times_applied if exists)."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    results = []

    for cfg in comp.COMPANIES:
        cid = cfg["odoo_company_id"]
        default_acc = env["account.account"].search([
            ("code", "=", DEFAULT_EXPENSE_CODE), ("company_id", "=", cid),
        ], limit=1)
        moves = env["account.move"].search([
            ("company_id", "=", cid),
            ("move_type", "in", ["in_invoice", "in_refund"]),
            ("state", "=", "posted"),
            ("write_date", ">=", cutoff),
        ])
        learned = 0
        for m in moves:
            for line in m.invoice_line_ids:
                if not line.account_id or line.account_id == default_acc:
                    continue
                desc = (line.name or "").strip()
                if len(desc) < 5:
                    continue
                # take first 40 chars as pattern
                pattern = desc[:40]
                existing = env["learned.rule"].search([
                    ("rule_type", "=", "invoice"),
                    ("pattern", "=", pattern),
                    ("company_id", "=", cid),
                ], limit=1)
                if existing:
                    if existing.account_id != line.account_id:
                        # don't override an active-source rule
                        if existing.source == "passive":
                            existing.account_id = line.account_id
                else:
                    env["learned.rule"].create({
                        "name": f"{m.partner_id.name or '?'} — {pattern[:30]}",
                        "pattern": pattern,
                        "rule_type": "invoice",
                        "company_id": cid,
                        "account_id": line.account_id.id,
                        "partner_id": m.partner_id.id or False,
                        "source": "passive",
                        "confidence": 0.85,
                        "notes": f"Aprendido el {datetime.now().date()} de factura {m.name or 'DRAFT'}",
                    })
                    learned += 1
        results.append({"company": cfg["name"], "learned": learned})
        log.info(f"[{cfg['name']}] passive: {learned} new rules from invoices in last {days} days")
    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["active", "passive", "both"], default="both")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--staging", default="/tmp/learning")
    args = p.parse_args()

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)

    out = {}
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        if args.mode in ("active", "both"):
            out["active"] = run_active(env, Path(args.staging))
        if args.mode in ("passive", "both"):
            out["passive"] = run_passive(env, args.days)
        cr.commit()

    import json
    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
