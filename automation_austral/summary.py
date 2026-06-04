#!/usr/bin/env python3
"""
Daily summary: returns JSON with vendor bills created on a given date.

Usage:
    python3 summary.py --date 2026-04-25

Output: single-line JSON to stdout. Logs to stderr.
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
import sys
from datetime import datetime, date

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam_test"

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    args = p.parse_args()

    target = datetime.strptime(args.date, "%Y-%m-%d").date()

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        domain = [
            ("move_type", "=", "in_invoice"),
            ("create_date", ">=", f"{target} 00:00:00"),
            ("create_date", "<=", f"{target} 23:59:59"),
        ]
        moves = env["account.move"].search(domain, order="create_date asc")

        items = []
        total_amount = 0.0
        by_state = {"draft": 0, "posted": 0, "cancel": 0}
        for m in moves:
            items.append({
                "id": m.id,
                "supplier": m.partner_id.name,
                "vat": m.partner_id.vat or "",
                "ref": m.ref or "",
                "invoice_date": str(m.invoice_date) if m.invoice_date else "",
                "amount_total": round(m.amount_total, 2),
                "state": m.state,
                "url": f"/odoo/action-account.action_move_in_invoice_type/{m.id}",
            })
            total_amount += m.amount_total
            by_state[m.state] = by_state.get(m.state, 0) + 1

    print(json.dumps({
        "ok": True,
        "date": args.date,
        "count": len(items),
        "total_amount": round(total_amount, 2),
        "by_state": by_state,
        "items": items,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
