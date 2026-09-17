"""Contabilización de documentos de TARJETA de crédito.

Criterio = el de los asientos ya contabilizados (p.ej. TARJ9/2026/0060 y BNK1/2026/0924):

- TICKET/FACTURA de compra pagada con tarjeta:
    (1) la factura de proveedor normal (process_invoice: gasto por histórico + IVA → 4xx)
    (2) el PAGO con tarjeta, asiento en el diario de la tarjeta:
        DEBE cuenta del proveedor (4xx) / HABER cuenta 5200xx de la tarjeta.
    (3) si no va a revisión, la factura se DA POR PAGADA en el momento (regla usuario
        20 jul 2026): se publican factura + pago y se casan las líneas 4xx —
        marcar_pagada(). Si alguien subió una factura equivocada, saldrá después.
- RECIBO/EXTRACTO mensual de la tarjeta → CARGO, asiento en el diario del banco que
  usan los cargos históricos de esa tarjeta: DEBE 5200xx / HABER 572xxx. Este cargo
  se hace UNA sola vez por mes: si el extracto bancario ya lo trajo (línea enrutada
  a la 5200xx), NO se duplica.
"""
import base64
import logging
from datetime import date, timedelta

log = logging.getLogger("tarjeta")


def _accion(rpc, cid, model, ids, method):
    """action_post/reconcile devuelven None y el XML-RPC no lo serializa
    ('cannot marshal None') — ese fault concreto es éxito."""
    try:
        rpc.call_method(model, ids, method, company_id=cid)
    except Exception as e:  # noqa: BLE001
        if 'cannot marshal None' not in str(e):
            raise


def marcar_pagada(rpc, company, bill_move_id, pago_move_id):
    """Publica factura + asiento de pago y CASA sus líneas 4xx → la factura queda
    PAGADA. Devuelve {'ok': bool, 'error': str|None}."""
    cid = company['odoo_company_id']
    try:
        for mid in (bill_move_id, pago_move_id):
            st = rpc.read('account.move', [mid], ['state'], company_id=cid)[0]['state']
            if st == 'draft':
                _accion(rpc, cid, 'account.move', [mid], 'action_post')
        amls = rpc.search_read('account.move.line',
                               [('move_id', 'in', [bill_move_id, pago_move_id]),
                                ('account_id.account_type', '=', 'liability_payable'),
                                ('reconciled', '=', False)],
                               ['id', 'account_id'], company_id=cid)
        # solo si comparten cuenta 4xx (factura + pago) y hay ambas patas
        cuentas = {a['account_id'][0] for a in amls}
        if len(amls) >= 2 and len(cuentas) == 1:
            _accion(rpc, cid, 'account.move.line', [a['id'] for a in amls], 'reconcile')
        pagada = rpc.read('account.move', [bill_move_id], ['payment_state'],
                          company_id=cid)[0].get('payment_state')
        if pagada not in ('paid', 'in_payment'):
            return {'ok': False, 'error': f'payment_state={pagada} tras casar'}
        return {'ok': True, 'error': None}
    except Exception as e:  # noqa: BLE001
        return {'ok': False, 'error': str(e)[:200]}


def card_account_id(rpc, cid, account_name):
    """Id de la cuenta 5200xx de la tarjeta, derivado de sus apuntes (fiable en Odoo 18)."""
    r = rpc.search_read('account.move.line',
                        [('company_id', '=', cid), ('account_id.name', '=', account_name)],
                        ['account_id'], limit=1, company_id=cid)
    if not r:
        r = rpc.search_read('account.move.line',
                            [('company_id', '=', cid), ('account_id.name', 'ilike', account_name[-4:])],
                            ['account_id'], limit=1, company_id=cid)
    return r[0]['account_id'][0] if r else None


def card_journal_id(rpc, cid, account_name):
    """Diario de la tarjeta (p.ej. 'Tarjeta Bankinter 3840') — por los 4 últimos dígitos."""
    last4 = account_name.strip()[-4:]
    r = rpc.search_read('account.journal',
                        [('company_id', '=', cid), ('name', 'ilike', last4)],
                        ['id', 'name'], limit=1, company_id=cid)
    return r[0]['id'] if r else None


