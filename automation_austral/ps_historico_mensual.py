#!/usr/bin/env python3
"""Cron día 2 de cada mes: descarga TODAS las facturas + abonos B2C del mes anterior
individualizadas por cliente, las añade como hoja al Google Sheet histórico
'Facturas B2C por clientes' en Drive AUSTRAL/informes.

Cada hoja se llama '<MES_ES> <AÑO>' (ej. 'MAYO 2026') y contiene:
- Detalle por factura: nº fac, fecha, cliente, base, IVA por tipo, total
- Bloque totales por tipo IVA
- Bloque "Contabilizado en Odoo": saldos 430098000 + 477000021/3/22 del mes
- Diferencia PS vs Odoo
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

import sys, os, json, argparse, subprocess
from datetime import date, timedelta, datetime
from calendar import monthrange
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from collections import defaultdict


def load_env_file(path):
    if not os.path.exists(path): return
    for line in open(path):
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k, v = line.split('=', 1); os.environ.setdefault(k, v.strip())
load_env_file('/etc/austral_prestashop.env')

API = os.environ['PS_AUSTRAL_API_URL']
KEY = os.environ['PS_AUSTRAL_WS_KEY']

COMPANY_ID = 4
# Google Sheet creado por el usuario en informes/. SA tiene permisos de edición.
SPREADSHEET_ID = '1ZX_KXMMfiQKhQdvEVIYjDhLGh7EciugOHKr8MtHa25Q'
SCOPES = ['https://www.googleapis.com/auth/drive', 'https://www.googleapis.com/auth/spreadsheets']

ACC_PUENTE = '430098000'
ACC_IVA = {'21%':'477000021', '3% IGIC':'477000003', 'intracom':'477000022'}

MESES_ES = ['','ENERO','FEBRERO','MARZO','ABRIL','MAYO','JUNIO',
            'JULIO','AGOSTO','SEPTIEMBRE','OCTUBRE','NOVIEMBRE','DICIEMBRE']


def classify_iva(base, total):
    if base <= 0.01: return 'exento'
    ratio = total / base
    if abs(ratio - 1.21) < 0.005: return '21%'
    if abs(ratio - 1.03) < 0.005: return '3% IGIC'
    if abs(ratio - 1.00) < 0.005: return 'intracom'
    return f'otro ({ratio:.4f})'


def fetch_ps_all(endpoint, desde, hasta):
    """Descarga todo en una llamada (PS soporta limit alto)."""
    r = requests.get(f"{API}/{endpoint}", params={
        'ws_key': KEY, 'output_format':'JSON', 'display':'full', 'date':'1',
        'limit': '10000', 'filter[date_add]': f'[{desde},{hasta}]',
    }, timeout=180)
    if r.status_code != 200: return []
    try: j = r.json()
    except: return []
    for k in [endpoint, endpoint.rstrip('s'), f'{endpoint}s']:
        if k in j and isinstance(j[k], list): return j[k]
    return []


def fetch_customer_map(customer_ids):
    out = {}
    for cid in set(str(c) for c in customer_ids if c):
        try:
            r = requests.get(f"{API}/customers/{cid}", params={'ws_key':KEY,'output_format':'JSON'}, timeout=30)
            if r.status_code == 200:
                c = r.json().get('customer', {})
                out[cid] = (f"{c.get('firstname','')} {c.get('lastname','')}".strip(), c.get('email',''))
        except: pass
    return out


def odoo_month_totals(month, year):
    last_day = monthrange(year, month)[1]
    start = f'{year}-{month:02d}-01'
    end = f'{year}-{month:02d}-{last_day:02d}'
    accs = [('puente', ACC_PUENTE),('iva_21', ACC_IVA['21%']),('iva_3', ACC_IVA['3% IGIC']),
            ('iva_intra', ACC_IVA['intracom']),('ventas','700000001'),('banco_tpv','572000039')]
    res = {}
    for label, code in accs:
        q = (f"SELECT COALESCE(SUM(aml.debit),0)::numeric(20,2), COALESCE(SUM(aml.credit),0)::numeric(20,2) "
             f"FROM account_move_line aml JOIN account_account a ON a.id=aml.account_id "
             f"JOIN account_move m ON m.id=aml.move_id "
             f"WHERE m.company_id={COMPANY_ID} AND m.state='posted' "
             f"AND a.code='{code}' AND m.date BETWEEN '{start}' AND '{end}'")
        out = subprocess.check_output(['sudo','-u','odoo','psql','-d','cararjfam_test','-tA','-F','|','-c', q], text=True).strip()
        parts = out.split('|') if out else ['0','0']
        d = float(parts[0] or 0); h = float(parts[1] or 0) if len(parts) > 1 else 0
        res[label] = {'debe': d, 'haber': h, 'saldo': d - h}
    return res


# === Google Sheets helpers ===
def get_sheets_service():
    creds = service_account.Credentials.from_service_account_file('/etc/automation_sa.json', scopes=SCOPES)
    return build('sheets','v4', credentials=creds, cache_discovery=False)


def ensure_sheet(svc, ssid, sheet_name):
    """Crea la hoja si no existe; si existe (case-insensitive), la limpia.
    Devuelve (sheetId, actual_title) — actual_title puede diferir en case del solicitado."""
    meta = svc.spreadsheets().get(spreadsheetId=ssid).execute()
    sheet_id = None
    actual_title = sheet_name
    for s in meta.get('sheets', []):
        if s['properties']['title'].lower() == sheet_name.lower():
            sheet_id = s['properties']['sheetId']
            actual_title = s['properties']['title']  # respetar case existente
            break
    if sheet_id is not None:
        svc.spreadsheets().values().clear(spreadsheetId=ssid, range=actual_title).execute()
    else:
        req = {'requests':[{'addSheet':{'properties':{'title': sheet_name}}}]}
        resp = svc.spreadsheets().batchUpdate(spreadsheetId=ssid, body=req).execute()
        sheet_id = resp['replies'][0]['addSheet']['properties']['sheetId']
        actual_title = sheet_name
    return sheet_id, actual_title


def write_rows(svc, ssid, sheet_name, rows, start_cell='A1'):
    svc.spreadsheets().values().update(
        spreadsheetId=ssid, range=f"{sheet_name}!{start_cell}",
        valueInputOption='USER_ENTERED',
        body={'values': rows},
    ).execute()


def format_sheet(svc, ssid, sheet_id, header_row=1, total_columns=9):
    """Formato: header bold + fondo azul + freeze, formato número en columnas €."""
    reqs = [
        # Header bold + bg color
        {'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':0,'endRowIndex':header_row,
                                 'startColumnIndex':0,'endColumnIndex':total_columns},
                       'cell':{'userEnteredFormat':{'textFormat':{'bold':True,'foregroundColor':{'red':1,'green':1,'blue':1}},
                                                     'backgroundColor':{'red':0.19,'green':0.31,'blue':0.59},
                                                     'horizontalAlignment':'CENTER'}},
                       'fields':'userEnteredFormat(textFormat,backgroundColor,horizontalAlignment)'}},
        # Freeze header
        {'updateSheetProperties':{'properties':{'sheetId':sheet_id,'gridProperties':{'frozenRowCount':header_row}},
                                   'fields':'gridProperties.frozenRowCount'}},
        # Format Base/IVA/Total columns as number with 2 decimals
        {'repeatCell':{'range':{'sheetId':sheet_id,'startRowIndex':1,
                                 'startColumnIndex':5,'endColumnIndex':9},
                       'cell':{'userEnteredFormat':{'numberFormat':{'type':'NUMBER','pattern':'#,##0.00'}}},
                       'fields':'userEnteredFormat.numberFormat'}},
    ]
    svc.spreadsheets().batchUpdate(spreadsheetId=ssid, body={'requests':reqs}).execute()


def build_month_data(month, year, invs, slips, cust_map, odoo_tot):
    rows = [['Tipo','Nº fac/abo PS','Fecha','Cliente','Email','Base €','Tipo IVA','IVA €','Total €']]
    totals_by_iva = defaultdict(lambda: {'base':0.0,'iva':0.0,'total':0.0,'n':0})

    # Facturas
    for inv in invs:
        base = float(inv['total_paid_tax_excl'])
        total = float(inv['total_paid_tax_incl'])
        cls = classify_iva(base, total)
        iva = total - base
        cli = cust_map.get('inv_' + str(inv['id']), ('', ''))
        rows.append(['Fac', inv['number'], inv['date_add'][:10], cli[0], cli[1],
                     round(base,2), cls, round(iva,2), round(total,2)])
        totals_by_iva[cls]['base'] += base
        totals_by_iva[cls]['iva'] += iva
        totals_by_iva[cls]['total'] += total
        totals_by_iva[cls]['n'] += 1

    # Abonos
    for slip in slips:
        base = float(slip.get('total_products_tax_excl',0) or 0) + float(slip.get('total_shipping_tax_excl',0) or 0)
        total = float(slip.get('amount',0) or 0)
        if total == 0:
            total = float(slip.get('total_products_tax_incl',0) or 0) + float(slip.get('total_shipping_tax_incl',0) or 0)
        cls = classify_iva(base, total)
        iva = total - base
        cli = cust_map.get('slip_' + str(slip['id']), ('', ''))
        rows.append(['Abo', slip['id'], slip['date_add'][:10], cli[0], cli[1],
                     -round(base,2), cls, -round(iva,2), -round(total,2)])
        totals_by_iva[cls]['base'] -= base
        totals_by_iva[cls]['iva'] -= iva
        totals_by_iva[cls]['total'] -= total

    # Bloque totales PS
    rows.append([])
    rows.append(['TOTALES PS','','','','','','','',''])
    for label in sorted(totals_by_iva):
        v = totals_by_iva[label]
        rows.append(['', '', '', '', f'IVA {label}',
                     round(v['base'],2), label, round(v['iva'],2), round(v['total'],2)])

    # Bloque Odoo
    rows.append([])
    rows.append(['CONTABILIZADO EN ODOO (mes)','','','','','','','',''])
    rows.append(['Cuenta','Debe €','Haber €','Saldo €','','','','',''])
    for lbl, code in [('430098000 (puente)','puente'),('477000021 (IVA 21%)','iva_21'),
                       ('477000003 (IGIC 3%)','iva_3'),('477000022 (intracom)','iva_intra'),
                       ('700000001 (ventas)','ventas'),('572000039 (BBVA TPV)','banco_tpv')]:
        t = odoo_tot.get(code, {'debe':0,'haber':0,'saldo':0})
        rows.append([lbl, round(t['debe'],2), round(t['haber'],2), round(t['saldo'],2),'','','','',''])

    # Bloque Diferencias
    rows.append([])
    rows.append(['DIFERENCIA PS vs ODOO','','','','','','','',''])
    ps_iva_21 = totals_by_iva.get('21%', {}).get('iva', 0)
    odoo_iva_21 = odoo_tot.get('iva_21', {}).get('haber', 0) - odoo_tot.get('iva_21', {}).get('debe', 0)
    rows.append(['IVA 21% PS', round(ps_iva_21,2),'Odoo neto', round(odoo_iva_21,2),
                 'Diff', round(ps_iva_21 - odoo_iva_21, 2),'','',''])
    ps_iva_3 = totals_by_iva.get('3% IGIC', {}).get('iva', 0)
    odoo_iva_3 = odoo_tot.get('iva_3', {}).get('haber', 0) - odoo_tot.get('iva_3', {}).get('debe', 0)
    rows.append(['IGIC 3% PS', round(ps_iva_3,2),'Odoo neto', round(odoo_iva_3,2),
                 'Diff', round(ps_iva_3 - odoo_iva_3, 2),'','',''])
    ps_total = sum(b['total'] for b in totals_by_iva.values())
    odoo_total_d = odoo_tot.get('puente', {}).get('debe', 0)
    rows.append(['Total facturado PS (D 430098000)', round(ps_total,2),
                 'Odoo D 430098000', round(odoo_total_d,2),
                 'Diff', round(ps_total - odoo_total_d, 2),'','',''])

    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--month', type=int, help='1-12 (default: mes anterior al actual)')
    parser.add_argument('--year', type=int, help='YYYY')
    args = parser.parse_args()

    today = date.today()
    if args.month:
        month, year = args.month, args.year or today.year
    else:
        prev = today.replace(day=1) - timedelta(days=1)
        month, year = prev.month, prev.year

    last_day = monthrange(year, month)[1]
    desde = f'{year}-{month:02d}-01 00:00:00'
    hasta = f'{year}-{month:02d}-{last_day:02d} 23:59:59'
    sheet_name = f'{MESES_ES[month]} {year}'
    print(f'[ps_historico_mensual] mes: {sheet_name}  rango {desde} → {hasta}')

    invs = fetch_ps_all('order_invoices', desde, hasta)
    slips = fetch_ps_all('order_slip', desde, hasta)
    print(f'PS: {len(invs)} facturas + {len(slips)} abonos')

    # Customers para abonos (directo via id_customer)
    cust_ids_slip = [s.get('id_customer') for s in slips]
    # Customers para facturas: hay que ir vía order
    print('Resolviendo customers de facturas (via order)...')
    inv_to_cust = {}
    cust_ids_inv = []
    for inv in invs:
        oid = inv.get('id_order')
        if not oid: continue
        try:
            r = requests.get(f"{API}/orders/{oid}", params={'ws_key':KEY,'output_format':'JSON'}, timeout=30)
            if r.status_code == 200:
                cid = r.json().get('order',{}).get('id_customer','')
                if cid:
                    inv_to_cust[str(inv['id'])] = str(cid)
                    cust_ids_inv.append(cid)
        except: pass

    all_cust_ids = list(set(cust_ids_slip + cust_ids_inv))
    print(f'Resolviendo {len(all_cust_ids)} customers únicos...')
    cust_data = fetch_customer_map(all_cust_ids)
    cust_map = {}
    for inv in invs:
        cid = inv_to_cust.get(str(inv['id']),'')
        if cid: cust_map['inv_' + str(inv['id'])] = cust_data.get(cid, ('',''))
    for s in slips:
        cid = str(s.get('id_customer',''))
        if cid: cust_map['slip_' + str(s['id'])] = cust_data.get(cid, ('',''))

    odoo_tot = odoo_month_totals(month, year)

    # Construir filas
    rows = build_month_data(month, year, invs, slips, cust_map, odoo_tot)
    print(f'Total filas a escribir: {len(rows)}')

    # Escribir en Google Sheet
    svc = get_sheets_service()
    sheet_id, actual_name = ensure_sheet(svc, SPREADSHEET_ID, sheet_name)
    write_rows(svc, SPREADSHEET_ID, actual_name, rows)
    format_sheet(svc, SPREADSHEET_ID, sheet_id)
    print(f'[ps_historico_mensual] OK — hoja "{actual_name}" escrita en https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}')


if __name__ == '__main__':
    main()
