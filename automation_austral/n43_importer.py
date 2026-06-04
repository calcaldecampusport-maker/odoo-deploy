#!/usr/bin/env python3
"""
Import a Spanish AEB43 (CSB-43, .n43) bank statement file into Odoo.

Usage:
    python3 n43_importer.py --file /path/to/extract.n43

The file is parsed with csb43, then for each account inside:
  - find the matching account.journal by IBAN
  - create or extend account.bank.statement for the date range
  - create account.bank.statement.line entries for each transaction

Exit codes:
  0  -> imported OK (printed STATEMENT_IDS=...)
  10 -> validation failed (no matching journal, format error, etc.)
  30 -> ORM error
  40 -> bad input
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
from datetime import date

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam_test"

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402

from csb43 import aeb43  # noqa: E402

log = logging.getLogger("n43_importer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _normalize_acc_number(s: str) -> str:
    return "".join(c for c in (s or "") if c.isalnum()).upper()


def _find_journal_by_iban(env, iban_partial: str):
    """Match a journal whose linked bank account number ends with iban_partial."""
    iban_partial = _normalize_acc_number(iban_partial)
    journals = env["account.journal"].search([("type", "=", "bank")])
    for j in journals:
        if not j.bank_account_id:
            continue
        acc = _normalize_acc_number(j.bank_account_id.acc_number or "")
        if acc.endswith(iban_partial) or iban_partial.endswith(acc[-10:]):
            return j
    return None


def _tx_concept(tx) -> str:
    """Build a human-readable concept from the transaction's optional items."""
    bits = []
    for item in tx.optional_items or []:
        text = (getattr(item, "item_1", "") or "").strip()
        if text:
            bits.append(text)
        text = (getattr(item, "item_2", "") or "").strip()
        if text:
            bits.append(text)
    return " ".join(bits) if bits else (getattr(tx, "concept", "") or "").strip()


def import_file(file_path: Path) -> dict:
    with open(file_path, "rb") as f:
        batch = aeb43.read_batch(f)

    results = {"file": str(file_path), "statements": []}

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})

        for account in batch.accounts:
            acc_num = (account.account_number or "").strip()
            journal = _find_journal_by_iban(env, acc_num)
            if not journal:
                log.error(f"no journal matches account number ending {acc_num!r}")
                results["statements"].append({"acc_number": acc_num, "error": "no journal match"})
                continue
            company_id = journal.company_id.id

            txs = list(account.transactions or [])
            if not txs:
                log.info(f"  account {acc_num} has no transactions, skipping")
                continue

            min_date = min(t.transaction_date for t in txs)
            max_date = max(t.transaction_date for t in txs)
            statement_name = f"{journal.name} {min_date.isoformat()} a {max_date.isoformat()}"

            balance_start = float(account.initial_balance)
            balance_end = float(account.final_balance) if hasattr(account, "final_balance") else balance_start + sum(float(t.amount) for t in txs)

            # Always create a new statement (Odoo allows multiple)
            statement = env["account.bank.statement"].with_company(company_id).create({
                "name": statement_name,
                "journal_id": journal.id,
                "date": max_date,
                "balance_start": balance_start,
                "balance_end_real": balance_end,
                "company_id": company_id,
            })
            log.info(f"  created statement id={statement.id} {statement_name!r}")

            line_ids = []
            for tx in txs:
                amount = float(tx.amount)
                concept = _tx_concept(tx)
                line_vals = {
                    "statement_id": statement.id,
                    "journal_id": journal.id,
                    "date": tx.transaction_date,
                    "payment_ref": (concept[:120] if concept else f"{tx.shared_item} {tx.own_item}").strip() or "Movimiento",
                    "amount": amount,
                    "company_id": company_id,
                }
                line = env["account.bank.statement.line"].with_company(company_id).create(line_vals)
                line_ids.append(line.id)

            results["statements"].append({
                "acc_number": acc_num,
                "journal_id": journal.id,
                "company_id": company_id,
                "statement_id": statement.id,
                "lines": len(line_ids),
                "min_date": str(min_date),
                "max_date": str(max_date),
                "balance_start": balance_start,
                "balance_end": balance_end,
            })
        cr.commit()

    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--file", required=True, help="Path to .n43 / .txt / .csb file")
    args = p.parse_args()

    fp = Path(args.file)
    if not fp.exists():
        log.error(f"file not found: {fp}")
        return 40

    try:
        results = import_file(fp)
    except Exception as e:
        log.exception("import error")
        print(f"ERROR={type(e).__name__}: {e}")
        return 30

    print("STATEMENT_RESULTS=" + json.dumps(results, ensure_ascii=False, default=str))
    statements = results.get("statements", [])
    ok = sum(1 for s in statements if "statement_id" in s)
    if ok == 0:
        return 10
    return 0


if __name__ == "__main__":
    sys.exit(main())
