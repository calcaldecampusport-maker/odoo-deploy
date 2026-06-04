#!/usr/bin/env python3
"""Puente ORM de conciliación bancaria para la web austral-contab-web.
Comandos:
  --propose --company-id 4                  -> JSON {items:[{line_id,date,amount,concept,journal,saldo,ref,proposals:[...]}]}
  --resolve --line-id N --action account --account CODE [--partner NAME] [--learn 1] [--pattern TXT]
  --resolve --line-id N --action move --move-id M [--learn 1] [--pattern TXT]
  --auto-reconcile --company-id 4           -> aplica learned.rule(bank) a líneas pendientes (post-import)
Reutiliza bank_matcher.propose_for_company y learned.rule. odoo17 venv.
"""
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
    return v.get('en_US') if isinstance(v, dict) else v


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
        aml = env['account.move.line'].browse(args.aml_id)
        if not aml.exists() or aml.reconciled:
            return {'ok': False, 'error': 'apunte no existe o ya conciliado'}
        acc = aml.account_id
        err = _route(env, line, acc, aml.partner_id)
        if err:
            return {'ok': False, 'error': err}
        bank_aml = line.move_id.line_ids.filtered(lambda l: l.account_id == acc and not l.reconciled)[:1]
        if bank_aml and acc.reconcile:
            (bank_aml + aml).reconcile()
        if args.learn:
            learned_id = _learn(env, company_id, pattern, account=acc, partner=aml.partner_id,
                                label=args.label or f'Mov {acc.code}')
        return {'ok': True, 'reconciled': True, 'apunte': aml.move_id.name, 'cuenta': acc.code,
                'learned_rule_id': learned_id}

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
    q = (q or '').strip()
    if not q:
        return {'items': []}
    ps = env['res.partner'].search(['|', ('name', 'ilike', q), ('vat', 'ilike', q)], limit=25, order='name')
    return {'items': [{'id': p.id, 'name': _tr(p.name), 'vat': p.vat or ''} for p in ps]}


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
            'aml_id': l.id, 'date': str(l.date), 'move_name': l.move_id.name,
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


def cmd_auto_reconcile(env, company_id):
    """Aplica learned.rule(bank, conf>=0.85) a las líneas pendientes. Post-import."""
    rules = env['learned.rule'].search([('rule_type', '=', 'bank'), ('company_id', 'in', [company_id, False]),
                                        ('active', '=', True), ('confidence', '>=', 0.85)], order='confidence desc')
    lines = env['account.bank.statement.line'].search([('company_id', '=', company_id), ('is_reconciled', '=', False)])
    applied = 0
    for ln in lines:
        txt = (ln.payment_ref or '')
        rule = env['learned.rule'].find_match(txt, 'bank', company_id) if rules else None
        if rule and rule.account_id:
            if _route(env, ln, rule.account_id, rule.partner_id) is None:
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
    p.add_argument('--company-id', type=int, default=4)
    p.add_argument('--line-id', type=int)
    p.add_argument('--action', choices=['account', 'move', 'aml'])
    p.add_argument('--account')
    p.add_argument('--move-id', type=int)
    p.add_argument('--aml-id', type=int)
    p.add_argument('--rule-id', type=int)
    p.add_argument('--name')
    p.add_argument('--confidence')
    p.add_argument('--active')
    p.add_argument('--partner')
    p.add_argument('--pattern')
    p.add_argument('--label')
    p.add_argument('--q')
    p.add_argument('--learn', type=int, default=0)
    args = p.parse_args()

    odoo.tools.config.parse_config(['-c', ODOO_CONF])
    reg = odoo.registry(DB_NAME)
    with reg.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {'tz': 'Europe/Madrid'})
        if args.propose:
            out = cmd_propose(env, args.company_id)
        elif args.search_accounts:
            out = cmd_search_accounts(env, args.company_id, args.q)
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
        else:
            out = {'error': 'sin comando'}
    print(json.dumps(out, ensure_ascii=False, default=str))


if __name__ == '__main__':
    main()
