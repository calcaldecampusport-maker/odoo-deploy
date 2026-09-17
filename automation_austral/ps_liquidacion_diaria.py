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


PEND_FILE = '/var/automation_austral/ps_pendientes.json'
REINTENTO_DIAS = 5     # ventana de reintento para un día que falló o vino vacío


def fetch_ps(endpoint, desde, hasta):
    """Registros de PrestaShop entre dos fechas.

    Ojo con la forma de la respuesta: cuando NO hay registros la API devuelve una
    LISTA vacía, y cuando sí los hay un diccionario con la clave en PLURAL
    (order_slips) aunque el endpoint sea singular (order_slip). El código antiguo
    hacía j.get(...) sobre la lista (petaba los días sin abonos) y solo miraba la
    clave singular (por eso los abonos salían siempre a 0)."""
    r = requests.get(f"{API}/{endpoint}", params={
        'ws_key': KEY, 'output_format':'JSON', 'display':'full', 'date':'1',
        'limit':'5000', 'filter[date_add]': f'[{desde},{hasta}]',
    }, timeout=120)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, list):          # sin registros ese día
        return j
    if not isinstance(j, dict):
        return []
    for clave in (endpoint, endpoint + 's', endpoint.rstrip('s'), endpoint.rstrip('s') + 's'):
        v = j.get(clave)
        if isinstance(v, list):
            return v
    # por si algún día cambian el nombre: la primera lista que traiga
    for v in j.values():
        if isinstance(v, list):
            return v
    return []


def cargar_pendientes():
    """{'YYYY-MM-DD': {'desde': 'YYYY-MM-DD', 'intentos': n, 'motivo': str}}"""
    try:
        with open(PEND_FILE) as f:
            return json.load(f)
    except Exception:          # noqa: BLE001 — sin fichero o ilegible: se empieza limpio
        return {}


def guardar_pendientes(p):
    try:
        os.makedirs(os.path.dirname(PEND_FILE), exist_ok=True)
        with open(PEND_FILE, 'w') as f:
            json.dump(p, f, indent=2, sort_keys=True)
    except Exception as e:     # noqa: BLE001 — no debe tumbar la ejecución
        print(f'[ps_liquidacion_diaria] AVISO: no se pudo guardar {PEND_FILE}: {e}')


