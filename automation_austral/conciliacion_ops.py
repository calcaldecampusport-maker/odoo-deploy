#!/usr/bin/env python3
"""Puente ORM de conciliación bancaria para la web austral-contab-web.
Comandos:
  --propose --company-id 4                  -> JSON {items:[{line_id,date,amount,concept,journal,saldo,ref,proposals:[...]}]}
  --resolve --line-id N --action account --account CODE [--partner NAME] [--learn 1] [--pattern TXT]
  --resolve --line-id N --action move --move-id M [--learn 1] [--pattern TXT]
  --auto-reconcile --company-id 4           -> aplica learned.rule(bank) a líneas pendientes (post-import)
Reutiliza bank_matcher.propose_for_company y learned.rule. odoo17 venv.
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

import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
try:
    import companies as _comp_guard
    if getattr(_comp_guard, "PIPELINE_NAME", None) != 'austral':
        raise RuntimeError("PIPELINE_MISMATCH")
except ImportError:
    pass

import argparse, json
ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
if ODOO_PATH not in _sys.path:
    _sys.path.insert(0, ODOO_PATH)
import odoo
from odoo.api import Environment
import bank_matcher

DB_NAME = getattr(_comp_guard, "DB_NAME", "cararjfam_test") if 'companies' in dir() else "cararjfam_test"


def _tr(v):
    return (v.get('es_ES') or v.get('en_US')) if isinstance(v, dict) else v


def _sanitize(items):
    """Convierte nombres jsonb {en_US} a string en items + proposals."""
    for it in items:
        it['journal'] = _tr(it.get('journal'))
        for p in it.get('proposals', []):
            for k in ('account_name', 'partner', 'rule_name', 'move_name', 'ref'):
                if k in p:
                    p[k] = _tr(p[k])
    return items


def cmd_propose(env, company_id):
    items = bank_matcher.propose_for_company(env, company_id, max_lines=500)
    # añadir saldo/ref desde la línea
    for it in items:
        ln = env['account.bank.statement.line'].browse(it['line_id'])
        it['ref'] = ln.payment_ref or ''
        it['partner_hint'] = _tr(ln.partner_id.name) if ln.partner_id else None
        it['statement'] = _tr(ln.statement_id.name) if ln.statement_id else None
        # fiabilidad = mejor score de las propuestas
        it['best_score'] = max([p.get('score', 0) for p in it.get('proposals', [])], default=0)
    _sanitize(items)
    return {'company_id': company_id, 'count': len(items), 'items': items}


def _route(env, bank_line, target_account, partner=None):
    if bank_line.is_reconciled:
        return "already reconciled"
    move = bank_line.move_id
    suspense_acc = bank_line.journal_id.suspense_account_id
    if not suspense_acc:
        return "no suspense account"
    susp = move.line_ids.filtered(lambda l: l.account_id == suspense_acc)
    if not susp:
        return "no suspense line"
    vals = {"account_id": target_account.id}
    if partner:
        vals["partner_id"] = partner.id
    susp[0].write(vals)
    return None


def _learn(env, company_id, pattern, account=None, partner=None, label=None):
    pattern = (pattern or '').strip()
    if not pattern:
        return None
    Rule = env['learned.rule']
    existing = Rule.search([('company_id', '=', company_id), ('rule_type', '=', 'bank'),
                            ('pattern', '=ilike', pattern)], limit=1)
    vals = {'confidence': 0.9, 'source': 'active'}
    if account:
        vals['account_id'] = account.id
    if partner:
        vals['partner_id'] = partner.id
    if existing:
        existing.write(vals)
        return existing.id
    vals.update({'name': label or f'Conciliación: {pattern[:40]}', 'pattern': pattern,
                 'rule_type': 'bank', 'company_id': company_id})
    return Rule.create(vals).id


def cmd_resolve(env, args):
    line = env['account.bank.statement.line'].browse(args.line_id)
    if not line.exists():
        return {'ok': False, 'error': 'line not found'}
    if line.is_reconciled:
        return {'ok': False, 'error': 'ya conciliada'}
    company_id = line.company_id.id
    concept = line.payment_ref or ''
    pattern = args.pattern or concept
    learned_id = None

    if args.action == 'account':
        acc = env['account.account'].search([('company_id', '=', company_id), ('code', '=', args.account)], limit=1)
        if not acc:
            return {'ok': False, 'error': f'cuenta {args.account} no existe'}
        partner = None
        if args.partner:
            partner = env['res.partner'].search([('name', '=ilike', args.partner)], limit=1) or None
        err = _route(env, line, acc, partner)
        if err:
            return {'ok': False, 'error': err}
        if args.learn:
            learned_id = _learn(env, company_id, pattern, account=acc, partner=partner, label=args.label)
        return {'ok': True, 'reconciled': True, 'account': args.account, 'learned_rule_id': learned_id}

    elif args.action == 'move':
        inv = env['account.move'].browse(args.move_id)
        if not inv.exists():
            return {'ok': False, 'error': 'move not found'}
        if inv.state == 'draft':
            # Odoo solo concilia asientos publicados: publicar el borrador primero
            # (guard: un borrador a 0 esta roto, no se publica a ciegas)
            if not inv.amount_total:
                return {'ok': False, 'error': 'la factura esta en BORRADOR y a 0 EUR: revisala/completala en Revision antes de conciliar'}
            try:
                inv.action_post()
            except Exception as e:
                return {'ok': False, 'error': 'no se pudo publicar el borrador: ' + str(e)[:200]}
        inv_line = inv.line_ids.filtered(
            lambda l: l.account_id.account_type in ('asset_receivable', 'liability_payable') and not l.reconciled)[:1]
        if not inv_line:
            return {'ok': False, 'error': 'la factura no tiene línea abierta a cobrar/pagar'}
        acc = inv_line.account_id
        err = _route(env, line, acc, inv.partner_id)
        if err:
            return {'ok': False, 'error': err}
        bank_aml = line.move_id.line_ids.filtered(lambda l: l.account_id == acc and not l.reconciled)[:1]
        if bank_aml:
            (bank_aml + inv_line).reconcile()
        if args.learn:
            learned_id = _learn(env, company_id, pattern, account=acc, partner=inv.partner_id,
                                label=args.label or f'Pago/cobro {_tr(inv.partner_id.name)}')
        return {'ok': True, 'reconciled': True, 'move': inv.name, 'learned_rule_id': learned_id}

    elif args.action == 'aml':
        # aml_ids_multi: admite un id o varios ('12,34,56') — un pago del banco puede
        # agrupar varias facturas del mismo proveedor; se concilian todas juntas (1:N)
        try:
            ids = [int(x) for x in str(args.aml_id).split(',') if str(x).strip()]
        except ValueError:
            return {'ok': False, 'error': 'aml_id invalido'}
        amls = env['account.move.line'].browse(ids)
        if not amls.exists() or len(amls) != len(ids):
            return {'ok': False, 'error': 'algun apunte no existe'}
        if any(a.reconciled for a in amls):
            return {'ok': False, 'error': 'algun apunte ya esta conciliado'}
        if len(set(amls.mapped('account_id').ids)) > 1:
            return {'ok': False, 'error': 'los apuntes seleccionados deben ser de la misma cuenta'}
        # publicar borradores antes de conciliar (mismo criterio que action=move)
        for mv in amls.mapped('move_id'):
            if mv.state == 'draft':
                if not mv.amount_total:
                    return {'ok': False, 'error': f'el borrador {mv.ref or mv.id} esta a 0: revisalo antes'}
                mv.action_post()
        acc = amls[0].account_id
        partner = amls[0].partner_id
        err = _route(env, line, acc, partner)
        if err:
            return {'ok': False, 'error': err}
        bank_aml = line.move_id.line_ids.filtered(lambda l: l.account_id == acc and not l.reconciled)[:1]
        if bank_aml and acc.reconcile:
            (bank_aml + amls).reconcile()
        if args.learn:
            learned_id = _learn(env, company_id, pattern, account=acc, partner=partner,
                                label=args.label or f'Mov {acc.code}')
        return {'ok': True, 'reconciled': True, 'apuntes': [m.name for m in amls.mapped('move_id')],
                'apunte': amls[0].move_id.name, 'cuenta': acc.code, 'learned_rule_id': learned_id}

    return {'ok': False, 'error': 'action desconocida'}


def cmd_search_accounts(env, company_id, q):
    q = (q or '').strip()
    if not q:
        return {'items': []}
    dom = [('company_id', '=', company_id), ('deprecated', '=', False),
           '|', ('code', '=ilike', q + '%'), ('name', 'ilike', q)]
    accs = env['account.account'].search(dom, limit=25, order='code')
    return {'items': [{'code': a.code, 'name': _tr(a.name), 'reconcile': a.reconcile,
                       'type': a.account_type} for a in accs]}


def cmd_search_partners(env, q):
    """Busqueda tolerante: cada palabra debe aparecer (AND), y si no hay resultado
    se reintenta ignorando espacios en ambos lados ('feu vert' <-> 'FEUVERT')."""
    q = (q or '').strip()
    if not q:
        return {'items': []}
    tokens = [t for t in q.split() if t]
    dom = []
    for t in tokens:
        dom += ['|', ('name', 'ilike', t), ('vat', 'ilike', t)]
    ps = env['res.partner'].search(dom, limit=25, order='name')
    if not ps:
        compact = q.replace(' ', '').lower()
        env.cr.execute(
            """SELECT id FROM res_partner
               WHERE active AND REPLACE(LOWER(name), ' ', '') LIKE %s
               ORDER BY name LIMIT 25""", ('%' + compact + '%',))
        ids = [r[0] for r in env.cr.fetchall()]
        ps = env['res.partner'].browse(ids)
    return {'items': [{'id': p.id, 'name': _tr(p.name), 'vat': p.vat or ''} for p in ps]}


def cmd_partner_account(env, company_id, partner_id, direction):
    """Cuenta del partner para conciliar (payable/receivable segun signo del movimiento).
    Si es proveedor y aun usa la payable generica 410000, se le crea su 410NNN propia
    (politica cuenta-por-proveedor) y se le asigna como property. Idempotente."""
    p = env['res.partner'].browse(int(partner_id))
    if not p.exists():
        return {'error': 'partner no existe'}
    p = p.with_company(company_id)
    created = False
    if direction == 'receivable':
        acc = p.property_account_receivable_id
    else:
        acc = p.property_account_payable_id
        generic = env['account.account'].search(
            [('company_id', '=', company_id), ('code', '=', '410000')], limit=1)
        if generic and (not acc or acc.id == generic.id):
            existing = {a.code for a in env['account.account'].search(
                [('company_id', '=', company_id), ('code', '=like', '410%')])}
            lens = sorted({len(c) for c in existing if c.isdigit()}, reverse=True)
            maxlen = lens[0] if lens else 6
            if maxlen == 8:  # plan legacy BEST 8 digitos (410000NN; 4100009xx=aparcamiento)
                nums = [int(c) for c in existing if len(c) == 8 and c.isdigit() and int(c) < 41000900]
                code = str((max(nums) + 1) if nums else 41000001)
            elif maxlen >= 9:  # plan tipo Sage 9 digitos
                code = ('4100' + str(p.id).zfill(5))[:9]
                if code in existing:
                    code = ('4101' + str(p.id).zfill(5))[:9]
            else:
                seq = 1
                while f'410{seq:03d}' in existing:
                    seq += 1
                code = f'410{seq:03d}'
            acc = env['account.account'].with_company(company_id).create({
                'code': code, 'name': (p.name or 'Proveedor')[:80],
                'account_type': 'liability_payable', 'reconcile': True,
                'company_id': company_id})
            p.property_account_payable_id = acc
            created = True
    if not acc:
        return {'error': 'el partner no tiene cuenta asignada'}
    return {'account': acc.code, 'name': _tr(acc.name), 'partner': _tr(p.name),
            'created': created}


def cmd_set_maturity(env, company_id, aml_id, move_id, fecha):
    """Cambia date_maturity (fecha estimada de cobro/pago) de un apunte o de las
    lineas de vencimiento de un asiento. Para la tesoreria de la web."""
    if aml_id:
        ls = env['account.move.line'].browse([int(aml_id)])
        if not ls.exists() or ls.company_id.id != company_id:
            return {'ok': False, 'error': 'apunte no existe'}
    else:
        ls = env['account.move.line'].search([
            ('move_id', '=', int(move_id)), ('company_id', '=', company_id),
            ('account_id.account_type', 'in', ['asset_receivable', 'liability_payable'])])
        if not ls:
            return {'ok': False, 'error': 'el asiento no tiene linea de cobro/pago'}
    ls.write({'date_maturity': fecha})
    return {'ok': True, 'lineas': len(ls), 'fecha': fecha}


def cmd_update_move(env, company_id, move_id, new_ref, new_date):
    """Edicion basica de un asiento desde la web (ref y/o fecha): draft -> write -> post.
    Guards: no statement lines (se editan en conciliacion), no cambiar de anyo sin resecuenciar."""
    m = env['account.move'].browse(int(move_id))
    if not m.exists() or m.company_id.id != company_id:
        return {'ok': False, 'error': 'asiento no existe en esta empresa'}
    if m.statement_line_id:
        return {'ok': False, 'error': 'es una linea de extracto: se gestiona desde Conciliacion'}
    vals = {}
    if new_ref is not None:
        vals['ref'] = new_ref
    reset_name = False
    if new_date:
        vals['date'] = new_date
        if m.name and m.name != '/' and str(m.date)[:4] != str(new_date)[:4]:
            reset_name = True  # cambio de anyo: la secuencia obliga a renumerar
    was_posted = m.state == 'posted'
    if was_posted:
        m.button_draft()
    if reset_name:
        vals['name'] = '/'
    m.write(vals)
    if was_posted:
        m.action_post()
    return {'ok': True, 'move_id': m.id, 'name': m.name, 'ref': m.ref, 'date': str(m.date)}


def cmd_invoices_range(env, company_id, amt_min, amt_max):
    """Facturas (posted y borrador) con total dentro de [amt_min, amt_max] para
    la busqueda por importe en conciliacion manual. Indica estado de pago."""
    ms = env['account.move'].search([
        ('company_id', '=', company_id), ('state', 'in', ['posted', 'draft']),
        ('move_type', 'in', ['in_invoice', 'in_refund', 'out_invoice', 'out_refund']),
        ('amount_total', '>=', amt_min), ('amount_total', '<=', amt_max),
    ], order='invoice_date desc, id desc', limit=300)
    items = []
    for m in ms:
        items.append({
            'move_id': m.id, 'name': m.name if m.name != '/' else '(borrador)',
            'ref': m.ref or '', 'date': str(m.invoice_date or m.date or ''),
            'partner': _tr(m.partner_id.name) if m.partner_id else '',
            'amount_total': float(m.amount_total), 'move_type': m.move_type,
            'state': m.state, 'payment_state': m.payment_state or 'not_paid',
            'conciliada': (m.payment_state in ('paid', 'in_payment', 'reversed')),
        })
    return {'items': items, 'count': len(items), 'amt_min': amt_min, 'amt_max': amt_max}


def cmd_account_amls(env, company_id, account_code):
    acc = env['account.account'].search([('company_id', '=', company_id), ('code', '=', account_code)], limit=1)
    if not acc:
        return {'items': [], 'error': 'cuenta no existe'}
    amls = env['account.move.line'].search([
        ('account_id', '=', acc.id), ('company_id', '=', company_id),
        ('parent_state', '=', 'posted'), ('reconciled', '=', False),
    ], limit=200, order='date desc')
    out = []
    for l in amls:
        out.append({
            'aml_id': l.id, 'move_id': l.move_id.id, 'date': str(l.date), 'move_name': l.move_id.name,
            'partner': _tr(l.partner_id.name) if l.partner_id else None,
            'name': l.name or '', 'ref': l.move_id.ref or '',
            'debit': float(l.debit), 'credit': float(l.credit),
            'residual': float(l.amount_residual), 'reconcilable': bool(acc.reconcile),
        })
    return {'cuenta': acc.code, 'nombre': _tr(acc.name), 'reconcilable': bool(acc.reconcile),
            'count': len(out), 'items': out}


def cmd_list_rules(env, company_id):
    rules = env['learned.rule'].with_context(active_test=False).search(
        [('rule_type', '=', 'bank'),
         '|', ('company_id', '=', company_id), ('company_id', '=', False)],
        order='active desc, confidence desc, times_applied desc')
    out = []
    for r in rules:
        out.append({
            'id': r.id, 'name': _tr(r.name), 'pattern': r.pattern,
            'account_code': r.account_id.code if r.account_id else None,
            'account_name': _tr(r.account_id.name) if r.account_id else None,
            'partner': _tr(r.partner_id.name) if r.partner_id else None,
            'confidence': round(r.confidence or 0, 2), 'times_applied': r.times_applied,
            'last_applied': str(r.last_applied) if r.last_applied else None,
            'active': r.active, 'source': r.source,
            'company_id': r.company_id.id if r.company_id else None,
        })
    return {'count': len(out), 'items': out}


def cmd_save_rule(env, args):
    Rule = env['learned.rule']
    vals = {}
    if args.name is not None:
        vals['name'] = args.name
    if args.pattern is not None:
        vals['pattern'] = args.pattern
    if args.confidence is not None:
        vals['confidence'] = float(args.confidence)
    if args.active is not None:
        vals['active'] = bool(int(args.active))
    if args.account is not None:
        if args.account == '':
            vals['account_id'] = False
        else:
            acc = env['account.account'].search([('company_id', '=', args.company_id), ('code', '=', args.account)], limit=1)
            if not acc:
                return {'ok': False, 'error': f'cuenta {args.account} no existe'}
            vals['account_id'] = acc.id
    if args.partner is not None:
        if args.partner == '':
            vals['partner_id'] = False
        else:
            p = env['res.partner'].search([('name', '=ilike', args.partner)], limit=1)
            vals['partner_id'] = p.id if p else False
    if args.rule_id:
        r = Rule.browse(args.rule_id)
        if not r.exists():
            return {'ok': False, 'error': 'regla no existe'}
        r.write(vals)
        return {'ok': True, 'rule_id': r.id, 'created': False}
    # crear
    vals.setdefault('rule_type', 'bank')
    vals.setdefault('company_id', args.company_id)
    vals.setdefault('confidence', 0.9)
    vals.setdefault('source', 'active')
    if not vals.get('name') or not vals.get('pattern'):
        return {'ok': False, 'error': 'name y pattern son obligatorios'}
    return {'ok': True, 'rule_id': Rule.create(vals).id, 'created': True}


def cmd_delete_rule(env, rule_id):
    r = env['learned.rule'].browse(rule_id)
    if not r.exists():
        return {'ok': False, 'error': 'no existe'}
    r.active = False  # desactivar (conserva histórico), no borrar
    return {'ok': True, 'rule_id': rule_id, 'desactivada': True}


def cmd_delete_move(env, company_id, move_id):
    """Elimina un asiento (para reprocesar su documento con reglas): a borrador si
    está publicado y unlink. Guards: company correcta y NUNCA asientos de extracto
    bancario (statement lines)."""
    mv = env['account.move'].browse(move_id)
    if not mv.exists():
        return {'ok': False, 'error': 'asiento no existe'}
    if mv.company_id.id != company_id:
        return {'ok': False, 'error': f'asiento de otra company ({mv.company_id.id})'}
    if mv.statement_line_id or mv.statement_line_ids:
        return {'ok': False, 'error': 'es un movimiento de extracto bancario, no se elimina'}
    name = mv.name
    if mv.state == 'posted':
        mv.button_draft()
    mv.with_context(force_delete=True).unlink()
    return {'ok': True, 'move_id': move_id, 'name': name, 'eliminado': True}


def cmd_resolve_split(env, args):
    """Reparte una línea de extracto pendiente entre VARIAS cuentas, cada una
    con su importe. Sustituye la línea transitoria por N contrapartidas.
    Guardas: suma = pendiente (±0,01); ni transitoria ni cuenta del banco como
    destino; diarios en moneda extranjera rechazados (corrompería la divisa)."""
    import json as _json
    ln = env['account.bank.statement.line'].browse(args.line_id)
    if not ln.exists():
        return {'ok': False, 'error': 'línea no existe'}
    if ln.is_reconciled:
        return {'ok': False, 'error': 'ya conciliada'}
    j = ln.journal_id
    if j.currency_id and j.currency_id != j.company_id.currency_id:
        return {'ok': False, 'error': 'diario en moneda extranjera: el reparto corrompería la divisa — usa "Conciliar a cuenta"'}
    try:
        reparto = _json.loads(args.reparto)
        assert isinstance(reparto, list) and len(reparto) >= 2
    except Exception:
        return {'ok': False, 'error': 'reparto inválido (mínimo 2 cuentas)'}
    move = ln.move_id
    susp = j.suspense_account_id
    susp_lines = move.line_ids.filtered(lambda l: l.account_id == susp)
    if not susp_lines:
        return {'ok': False, 'error': 'sin línea transitoria (¿ya ruteada? deshaz primero)'}
    pend = sum(susp_lines.mapped('balance'))
    veto = {susp.id, j.default_account_id.id}
    destinos, total = [], 0.0
    for r in reparto:
        try:
            amt = round(float(r.get('amount')), 2)
        except Exception:
            return {'ok': False, 'error': 'importe inválido'}
        if amt <= 0:
            return {'ok': False, 'error': 'los importes deben ser mayores que 0'}
        acc = env['account.account'].search([('company_id', '=', args.company_id),
                                             ('code', '=', str(r.get('account')))], limit=1)
        if not acc:
            return {'ok': False, 'error': f"cuenta {r.get('account')} no existe"}
        if acc.id in veto:
            return {'ok': False, 'error': f'la cuenta {acc.code} es la transitoria o la del banco: no puede ser destino'}
        destinos.append((acc, amt))
        total += amt
    if abs(abs(pend) - total) > 0.01:
        return {'ok': False, 'error': f'la suma del reparto ({total:.2f}) no cuadra con el pendiente ({abs(pend):.2f})'}
    sign = 1 if pend > 0 else -1
    nombre = (ln.payment_ref or 'Reparto')[:200]
    cmds = [(2, l.id, 0) for l in susp_lines]
    for acc, amt in destinos:
        cmds.append((0, 0, {'account_id': acc.id, 'name': nombre,
                            'debit': amt if sign > 0 else 0.0,
                            'credit': amt if sign < 0 else 0.0,
                            'partner_id': ln.partner_id.id or False}))
    move.button_draft()
    move.write({'line_ids': cmds})
    move.action_post()
    try:
        move.message_post(body='✅ Conciliado desde la web repartido en %d cuentas · %s' % (
            len(destinos), ' · '.join(f'{a.code} → {amt:.2f}' for a, amt in destinos)))
    except Exception:  # noqa: BLE001 — el chatter es informativo
        pass
    return {'ok': True, 'line_id': ln.id,
            'cuentas': [f'{a.code}:{amt:.2f}' for a, amt in destinos]}


def cmd_sueltos(env, company_id):
    """Pagos/cobros de banco en cuentas de TERCERO (410x/430x…) sin casar con
    factura: cada uno con propuesta (suma exacta, 1..4 facturas) y la lista de
    facturas/apuntes abiertos del mismo tercero para casar a mano."""
    import itertools
    Aml = env['account.move.line']
    sueltos = Aml.search([
        ('company_id', '=', company_id), ('parent_state', '=', 'posted'),
        ('account_id.account_type', 'in', ['asset_receivable', 'liability_payable']),
        ('account_id.reconcile', '=', True), ('reconciled', '=', False),
        ('amount_residual', '!=', 0), ('statement_line_id', '!=', False),
    ], order='date desc', limit=300)
    items = []
    for a in sueltos:
        acc = a.account_id
        dom = [('company_id', '=', company_id), ('account_id', '=', acc.id),
               ('parent_state', '=', 'posted'), ('reconciled', '=', False),
               ('amount_residual', '!=', 0), ('statement_line_id', '=', False),
               ('id', '!=', a.id)]
        if a.partner_id:
            dom.append(('partner_id', '=', a.partner_id.id))
        cands = Aml.search(dom, order='date desc', limit=60)
        cands = cands.filtered(lambda c: (c.amount_residual > 0) != (a.amount_residual > 0))
        objetivo = -a.amount_residual
        propuesta = [c.id for c in cands if abs(c.amount_residual - objetivo) < 0.01][:1]
        if not propuesta and 1 < len(cands) <= 24:
            vals = [(c.id, c.amount_residual) for c in cands]
            for k in (2, 3, 4):
                hit = next((co for co in itertools.combinations(vals, k)
                            if abs(sum(v for _, v in co) - objetivo) < 0.01), None)
                if hit:
                    propuesta = [i for i, _ in hit]
                    break
        items.append({
            'aml_id': a.id, 'date': str(a.date),
            'account': acc.code, 'account_name': _tr(acc.name),
            'partner': a.partner_id.name if a.partner_id else _tr(acc.name),
            'concepto': (a.statement_line_id.payment_ref or a.name or '')[:120],
            'importe': a.amount_residual,
            'propuesta': propuesta,
            'facturas': [{
                'aml_id': c.id, 'move_id': c.move_id.id, 'name': c.move_id.name,
                'ref': c.move_id.ref or '', 'tipo': c.move_id.move_type,
                'date': str(c.move_id.invoice_date or c.date), 'residual': c.amount_residual,
            } for c in cands[:40]],
        })
    return {'count': len(items), 'items': items}


def cmd_match_amls(env, aml_id, contra):
    """Casa un apunte de pago/cobro (aml_id) contra 1..N apuntes de factura de
    la MISMA cuenta. contra = '12,34'. Permite casado parcial (Odoo reparte)."""
    ids = [int(x) for x in str(contra).replace(' ', '').split(',') if x]
    a = env['account.move.line'].browse(int(aml_id))
    b = env['account.move.line'].browse(ids)
    if not a.exists() or len(b.exists()) != len(ids):
        return {'ok': False, 'error': 'apunte no existe'}
    if any(x.account_id != a.account_id for x in b):
        return {'ok': False, 'error': 'las facturas no están en la misma cuenta que el pago'}
    if a.reconciled:
        return {'ok': False, 'error': 'el pago ya está conciliado'}
    (a | b).reconcile()
    return {'ok': True, 'aml_id': a.id, 'casado_con': ids,
            'residual_pago': a.amount_residual}


def cmd_auto_reconcile(env, company_id):
    """Aplica learned.rule(bank, conf>=0.85) a las líneas pendientes. Post-import."""
    rules = env['learned.rule'].search([('rule_type', '=', 'bank'), ('company_id', 'in', [company_id, False]),
                                        ('active', '=', True), ('confidence', '>=', 0.85)], order='confidence desc')
    lines = env['account.bank.statement.line'].search([('company_id', '=', company_id), ('is_reconciled', '=', False)])
    applied = 0
    for ln in lines:
        txt = (ln.payment_ref or '')
        rule = env['learned.rule'].find_match(txt, 'bank', company_id) if rules else None
        if not rule:
            continue
        acc = rule.account_id
        if not acc and rule.partner_id:
            # regla solo-partner: usar su cuenta contable propia según el signo del movimiento
            p = rule.partner_id.with_company(company_id)
            acc = p.property_account_payable_id if ln.amount < 0 else p.property_account_receivable_id
        if acc:
            if _route(env, ln, acc, rule.partner_id) is None:
                rule.mark_applied()
                applied += 1
    return {'company_id': company_id, 'pendientes': len(lines), 'auto_conciliadas': applied}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--propose', action='store_true')
    p.add_argument('--resolve', action='store_true')
    p.add_argument('--auto-reconcile', action='store_true')
    p.add_argument('--search-accounts', action='store_true')
    p.add_argument('--search-partners', action='store_true')
    p.add_argument('--account-amls', action='store_true')
    p.add_argument('--list-rules', action='store_true')
    p.add_argument('--save-rule', action='store_true')
    p.add_argument('--delete-rule', action='store_true')
    p.add_argument('--delete-move', action='store_true')
    p.add_argument('--sueltos', action='store_true')
    p.add_argument('--match-amls', action='store_true')
    p.add_argument('--contra')  # ids de facturas separados por comas
    p.add_argument('--resolve-split', action='store_true')
    p.add_argument('--reparto')  # JSON: [{"account":"626000","amount":100.0}, ...]
    p.add_argument('--company-id', type=int, default=4)
    p.add_argument('--db')
    p.add_argument('--line-id', type=int)
    p.add_argument('--action', choices=['account', 'move', 'aml'])
    p.add_argument('--account')
    p.add_argument('--move-id', type=int)
    p.add_argument('--aml-id')  # int o lista separada por comas: '12,34,56' (pago que agrupa varias facturas)
    p.add_argument('--rule-id', type=int)
    p.add_argument('--name')
    p.add_argument('--confidence')
    p.add_argument('--active')
    p.add_argument('--partner')
    p.add_argument('--pattern')
    p.add_argument('--label')
    p.add_argument('--q')
    p.add_argument('--invoices-range', action='store_true')
    p.add_argument('--amt-min', type=float)
    p.add_argument('--amt-max', type=float)
    p.add_argument('--update-move', action='store_true')
    p.add_argument('--move-id2', type=int)  # (no usado; compat)
    p.add_argument('--set-ref')
    p.add_argument('--set-date')
    p.add_argument('--set-maturity', action='store_true')
    p.add_argument('--partner-account', action='store_true')
    p.add_argument('--partner-id', type=int)
    p.add_argument('--dir', choices=['payable', 'receivable'], default='payable')
    p.add_argument('--learn', type=int, default=0)
    args = p.parse_args()

    odoo.tools.config.parse_config(['-c', ODOO_CONF])
    reg = odoo.registry(args.db or DB_NAME)
    with reg.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {'tz': 'Europe/Madrid', 'lang': 'es_ES'})
        if args.propose:
            out = cmd_propose(env, args.company_id)
        elif args.search_accounts:
            out = cmd_search_accounts(env, args.company_id, args.q)
        elif args.set_maturity:
            out = cmd_set_maturity(env, args.company_id, args.aml_id, args.move_id, args.set_date)
            cr.commit()
        elif args.update_move:
            out = cmd_update_move(env, args.company_id, args.move_id, args.set_ref, args.set_date)
            cr.commit()
        elif args.invoices_range:
            out = cmd_invoices_range(env, args.company_id, args.amt_min or 0.0, args.amt_max or 0.0)
        elif args.partner_account:
            out = cmd_partner_account(env, args.company_id, args.partner_id, args.dir)
            cr.commit()
        elif args.search_partners:
            out = cmd_search_partners(env, args.q)
        elif args.account_amls:
            out = cmd_account_amls(env, args.company_id, args.account)
        elif args.list_rules:
            out = cmd_list_rules(env, args.company_id)
        elif args.save_rule:
            out = cmd_save_rule(env, args)
            if out.get('ok'):
                cr.commit()
        elif args.delete_rule:
            out = cmd_delete_rule(env, args.rule_id)
            if out.get('ok'):
                cr.commit()
        elif args.resolve:
            out = cmd_resolve(env, args)
            if out.get('ok'):
                cr.commit()
        elif args.auto_reconcile:
            out = cmd_auto_reconcile(env, args.company_id)
            cr.commit()
        elif args.delete_move:
            out = cmd_delete_move(env, args.company_id, args.move_id)
            if out.get('ok'):
                cr.commit()
        elif args.sueltos:
            out = cmd_sueltos(env, args.company_id)
        elif args.match_amls:
            out = cmd_match_amls(env, args.aml_id, args.contra)
            if out.get('ok'):
                cr.commit()
        elif args.resolve_split:
            out = cmd_resolve_split(env, args)
            if out.get('ok'):
                cr.commit()
        else:
            out = {'error': 'sin comando'}
    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