def _attach(rpc, cid, move_id, pdf_bytes, pdf_name):
    if not (pdf_bytes and move_id):
        return None
    try:
        return rpc.create('ir.attachment', {
            'name': pdf_name, 'res_model': 'account.move', 'res_id': move_id,
            'type': 'binary', 'datas': base64.b64encode(pdf_bytes).decode(),
        }, company_id=cid)
    except Exception as e:  # noqa: BLE001 — el adjunto es secundario
        log.warning(f'attach fallo move {move_id}: {e}')
        return None


def pago_tarjeta(rpc, company, card, data, bill_move_id, partner_id):
    """Asiento de PAGO con tarjeta (borrador): DEBE 4xx proveedor / HABER 5200xx."""
    cid = company['odoo_company_id']
    total = round(float(data.get('total') or 0), 2)
    ref = f"{data.get('invoice_ref') or ''} {data.get('supplier_name') or ''} (tarjeta {card['account_name'][-4:]})".strip()
    # dedup por ref en el diario de la tarjeta
    jid = card_journal_id(rpc, cid, card['account_name'])
    acc_card = card_account_id(rpc, cid, card['account_name'])
    if not jid or not acc_card:
        return {'status': 'error', 'error': f"tarjeta sin diario/cuenta en Odoo ({card['account_name']})"}
    dup = rpc.search_read('account.move',
                          [('journal_id', '=', jid), ('ref', '=', ref), ('state', '!=', 'cancel')],
                          ['id'], limit=1, company_id=cid)
    if dup:
        return {'status': 'duplicate', 'move_id': dup[0]['id']}
    # línea de proveedor (4xx) de la factura creada: su cuenta y, si la factura va
    # en DIVISA (p.ej. Atlassian en USD), su moneda e importes para espejar el pago
    # en la misma divisa. Así Odoo hace el cambio a EUR y la 4xx casa exacta, sin
    # diferencias de cambio espurias.
    pay = rpc.search_read('account.move.line',
                          [('move_id', '=', bill_move_id),
                           ('account_id.account_type', '=', 'liability_payable')],
                          ['account_id', 'currency_id', 'amount_currency', 'balance'],
                          limit=1, company_id=cid)
    if not pay:
        return {'status': 'error', 'error': 'la factura no tiene línea de proveedor (4xx)'}
    inv = pay[0]
    acc_prov = inv['account_id'][0]
    concepto = ref[:120]

    # ¿la factura está en una divisa distinta a la de la empresa?
    comp_cur = None
    try:
        co = rpc.read('res.company', [cid], ['currency_id'], company_id=cid)
        comp_cur = co[0]['currency_id'][0] if co and co[0].get('currency_id') else None
    except Exception:  # noqa: BLE001
        comp_cur = None
    inv_cur = inv['currency_id'][0] if inv.get('currency_id') else None
    eur = round(abs(float(inv.get('balance') or 0)), 2)          # contravalor EUR (Odoo)
    div = round(abs(float(inv.get('amount_currency') or 0)), 2)  # importe en divisa
    en_divisa = bool(inv_cur and comp_cur and inv_cur != comp_cur and eur > 0 and div > 0)

    if en_divisa:
        # la 4xx de una factura de proveedor es HABER → el pago la lleva al DEBE.
        # amount_currency: + en el DEBE (410), − en el HABER (520). debit/credit en EUR.
        linea_prov = {'account_id': acc_prov, 'partner_id': partner_id, 'name': concepto,
                      'debit': eur, 'credit': 0.0,
                      'currency_id': inv_cur, 'amount_currency': div}
        linea_card = {'account_id': acc_card, 'partner_id': partner_id, 'name': concepto,
                      'debit': 0.0, 'credit': eur,
                      'currency_id': inv_cur, 'amount_currency': -div}
    else:
        linea_prov = {'account_id': acc_prov, 'partner_id': partner_id, 'name': concepto,
                      'debit': total, 'credit': 0.0}
        linea_card = {'account_id': acc_card, 'partner_id': partner_id, 'name': concepto,
                      'debit': 0.0, 'credit': total}

    move_id = rpc.create('account.move', {
        'move_type': 'entry', 'journal_id': jid, 'date': data.get('invoice_date'),
        'ref': ref, 'company_id': cid,
        'line_ids': [(0, 0, linea_prov), (0, 0, linea_card)],
    }, company_id=cid)
    return {'status': 'created', 'move_id': move_id}


