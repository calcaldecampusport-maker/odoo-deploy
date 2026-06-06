#!/usr/bin/env python3
"""Operaciones sobre account.account (multi-empresa) desde la web.
  --archive --account-id N --db DB --company-id C   -> deprecated=True
  --merge --src N --dst M --db DB --company-id C     -> reasigna apuntes src->dst y archiva src
"""
import os as _os, sys as _sys
_HERE=_os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path: _sys.path.insert(0,_HERE)
import argparse, json
ODOO_PATH='/opt/odoo17/odoo'; ODOO_CONF='/etc/odoo17.conf'
if ODOO_PATH not in _sys.path: _sys.path.insert(0,ODOO_PATH)
import odoo
from odoo.api import Environment

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--archive',action='store_true')
    p.add_argument('--merge',action='store_true')
    p.add_argument('--account-id',type=int)
    p.add_argument('--src',type=int); p.add_argument('--dst',type=int)
    p.add_argument('--db',required=True); p.add_argument('--company-id',type=int,required=True)
    a=p.parse_args()
    odoo.tools.config.parse_config(['-c',ODOO_CONF])
    reg=odoo.registry(a.db)
    with reg.cursor() as cr:
        env=Environment(cr,odoo.SUPERUSER_ID,{})
        AA=env['account.account']
        if a.archive:
            acc=AA.browse(a.account_id)
            if not acc.exists() or acc.company_id.id!=a.company_id:
                print(json.dumps({'ok':False,'error':'cuenta no válida'})); return
            acc.deprecated=True; cr.commit()
            print(json.dumps({'ok':True,'archivada':acc.code})); return
        if a.merge:
            src=AA.browse(a.src); dst=AA.browse(a.dst)
            if not src.exists() or not dst.exists() or src.company_id.id!=a.company_id or dst.company_id.id!=a.company_id:
                print(json.dumps({'ok':False,'error':'cuentas no válidas'})); return
            if src.id==dst.id:
                print(json.dumps({'ok':False,'error':'src==dst'})); return
            lines=env['account.move.line'].search([('account_id','=',src.id),('company_id','=',a.company_id)])
            n=len(lines)
            try:
                if lines:
                    lines.write({'account_id':dst.id})
                src.deprecated=True
                cr.commit()
                print(json.dumps({'ok':True,'merged':src.code,'into':dst.code,'apuntes_movidos':n}))
            except Exception as e:
                cr.rollback()
                print(json.dumps({'ok':False,'error':str(e)[:250]}))
            return
        print(json.dumps({'ok':False,'error':'sin operacion'}))

if __name__=='__main__': main()
