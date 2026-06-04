#!/usr/bin/env python3
"""Cron L-V 02:00: descarga liquidación PS del día anterior (o si lunes, V+S+D)
y crea los asientos Odoo siguiendo el patrón Sage 430098000.

Cuentas usadas (AUSTRAL company 4):
  430098000 — CLIENTES WEB - DETAIL (cuenta puente)
  700000001 — VENTAS TIENDAS
  477000021 — IVA Repercutido 21%
  477000003 — HP IGIC 3% Canarias
  477000022 — IVA Rep. Intracom 0%
  572000039 — C/C BBVA TPV (0182-2355-2802-0155-0576)

Resultado: JSON con asientos creados en /tmp/ps_liquidacion_<fecha>.json
para que email_summary.py lo recoja.
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

import sys, os, json, argparse
from datetime import date, timedelta, datetime
sys.path.insert(0, '/opt/odoo17/odoo')
sys.path.insert(0, _HERE)
os.environ['ODOO_RC'] = '/etc/odoo17.conf'
import odoo
from odoo.tools import config
config.parse_config(['-c', '/etc/odoo17.conf'])
import requests
from collections import defaultdict

# Cargar credenciales PS
def load_env_file(path):
    if not os.path.exists(path): return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k, v = line.split('=', 1)
        os.environ.setdefault(k, v.strip())
load_env_file('/etc/austral_prestashop.env')

API = os.environ['PS_AUSTRAL_API_URL']
KEY = os.environ['PS_AUSTRAL_WS_KEY']

COMPANY_ID = 4
JOURNAL_ID = 34  # LIQ - Liquidaciones B2C

ACC_CODES = {
    'puente':    '430098000',
    'ventas':    '700000001',
    'iva_21':    '477000021',
    'iva_3':     '477000003',
    'iva_intra': '477000022',
    'iva_exento':'477000002',
    'banco_tpv': '572000039',
}


def classify_iva(base, total):
    if base <= 0.01: return 'iva_exento'
    ratio = total / base
    if abs(ratio - 1.21) < 0.005: return 'iva_21'
    if abs(ratio - 1.03) < 0.005: return 'iva_3'
    if abs(ratio - 1.00) < 0.005: return 'iva_intra'
    return 'iva_21'  # ratio raro → 21% por defecto


def fetch_ps(endpoint, desde, hasta):
    r = requests.get(f"{API}/{endpoint}", params={
        'ws_key': KEY, 'output_format':'JSON', 'display':'full', 'date':'1',
        'limit':'5000', 'filter[date_add]': f'[{desde},{hasta}]',
    }, timeout=120)
    r.raise_for_status()
    j = r.json()
    return j.get(endpoint, j.get(endpoint.rstrip('s'), []))


def fechas_a_procesar(arg_date=None):
    """Devuelve lista de fechas (date) a procesar.
    - si arg_date dado: solo esa
    - si hoy lunes: viernes + sábado + domingo
    - otro: día anterior
    """
    if arg_date:
        return [datetime.strptime(arg_date, '%Y-%m-%d').date()]
    today = date.today()
    if today.weekday() == 0:  # Monday
        return [today - timedelta(days=3), today - timedelta(days=2), today - timedelta(days=1)]
    return [today - timedelta(days=1)]


def crear_asiento_dia(env, acc_by_code, d, fac_data, abo_data):
    """Crea (si no existe) los asientos FAC + ABO del día d. Devuelve lista de dicts log."""
    log = []
    d_str = d.isoformat() if isinstance(d, date) else d

    def make_move(ref, total_dia, base_dia, n, buckets, is_abono):
        existing = env['account.move'].search([('company_id','=',COMPANY_ID),('ref','=',ref)], limit=1)
        if existing:
            return {'ref':ref, 'skip':True, 'existing_id':existing.id, 'name':existing.name}
        signs = (-1 if is_abono else 1)  # for sense of D/H
        lines = []
        # Para facturación: 430098000 D total, ventas H, IVAs H, 430098000 H total, banco D total
        # Para abono: invertir
        if not is_abono:
            lines.append({'name': f'LIQUIDACION PS DIA {d_str} ({n} fac)',
                          'account_id': acc_by_code[ACC_CODES['puente']], 'debit': total_dia, 'credit': 0})
            lines.append({'name': f'VENTAS WEB {d_str}',
                          'account_id': acc_by_code[ACC_CODES['ventas']], 'debit': 0, 'credit': base_dia})
            for cls, b in buckets.items():
                if cls in ('iva_exento','iva_intra'): continue
                if b['iva'] < 0.005: continue
                lines.append({'name': f'IVA {cls} {d_str}',
                              'account_id': acc_by_code[ACC_CODES[cls]], 'debit': 0, 'credit': b['iva']})
            lines.append({'name': f'LIQUIDACIÓN FECHA {d_str}',
                          'account_id': acc_by_code[ACC_CODES['puente']], 'debit': 0, 'credit': total_dia})
            lines.append({'name': f'C/C BBVA TPV cobro {d_str}',
                          'account_id': acc_by_code[ACC_CODES['banco_tpv']], 'debit': total_dia, 'credit': 0})
        else:
            lines.append({'name': f'ABONO PS DIA {d_str} ({n} abonos)',
                          'account_id': acc_by_code[ACC_CODES['puente']], 'debit': 0, 'credit': total_dia})
            lines.append({'name': f'ABONO VENTAS WEB {d_str}',
                          'account_id': acc_by_code[ACC_CODES['ventas']], 'debit': base_dia, 'credit': 0})
            for cls, b in buckets.items():
                if cls in ('iva_exento','iva_intra'): continue
                if b['iva'] < 0.005: continue
                lines.append({'name': f'ABONO IVA {cls} {d_str}',
                              'account_id': acc_by_code[ACC_CODES[cls]], 'debit': b['iva'], 'credit': 0})
            lines.append({'name': f'LIQUIDACIÓN ABO FECHA {d_str}',
                          'account_id': acc_by_code[ACC_CODES['puente']], 'debit': total_dia, 'credit': 0})
            lines.append({'name': f'C/C BBVA TPV devolución {d_str}',
                          'account_id': acc_by_code[ACC_CODES['banco_tpv']], 'debit': 0, 'credit': total_dia})

        payload = [(0,0,{**l, 'debit': round(l['debit'],2), 'credit': round(l['credit'],2)}) for l in lines]
        sd = sum(l[2]['debit'] for l in payload)
        sh = sum(l[2]['credit'] for l in payload)
        if abs(sd - sh) > 0.05:
            return {'ref':ref, 'error':f'descuadre D={sd} H={sh}'}
        m = env['account.move'].create({
            'move_type':'entry','company_id':COMPANY_ID,'journal_id':JOURNAL_ID,
            'date': d_str, 'ref': ref, 'line_ids': payload,
        })
        m.action_post()
        # Detalle por IVA
        iva_detail = {cls: round(b['iva'],2) for cls,b in buckets.items() if b['iva'] >= 0.005}
        return {'ref':ref,'name':m.name,'id':m.id,'total':round(sd,2),'n':n,
                'base': round(base_dia,2), 'iva_detail': iva_detail}

    if fac_data:
        total = sum(b['total'] for b in fac_data.values())
        base = sum(b['base'] for b in fac_data.values())
        n = sum(b['n'] for b in fac_data.values())
        log.append({'tipo':'FAC', **make_move(f'PS-LIQ-FAC-{d_str}', total, base, n, fac_data, is_abono=False)})
    if abo_data:
        total = sum(b['total'] for b in abo_data.values())
        base = sum(b['base'] for b in abo_data.values())
        n = sum(b['n'] for b in abo_data.values())
        log.append({'tipo':'ABO', **make_move(f'PS-LIQ-ABO-{d_str}', total, base, n, abo_data, is_abono=True)})
    return log


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', help='YYYY-MM-DD (procesar solo ese día)')
    parser.add_argument('--from', dest='dfrom', help='YYYY-MM-DD (rango inicio)')
    parser.add_argument('--to', dest='dto', help='YYYY-MM-DD (rango fin)')
    args = parser.parse_args()

    if args.dfrom and args.dto:
        start = datetime.strptime(args.dfrom, '%Y-%m-%d').date()
        end = datetime.strptime(args.dto, '%Y-%m-%d').date()
        fechas = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        fechas = fechas_a_procesar(args.date)
    print(f'[ps_liquidacion_diaria] fechas a procesar: {[f.isoformat() for f in fechas]}')

    desde = fechas[0].isoformat() + ' 00:00:00'
    hasta = fechas[-1].isoformat() + ' 23:59:59'

    invs = fetch_ps('order_invoices', desde, hasta)
    slips = fetch_ps('order_slip', desde, hasta)
    print(f'[ps_liquidacion_diaria] PS API: {len(invs)} facturas + {len(slips)} abonos')

    # Agrupar por día → tipo IVA
    days_fac = defaultdict(lambda: defaultdict(lambda: {'n':0,'base':0.0,'iva':0.0,'total':0.0}))
    days_abo = defaultdict(lambda: defaultdict(lambda: {'n':0,'base':0.0,'iva':0.0,'total':0.0}))
    for inv in invs:
        d = inv['date_add'][:10]
        base = float(inv['total_paid_tax_excl']); total = float(inv['total_paid_tax_incl'])
        cls = classify_iva(base, total)
        b = days_fac[d][cls]; b['n']+=1; b['base']+=base; b['iva']+=(total-base); b['total']+=total
    for s in slips:
        d = s['date_add'][:10]
        base = float(s.get('total_products_tax_excl',0) or 0) + float(s.get('total_shipping_tax_excl',0) or 0)
        total = float(s.get('amount',0) or 0)
        if total == 0:
            total = float(s.get('total_products_tax_incl',0) or 0) + float(s.get('total_shipping_tax_incl',0) or 0)
        cls = classify_iva(base, total)
        b = days_abo[d][cls]; b['n']+=1; b['base']+=base; b['iva']+=(total-base); b['total']+=total

    # Crear asientos
    reg = odoo.registry('cararjfam_test')
    all_log = []
    with reg.cursor() as cr:
        env = odoo.api.Environment(cr, 1, {'allowed_company_ids':[COMPANY_ID]})
        cr.execute("SELECT id, code FROM account_account WHERE company_id=%s", (COMPANY_ID,))
        acc_by_code = {code:aid for aid,code in cr.fetchall()}
        # Reactivar cuentas si están deprecadas
        for code in ACC_CODES.values():
            if code in acc_by_code:
                cr.execute("UPDATE account_account SET deprecated=false WHERE id=%s AND deprecated=true", (acc_by_code[code],))

        for f in fechas:
            d_str = f.isoformat()
            log = crear_asiento_dia(env, acc_by_code, f, days_fac.get(d_str), days_abo.get(d_str))
            all_log.extend(log)
        cr.commit()

    # Guardar JSON
    out_path = f'/tmp/ps_liquidacion_{date.today().isoformat()}.json'
    open(out_path,'w').write(json.dumps({'fechas':[f.isoformat() for f in fechas], 'asientos': all_log}, indent=2, default=str))
    print(f'[ps_liquidacion_diaria] OK {len(all_log)} asientos. JSON: {out_path}')
    for x in all_log:
        if x.get('skip'):
            print(f'  SKIP {x["ref"]} (ya existe {x["name"]})')
        elif x.get('error'):
            print(f'  ERROR {x["ref"]}: {x["error"]}')
        else:
            print(f'  + {x["tipo"]} {x["name"]} ref={x["ref"]} total={x["total"]:.2f} n={x["n"]}')

if __name__ == '__main__':
    main()
