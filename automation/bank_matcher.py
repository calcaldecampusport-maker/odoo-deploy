#!/usr/bin/env python3
"""
Propose reconciliations for unmatched bank.statement.line records.

For each open statement line in any bank journal, returns up to 3 candidate
counterparts (Odoo `account.move` invoices/bills) ordered by confidence score.

Exposed via `propose_for_company(env, company_id) -> list[dict]` for use by
email_summary.py.

Standalone CLI: print JSON of all proposals to stdout.
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

import argparse
import json
import logging
import sys
from datetime import timedelta
from difflib import SequenceMatcher

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

log = logging.getLogger("bank_matcher")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _name_similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _score(line, move, name_match: float) -> tuple[int, list[str]]:
    """Heuristic confidence score 0..100 with reasons list."""
    reasons = []
    score = 0
    line_amount = abs(line.amount)
    move_amount = abs(move.amount_residual or move.amount_total)

    # Amount
    if abs(line_amount - move_amount) < 0.01:
        score += 50
        reasons.append("importe coincide exacto")
    elif abs(line_amount - move_amount) < 1.0:
        score += 30
        reasons.append("importe casi exacto (±1€)")
    else:
        return 0, []

    # Direction
    line_neg = line.amount < 0
    if move.move_type == "in_invoice" and line_neg:
        score += 10
        reasons.append("salida bancaria + factura proveedor")
    elif move.move_type == "out_invoice" and not line_neg:
        score += 10
        reasons.append("entrada bancaria + factura cliente")

    # Partner name in concept
    if name_match >= 0.6:
        score += 25
        reasons.append(f"nombre proveedor en concepto ({name_match*100:.0f}%)")
    elif name_match >= 0.35:
        score += 12
        reasons.append(f"nombre proveedor parcial en concepto ({name_match*100:.0f}%)")

    # Invoice ref present in concept
    ref = (move.ref or "").strip()
    concept = (line.payment_ref or "").lower()
    if ref and len(ref) >= 4 and ref.lower() in concept:
        score += 15
        reasons.append("ref factura aparece en concepto")

    # Date proximity (within 60 days)
    if move.invoice_date:
        diff = abs((line.date - move.invoice_date).days)
        if diff <= 7:
            score += 5
            reasons.append("misma semana que la factura")
        elif diff <= 30:
            score += 3
            reasons.append("dentro del mes")
        elif diff > 90:
            score = max(0, score - 10)
            reasons.append("factura > 90d antes (penaliza)")

    return min(score, 100), reasons



def _is_contactless(ref: str) -> bool:
    """Lines starting with \"Transaccion Contactless\" must only match invoices 100% exactly.
    Never propose partials, near-matches, rules or open AMLs for these."""
    return (ref or "").upper().lstrip().startswith("TRANSACCION CONTACTLESS")


OPEN_LIABILITY_CODES = ("465000", "476000", "475100", "410000", "430000")


def _find_open_aml_matches(env, company_id, line_amount, line_date):
    """Find unreconciled account.move.line on key liability/receivable accounts
    whose balance matches the bank line amount within 1€ tolerance."""
    abs_amt = abs(line_amount)
    accs = env["account.account"].search([
        ("code", "in", OPEN_LIABILITY_CODES), ("company_id", "=", company_id)
    ])
    if not accs:
        return []
    candidates = env["account.move.line"].search([
        ("account_id", "in", accs.ids),
        ("company_id", "=", company_id),
        ("parent_state", "=", "posted"),
        ("reconciled", "=", False),
    ])
    out = []
    for aml in candidates:
        bal = abs(round(aml.balance, 2))
        if abs(bal - abs_amt) > 1.0:
            continue
        score = 50
        reasons = ["importe coincide en linea abierta"]
        if abs(bal - abs_amt) < 0.01:
            score = 80
            reasons = ["importe exacto en linea abierta"]
        if line_date and aml.date:
            diff = abs((line_date - aml.date).days)
            if diff <= 7:
                score += 10; reasons.append("misma semana")
            elif diff <= 30:
                score += 5; reasons.append("dentro del mes")
        if aml.partner_id:
            reasons.append(f"partner: {aml.partner_id.name}")
        out.append({
            "kind": "open_line",
            "aml_id": aml.id,
            "move_id": aml.move_id.id,
            "move_name": aml.move_id.name or "DRAFT",
            "move_type": aml.move_id.move_type,
            "partner": aml.partner_id.name or "",
            "ref": aml.move_id.ref or "",
            "amount_total": float(bal),
            "state": "posted",
            "account_code": aml.account_id.code,
            "url": f"/odoo/action-account.action_move_journal_line/{aml.move_id.id}",
            "score": min(score, 100),
            "reasons": reasons,
        })
    return out


def propose_for_company(env, company_id: int, max_lines: int = 200) -> list[dict]:
    lines = env["account.bank.statement.line"].search([
        ("company_id", "=", company_id),
        ("is_reconciled", "=", False),
    ], limit=max_lines, order="date desc")

    if not lines:
        return []

    has_rule_model = bool(env["ir.model"].search([("model", "=", "learned.rule")], limit=1))

    out = []
    for line in lines:
        contactless = _is_contactless(line.payment_ref or "")
        rule_proposal = None
        if has_rule_model and not contactless:
            rule = env["learned.rule"].find_match(line.payment_ref or "", "bank", company_id)
            if rule:
                rule_proposal = {
                    "kind": "rule",
                    "rule_id": rule.id,
                    "rule_name": rule.name,
                    "account_code": rule.account_id.code if rule.account_id else None,
                    "account_name": rule.account_id.name if rule.account_id else None,
                    "partner": rule.partner_id.name if rule.partner_id else None,
                    "score": int(round((rule.confidence or 0.95) * 100)),
                    "reasons": [f"regla aprendida: {rule.name!r}"],
                }
        line_amount = abs(line.amount)
        amount_lo = line_amount - 1.0
        amount_hi = line_amount + 1.0

        if contactless:
            # User rule: contactless TPV lines only match invoices 100% exactly,
            # not partial. Restrict the search range to ±0.005€ so partial matches
            # never appear; user reviews the proposal and approves.
            candidates = env["account.move"].search([
                ("company_id", "=", company_id),
                ("state", "=", "posted"),
                ("move_type", "in", ["in_invoice", "in_refund"]),
                ("amount_total", ">=", line_amount - 0.005),
                ("amount_total", "<=", line_amount + 0.005),
                ("payment_state", "in", ["not_paid", "partial"]),
            ], limit=10)
        else:
            candidates = env["account.move"].search([
                ("company_id", "=", company_id),
                ("state", "=", "posted"),
                ("move_type", "in", ["in_invoice", "out_invoice", "in_refund", "out_refund"]),
                ("amount_total", ">=", amount_lo),
                ("amount_total", "<=", amount_hi),
                ("payment_state", "in", ["not_paid", "partial"]),
            ], limit=20)

            # Also search drafts (not yet posted) for visibility
            draft_candidates = env["account.move"].search([
                ("company_id", "=", company_id),
                ("state", "=", "draft"),
                ("move_type", "in", ["in_invoice", "out_invoice"]),
                ("amount_total", ">=", amount_lo),
                ("amount_total", "<=", amount_hi),
            ], limit=20)
            candidates = candidates | draft_candidates

        scored = []
        for c in candidates:
            partner_name = c.partner_id.name or ""
            name_match = _name_similarity(partner_name, line.payment_ref or "")
            s, reasons = _score(line, c, name_match)
            if s > 0:
                scored.append({
                    "move_id": c.id,
                    "move_name": c.name or "DRAFT",
                    "move_type": c.move_type,
                    "partner": partner_name,
                    "ref": c.ref or "",
                    "amount_total": float(c.amount_total),
                    "state": c.state,
                    "url": f"/odoo/action-account.action_move_in_invoice_type/{c.id}",
                    "score": s,
                    "reasons": reasons,
                })

        scored.sort(key=lambda x: -x["score"])

        # Also match against open liability lines (nomina liquidos, TGSS, IRPF, proveedores)
        if contactless:
            open_line_matches = []
        else:
            open_line_matches = _find_open_aml_matches(env, company_id, line.amount, line.date)
            open_line_matches.sort(key=lambda x: -x["score"])

        proposals = []
        if rule_proposal:
            proposals.append(rule_proposal)
        proposals.extend(open_line_matches[:3])
        proposals.extend(scored[:3])

        out.append({
            "line_id": line.id,
            "date": str(line.date),
            "amount": float(line.amount),
            "concept": line.payment_ref or "",
            "journal": line.journal_id.name,
            "proposals": proposals,
        })
    return out


def find_near_matches_for_company(env, company_id: int, min_diff: float = 0.5, max_diff: float = 100.0) -> list[dict]:
    """Bank lines that ALMOST match an open AML by amount AND have partner/concept
    similarity. Used for the "diferencias pendientes" email section.

    Excludes:
      - already-reconciled bank lines
      - exact matches (diff <= min_diff)
      - bank lines that have a strong learned.rule match (>=85 score)
      - candidates without name/concept similarity (avoids "SEGUROS TUIO" matching unrelated
        FACTU/X by amount alone)
    """
    bank_lines = env["account.bank.statement.line"].search([
        ("company_id", "=", company_id),
        ("is_reconciled", "=", False),
    ])
    accs = env["account.account"].search([
        ("code", "in", OPEN_LIABILITY_CODES),
        ("company_id", "=", company_id),
    ])
    if not accs or not bank_lines:
        return []

    has_rule_model = bool(env["ir.model"].search([("model", "=", "learned.rule")], limit=1))

    out = []
    for bl in bank_lines:
        if _is_contactless(bl.payment_ref or ""):
            continue
        target = abs(bl.amount)
        if target < 1.0:
            continue

        if has_rule_model:
            rule = env["learned.rule"].find_match(bl.payment_ref or "", "bank", company_id)
            if rule and (rule.confidence or 0) >= 0.85:
                continue

        concept_l = (bl.payment_ref or "").lower()
        bank_neg = bl.amount < 0
        best = None
        for acc in accs:
            amls = env["account.move.line"].search([
                ("account_id", "=", acc.id),
                ("company_id", "=", company_id),
                ("parent_state", "=", "posted"),
                ("reconciled", "=", False),
            ])
            for aml in amls:
                if bank_neg and aml.credit <= 0:
                    continue
                if not bank_neg and aml.debit <= 0:
                    continue
                bal = abs(aml.balance)
                diff = abs(bal - target)
                if diff <= min_diff:
                    continue
                if diff > max_diff:
                    continue
                partner_name = (aml.partner_id.name or "").lower() if aml.partner_id else ""
                ref = (aml.move_id.ref or "").lower()
                name_sim = _name_similarity(partner_name, concept_l) if partner_name else 0.0
                in_concept = bool(partner_name and partner_name.split()[0] in concept_l) if partner_name else False
                ref_in = bool(ref and len(ref) >= 4 and ref in concept_l)
                if name_sim < 0.3 and not in_concept and not ref_in:
                    continue
                if best is None or diff < best["diff"]:
                    best = {
                        "diff": diff,
                        "aml_id": aml.id,
                        "aml_account": aml.account_id.code,
                        "aml_partner": aml.partner_id.name if aml.partner_id else "",
                        "aml_amount": float(bal),
                        "aml_label": (aml.name or "")[:80],
                        "aml_move_id": aml.move_id.id,
                        "aml_move_name": aml.move_id.name or "DRAFT",
                    }
        if best:
            out.append({
                "bank_line_id": bl.id,
                "bank_date": str(bl.date),
                "bank_amount": float(bl.amount),
                "bank_concept": (bl.payment_ref or "")[:80],
                "near": best,
            })
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--company-id", type=int, help="Restrict to one company. If omitted, all.")
    args = p.parse_args()

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    out = {}
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        company_ids = [args.company_id] if args.company_id else env["res.company"].search([]).ids
        for cid in company_ids:
            cname = env["res.company"].browse(cid).name
            out[cname] = propose_for_company(env, cid)
    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