def _banco_historico(rpc, cid, acc_card):
    """(journal_id, account_id del banco 572) que usan los CARGOS históricos de la tarjeta."""
    lines = rpc.search_read('account.move.line',
                            [('company_id', '=', cid), ('account_id', '=', acc_card),
                             ('debit', '>', 0), ('parent_state', '=', 'posted'),
                             ('journal_id.type', '=', 'bank')],
                            ['move_id', 'journal_id'], order='date desc, id desc',
                            limit=5, company_id=cid)
    for l in lines:
        contra = rpc.search_read('account.move.line',
                                 [('move_id', '=', l['move_id'][0]), ('credit', '>', 0),
                                  ('account_id.account_type', '=', 'asset_cash')],
                                 ['account_id'], limit=1, company_id=cid)
        if contra:
            return l['journal_id'][0], contra[0]['account_id'][0]
    return None, None


def cargo_tarjeta(rpc, company, card, data, pdf_bytes=None, pdf_name=None):
    """CARGO del extracto de la tarjeta (borrador): DEBE 5200xx / HABER 572 banco."""
    cid = company['odoo_company_id']
    total = round(abs(float(data.get('total') or 0)), 2)
    fecha = data.get('invoice_date')
    if not total or not fecha:
        return {'status': 'error', 'error': 'extracto sin total o sin fecha de cargo'}
    acc_card = card_account_id(rpc, cid, card['account_name'])
    if not acc_card:
        return {'status': 'error', 'error': f"cuenta de tarjeta no encontrada ({card['account_name']})"}
    jid, acc_bank = _banco_historico(rpc, cid, acc_card)
    if not jid:
        return {'status': 'error',
                'error': 'sin cargos históricos: no sé qué banco usa esta tarjeta (contabiliza el primero a mano)'}
    ref = f"Cargo tarjeta {card['account_name'][-4:]} {data.get('invoice_ref') or fecha}"
    dup = rpc.search_read('account.move',
                          [('journal_id', '=', jid), ('ref', '=', ref), ('state', '!=', 'cancel')],
                          ['id'], limit=1, company_id=cid)
    if dup:
        return {'status': 'duplicate', 'move_id': dup[0]['id']}
    # el cargo mensual se hace UNA vez: si el extracto bancario ya lo trajo (línea
    # de extracto enrutada a la 5200xx por el mismo importe ±7 días), no duplicar
    try:
        d0 = date.fromisoformat(str(fecha))
        ya_ext = rpc.search_read('account.move.line',
                                 [('account_id', '=', acc_card), ('debit', '=', total),
                                  ('statement_line_id', '!=', False),
                                  ('parent_state', '!=', 'cancel'),
                                  ('date', '>=', (d0 - timedelta(days=7)).isoformat()),
                                  ('date', '<=', (d0 + timedelta(days=7)).isoformat())],
                                 ['move_id'], limit=1, company_id=cid)
        if ya_ext:
            return {'status': 'duplicate', 'move_id': ya_ext[0]['move_id'][0],
                    'nota': 'el extracto bancario ya contabilizó este cargo'}
    except Exception as e:  # noqa: BLE001 — el guard nunca bloquea el flujo normal
        log.warning(f'guard extracto cargo tarjeta: {e}')
    move_id = rpc.create('account.move', {
        'move_type': 'entry', 'journal_id': jid, 'date': fecha, 'ref': ref, 'company_id': cid,
        'line_ids': [
            (0, 0, {'account_id': acc_card, 'name': ref, 'debit': total, 'credit': 0.0}),
            (0, 0, {'account_id': acc_bank, 'name': ref, 'debit': 0.0, 'credit': total}),
        ],
    }, company_id=cid)
    _attach(rpc, cid, move_id, pdf_bytes, pdf_name)
    return {'status': 'created', 'move_id': move_id}
