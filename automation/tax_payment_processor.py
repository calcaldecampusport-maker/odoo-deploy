#!/usr/bin/env python3
"""
Tax payment processor — handles IRPF / SS / other AEAT payments as proper
journal entries (asientos), NOT invoices.

For irpf_payment (modelos 111, 115, 130, 190, 216):
  DR 475100 HP, acreedora por retenciones practicadas    = total
     CR 572xxx Banco                                      = total

For ss_payment (TGSS):
  DR 476000 Org. SS acreedores                            = total
     CR 572xxx Banco                                      = total

Usage:
  python3 tax_payment_processor.py --json /path/to/payment.json --pdf /path/to/.pdf --company-id N
"""
import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"
IRPF_ACCOUNT_CODE = "475100"
SS_ACCOUNT_CODE = "476000"
TOLERANCE = 0.05

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

log = logging.getLogger("tax_payment_processor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _account(env, code: str, company_id: int):
    return env["account.account"].search(
        [("code", "=", code), ("company_id", "=", company_id)], limit=1
    )


def _pick_bank_account(env, company_id: int):
    j = env["account.journal"].search(
        [("type", "=", "bank"), ("company_id", "=", company_id)], limit=1)
    if j and j.default_account_id:
        return j.default_account_id
    return env["account.account"].search(
        [("code", "=like", "572%"), ("company_id", "=", company_id)], limit=1)


def process(env, data: dict, pdf_path: Path | None, company_id: int) -> int:
    doc_type = (data.get("document_type") or "").lower()
    if doc_type not in ("irpf_payment", "ss_payment", "other_official"):
        log.error(f"unsupported doc_type: {doc_type}")
        return 10

    total = round(float(data.get("total") or 0), 2)
    if total <= 0:
        log.error(f"invalid total: {total}")
        return 10
    invoice_date = data.get("invoice_date")
    if not invoice_date:
        log.error("missing invoice_date")
        return 10
    move_date = datetime.strptime(invoice_date, "%Y-%m-%d").date()

    if doc_type == "irpf_payment":
        debit_acc = _account(env, IRPF_ACCOUNT_CODE, company_id)
        modelo = (data.get("extra") or {}).get("modelo", "")
        ejercicio = (data.get("extra") or {}).get("ejercicio", "")
        periodo = (data.get("extra") or {}).get("periodo", "")
        ref = f"IRPF mod {modelo} {ejercicio}-{periodo}".strip().rstrip("-")
        narration = f"Pago IRPF mod {modelo} ({ejercicio} {periodo}). Cancela saldo 475100."
    elif doc_type == "ss_payment":
        debit_acc = _account(env, SS_ACCOUNT_CODE, company_id)
        periodo = (data.get("extra") or {}).get("periodo", "")
        ref = f"Pago SS {periodo}".strip()
        narration = f"Pago Seguridad Social {periodo}. Cancela saldo 476000."
    else:
        debit_acc = _account(env, "629000", company_id)
        ref = data.get("invoice_ref", "Otro pago oficial")
        narration = f"Otro documento oficial: {data.get('supplier_name','')} - {ref}"

    if not debit_acc:
        log.error(f"missing debit account for {doc_type} in company {company_id}")
        return 30

    bank_acc = _pick_bank_account(env, company_id)
    if not bank_acc:
        log.error(f"no bank account in company {company_id}")
        return 30

    misc_journal = env["account.journal"].search(
        [("type", "=", "general"), ("company_id", "=", company_id)], limit=1)
    if not misc_journal:
        log.error(f"no general journal for company {company_id}")
        return 30

    if not ref:
        ref = f"{doc_type} {invoice_date}"

    existing = env["account.move"].search([
        ("ref", "=", ref), ("company_id", "=", company_id), ("move_type", "=", "entry"),
    ], limit=1)
    if existing:
        log.warning(f"duplicate ref={ref!r}, existing id={existing.id}")
        print(f"INVOICE_ID={existing.id}")
        print("DUPLICATE=1")
        return 20

    line_ids = [
        (0, 0, {
            "name": ref,
            "account_id": debit_acc.id,
            "debit": total, "credit": 0.0,
        }),
        (0, 0, {
            "name": ref,
            "account_id": bank_acc.id,
            "debit": 0.0, "credit": total,
        }),
    ]

    move_vals = {
        "move_type": "entry",
        "journal_id": misc_journal.id,
        "company_id": company_id,
        "date": move_date,
        "ref": ref,
        "narration": (
            f"{narration}\n\n"
            f"Total: {total} €\n"
            f"Confianza extraccion: {data.get('extraction_confidence', 0):.2f}"
        ),
        "line_ids": line_ids,
    }

    move = env["account.move"].with_company(company_id).create(move_vals)
    log.info(f"created {doc_type} entry id={move.id} ref={ref!r} total={total}")

    if pdf_path and pdf_path.exists():
        with open(pdf_path, "rb") as f:
            data_bytes = f.read()
        env["ir.attachment"].create({
            "name": pdf_path.name, "type": "binary", "raw": data_bytes,
            "res_model": "account.move", "res_id": move.id,
            "mimetype": "application/pdf",
        })

    try:
        move.action_post()
    except Exception as e:
        log.warning(f"auto-post failed: {e} — kept in draft")

    print(f"INVOICE_ID={move.id}")
    print(f"AMOUNT_TOTAL={total}")
    print(f"STATE={move.state}")
    print(f"COMPANY_ID={company_id}")
    print(f"DOC_TYPE={doc_type}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--json", required=True)
    p.add_argument("--pdf", required=False)
    p.add_argument("--company-id", type=int, required=True)
    args = p.parse_args()

    json_path = Path(args.json)
    pdf_path = Path(args.pdf) if args.pdf else None
    if not json_path.exists():
        return 40
    with open(json_path) as f:
        data = json.load(f)

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid", "allowed_company_ids": [args.company_id]})
        try:
            rc = process(env, data, pdf_path, args.company_id)
            if rc in (0, 20):
                cr.commit()
            else:
                cr.rollback()
            return rc
        except Exception as e:
            cr.rollback()
            log.exception("ORM error")
            print(f"ERROR={type(e).__name__}: {e}")
            return 30


if __name__ == "__main__":
    sys.exit(main())