def anotar_pendiente(pend, dia, motivo):
    """Apunta el día para reintentarlo en las próximas ejecuciones."""
    hoy = date.today().isoformat()
    e = pend.get(dia) or {'desde': hoy, 'intentos': 0}
    e['intentos'] = int(e.get('intentos', 0)) + 1
    e['motivo'] = motivo
    e['ultimo_intento'] = hoy
    pend[dia] = e
    print(f'  ↻ {dia} queda pendiente ({motivo}) — intento {e["intentos"]}, '
          f'se reintentará hasta {REINTENTO_DIAS} días desde {e["desde"]}')


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
    parser.add_argument('--dry-run', action='store_true',
                        help='consulta PrestaShop y dice qué haría, sin contabilizar')
    parser.add_argument('--sin-reintentos', action='store_true',
                        help='no arrastrar la cola de días pendientes')
    args = parser.parse_args()

    manual = bool(args.date or (args.dfrom and args.dto))
    if args.dfrom and args.dto:
        start = datetime.strptime(args.dfrom, '%Y-%m-%d').date()
        end = datetime.strptime(args.dto, '%Y-%m-%d').date()
        fechas = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    else:
        fechas = fechas_a_procesar(args.date)

    # --- cola de reintentos: días que fallaron o vinieron vacíos ---
    pend = {} if (manual or args.sin_reintentos) else cargar_pendientes()
    hoy = date.today()
    reintentos = []
    for dia, info in sorted(pend.items()):
        try:
            edad = (hoy - datetime.strptime(info.get('desde', dia), '%Y-%m-%d').date()).days
        except Exception:      # noqa: BLE001
            edad = 0
        if edad > REINTENTO_DIAS:
            print(f'[ps_liquidacion_diaria] ABANDONADO {dia}: {REINTENTO_DIAS} días sin '
                  f'conseguir datos ({info.get("motivo")}) — revísalo a mano')
            continue
        reintentos.append(datetime.strptime(dia, '%Y-%m-%d').date())
    for dia in list(pend):
        try:
            if (hoy - datetime.strptime(pend[dia].get('desde', dia), '%Y-%m-%d').date()).days > REINTENTO_DIAS:
                pend.pop(dia)
        except Exception:      # noqa: BLE001
            pend.pop(dia)

    fechas = sorted(set(fechas) | set(reintentos))
    print(f'[ps_liquidacion_diaria] fechas a procesar: {[f.isoformat() for f in fechas]}'
          + (f' (de ellas {len(reintentos)} de la cola de reintentos)' if reintentos else '')
          + (' [DRY-RUN: no se contabiliza]' if args.dry_run else ''))

    # --- descarga POR DÍA: un día que falle no arrastra a los demás ---
    days_fac = defaultdict(lambda: defaultdict(lambda: {'n':0,'base':0.0,'iva':0.0,'total':0.0}))
    days_abo = defaultdict(lambda: defaultdict(lambda: {'n':0,'base':0.0,'iva':0.0,'total':0.0}))
    con_datos = []
    for f in fechas:
        d_str = f.isoformat()
        try:
            invs = fetch_ps('order_invoices', d_str + ' 00:00:00', d_str + ' 23:59:59')
            slips = fetch_ps('order_slip', d_str + ' 00:00:00', d_str + ' 23:59:59')
        except Exception as e:  # noqa: BLE001 — API caída, timeout, 500…
            anotar_pendiente(pend, d_str, f'error de la API: {str(e)[:80]}')
            continue
        print(f'  {d_str}: {len(invs)} facturas + {len(slips)} abonos')
        if not invs and not slips:
            anotar_pendiente(pend, d_str, 'PrestaShop no devolvió ningún registro')
            continue
        for inv in invs:
            d = inv['date_add'][:10]
            base = float(inv['total_paid_tax_excl']); total = float(inv['total_paid_tax_incl'])
            cls = classify_iva(base, total)
            b = days_fac[d][cls]; b['n']+=1; b['base']+=base; b['iva']+=(total-base); b['total']+=total
        for sl in slips:
            d = sl['date_add'][:10]
            base = float(sl.get('total_products_tax_excl',0) or 0) + float(sl.get('total_shipping_tax_excl',0) or 0)
            total = float(sl.get('amount',0) or 0)
            if total == 0:
                total = float(sl.get('total_products_tax_incl',0) or 0) + float(sl.get('total_shipping_tax_incl',0) or 0)
            cls = classify_iva(base, total)
            b = days_abo[d][cls]; b['n']+=1; b['base']+=base; b['iva']+=(total-base); b['total']+=total
        con_datos.append(f)
        pend.pop(d_str, None)          # ese día ya está resuelto

    if args.dry_run:
        print('[ps_liquidacion_diaria] DRY-RUN: no se contabiliza nada. Resumen:')
        for f in fechas:
            d_str = f.isoformat()
            fac = sum(v['total'] for v in (days_fac.get(d_str) or {}).values())
            abo = sum(v['total'] for v in (days_abo.get(d_str) or {}).values())
            nf = sum(v['n'] for v in (days_fac.get(d_str) or {}).values())
            na = sum(v['n'] for v in (days_abo.get(d_str) or {}).values())
            print(f'   {d_str}: facturas {nf} ({fac:.2f}) · abonos {na} ({abo:.2f})')
        if not (manual or args.sin_reintentos):
            guardar_pendientes(pend)
            print(f'[ps_liquidacion_diaria] cola de reintentos: {sorted(pend)}')
        return

    # --- contabilizar solo los días que trajeron datos ---
    reg = odoo.registry('cararjfam_test')
    all_log = []
    with reg.cursor() as cr:
        env = odoo.api.Environment(cr, 1, {'allowed_company_ids':[COMPANY_ID]})
        cr.execute("SELECT id, code FROM account_account WHERE company_id=%s", (COMPANY_ID,))
        acc_by_code = {code:aid for aid,code in cr.fetchall()}
        for code in ACC_CODES.values():
            if code in acc_by_code:
                cr.execute("UPDATE account_account SET deprecated=false WHERE id=%s AND deprecated=true", (acc_by_code[code],))
        for f in con_datos:
            d_str = f.isoformat()
            try:
                log = crear_asiento_dia(env, acc_by_code, f, days_fac.get(d_str), days_abo.get(d_str))
                all_log.extend(log)
            except Exception as e:      # noqa: BLE001 — un día no debe tumbar el resto
                print(f'  ERROR contabilizando {d_str}: {str(e)[:120]}')
                anotar_pendiente(pend, d_str, f'error al contabilizar: {str(e)[:80]}')
        cr.commit()

    if not (manual or args.sin_reintentos):
        guardar_pendientes(pend)
        if pend:
            print(f'[ps_liquidacion_diaria] cola de reintentos: {sorted(pend)}')

    out_path = f'/tmp/ps_liquidacion_{date.today().isoformat()}.json'
    open(out_path,'w').write(json.dumps({'fechas':[f.isoformat() for f in fechas],
                                         'pendientes': sorted(pend),
                                         'asientos': all_log}, indent=2, default=str))
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
