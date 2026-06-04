#!/usr/bin/env python3
"""
Odoo-side helper for dudas_apply.py. Reads classified actions, executes them
via ORM, writes results back as JSON.

Usage:
  python3 dudas_apply_odoo.py --input <actions.json> --output <result.json>
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
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam_test"

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

log = logging.getLogger("dudas_apply_odoo")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _account(env, code, company_id):
    return env["account.account"].search(
        [("code", "=", code), ("company_id", "=", company_id)], limit=1
    )


def _route_bank_to_account(env, bank_line, target_account, narration_extra=None):
    """Re-route the suspense line of a bank statement entry to target_account
    and reconcile (pseudo-direct entry: bank charge → expense/asset)."""
    if bank_line.is_reconciled:
        return "already reconciled"
    move = bank_line.move_id
    suspense_acc = bank_line.journal_id.suspense_account_id
    if not suspense_acc:
        return "no suspense account"
    susp = move.line_ids.filtered(lambda l: l.account_id == suspense_acc)
    if not susp:
        return "no suspense line"
    susp = susp[0]
    susp.write({"account_id": target_account.id})
    if narration_extra:
        existing = bank_line.move_id.narration or ""
        bank_line.move_id.narration = (existing + "\n" + narration_extra)[:5000]
    return None


def _partial_reconcile_with_open_aml(env, bank_line, account_codes_priority):
    """Find an open AML on one of the priority accounts whose amount is the
    closest to bank line amount; reconcile bank with it (Odoo handles partial
    automatically when amounts differ)."""
    if bank_line.is_reconciled:
        return "already reconciled", None
    target = abs(bank_line.amount)
    bank_neg = bank_line.amount < 0
    for code in account_codes_priority:
        acc = _account(env, code, bank_line.company_id.id)
        if not acc:
            continue
        amls = env["account.move.line"].search([
            ("account_id", "=", acc.id),
            ("company_id", "=", bank_line.company_id.id),
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
        ])
        if not amls:
            continue
        if bank_neg:
            amls = amls.filtered(lambda l: l.credit > 0)
        else:
            amls = amls.filtered(lambda l: l.debit > 0)
        if not amls:
            continue
        sorted_amls = sorted(amls, key=lambda a: abs(abs(a.balance) - target))
        target_aml = sorted_amls[0]
        suspense_acc = bank_line.journal_id.suspense_account_id
        susp = bank_line.move_id.line_ids.filtered(lambda l: l.account_id == suspense_acc)
        if not susp:
            return "no suspense", None
        susp[0].write({
            "account_id": target_aml.account_id.id,
            "partner_id": target_aml.partner_id.id if target_aml.partner_id else False,
        })
        try:
            (susp[0] + target_aml).reconcile()
        except Exception as e:
            return f"reconcile failed: {e}", None
        return None, target_aml
    return "no candidate aml found", None


DIRECT_ENTRY_LABELS_TO_PROPAGATE = {
    "COMISION_BANCARIA": "626000",
    "HIPOTECA": "520000",
    "GASTO_NO_DEDUCIBLE": "629000",
    "SEGURO": "625000",
    "COBRO_TPV": "430000",
    "COBRO_GYMPASS": "430000",
    "DEVOLUCION_CLIENTE": "430000",
    "COBRO_REMESA_SEPA": "430000",
}


def _derive_pattern(concept: str) -> str:
    """Extract a short reusable pattern from a bank concept (first 2 substantial words)."""
    s = (concept or "").strip()
    s = s.split("|")[0].strip()
    parts = [p for p in s.split() if len(p) >= 3 and p.lower() not in ("del", "los", "las", "una", "uno", "con", "por", "para", "que", "este", "esta")]
    if not parts:
        return ""
    return " ".join(parts[:2]).upper()


def _propagate_to_similar(env, source_bank_line, target_account, label: str, partner=None) -> dict:
    """Apply same direct-entry action to all other unreconciled bank lines that share the
    derived pattern. Also create a learned.rule so future imports auto-categorise."""
    pattern = _derive_pattern(source_bank_line.payment_ref or "")
    if not pattern or len(pattern) < 4:
        return {"propagated": 0, "rule_created": False}

    # Create / update learned.rule
    rule_created = False
    if env["ir.model"].search([("model", "=", "learned.rule")], limit=1):
        existing = env["learned.rule"].search([
            ("pattern", "=", pattern), ("rule_type", "=", "bank"),
            ("company_id", "=", source_bank_line.company_id.id),
        ], limit=1)
        if not existing:
            env["learned.rule"].create({
                "name": f"Auto-{label} - {pattern[:40]}",
                "pattern": pattern,
                "rule_type": "bank",
                "company_id": source_bank_line.company_id.id,
                "account_id": target_account.id,
                "partner_id": partner.id if partner else False,
                "source": "passive",
                "confidence": 0.9,
                "notes": f"Aprendida desde dudas_apply ({label})",
            })
            rule_created = True

    # Apply to similar unreconciled bank lines (concept contains pattern)
    candidates = env["account.bank.statement.line"].search([
        ("company_id", "=", source_bank_line.company_id.id),
        ("is_reconciled", "=", False),
    ])
    propagated = 0
    for bl in candidates:
        if bl.id == source_bank_line.id:
            continue
        ref_upper = (bl.payment_ref or "").upper()
        if pattern not in ref_upper:
            continue
        err = _route_bank_to_account(env, bl, target_account)
        if not err:
            if partner:
                susp = bl.move_id.line_ids.filtered(lambda l: l.account_id == target_account)
                if susp:
                    susp[0].partner_id = partner.id
            propagated += 1
    return {"propagated": propagated, "rule_created": rule_created}




import re as _re



def _create_vat_correction(env, action: dict) -> dict:
    """Create/update learned.rule(vat_correction) and try to fix existing partner."""
    res = {"row_index": action.get("row_index"), "archivo": action.get("archivo")}
    partner_name = (action.get("partner_name") or "").upper().strip()
    vat = (action.get("vat") or "").upper().strip()
    company_id = action.get("company_id")
    if not (partner_name and vat):
        res.update({"estado_actual": "ERROR_RECHAZO", "note": "partner/vat ausentes"})
        return res
    Rule = env["learned.rule"]
    domain = [("rule_type", "=", "vat_correction"), ("pattern", "=", partner_name)]
    if company_id:
        domain.append(("company_id", "=", company_id))
    existing = Rule.search(domain, limit=1)
    if existing:
        existing.write({"notes": vat, "confidence": 0.99})
    else:
        vals = {
            "name": f"VAT correcto {partner_name[:30]}",
            "pattern": partner_name,
            "rule_type": "vat_correction",
            "notes": vat,
            "confidence": 0.99,
            "source": "active",
        }
        if company_id:
            vals["company_id"] = company_id
        Rule.create(vals)

    # Try to update existing partner if any
    p = env["res.partner"].search([("name", "ilike", partner_name)], limit=1)
    extra = ""
    if p:
        try:
            env.cr.execute("UPDATE res_partner SET vat=%s WHERE id=%s", (vat, p.id))
            extra = f" + partner {p.name} actualizado"
        except Exception as e:
            extra = f" (no se actualizó partner: {str(e)[:60]})"
    res.update({"estado_actual": "CIF_CORREGIDO", "note": f"learned.rule guardada {partner_name}={vat}{extra}"})
    return res


def execute(env, action: dict) -> dict:
    # New: VAT correction comes from rechazo_cif processed by dudas_apply
    if action.get("action") == "create_vat_correction":
        return _create_vat_correction(env, action)
    # Skip rechazo_* actions that have no id_odoo (Drive-only ops handled upstream)
    if action.get("action", "").startswith("rechazo_") or "id_odoo" not in action:
        return {"row_index": action.get("row_index"), "estado_actual": action.get("label","SKIP"), "note": "drive op"}
    res = {"row_index": action["row_index"], "id_odoo": action["id_odoo"]}
    label = action.get("label", "")
    if label == "PENDIENTE_HUMANO" or action["action"] == "human":
        res.update({"estado_actual": "PENDIENTE_HUMANO", "note": "decision libre, no clasificada"})
        return res

    if action["action"] == "skip":
        res.update({"estado_actual": label, "note": "no requiere accion contable (cubierto en otro asiento)"})
        return res

    if action["tipo"] not in ("banco", "banco_descuadre"):
        res.update({"estado_actual": label, "note": "skip: solo bancos manejado"})
        return res

    bank_line = env["account.bank.statement.line"].browse(int(action["id_odoo"]))
    if not bank_line.exists():
        res.update({"estado_actual": "ERROR", "note": "bank line not found"})
        return res

    if action["action"] == "direct_entry":
        code = action.get("account_code", "629000")
        acc = _account(env, code, bank_line.company_id.id)
        if not acc:
            res.update({"estado_actual": "ERROR", "note": f"cuenta {code} no encontrada"})
            return res
        # If partner_name given, find or create partner
        partner_name = action.get("partner_name")
        if partner_name:
            partner = env["res.partner"].search([("name", "=", partner_name)], limit=1)
            if not partner:
                es = env.ref("base.es", raise_if_not_found=False)
                partner = env["res.partner"].create({
                    "name": partner_name, "is_company": True,
                    "customer_rank": 1,
                    "country_id": es.id if es else False,
                })
            try:
                bank_line.move_id.line_ids.filtered(lambda l: l.account_id == bank_line.journal_id.suspense_account_id).write({"partner_id": partner.id})
            except Exception:
                pass
        err = _route_bank_to_account(env, bank_line, acc, narration_extra=action.get("narration"))
        already_done = err and ("already reconciled" in err or "no suspense" in err)
        if err and not already_done:
            res.update({"estado_actual": "ERROR", "note": err})
            return res

        note_extra = ""
        if label in DIRECT_ENTRY_LABELS_TO_PROPAGATE:
            partner_obj = None
            if partner_name:
                partner_obj = env["res.partner"].search([("name", "=", partner_name)], limit=1)
            try:
                propagated = _propagate_to_similar(env, bank_line, acc, label, partner=partner_obj)
                if propagated.get("propagated", 0):
                    note_extra = f" + propagado a {propagated['propagated']} lineas similares"
                if propagated.get("rule_created"):
                    note_extra += " + regla creada"
            except Exception as e:
                log.exception(f"propagation failed: {e}")
                note_extra = f" (propagacion fallo: {str(e)[:80]})"

        prefix = "ya aplicado en pasada anterior" if already_done else f"DR {code} / CR 572 aplicado"
        res.update({"estado_actual": label, "note": f"{prefix}{note_extra}"})
        return res

    if action["action"] == "match_open_aml":
        code = action.get("account_code", "430000")
        err, aml = _partial_reconcile_with_open_aml(env, bank_line, [code])
        if err:
            ok_kw = "already reconciled" in err
            if ok_kw:
                res.update({"estado_actual": "RECONCILED_OPEN_LINE", "note": "ya reconciliado en pasada anterior"})
            else:
                res.update({"estado_actual": "ERROR", "note": err})
        else:
            res.update({"estado_actual": "RECONCILED_OPEN_LINE", "note": f"reconciliado vs {aml.move_id.name or aml.move_id.id} ({aml.account_id.code})"})
        return res

    if action["action"] == "partial_reconcile":
        err, aml = _partial_reconcile_with_open_aml(env, bank_line, ["465000", "475100", "476000", "410000", "430000"])
        if err:
            ok_kw = "already reconciled" in err
            res.update({"estado_actual": "PARTIAL_RECONCILE" if ok_kw else "ERROR", "note": err})
        else:
            res.update({"estado_actual": "PARTIAL_RECONCILE", "note": f"partial vs {aml.account_id.code} ({aml.partner_id.name or '?'})"})
        return res

    if action["action"] == "partial_reconcile_465":
        err, aml = _partial_reconcile_with_open_aml(env, bank_line, ["465000"])
        if err:
            ok_kw = "already reconciled" in err
            res.update({"estado_actual": "PARTIAL_RECONCILE" if ok_kw else "ERROR", "note": err})
        else:
            res.update({"estado_actual": "PARTIAL_RECONCILE", "note": f"vs nomina liquido ({aml.partner_id.name or '?'})"})
        return res

    if action["action"] == "confirm_proposal":
        # Parse sugerencia_actual to find the proposed move name and partial-reconcile against its open AML
        import re
        sug = (action.get("sugerencia_actual") or "")
        m = re.search(r"(FACTU[A-Z0-9/_-]+|RFACTU[A-Z0-9/_-]+|Vario/[0-9/]+|BNK1/[0-9/]+|[A-Z]+/[0-9/]+)", sug)
        if not m:
            res.update({"estado_actual": "ERROR", "note": f"no extraje move name de sugerencia: {sug[:80]}"})
            return res
        move_name = m.group(1)
        target_move = env["account.move"].search([("name", "=", move_name), ("company_id", "=", bank_line.company_id.id)], limit=1)
        if not target_move:
            res.update({"estado_actual": "ERROR", "note": f"move {move_name} no encontrado"})
            return res
        bank_neg = bank_line.amount < 0
        amls = target_move.line_ids.filtered(
            lambda l: not l.reconciled and (l.credit > 0 if bank_neg else l.debit > 0)
            and l.account_id.code in ("410000","430000","465000","475100","476000")
        )
        if not amls:
            res.update({"estado_actual": "ERROR", "note": f"{move_name} sin AML abiertos compatibles"})
            return res
        # Prefer AML whose balance closest matches the sug amount (parseable e.g. "= 430.41")
        sug_amount = None
        m_amt = re.search(r"=\s*([0-9]+(?:[.,][0-9]+)?)", sug)
        if m_amt:
            try: sug_amount = float(m_amt.group(1).replace(",", "."))
            except ValueError: pass
        # Sort: by closeness to sug_amount (if any), then by account priority
        prio = {"465000":0,"410000":1,"430000":2,"475100":3,"476000":4}
        def _key(a):
            if sug_amount is not None:
                return (abs(abs(a.balance) - sug_amount), prio.get(a.account_id.code, 9))
            return (prio.get(a.account_id.code, 9), 0)
        target_aml = sorted(amls, key=_key)[0]
        susp_acc = bank_line.journal_id.suspense_account_id
        susp = bank_line.move_id.line_ids.filtered(lambda l: l.account_id == susp_acc)
        if not susp:
            res.update({"estado_actual": "PARTIAL_RECONCILE", "note": "ya aplicado en pasada anterior"})
            return res
        susp[0].write({
            "account_id": target_aml.account_id.id,
            "partner_id": target_aml.partner_id.id if target_aml.partner_id else False,
        })
        try:
            (susp[0] + target_aml).reconcile()
        except Exception as e:
            res.update({"estado_actual": "ERROR", "note": f"reconcile falló: {str(e)[:200]}"})
            return res
        res.update({"estado_actual": "PARTIAL_RECONCILE", "note": f"confirmado vs {move_name} ({target_aml.account_id.code})"})
        return res

    if action["action"] == "smart_subida_match":
        import re
        amt = abs(bank_line.amount)
        concept = (bank_line.payment_ref or "").upper()
        # Search open in_invoices in BT with amount close (±0.05€)
        candidates = env["account.move"].search([
            ("company_id", "=", bank_line.company_id.id),
            ("move_type", "in", ["in_invoice", "in_refund"]),
            ("state", "=", "posted"),
            ("payment_state", "in", ["not_paid", "partial"]),
            ("amount_total", ">=", amt - 0.05),
            ("amount_total", "<=", amt + 0.05),
        ])
        # Filter by partner first significant word appearing in concept
        def first_token(name):
            toks = re.findall(r"[A-ZÑÁÉÍÓÚ]{4,}", (name or "").upper())
            return toks[0] if toks else ""
        matched = [c for c in candidates if first_token(c.partner_id.name) and first_token(c.partner_id.name) in concept]
        if not matched:
            matched = [c for c in candidates if any(t in concept for t in re.findall(r"[A-ZÑ]{4,}", (c.partner_id.name or "").upper()))]
        if len(matched) != 1:
            res.update({"estado_actual": "FACTURA_SUBIDA", "note": f"esperando — {len(matched)} candidatos con importe~{amt}"})
            return res
        inv = matched[0]
        oa = inv.line_ids.filtered(lambda l: l.account_id.code == "410000" and not l.reconciled)
        if not oa:
            res.update({"estado_actual": "FACTURA_SUBIDA", "note": f"{inv.name} ya reconciliada"})
            return res
        oa = oa[0]
        susp_acc = bank_line.journal_id.suspense_account_id
        susp = bank_line.move_id.line_ids.filtered(lambda l: l.account_id == susp_acc)
        if not susp:
            res.update({"estado_actual": "RECONCILED_OPEN_LINE", "note": "ya aplicado en pasada anterior"})
            return res
        susp[0].write({"account_id": oa.account_id.id, "partner_id": oa.partner_id.id})
        try:
            (susp[0] + oa).reconcile()
            res.update({"estado_actual": "RECONCILED_OPEN_LINE", "note": f"factura subida -> reconciliado vs {inv.name} ({oa.partner_id.name})"})
        except Exception as e:
            res.update({"estado_actual": "ERROR", "note": f"reconcile fallo: {str(e)[:200]}"})
        return res

        res.update({"estado_actual": "PENDIENTE_HUMANO", "note": "accion no implementada"})
    return res


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    payload = json.loads(Path(args.input).read_text())
    company_id = payload["company_id"]
    actions = payload["actions"]

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    results = []
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid", "allowed_company_ids": [company_id]})
        for a in actions:
            try:
                results.append(execute(env, a))
            except Exception as e:
                log.exception(f"row {a.get('row_index')}: {e}")
                results.append({
                    "row_index": a["row_index"],
                    "id_odoo": a["id_odoo"],
                    "estado_actual": "ERROR",
                    "note": f"exception: {e}"[:300],
                })
        cr.commit()

    Path(args.output).write_text(json.dumps(results, ensure_ascii=False, default=str))
    log.info(f"wrote {len(results)} results to {args.output}")


if __name__ == "__main__":
    main()
