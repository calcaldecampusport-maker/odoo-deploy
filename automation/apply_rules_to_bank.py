#!/usr/bin/env python3
"""
Aggressive pass: for every unreconciled bank statement line, find the best
matching learned.rule (rule_type=bank, confidence>=0.85) and route the line
to that account. Idempotent — already-reconciled lines are skipped.

Run:
  python3 apply_rules_to_bank.py [--company-id N] [--min-confidence 0.85]
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

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

log = logging.getLogger("apply_rules")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _route(env, bank_line, target_account, partner=None) -> str | None:
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
    vals = {"account_id": target_account.id}
    if partner:
        vals["partner_id"] = partner.id
    susp.write(vals)
    return None


def process_company(env, company_id: int, min_conf: float, dry_run: bool) -> dict:
    rules = env["learned.rule"].search([
        ("rule_type", "=", "bank"),
        ("company_id", "=", company_id),
        ("active", "=", True),
        ("confidence", ">=", min_conf),
    ], order="confidence desc")
    if not rules:
        return {"company_id": company_id, "rules": 0, "applied": 0}

    bank_lines = env["account.bank.statement.line"].search([
        ("company_id", "=", company_id),
        ("is_reconciled", "=", False),
    ])
    log.info(f"company {company_id}: {len(rules)} rules, {len(bank_lines)} unreconciled lines")

    applied = 0
    by_rule = {}
    for line in bank_lines:
        ref = (line.payment_ref or "").upper()
        if not ref:
            continue
        if ref.lstrip().startswith("TRANSACCION CONTACTLESS"):
            continue
        chosen = None
        for r in rules:
            pat = (r.pattern or "").strip().upper()
            if not pat:
                continue
            words = pat.split()
            if all(w in ref for w in words):
                chosen = r
                break
        if not chosen or not chosen.account_id:
            continue
        if dry_run:
            applied += 1
            by_rule[chosen.pattern] = by_rule.get(chosen.pattern, 0) + 1
            continue
        err = _route(env, line, chosen.account_id, chosen.partner_id or None)
        if err:
            continue
        chosen.times_applied = (chosen.times_applied or 0) + 1
        applied += 1
        by_rule[chosen.pattern] = by_rule.get(chosen.pattern, 0) + 1
    return {"company_id": company_id, "rules": len(rules), "applied": applied, "by_rule": by_rule}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--company-id", type=int)
    p.add_argument("--min-confidence", type=float, default=0.85)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    out = []
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        cids = [args.company_id] if args.company_id else env["res.company"].search([]).ids
        for cid in cids:
            stats = process_company(env, cid, args.min_confidence, args.dry_run)
            out.append(stats)
            log.info(f"  {stats}")
        if not args.dry_run:
            cr.commit()
    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
