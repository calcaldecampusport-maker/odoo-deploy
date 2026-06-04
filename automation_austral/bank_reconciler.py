#!/usr/bin/env python3
"""
Auto-reconcile bank.statement.line entries with their matching account.move
(invoice) when the heuristic score is >= 90%.

For each unreconciled bank line, finds the best invoice match (using
bank_matcher.propose_for_company), and if score >= AUTO_RECONCILE_THRESHOLD
performs the reconciliation:
  1. Re-route the suspense move-line of the bank statement line to use the
     partner's payable/receivable account.
  2. Call .reconcile() on (bank suspense line, invoice payable/receivable line).

Resulting effect: invoice payment_state -> paid; bank line is_reconciled -> True.

Usage:
  python3 bank_reconciler.py [--company-id N] [--threshold 90] [--dry-run]
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
AUTO_RECONCILE_THRESHOLD = 90

sys.path.insert(0, ODOO_PATH)
sys.path.insert(0, _HERE)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

import companies as comp  # noqa: E402
import bank_matcher  # noqa: E402

log = logging.getLogger("bank_reconciler")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def reconcile_pair(env, bank_line, target_aml) -> str | None:
    """Reconcile bank_line against any open account.move.line. Returns None on success."""
    if bank_line.is_reconciled:
        return f"bank line {bank_line.id} already reconciled"
    if target_aml.reconciled:
        return f"target line {target_aml.id} already reconciled"
    if target_aml.parent_state != "posted":
        return f"target move not posted (state={target_aml.parent_state})"

    bank_move = bank_line.move_id
    suspense_acc = bank_line.journal_id.suspense_account_id
    if not suspense_acc:
        return "journal has no suspense_account_id"

    susp_aml = bank_move.line_ids.filtered(lambda l: l.account_id == suspense_acc)
    if not susp_aml:
        return "bank line has no suspense move-line"
    if len(susp_aml) > 1:
        susp_aml = susp_aml[0]

    susp_aml.write({
        "account_id": target_aml.account_id.id,
        "partner_id": target_aml.partner_id.id if target_aml.partner_id else False,
    })

    try:
        (susp_aml + target_aml).reconcile()
    except Exception as e:
        return f"reconcile() failed: {e}"
    return None


def _pick_invoice_aml(invoice):
    aml = invoice.line_ids.filtered(
        lambda l: l.account_id.account_type in ("liability_payable", "asset_receivable")
        and not l.reconciled
    )
    return aml[0] if aml else None


def process_company(env, cid: int, threshold: int, dry_run: bool) -> dict:
    proposals = bank_matcher.propose_for_company(env, cid, max_lines=500)
    stats = {"company_id": cid, "considered": len(proposals), "reconciled": 0,
             "skipped_low": 0, "skipped_other": 0, "errors": []}
    for p in proposals:
        # rule proposals don't have a target move/aml — skip them here
        non_rule = [x for x in p["proposals"] if x.get("kind") != "rule"]
        if not non_rule:
            stats["skipped_low"] += 1
            continue
        top = non_rule[0]
        if top.get("score", 0) < threshold:
            stats["skipped_low"] += 1
            continue

        bank_line = env["account.bank.statement.line"].browse(p["line_id"])

        # Resolve target_aml
        if top.get("kind") == "open_line":
            target_aml = env["account.move.line"].browse(top["aml_id"])
            label = f"open-line {target_aml.account_id.code} ({top.get('partner','')})"
        else:
            invoice = env["account.move"].browse(top["move_id"])
            target_aml = _pick_invoice_aml(invoice)
            if not target_aml:
                stats["skipped_other"] += 1
                continue
            label = f"invoice {invoice.name}"

        if dry_run:
            log.info(f"DRY: would reconcile bank line {bank_line.id} ({p['amount']:.2f}) "
                     f"with {label} score={top['score']}")
            stats["reconciled"] += 1
            continue
        err = reconcile_pair(env, bank_line, target_aml)
        if err:
            stats["errors"].append({"bank_line": bank_line.id, "target": target_aml.id, "error": err})
            log.warning(f"  could not reconcile {bank_line.id} <-> {label}: {err}")
        else:
            stats["reconciled"] += 1
            log.info(f"  reconciled bank line {bank_line.id} ({p['amount']:.2f}€) <-> {label} (score {top['score']}%)")
    return stats


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--company-id", type=int)
    p.add_argument("--threshold", type=int, default=AUTO_RECONCILE_THRESHOLD)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    out = []
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        cids = [args.company_id] if args.company_id else env["res.company"].search([]).ids
        for cid in cids:
            stats = process_company(env, cid, args.threshold, args.dry_run)
            out.append(stats)
        if not args.dry_run:
            cr.commit()

    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
