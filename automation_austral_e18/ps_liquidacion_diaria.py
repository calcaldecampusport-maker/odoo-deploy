#!/usr/bin/env python3
"""Liquidación diaria de PrestaShop de AUSTRAL contra Odoo 18 Enterprise (company 12).

Portado del que corría contra Odoo 17 Community por ORM: aquí todo va por XML-RPC,
con las credenciales del backend de Austral (/opt/austral-contab/backend/.env).

Por cada día crea, en el diario LIQ:
  · FAC: 430098000 D total / 700000001 H base POR TIPO DE IVA (con su impuesto: la
         linea de IVA la genera Odoo) / 430098000 H total
         / 572000039 D total  (el cobro entra por el TPV del BBVA)
  · ABO: el mismo asiento invertido, si ese día hay abonos
Idempotente por `ref` (PS-LIQ-FAC-<fecha> / PS-LIQ-ABO-<fecha>): relanzarlo no duplica.

Robustez (ago 2026): la API de PrestaShop devuelve [] cuando no hay registros y la
clave en plural cuando sí los hay; cada día se procesa aislado y el que falla o viene
vacío se apunta para reintentarlo en las 5 ejecuciones siguientes.

Uso:
  ps_liquidacion_diaria.py                          # el día anterior (lunes: vie+sáb+dom)
  ps_liquidacion_diaria.py --date 2026-07-01
  ps_liquidacion_diaria.py --from 2026-07-01 --to 2026-08-11
  ps_liquidacion_diaria.py --dry-run ...            # sin contabilizar
  ps_liquidacion_diaria.py --from ... --to ... --rehacer   # rehace los ya contabilizados
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import requests                                              # noqa: E402
import companies                                             # noqa: E402
import odoo as odoo_mod                                      # noqa: E402

COMPANY_ID = 12
JOURNAL_CODE = 'LIQ'
PEND_FILE = '/var/automation_austral_e18/ps_pendientes.json'
REINTENTO_DIAS = 5
ENV_PS = '/etc/austral_prestashop.env'

# cuentas de la liquidación (plan de Austral, 9 dígitos)
ACC_CODES = {
    'puente':     '430098000',   # CLIENTES WEB - DETAIL
    'ventas':     '700000001',   # VENTAS TIENDAS
    'iva_21':     '477000021',
    'iva_3':      '477000003',   # IGIC Canarias
    'iva_intra':  '477000022',
    'iva_exento': '477000002',
    'banco_tpv':  '572000039',   # C/C BBVA TPV
}

# El IVA se pone como IMPUESTO en la línea de venta, no escribiendo la cuenta 477 a
# mano: así Odoo genera la línea de IVA con sus etiquetas fiscales y las ventas web
# aparecen en el modelo 303 (antes salía a cero porque los apuntes no llevaban
# impuesto). Los nombres son los de los impuestos de Austral en Odoo.
TAX_NAMES = {
    'iva_21':     'IVA 21% G',
    'iva_3':      'IGIC 3%',                  # Canarias: no va al 303
    'iva_intra':  'IVA 0% Intracomunitario',  # casilla 59
    'iva_exento': 'IVA 0% Exportación',       # casilla 60
}

for _l in open(ENV_PS):
    _l = _l.strip()
    if _l and not _l.startswith('#') and '=' in _l:
        _k, _v = _l.split('=', 1)
        os.environ.setdefault(_k, _v.strip())
API, KEY = os.environ['PS_AUSTRAL_API_URL'], os.environ['PS_AUSTRAL_WS_KEY']

_rpc = None


def rpc():
    global _rpc
    if _rpc is None:
        _rpc = odoo_mod.get_rpc()
    return _rpc


def E(model, met, args, kw=None):
    return rpc().execute_kw(model, met, args,
                            dict(kw or {}, context={'allowed_company_ids': [COMPANY_ID]}))


def r2(x):
    """Redondeo a dos decimales con la mitad hacia arriba, como el de Odoo (el round()
    de Python redondea al par y descuadraría el asiento por un céntimo)."""
    return float(Decimal(str(x)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))


def base_y_cuota(total_bucket, tipo):
    """Base y cuota de un tramo de IVA de forma que base + cuota == total del tramo.

    PrestaShop suma la cuota pedido a pedido, así que su base y su cuota no siempre
    encajan al céntimo con el tipo (7.640,56 x 21% = 1.604,52 y no 1.604,54). Como el
    IVA lo calcula Odoo a partir de la base, se ajusta la base para que el asiento ate
    exacto con el cobro del banco; la diferencia son céntimos.
    """
    total_bucket = r2(total_bucket)
    if tipo <= 0:
        return total_bucket, 0.0
    base = r2(total_bucket / (1.0 + tipo / 100.0))
    for _ in range(4):
        cuota = r2(base * tipo / 100.0)
        if abs(base + cuota - total_bucket) < 0.005:
            return base, cuota
        base = r2(total_bucket - cuota)
    return base, r2(base * tipo / 100.0)


def classify_iva(base, total):
    if base <= 0.01:
        return 'iva_exento'
    ratio = total / base
    if abs(ratio - 1.21) < 0.005:
        return 'iva_21'
    if abs(ratio - 1.03) < 0.005:
        return 'iva_3'
    if abs(ratio - 1.00) < 0.005:
        return 'iva_intra'
    return 'iva_21'                      # ratio raro -> 21% por defecto


def fetch_ps(endpoint, desde, hasta):
    """Registros de PrestaShop. Cuidado con la forma de la respuesta: lista vacía
    cuando no hay nada, y clave en PLURAL cuando sí (order_slips)."""
    r = requests.get('%s/%s' % (API, endpoint), params={
        'ws_key': KEY, 'output_format': 'JSON', 'display': 'full', 'date': '1',
        'limit': '5000', 'filter[date_add]': '[%s,%s]' % (desde, hasta)}, timeout=120)
    r.raise_for_status()
    j = r.json()
    if isinstance(j, list):
        return j
    if not isinstance(j, dict):
        return []
    for clave in (endpoint, endpoint + 's', endpoint.rstrip('s'), endpoint.rstrip('s') + 's'):
        v = j.get(clave)
        if isinstance(v, list):
            return v
    for v in j.values():
        if isinstance(v, list):
            return v
    return []


def cargar_pendientes():
    try:
        with open(PEND_FILE) as f:
            return json.load(f)
    except Exception:          # noqa: BLE001
        return {}


def guardar_pendientes(p):
    try:
        os.makedirs(os.path.dirname(PEND_FILE), exist_ok=True)
        with open(PEND_FILE, 'w') as f:
            json.dump(p, f, indent=2, sort_keys=True)
    except Exception as e:     # noqa: BLE001
        print('[ps_liq] AVISO: no se pudo guardar %s: %s' % (PEND_FILE, e))


def anotar_pendiente(pend, dia, motivo):
    hoy = date.today().isoformat()
    e = pend.get(dia) or {'desde': hoy, 'intentos': 0}
    e['intentos'] = int(e.get('intentos', 0)) + 1
    e['motivo'] = motivo
    e['ultimo_intento'] = hoy
    pend[dia] = e
    print('  ↻ %s queda pendiente (%s) — intento %d, se reintenta hasta %d días desde %s'
          % (dia, motivo, e['intentos'], REINTENTO_DIAS, e['desde']))


def fechas_a_procesar(arg_date=None):
    if arg_date:
        return [datetime.strptime(arg_date, '%Y-%m-%d').date()]
    hoy = date.today()
    if hoy.weekday() == 0:               # lunes: viernes, sábado y domingo
        return [hoy - timedelta(days=3), hoy - timedelta(days=2), hoy - timedelta(days=1)]
    return [hoy - timedelta(days=1)]


def crear_asiento_dia(acc, jid, d, fac, abo, tax, dry=False, rehacer=False):
    """Crea (o rehace, con --rehacer) los asientos FAC y ABO del día.

    `tax` es {clase de IVA: impuesto de Odoo}. Cada clase lleva su propia línea de
    venta con su impuesto; la línea de IVA la añade Odoo con sus etiquetas.
    """
    d_str = d.isoformat()
    log = []

    def construir(total, n, buckets, is_abono):
        L = [{'name': ('LIQUIDACION PS DIA %s (%d fac)' % (d_str, n)) if not is_abono
              else ('ABONO PS DIA %s (%d abonos)' % (d_str, n)),
              'account_id': acc[ACC_CODES['puente']],
              'debit': 0 if is_abono else total, 'credit': total if is_abono else 0}]
        for cls in sorted(buckets):
            b = buckets[cls]
            tot_b = r2(b['total'])
            if abs(tot_b) < 0.005:
                continue
            t = tax.get(cls)
            base, _cuota = base_y_cuota(tot_b, (t or {}).get('amount') or 0.0)
            linea = {'name': '%s %s - %s' % ('ABONO VENTAS WEB' if is_abono else 'VENTAS WEB',
                                             d_str, cls),
                     'account_id': acc[ACC_CODES['ventas']],
                     'debit': base if is_abono else 0, 'credit': 0 if is_abono else base}
            if t:
                linea['tax_ids'] = [(6, 0, [t['id']])]
            L.append(linea)
        L.append({'name': 'LIQUIDACION%s FECHA %s' % (' ABO' if is_abono else '', d_str),
                  'account_id': acc[ACC_CODES['puente']],
                  'debit': total if is_abono else 0, 'credit': 0 if is_abono else total})
        L.append({'name': ('C/C BBVA TPV devolucion %s' if is_abono
                           else 'C/C BBVA TPV cobro %s') % d_str,
                  'account_id': acc[ACC_CODES['banco_tpv']],
                  'debit': 0 if is_abono else total, 'credit': total if is_abono else 0})
        return [dict(l, debit=round(l['debit'], 2), credit=round(l['credit'], 2)) for l in L]

    def cuadrar(mid, is_abono):
        """Deja el asiento cuadrado al céntimo contra el cobro del banco.

        Se hace todo en UNA escritura: se quita la línea de compensación que Odoo mete en
        su cuenta transitoria mientras el borrador está descuadrado y se coloca el céntimo
        a la vez, para que el asiento no pase por un estado descuadrado.

        El céntimo va a una línea exenta (0%, no arrastra cuota) o, si no hay, a una línea
        nueva de REDONDEO sin impuesto. A la base del 21% no, porque Odoo recalcularía la
        cuota y el ajuste no converge.
        """
        mias = set(acc.values())
        cero = {t['id'] for t in tax.values() if not (t.get('amount') or 0)}
        for _ in range(6):
            ls = E('account.move.line', 'search_read', [[('move_id', '=', mid)]],
                   {'fields': ['account_id', 'name', 'debit', 'credit', 'tax_ids',
                               'tax_line_id']})
            intrusas = [l for l in ls
                        if l['account_id'][0] not in mias and not l['tax_line_id']]
            ids_intrusas = {l['id'] for l in intrusas}
            propias = [l for l in ls if l['id'] not in ids_intrusas]
            desc = r2(sum(l['debit'] for l in propias) - sum(l['credit'] for l in propias))
            if not intrusas and abs(desc) < 0.005:
                return None
            cmds = [(2, l['id'], 0) for l in intrusas]
            if abs(desc) >= 0.005:
                # 1) una línea exenta o de redondeo ya existente (0% o sin impuesto)
                sueltas = [l for l in propias
                           if not l['tax_line_id'] and l['account_id'][0] == acc[ACC_CODES['ventas']]
                           and (not l['tax_ids'] or set(l['tax_ids']) <= cero)]
                if sueltas:
                    l = max(sueltas, key=lambda x: x['debit'] + x['credit'])
                    cmds.append((1, l['id'], {'debit': r2(l['debit'] - desc)} if is_abono
                                             else {'credit': r2(l['credit'] + desc)}))
                else:
                    # 2) línea nueva de redondeo, sin impuesto
                    cmds.append((0, 0, {'name': 'REDONDEO %s' % d_str,
                                        'account_id': acc[ACC_CODES['ventas']],
                                        'debit': r2(-desc) if desc < 0 else 0,
                                        'credit': r2(desc) if desc > 0 else 0}))
            E('account.move', 'write', [[mid], {'line_ids': cmds}])
        return 'no se pudo cuadrar el asiento'

    def publicar(mid):
        try:
            E('account.move', 'action_post', [[mid]])
        except Exception as e:                                # noqa: BLE001
            if 'cannot marshal None' not in str(e):           # ese fault = exito
                return str(e)[:90]
        return None

    def make_move(ref, total, base, n, buckets, is_abono):
        total = round(total, 2)
        ya = E('account.move', 'search_read', [[('company_id', '=', COMPANY_ID), ('ref', '=', ref)]],
               {'fields': ['name', 'state'], 'limit': 1})
        if ya and not rehacer:
            return {'ref': ref, 'skip': True, 'name': ya[0]['name']}
        payload = construir(total, n, buckets, is_abono)
        if dry:
            return {'ref': ref, 'dry': True, 'total': total, 'n': n, 'base': round(base, 2),
                    'rehecho': bool(ya)}
        if ya:
            # rehacer CONSERVANDO el numero: a borrador, se cambian las lineas y se
            # vuelve a publicar (no se borra el asiento, para no dejar huecos)
            mid = ya[0]['id']
            if ya[0]['state'] == 'posted':
                try:
                    E('account.move', 'button_draft', [[mid]])
                except Exception as e:                        # noqa: BLE001
                    if 'cannot marshal None' not in str(e):
                        return {'ref': ref, 'error': 'no se pudo pasar a borrador: %s' % str(e)[:80]}
            # las lineas de IVA no se pueden borrar a mano (Odoo protege el informe de
            # impuestos): se quita el impuesto de la linea de venta y Odoo las retira
            viejas = E('account.move.line', 'search_read', [[('move_id', '=', mid)]],
                       {'fields': ['id', 'tax_ids', 'tax_line_id']})
            con_imp = [l['id'] for l in viejas if l['tax_ids']]
            if con_imp:
                E('account.move.line', 'write', [con_imp, {'tax_ids': [(5, 0, 0)]}])
            viejas = E('account.move.line', 'search_read', [[('move_id', '=', mid)]],
                       {'fields': ['id']})
            E('account.move', 'write', [[mid], {
                'line_ids': [(2, l['id'], 0) for l in viejas] + [(0, 0, l) for l in payload]}])
        else:
            mid = E('account.move', 'create', [{'move_type': 'entry', 'company_id': COMPANY_ID,
                                                'journal_id': jid, 'date': d_str, 'ref': ref,
                                                'line_ids': [(0, 0, l) for l in payload]}])
        mal = cuadrar(mid, is_abono)
        if mal:
            return {'ref': ref, 'error': mal}
        mal = publicar(mid)
        if mal:
            return {'ref': ref, 'error': 'no se pudo publicar: %s' % mal}
        ls = E('account.move.line', 'search_read', [[('move_id', '=', mid)]],
               {'fields': ['account_id', 'debit', 'credit', 'tax_line_id', 'tax_tag_ids']})
        m = E('account.move', 'read', [[mid]], {'fields': ['name', 'state']})[0]
        ajenas = [l['account_id'][1] for l in ls
                  if l['account_id'][0] not in set(acc.values()) and not l['tax_line_id']]
        return {'ref': ref, 'name': m['name'], 'id': mid, 'total': total, 'n': n,
                'base': round(base, 2), 'rehecho': bool(ya), 'estado': m['state'],
                'iva': {c: round(b['iva'], 2) for c, b in buckets.items() if b['iva'] >= 0.005},
                'lineas_iva': sum(1 for l in ls if l['tax_line_id']),
                'con_etiqueta': sum(1 for l in ls if l['tax_tag_ids']),
                'cuentas_ajenas': ajenas}

    if fac:
        log.append({'tipo': 'FAC', **make_move('PS-LIQ-FAC-%s' % d_str,
                                               sum(b['total'] for b in fac.values()),
                                               sum(b['base'] for b in fac.values()),
                                               sum(b['n'] for b in fac.values()), fac, False)})
    if abo:
        log.append({'tipo': 'ABO', **make_move('PS-LIQ-ABO-%s' % d_str,
                                               sum(b['total'] for b in abo.values()),
                                               sum(b['base'] for b in abo.values()),
                                               sum(b['n'] for b in abo.values()), abo, True)})
    return log


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date')
    ap.add_argument('--from', dest='dfrom')
    ap.add_argument('--to', dest='dto')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--sin-reintentos', action='store_true')
    ap.add_argument('--rehacer', action='store_true',
                    help='rehace los asientos que ya existan, conservando su numero')
    args = ap.parse_args()

    # guard: este script SOLO contabiliza en la company 12 de Austral
    if companies.PIPELINE_NAME != 'austral_e18' or COMPANY_ID not in companies.BY_COMPANY_ID:
        raise SystemExit('PIPELINE_MISMATCH: este script es de austral_e18 (company 12)')

    manual = bool(args.date or (args.dfrom and args.dto))
    if args.dfrom and args.dto:
        ini = datetime.strptime(args.dfrom, '%Y-%m-%d').date()
        fin = datetime.strptime(args.dto, '%Y-%m-%d').date()
        fechas = [ini + timedelta(days=i) for i in range((fin - ini).days + 1)]
    else:
        fechas = fechas_a_procesar(args.date)

    pend = {} if (manual or args.sin_reintentos) else cargar_pendientes()
    hoy = date.today()
    reintentos = []
    for dia, info in sorted(pend.items()):
        try:
            edad = (hoy - datetime.strptime(info.get('desde', dia), '%Y-%m-%d').date()).days
        except Exception:      # noqa: BLE001
            edad = 0
        if edad > REINTENTO_DIAS:
            print('[ps_liq] ABANDONADO %s: %d días sin datos (%s) — revísalo a mano'
                  % (dia, REINTENTO_DIAS, info.get('motivo')))
            continue
        reintentos.append(datetime.strptime(dia, '%Y-%m-%d').date())
    for dia in list(pend):
        try:
            if (hoy - datetime.strptime(pend[dia].get('desde', dia), '%Y-%m-%d').date()).days > REINTENTO_DIAS:
                pend.pop(dia)
        except Exception:      # noqa: BLE001
            pend.pop(dia)

    fechas = sorted(set(fechas) | set(reintentos))
    print('[ps_liq] fechas: %s%s%s' % (
        '%s → %s (%d días)' % (fechas[0], fechas[-1], len(fechas)) if len(fechas) > 3
        else [f.isoformat() for f in fechas],
        ' · %d de la cola de reintentos' % len(reintentos) if reintentos else '',
        ' [DRY-RUN]' if args.dry_run else ''))

    acc = {a['code']: a['id'] for a in E('account.account', 'search_read',
                                         [[('company_ids', 'in', [COMPANY_ID]),
                                           ('code', 'in', list(ACC_CODES.values()))]],
                                         {'fields': ['code']})}
    faltan = [c for c in ACC_CODES.values() if c not in acc]
    if faltan:
        raise SystemExit('faltan cuentas en la company %d: %s' % (COMPANY_ID, faltan))
    tax = {}
    for cls, nombre in TAX_NAMES.items():
        t = E('account.tax', 'search_read', [[('company_id', '=', COMPANY_ID),
                                              ('type_tax_use', '=', 'sale'),
                                              ('name', '=', nombre)]],
              {'fields': ['name', 'amount'], 'limit': 1})
        if not t:
            raise SystemExit('falta el impuesto "%s" en la company %d' % (nombre, COMPANY_ID))
        tax[cls] = t[0]
    print('[ps_liq] impuestos: %s' % ', '.join('%s=%s' % (c, t['name'])
                                               for c, t in sorted(tax.items())))

    jid = E('account.journal', 'search', [[('company_id', '=', COMPANY_ID),
                                           ('code', '=', JOURNAL_CODE)]], {'limit': 1})
    if not jid:
        raise SystemExit('no existe el diario %s en la company %d' % (JOURNAL_CODE, COMPANY_ID))
    jid = jid[0]

    todo, saltados, errores = [], [], []
    for f in fechas:
        d_str = f.isoformat()
        try:
            invs = fetch_ps('order_invoices', d_str + ' 00:00:00', d_str + ' 23:59:59')
            slips = fetch_ps('order_slip', d_str + ' 00:00:00', d_str + ' 23:59:59')
        except Exception as e:                                # noqa: BLE001
            anotar_pendiente(pend, d_str, 'error de la API: %s' % str(e)[:70])
            continue
        if not invs and not slips:
            anotar_pendiente(pend, d_str, 'PrestaShop no devolvió ningún registro')
            continue
        fac = defaultdict(lambda: {'n': 0, 'base': 0.0, 'iva': 0.0, 'total': 0.0})
        abo = defaultdict(lambda: {'n': 0, 'base': 0.0, 'iva': 0.0, 'total': 0.0})
        for inv in invs:
            base = float(inv['total_paid_tax_excl']); total = float(inv['total_paid_tax_incl'])
            b = fac[classify_iva(base, total)]
            b['n'] += 1; b['base'] += base; b['iva'] += total - base; b['total'] += total
        for s in slips:
            base = float(s.get('total_products_tax_excl', 0) or 0) + \
                float(s.get('total_shipping_tax_excl', 0) or 0)
            total = float(s.get('amount', 0) or 0)
            if total == 0:
                total = float(s.get('total_products_tax_incl', 0) or 0) + \
                    float(s.get('total_shipping_tax_incl', 0) or 0)
            b = abo[classify_iva(base, total)]
            b['n'] += 1; b['base'] += base; b['iva'] += total - base; b['total'] += total
        try:
            for x in crear_asiento_dia(acc, jid, f, dict(fac), dict(abo), tax,
                                       dry=args.dry_run, rehacer=args.rehacer):
                if x.get('skip'):
                    saltados.append(x)
                elif x.get('error'):
                    errores.append(x)
                else:
                    todo.append(x)
            pend.pop(d_str, None)
        except Exception as e:                                # noqa: BLE001
            errores.append({'ref': d_str, 'error': str(e)[:110]})
            anotar_pendiente(pend, d_str, 'error al contabilizar: %s' % str(e)[:70])

    if not (manual or args.sin_reintentos):
        guardar_pendientes(pend)

    print('[ps_liq] asientos %s: %d · ya existían: %d · errores: %d'
          % ('que se crearían' if args.dry_run else 'creados', len(todo), len(saltados), len(errores)))
    for x in todo[:80]:
        print('   %s %-4s %-22s %-18s total %10.2f n=%-5d lineas IVA:%s con etiqueta:%s'
              % ('~' if x.get('rehecho') else '+', x['tipo'], x['ref'],
                 x.get('name') or '(dry)', x['total'], x['n'],
                 x.get('lineas_iva', '-'), x.get('con_etiqueta', '-')))
    for x in saltados[:10]:
        print('   = ya estaba %-22s %s' % (x['ref'], x.get('name')))
    for x in errores[:10]:
        print('   ✗ %-22s %s' % (x['ref'], x['error']))
    if pend:
        print('[ps_liq] cola de reintentos: %s' % sorted(pend))
    out = '/tmp/ps_liq_e18_%s.json' % date.today().isoformat()
    try:
        with open(out, 'w') as fh:
            json.dump({'fechas': [f.isoformat() for f in fechas], 'asientos': todo,
                       'saltados': saltados, 'errores': errores,
                       'pendientes': sorted(pend)}, fh, indent=2, default=str)
        print('[ps_liq] detalle en %s' % out)
    except Exception:          # noqa: BLE001
        pass


if __name__ == '__main__':
    main()
