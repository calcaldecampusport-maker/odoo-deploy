#!/usr/bin/env python3
"""
1:N bank reconciler — reconciles a single bank.statement.line against multiple
open account.move.line records on aggregating accounts.

Use case: AEAT pays IRPF quarterly aggregating multiple months. TGSS aggregates
SS payments. Each bank charge corresponds to several monthly accruals on
475100/476000.

Algorithm:
  For each unreconciled bank line in the company:
    For each AGGREGATING_ACCOUNT (475100, 476000, 465000, 410000):
      Get all open AMLs on that account
      Find a subset whose abs(sum balance) ≈ abs(bank line amount) within tolerance
      If unique subset found, re-route bank suspense to that account and
      reconcile (bank_susp + subset).

Usage:
  python3 bank_multi_reconciler.py [--company-id N] [--threshold-eur 1.0] [--dry-run]
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
import itertools
import json
import logging
import sys
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"

AGGREGATING_ACCOUNT_CODES = ("475100", "476000", "465000", "410000", "430000")
DEFAULT_TOLERANCE = 0.1
MAX_SUBSET_SIZE = 12

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

log = logging.getLogger("bank_multi_reconciler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _find_subset(amls_with_amounts, target: float, tolerance: float):
    """Find a subset of amls whose summed amount ≈ target within tolerance.
    Returns list of AMLs (ids) or None. Picks shortest-first.
    Each entry is (aml, signed_amount)."""
    n = len(amls_with_amounts)
    if n == 0:
        return None
    # Direct single match
    for aml, amt in amls_with_amounts:
        if abs(amt - target) <= tolerance:
            return [aml]
    if n > MAX_SUBSET_SIZE:
        amls_with_amounts = amls_with_amounts[:MAX_SUBSET_SIZE]
        n = MAX_SUBSET_SIZE
    for k in range(2, n + 1):
        best = None
        for combo in itertools.combinations(amls_with_amounts, k):
            s = sum(amt for _, amt in combo)
            if abs(s - target) <= tolerance:
                if best is None or len(combo) < len(best):
                    best = combo
                    break
        if best:
            return [aml for aml, _ in best]
    return None


def reconcile_bank_against_amls(env, bank_line, target_amls) -> str | None:
    if bank_line.is_reconciled:
        return "already reconciled"
    if not target_amls:
        return "empty target"
    target_acc = target_amls[0].account_id
    if any(a.account_id != target_acc for a in target_amls):
        return "amls not on same account"

    bank_move = bank_line.move_id
    suspense_acc = bank_line.journal_id.suspense_account_id
    if not suspense_acc:
        return "no suspense account"
    susp_aml = bank_move.line_ids.filtered(lambda l: l.account_id == suspense_acc)
    if not susp_aml:
        return "no suspense line on bank move"
    susp_aml = susp_aml[0]

    common_partner = None
    partners = {a.partner_id.id for a in target_amls if a.partner_id}
    if len(partners) == 1:
        common_partner = list(partners)[0]

    susp_aml.write({
        "account_id": target_acc.id,
        "partner_id": common_partner or False,
    })

    amls_to_reconcile = susp_aml
    for a in target_amls:
        amls_to_reconcile += a
    try:
        amls_to_reconcile.reconcile()
    except Exception as e:
        return f"reconcile failed: {e}"
    return None


def process_company(env, cid: int, tolerance: float, dry_run: bool) -> dict:
    bank_lines = env["account.bank.statement.line"].search([
        ("company_id", "=", cid),
        ("is_reconciled", "=", False),
    ])
    accs = env["account.account"].search([
        ("code", "in", AGGREGATING_ACCOUNT_CODES),
        ("company_id", "=", cid),
    ])
    stats = {"company_id": cid, "considered": len(bank_lines),
             "reconciled": 0, "errors": [], "details": []}

    for bl in bank_lines:
        if (bl.payment_ref or "").upper().lstrip().startswith("TRANSACCION CONTACTLESS"):
            continue
        target_amount = abs(bl.amount)
        if target_amount < 0.5:
            continue
        bank_neg = bl.amount < 0
        match_result = None
        for acc in accs:
            amls = env["account.move.line"].search([
                ("account_id", "=", acc.id),
                ("company_id", "=", cid),
                ("parent_state", "=", "posted"),
                ("reconciled", "=", False),
            ])
            if not amls:
                continue
            if bank_neg:
                relevant = [(a, abs(a.balance)) for a in amls if a.credit > 0]
            else:
                relevant = [(a, abs(a.balance)) for a in amls if a.debit > 0]
            if not relevant:
                continue
            subset = _find_subset(relevant, target_amount, tolerance)
            if subset and len(subset) >= 2:
                match_result = (acc, subset)
                break

        if not match_result:
            continue

        acc, subset = match_result
        partner_names = [a.partner_id.name for a in subset if a.partner_id]
        partner_label = ",".join(set(partner_names))[:60] if partner_names else "(varios)"
        sum_str = " + ".join(f"{abs(a.balance):.2f}" for a in subset)
        log.info(f"  multi-match line {bl.id} ({bl.amount:.2f}) <-> {acc.code} subset[{len(subset)}] "
                 f"sum {sum(abs(a.balance) for a in subset):.2f} = {sum_str} ({partner_label})")

        if dry_run:
            stats["reconciled"] += 1
            stats["details"].append({
                "bank_line": bl.id, "amount": float(bl.amount),
                "account": acc.code, "subset_size": len(subset),
                "subset_sum": sum(abs(a.balance) for a in subset),
            })
            continue

        err = reconcile_bank_against_amls(env, bl, subset)
        if err:
            stats["errors"].append({"bank_line": bl.id, "error": err})
            log.warning(f"  could not reconcile multi: {err}")
        else:
            stats["reconciled"] += 1
            log.info(f"  reconciled multi: bank {bl.id} ↔ {len(subset)} AMLs on {acc.code}")
    return stats


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--company-id", type=int)
    p.add_argument("--tolerance-eur", type=float, default=DEFAULT_TOLERANCE)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    out = []
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        cids = [args.company_id] if args.company_id else env["res.company"].search([]).ids
        for cid in cids:
            out.append(process_company(env, cid, args.tolerance_eur, args.dry_run))
        if not args.dry_run:
            cr.commit()
    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
