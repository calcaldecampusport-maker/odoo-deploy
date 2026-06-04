#!/usr/bin/env python3
"""
Nomina processor — asiento contable con la nueva especificación del usuario:

DR 640    Total devengo (suma columna devengo de todos los trabajadores)
DR 642    SS empresa (aportaciones_empresa_total: CC empresa + AT + FOGASA + formación + desempleo)
        CR 4751   Retención IRPF (suma)
        CR 476    Total a TGSS (= aport_empresa + ss_empleado_total + especie_socio)
        CR 465.NNN Líquido por trabajador (subcuenta única por empleado, partner_id=empleado)

El salario_especie de socios autónomos queda implícito en bruto del 640 y aparece
en el CR 476 como cuota autónomo que la empresa paga a TGSS en nombre del socio.
NO se duplica en el DR 642 (eso descuadraría el asiento por el importe del especie).

Validaciones:
- Math per empleado: bruto - irpf - ss_empleado - salario_especie = liquido
- Validación adicional: total_devengo == sum(base_contingencias_comunes)
  (si no coincide → AVISO en narration y log, NO aborta)

Subcuenta 465 por trabajador:
- Busca cuenta 465* cuyo nombre contenga el NIF del empleado
- Si no existe, crea una nueva con código 465NNN (NNN incremental, primer libre)
- Reutilizable: la próxima nómina del mismo empleado usa la misma subcuenta
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

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam_test"

SALARY_ACCOUNT_CODE = "640000"
SS_EMPRESA_ACCOUNT_CODE = "642000"
IRPF_ACCOUNT_CODE = "475100"
SS_ACCOUNT_CODE = "476000"
PAYABLE_EMP_BASE_CODE = "465"        # subcuentas 465NNN por empleado
PAYABLE_EMP_FALLBACK = "465000"      # cuenta genérica si no hay NIF
TOLERANCE = 0.05

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

log = logging.getLogger("nomina_processor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


# Tipos cotización empresa 2026 (Orden ISM/118/2026, actualizable anualmente)
TIPO_CC_EMPRESA = 0.2435            # Contingencias Comunes empresa
TIPO_DESEMPLEO_INDEF = 0.0550       # Desempleo contrato indefinido
TIPO_DESEMPLEO_TEMP = 0.0670        # Desempleo contrato temporal
TIPO_FP_EMPRESA = 0.0060            # Formación profesional empresa
TIPO_FOGASA = 0.0020                # Fondo Garantía Salarial
TIPO_MEI_EMPRESA = 0.0067           # Mecanismo Equidad Intergeneracional empresa
TOL_VALIDATION = 0.05               # ± 5 céntimos
TOL_DEVENGO_PCT = 0.02              # 2% diff devengo vs BCCC (permite dietas exentas)


def _run_nomina_ss_validations(extra: dict, employees: list) -> tuple[list[str], list[str]]:
    """Ejecuta 10 reglas de validación sobre la nómina española.
    Devuelve (errores, avisos). Ambos se anotan en la narración del asiento;
    errores marcan como crítico para que el usuario los revise.

    Reglas:
    1. BCCC empleado == BCCC empresa (LGSS art 147 — base única)
    2. BCCC ≈ total devengo ± 2% (permite conceptos exentos: dietas, km)
    3. Base AT/EP ≈ BCCC (deben coincidir salvo horas extras estructurales)
    4. Cuota CC empresa = BCCC × 24,35%
    5. Cuota desempleo empresa = BCCC × 5,50% (indef) o × 6,70% (temp)
    6. Cuota FP empresa = BCCC × 0,60%
    7. Cuota FOGASA = BCCC × 0,20%
    8. (skip) Topes BCCC por grupo cotización — depende de tablas anuales
    9. (skip) Jornada parcial coherente — necesita datos de contrato
    10. Σ cuotas empresa calculadas ≈ aportaciones_empresa_total declarado ± 0,10€
    """
    errors, warnings = [], []
    aport_total = round(float(extra.get("aportaciones_empresa_total") or 0), 2)
    sum_cuotas_calc = 0.0

    for emp in employees:
        name = emp.get("name", "?")
        bccc_t = round(float(emp.get("base_contingencias_comunes") or 0), 2)
        bccc_e = round(float(emp.get("base_cc_empresa") or 0), 2)
        base_at = round(float(emp.get("base_at_ep") or 0), 2)
        devengo = round(float(emp.get("bruto") or 0), 2)
        cuota_cc = round(float(emp.get("cuota_cc_empresa") or 0), 2)
        cuota_at = round(float(emp.get("cuota_at_empresa") or 0), 2)
        cuota_des = round(float(emp.get("cuota_desempleo_empresa") or 0), 2)
        cuota_fp = round(float(emp.get("cuota_fp_empresa") or 0), 2)
        cuota_fog = round(float(emp.get("cuota_fogasa_empresa") or 0), 2)
        tipo_contrato = (emp.get("tipo_contrato") or "indefinido").lower()

        # Regla 1 — BCCC empleado == BCCC empresa  (CRÍTICA)
        if bccc_t and bccc_e and abs(bccc_t - bccc_e) > TOL_VALIDATION:
            errors.append(
                f"[R1] {name}: BCCC trabajador ({bccc_t:.2f}) ≠ BCCC empresa ({bccc_e:.2f}). "
                f"Deben coincidir (LGSS 147). Posible error gestor; la empresa sobrepaga "
                f"~{(bccc_e - bccc_t) * (TIPO_CC_EMPRESA + TIPO_DESEMPLEO_INDEF + TIPO_FP_EMPRESA + TIPO_FOGASA):.2f}€"
            )

        # Regla 2 — BCCC ≈ total devengo ± 2%
        if bccc_t and devengo:
            tol = max(devengo * TOL_DEVENGO_PCT, 1.0)
            if abs(bccc_t - devengo) > tol:
                warnings.append(
                    f"[R2] {name}: BCCC ({bccc_t:.2f}) difiere del devengo ({devengo:.2f}) "
                    f"en {bccc_t - devengo:+.2f} (>2%). Revisar conceptos exentos (dietas, km) o error"
                )

        # Regla 3 — Base AT/EP ≈ BCCC
        if base_at and bccc_t and abs(base_at - bccc_t) > TOL_VALIDATION:
            warnings.append(
                f"[R3] {name}: base AT/EP ({base_at:.2f}) ≠ BCCC ({bccc_t:.2f}); "
                f"normal sólo si hay horas extras estructurales"
            )

        # Regla 4 — Cuota CC empresa = BCCC × 24,35%
        if bccc_e and cuota_cc:
            exp = round(bccc_e * TIPO_CC_EMPRESA, 2)
            if abs(cuota_cc - exp) > TOL_VALIDATION:
                errors.append(f"[R4] {name}: cuota CC empresa aplicada {cuota_cc:.2f} ≠ esperada {exp:.2f} (BCCC × 24,35%)")

        # Regla 5 — Cuota desempleo empresa
        if bccc_e and cuota_des:
            tipo = TIPO_DESEMPLEO_TEMP if "temp" in tipo_contrato else TIPO_DESEMPLEO_INDEF
            exp = round(bccc_e * tipo, 2)
            if abs(cuota_des - exp) > TOL_VALIDATION:
                warnings.append(
                    f"[R5] {name}: cuota desempleo {cuota_des:.2f} ≠ esperada {exp:.2f} "
                    f"({tipo*100:.2f}% sobre BCCC; contrato={tipo_contrato})"
                )

        # Regla 6 — Cuota FP empresa
        if bccc_e and cuota_fp:
            exp = round(bccc_e * TIPO_FP_EMPRESA, 2)
            if abs(cuota_fp - exp) > TOL_VALIDATION:
                warnings.append(f"[R6] {name}: cuota FP {cuota_fp:.2f} ≠ esperada {exp:.2f} (×0,60%)")

        # Regla 7 — Cuota FOGASA
        if bccc_e and cuota_fog:
            exp = round(bccc_e * TIPO_FOGASA, 2)
            if abs(cuota_fog - exp) > TOL_VALIDATION:
                warnings.append(f"[R7] {name}: cuota FOGASA {cuota_fog:.2f} ≠ esperada {exp:.2f} (×0,20%)")

        sum_cuotas_calc += cuota_cc + cuota_at + cuota_des + cuota_fp + cuota_fog

    # Regla 10 — Σ cuotas calculadas ≈ aportaciones_empresa_total declarado
    if sum_cuotas_calc and aport_total:
        if abs(sum_cuotas_calc - aport_total) > 0.10:
            warnings.append(
                f"[R10] Σ cuotas empresa calculadas ({sum_cuotas_calc:.2f}) ≠ "
                f"aport_empresa_total declarado ({aport_total:.2f}), diff {sum_cuotas_calc - aport_total:+.2f}"
            )

    return errors, warnings


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


def _get_or_create_465_for_employee(env, partner, company_id: int):
    """Subcuenta 465NNN del empleado. Reutilizable por NIF."""
    nif = (partner.vat or "").replace("ES", "").strip()
    # 1) Buscar por NIF en nombre de la cuenta
    if nif:
        acc = env["account.account"].search([
            ("code", "=like", f"{PAYABLE_EMP_BASE_CODE}%"),
            ("company_id", "=", company_id),
            ("name", "ilike", nif),
        ], limit=1)
        if acc:
            return acc
    # 2) Crear nueva con código siguiente disponible 465NNN
    existing = env["account.account"].search([
        ("code", "=like", f"{PAYABLE_EMP_BASE_CODE}%"),
        ("company_id", "=", company_id),
    ])
    used_codes = {a.code for a in existing}
    next_n = 1
    while f"{PAYABLE_EMP_BASE_CODE}{next_n:03d}" in used_codes:
        next_n += 1
    new_code = f"{PAYABLE_EMP_BASE_CODE}{next_n:03d}"
    parent = _account(env, PAYABLE_EMP_FALLBACK, company_id)
    acc_type = parent.account_type if parent else "liability_current"
    name = f"Liquido pdte pago {partner.name}"
    if nif:
        name += f" ({nif})"
    new_acc = env["account.account"].create({
        "code": new_code,
        "name": name[:64],
        "account_type": acc_type,
        "company_id": company_id,
        "reconcile": True,
    })
    log.info(f"  creada subcuenta {new_code} para {partner.name}")
    return new_acc


def process(env, data: dict, pdf_path: Path | None, company_id: int) -> int:
    extra = data.get("extra") or {}
    employees = extra.get("employees") or []

    # Totales del PDF
    sub = round(float(data.get("subtotal") or 0), 2)                            # total devengo
    irpf_total = round(float(extra.get("irpf_total") or 0), 2)
    ss_total = round(float(extra.get("ss_empleado_total") or 0), 2)
    autonomo_total = round(float(extra.get("salario_especie_total") or 0), 2)
    aport_empresa = round(float(extra.get("aportaciones_empresa_total") or 0), 2)
    base_cc_total = round(float(extra.get("base_contingencias_comunes_total") or 0), 2)
    liquido_total = round(float(extra.get("liquido_total") or data.get("total") or 0), 2)

    notas_warning = []

    # Reconciliar totales con la suma per-empleado si hay desviación
    if employees:
        sum_brutos = round(sum(float(e.get("bruto", 0)) for e in employees), 2)
        sum_irpf = round(sum(float(e.get("irpf", 0)) for e in employees), 2)
        sum_ss = round(sum(float(e.get("ss", 0)) for e in employees), 2)
        sum_especie = round(sum(float(e.get("salario_especie", 0)) for e in employees), 2)
        sum_liq = round(sum(float(e.get("liquido", 0)) for e in employees), 2)
        sum_base_cc = round(sum(float(e.get("base_contingencias_comunes", 0) or e.get("bruto", 0)) for e in employees), 2)
        if abs(sum_brutos - sub) > TOLERANCE: sub = sum_brutos
        if abs(sum_irpf - irpf_total) > TOLERANCE: irpf_total = sum_irpf
        if abs(sum_ss - ss_total) > TOLERANCE: ss_total = sum_ss
        if abs(sum_especie - autonomo_total) > TOLERANCE: autonomo_total = sum_especie
        if abs(sum_liq - liquido_total) > TOLERANCE: liquido_total = sum_liq
        if base_cc_total == 0:
            base_cc_total = sum_base_cc

    # Validación matemática per empleado
    if abs((sub - irpf_total - ss_total - autonomo_total) - liquido_total) > TOLERANCE:
        log.error(f"math mismatch: devengo({sub}) - irpf({irpf_total}) - ss({ss_total}) - especie({autonomo_total}) != liquido({liquido_total})")
        return 10

    # Validación devengo vs base contingencias comunes (aviso, no error)
    if base_cc_total and abs(base_cc_total - sub) > TOLERANCE:
        msg = f"AVISO: total devengo ({sub:.2f}) NO coincide con base contingencias comunes ({base_cc_total:.2f}), diff {sub - base_cc_total:+.2f}"
        log.warning(msg)
        notas_warning.append(msg)

    # === Validaciones SS exhaustivas (10 reglas) ===
    ss_errors, ss_warnings = _run_nomina_ss_validations(extra, employees)
    for e in ss_errors:
        log.error(e)
        notas_warning.append("❌ " + e)
    for w in ss_warnings:
        log.warning(w)
        notas_warning.append("⚠ " + w)

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
    if not all([salary_acc, ss_empresa_acc, irpf_acc, ss_acc]):
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

    # === Construir asiento ===
    line_ids = []

    # DR 640 — total devengo agregado
    if sub > 0:
        line_ids.append((0, 0, {
            "name": f"Nomina {period} - Total devengo ({len(employees) or 1} empleados)",
            "account_id": salary_acc.id,
            "debit": sub, "credit": 0.0,
        }))

    # DR 642 — SS empresa SOLO (aportación empresa total: CC empresa + AT + FOGASA + formación + desempleo)
    # NO incluye salario_especie: el especie va a CR 476 (cuota autónomo socio a TGSS) y queda
    # implícito en bruto del DR 640
    dr_642 = round(aport_empresa, 2)
    if dr_642 > 0:
        line_ids.append((0, 0, {
            "name": f"Nomina {period} - SS empresa (aportación total)",
            "account_id": ss_empresa_acc.id,
            "debit": dr_642, "credit": 0.0,
        }))

    # CR 4751 — IRPF total
    if irpf_total > 0:
        line_ids.append((0, 0, {
            "name": f"Retencion IRPF nomina {period}",
            "account_id": irpf_acc.id,
            "debit": 0.0, "credit": irpf_total,
        }))

    # CR 476 — total a pagar TGSS (aport_empresa + ss_empleado + autonomo)
    cr_476 = round(aport_empresa + ss_total + autonomo_total, 2)
    if cr_476 > 0:
        line_ids.append((0, 0, {
            "name": f"A pagar TGSS nomina {period}",
            "account_id": ss_acc.id,
            "debit": 0.0, "credit": cr_476,
        }))

    # CR 465.NNN — líquido por trabajador (subcuenta única por empleado)
    if employees:
        for emp in employees:
            partner = _find_or_create_employee(env, emp.get("name", "Empleado"), emp.get("nif", ""))
            acc_465 = _get_or_create_465_for_employee(env, partner, company_id)
            liq = round(float(emp.get("liquido", 0)), 2)
            if liq > 0:
                line_ids.append((0, 0, {
                    "name": f"Liquido {emp.get('name','?')} {period}",
                    "partner_id": partner.id,
                    "account_id": acc_465.id,
                    "debit": 0.0, "credit": liq,
                }))
    else:
        fallback = _account(env, PAYABLE_EMP_FALLBACK, company_id)
        if fallback and liquido_total > 0:
            line_ids.append((0, 0, {
                "name": f"Liquido pdte pago empleados {period}",
                "account_id": fallback.id,
                "debit": 0.0, "credit": liquido_total,
            }))

    # Verificación de cuadre antes de postear
    total_dr = round(sum(l[2].get("debit", 0) for l in line_ids), 2)
    total_cr = round(sum(l[2].get("credit", 0) for l in line_ids), 2)
    if abs(total_dr - total_cr) > TOLERANCE:
        log.error(f"asiento desbalanceado DR={total_dr} CR={total_cr} diff={total_dr - total_cr:+.2f}")
        log.error(f"  inputs: devengo={sub} aport_empresa={aport_empresa} ss_empleado={ss_total} irpf={irpf_total} autonomo={autonomo_total} liquido={liquido_total}")
        return 10

    narration_parts = [
        f"Asiento de devengo de nomina {period}.",
        f"Devengo: {sub} / IRPF retenido: {irpf_total} / SS empleado: {ss_total} / "
        f"SS empresa: {aport_empresa} / Autonomo socios: {autonomo_total} / Liquido: {liquido_total}",
        f"Empleados: {len(employees)}",
        f"Confianza: {data.get('extraction_confidence','?')}",
    ]
    if notas_warning:
        narration_parts.append("AVISOS:")
        narration_parts.extend(notas_warning)
    narration_parts.append("Asiento: DR 640+642 == CR 4751+476+sum(465.NNN)")
    narration = "\n".join(narration_parts)

    move = env["account.move"].with_company(company_id).create({
        "move_type": "entry",
        "ref": ref,
        "date": invoice_date,
        "journal_id": misc_journal.id,
        "company_id": company_id,
        "narration": narration,
        "line_ids": line_ids,
    })

    # Adjuntar PDF
    if pdf_path and pdf_path.exists():
        with open(pdf_path, "rb") as f:
            import base64
            env["ir.attachment"].create({
                "name": pdf_path.name,
                "datas": base64.b64encode(f.read()).decode(),
                "res_model": "account.move",
                "res_id": move.id,
                "company_id": company_id,
            })

    try:
        move.action_post()
    except Exception as e:
        log.exception(f"action_post failed: {e}")
        return 30

    log.info(f"created nomina move id={move.id} ref={ref} total devengo {sub} ({len(employees)} empleados)")
    print(f"INVOICE_ID={move.id}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--json", required=True)
    p.add_argument("--pdf")
    p.add_argument("--company-id", type=int, required=True)
    args = p.parse_args()

    json_path = Path(args.json)
    pdf_path = Path(args.pdf) if args.pdf else None
    if not json_path.exists():
        log.error(f"no json at {json_path}"); return 40
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        log.exception(f"bad json: {e}"); return 40

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"allowed_company_ids": [args.company_id]})
        rc = process(env, data, pdf_path, args.company_id)
        if rc == 0:
            cr.commit()
    return rc


if __name__ == "__main__":
    sys.exit(main() or 0)
