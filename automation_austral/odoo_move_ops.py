#!/usr/bin/env python3
"""Operaciones puntuales sobre account.move desde la web (multi-empresa).
  --to-draft --move-id N --db DB --company-id C  -> button_draft() con guard de company.
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
    p.add_argument('--to-draft',action='store_true')
    p.add_argument('--move-id',type=int,required=True)
    p.add_argument('--db',required=True)
    p.add_argument('--company-id',type=int,required=True)
    a=p.parse_args()
    odoo.tools.config.parse_config(['-c',ODOO_CONF])
    reg=odoo.registry(a.db)
    with reg.cursor() as cr:
        env=Environment(cr,odoo.SUPERUSER_ID,{})
        m=env['account.move'].browse(a.move_id)
        if not m.exists():
            print(json.dumps({'ok':False,'error':'no existe'})); return
        if m.company_id.id != a.company_id:
            print(json.dumps({'ok':False,'error':'company mismatch'})); return
        if a.to_draft:
            if m.state!='posted':
                print(json.dumps({'ok':False,'error':f'estado {m.state}, no posted'})); return
            try:
                m.button_draft()
                cr.commit()
                print(json.dumps({'ok':True,'move':m.name,'state':m.state}))
            except Exception as e:
                print(json.dumps({'ok':False,'error':str(e)[:200]}))
        else:
            print(json.dumps({'ok':False,'error':'sin operacion'}))

if __name__=='__main__': main()
