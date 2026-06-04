#!/usr/bin/env python3
"""Cambia la fecha estimada (date_maturity de línea, o invoice_date_due de move) en Odoo.
Uso:
  --line-id 30330 --date 2026-07-15 --company 4   (preferente: nivel línea)
  --move-id 16749 --date 2026-07-15 --company 4   (nivel factura)
"""
import sys, os, argparse
sys.path.insert(0, '/opt/odoo17/odoo')
os.environ['ODOO_RC'] = '/etc/odoo17.conf'
import odoo
from odoo.tools import config
config.parse_config(['-c', '/etc/odoo17.conf'])
from datetime import date


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--line-id', type=int)
    p.add_argument('--move-id', type=int)
    p.add_argument('--date', required=True)
    p.add_argument('--company', type=int, default=4)
    args = p.parse_args()
    nueva = date.fromisoformat(args.date)

    reg = odoo.registry('cararjfam_test')
    with reg.cursor() as cr:
        env = odoo.api.Environment(cr, 1, {'allowed_company_ids': [args.company]})

        if args.line_id:
            line = env['account.move.line'].browse(args.line_id)
            if not line.exists():
                print(f'ERROR: line {args.line_id} no existe', file=sys.stderr); sys.exit(1)
            if line.company_id.id != args.company:
                print('ERROR: line de otra company', file=sys.stderr); sys.exit(1)
            move = line.move_id
            was_posted = move.state == 'posted'
            try:
                line.write({'date_maturity': nueva})
            except Exception:
                if was_posted:
                    move.button_draft()
                line.write({'date_maturity': nueva})
                if was_posted:
                    move.action_post()
            cr.commit()
            print(f'OK line {args.line_id} date_maturity={nueva.isoformat()}')
            return

        # move-level
        m = env['account.move'].browse(args.move_id)
        if not m.exists():
            print(f'ERROR: move {args.move_id} no existe', file=sys.stderr); sys.exit(1)
        was_posted = m.state == 'posted'
        try:
            m.write({'invoice_date_due': nueva})
        except Exception:
            if was_posted:
                m.button_draft()
            m.write({'invoice_date_due': nueva})
            if was_posted:
                m.action_post()
        for line in m.line_ids:
            if line.account_id.account_type in ('asset_receivable', 'liability_payable'):
                try:
                    line.write({'date_maturity': nueva})
                except Exception:
                    pass
        cr.commit()
        print(f'OK move {args.move_id} invoice_date_due={nueva.isoformat()}')


if __name__ == '__main__':
    main()
