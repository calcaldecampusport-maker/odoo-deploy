#!/usr/bin/env python3
"""
Proper nomina processor — creates accounting entry with the correct Spanish
chart structure (640 / 642 / 4751 / 4760 / 465).

This entry represents the DEVENGO (recognition of payroll expense). The PAYMENT
to the employee (against 465) is a separate entry created when the bank
statement line "PAG NOMINAS" is reconciled against the 465 open line.

Asiento generado:
  DR 640000 Sueldos y salarios            = bruto (una linea por empleado)
     CR 475100 HP acreedora retenciones IRPF       = irpf_total
     CR 476000 Organismos SS acreedores             = ss_empleado_total
     CR 465000 Remuneraciones pendientes de pago    = liquido_total

NOTE: SS a cargo empresa (DR 642 / CR 476000) NO se registra aqui — eso viene
del documento TGSS aparte y se procesa por separado.

Usage:
  python3 nomina_processor.py --json /path/to/nomina.json --pdf /path/to/nomina.pdf --company-id N

Exit codes:
  0  -> created and posted OK (printed INVOICE_ID=<n>)
  10 -> validation failed
  20 -> duplicate (already exists, no action)
  30 -> ORM error
  40 -> bad input
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
SALARY_ACCOUNT_CODE = "640000"
SS_EMPRESA_ACCOUNT_CODE = "642000"  # SS a cargo empresa (incluye autónomo socios)
IRPF_ACCOUNT_CODE = "475100"
SS_ACCOUNT_CODE = "476000"
PAYABLE_EMP_ACCOUNT_CODE = "465000"
TOLERANCE = 0.05

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

log = logging.getLogger("nomina_processor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _account(env, code: str, company_id: int):
    return env["account.account"].search(
        [("code", "=", code), ("company_id", "=", company_id)], limit=1
    )


def _find_or_create_employee(env, name: str, nif: str):
    if nif:
        p = env["res.partner"].search([("vat", "=", nif)], limit=1)
        if p:
            return p
    p = env["res.partner"].search([("name", "ilike", name)], limit=1)
    if p:
        if nif and not p.vat:
            try: p.vat = nif
            except Exception: pass
        return p
    es = env.ref("base.es", raise_if_not_found=False)
    return env["res.partner"].create({
        "name": name, "vat": nif or False,
        "is_company": False, "country_id": es.id if es else False,
        "company_id": False,
    })


def process(env, data: dict, pdf_path: Path | None, company_id: int) -> int:
    extra = data.get("extra") or {}
    employees = extra.get("employees") or []

    sub = round(float(data.get("subtotal") or 0), 2)
    irpf_total = round(float(extra.get("irpf_total") or 0), 2)
    ss_total = round(float(extra.get("ss_empleado_total") or 0), 2)
    autonomo_total = round(float(extra.get("salario_especie_total") or 0), 2)
    liquido_total = round(float(extra.get("liquido_total") or data.get("total") or 0), 2)

    if employees:
        sum_brutos = round(sum(float(e.get("bruto", 0)) for e in employees), 2)
        sum_irpf = round(sum(float(e.get("irpf", 0)) for e in employees), 2)
        sum_ss = round(sum(float(e.get("ss", 0)) for e in employees), 2)
        sum_especie = round(sum(float(e.get("salario_especie", 0)) for e in employees), 2)
        sum_liq = round(sum(float(e.get("liquido", 0)) for e in employees), 2)
        if abs(sum_brutos - sub) > TOLERANCE: sub = sum_brutos
        if abs(sum_irpf - irpf_total) > TOLERANCE: irpf_total = sum_irpf
        if abs(sum_ss - ss_total) > TOLERANCE: ss_total = sum_ss
        if abs(sum_especie - autonomo_total) > TOLERANCE: autonomo_total = sum_especie
        if abs(sum_liq - liquido_total) > TOLERANCE: liquido_total = sum_liq

    if abs((sub - irpf_total - ss_total - autonomo_total) - liquido_total) > TOLERANCE:
        log.error(f"math mismatch: bruto({sub}) - irpf({irpf_total}) - ss({ss_total}) - especie({autonomo_total}) != liquido({liquido_total})")
        return 10

    invoice_date = data.get("invoice_date")
    if not invoice_date:
        log.error("missing invoice_date")
        return 10
    period = (extra.get("period") or invoice_date[:7])
    ref = f"Nomina {period} ({company_id})"

    salary_acc = _account(env, SALARY_ACCOUNT_CODE, company_id)
    ss_empresa_acc = _account(env, SS_EMPRESA_ACCOUNT_CODE, company_id)
    irpf_acc = _account(env, IRPF_ACCOUNT_CODE, company_id)
    ss_acc = _account(env, SS_ACCOUNT_CODE, company_id)
    payable_emp_acc = _account(env, PAYABLE_EMP_ACCOUNT_CODE, company_id)
    if not all([salary_acc, ss_empresa_acc, irpf_acc, ss_acc, payable_emp_acc]):
        log.error(f"missing accounts in company {company_id}")
        return 30

    misc_journal = env["account.journal"].search(
        [("type", "=", "general"), ("company_id", "=", company_id)], limit=1)
    if not misc_journal:
        log.error(f"no general journal for company {company_id}")
        return 30

    existing = env["account.move"].search([
        ("ref", "=", ref), ("company_id", "=", company_id), ("move_type", "=", "entry"),
    ], limit=1)
    if existing:
        log.warning(f"duplicate nomina ref={ref!r}, existing id={existing.id}")
        print(f"INVOICE_ID={existing.id}")
        print("DUPLICATE=1")
        return 20

    line_ids = []

    if employees:
        for emp in employees:
            partner = _find_or_create_employee(env, emp.get("name", "Empleado"), emp.get("nif", ""))
            bruto = round(float(emp.get("bruto", 0)), 2)
            especie = round(float(emp.get("salario_especie", 0)), 2)
            bruto_cash = round(bruto - especie, 2)
            if bruto_cash > 0:
                line_ids.append((0, 0, {
                    "name": f"Nomina {period} - {emp.get('name','?')} ({emp.get('nif','?')}) - Sueldo cash",
                    "partner_id": partner.id,
                    "account_id": salary_acc.id,
                    "debit": bruto_cash, "credit": 0.0,
                }))
    else:
        bruto_cash_total = round(sub - autonomo_total, 2)
        if bruto_cash_total > 0:
            line_ids.append((0, 0, {
                "name": f"Nomina {period} - sueldos brutos (agregado, sin especie)",
                "account_id": salary_acc.id,
                "debit": bruto_cash_total, "credit": 0.0,
            }))

    if irpf_total > 0:
        line_ids.append((0, 0, {
            "name": f"Retencion IRPF nomina {period}",
            "account_id": irpf_acc.id,
            "debit": 0.0, "credit": irpf_total,
        }))
    if ss_total > 0:
        line_ids.append((0, 0, {
            "name": f"SS empleado retenido {period}",
            "account_id": ss_acc.id,
            "debit": 0.0, "credit": ss_total,
        }))
    if autonomo_total > 0:
        line_ids.append((0, 0, {
            "name": f"Autonomo socios pagado por la empresa (salario en especie) {period}",
            "account_id": ss_empresa_acc.id,
            "debit": autonomo_total, "credit": 0.0,
        }))
        line_ids.append((0, 0, {
            "name": f"Cuota autonomo socios pendiente pago a TGSS {period}",
            "account_id": ss_acc.id,
            "debit": 0.0, "credit": autonomo_total,
        }))

    if employees:
        for emp in employees:
            partner = _find_or_create_employee(env, emp.get("name", "Empleado"), emp.get("nif", ""))
            liq = round(float(emp.get("liquido", 0)), 2)
            line_ids.append((0, 0, {
                "name": f"Liquido pendiente pago - {emp.get('name','?')} {period}",
                "partner_id": partner.id,
                "account_id": payable_emp_acc.id,
                "debit": 0.0, "credit": liq,
            }))
    else:
        line_ids.append((0, 0, {
            "name": f"Liquido pendiente pago empleados {period}",
            "account_id": payable_emp_acc.id,
            "debit": 0.0, "credit": liquido_total,
        }))

    move_date = datetime.strptime(invoice_date, "%Y-%m-%d").date()

    move_vals = {
        "move_type": "entry",
        "journal_id": misc_journal.id,
        "company_id": company_id,
        "date": move_date,
        "ref": ref,
        "narration": (
            f"Asiento de devengo de nomina {period}.\n"
            f"Bruto: {sub} / IRPF retenido: {irpf_total} / SS empleado: {ss_total} / Liquido: {liquido_total}\n"
            f"Empleados: {len(employees)}\n"
            f"Confianza: {data.get('extraction_confidence', 0):.2f}\n"
            f"NOTA: La SS a cargo empresa (DR 642 / CR 476) se contabiliza al recibir el documento TGSS."
        ),
        "line_ids": line_ids,
    }

    move = env["account.move"].with_company(company_id).create(move_vals)
    log.info(f"created nomina account.move id={move.id} bruto={sub} liquido={liquido_total}")

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
        log.info(f"posted move id={move.id}")
    except Exception as e:
        log.warning(f"auto-post failed: {e} — kept in draft")

    print(f"INVOICE_ID={move.id}")
    print(f"AMOUNT_TOTAL={liquido_total}")
    print(f"STATE={move.state}")
    print(f"COMPANY_ID={company_id}")
    print(f"DOC_TYPE=nomina")
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
        log.error(f"JSON not found: {json_path}")
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
