#!/usr/bin/env python3
"""Migra apuntes de clientes/proveedores desde las cuentas genéricas 430000/410000
a cuentas individuales por tercero (plan Sage 8 dígitos) en cararjfam.
- Reutiliza la cuenta propia del partner si ya existe (property); si no, la crea.
- Mueve TODOS los apuntes del partner en la genérica (cualquier estado).
- Arrastra contrapartidas conciliadas sin partner (les asigna el partner).
- Los apuntes sin partner NO conciliados se quedan (pendientes de conciliación).
"""
import sys
import json
sys.path.insert(0, "/opt/odoo17/odoo")
import odoo
odoo.tools.config.parse_config(["-c", "/etc/odoo17.conf"])
from odoo.modules.registry import Registry
from odoo.api import Environment

DB = "cararjfam"
CID = 1

reg = Registry(DB)
result = {"clientes": [], "proveedores": []}
with reg.cursor() as cr:
    env = Environment(cr, 1, {"lang": "es_ES", "allowed_company_ids": [CID]})
    Acc = env["account.account"]

    def next_code(prefix):
        cr.execute("SELECT max(code) FROM account_account WHERE company_id=%s AND code LIKE %s AND length(code)=8",
                   (CID, prefix + "%"))
        m = cr.fetchone()[0]
        return str(int(m) + 1) if m else prefix + "00001"[:8 - len(prefix)]

    def generic_id(code):
        cr.execute("SELECT id FROM account_account WHERE company_id=%s AND code=%s", (CID, code))
        return cr.fetchone()[0]

    def fix(gen_code, acc_type, prop_field, prefix, bucket):
        gen = generic_id(gen_code)
        cr.execute("SELECT DISTINCT partner_id FROM account_move_line WHERE account_id=%s AND partner_id IS NOT NULL", (gen,))
        pids = [r[0] for r in cr.fetchall()]
        for pid in sorted(pids):
            partner = env["res.partner"].browse(pid)
            pc = partner.with_company(CID)
            acc = getattr(pc, prop_field)
            if not acc or acc.id == gen:
                code = next_code(prefix)
                acc = Acc.create({"code": code, "name": partner.name, "account_type": acc_type,
                                  "reconcile": True, "company_id": CID})
                setattr(pc, prop_field, acc)
                env.flush_all()
            # mover apuntes del partner
            cr.execute("UPDATE account_move_line SET account_id=%s WHERE account_id=%s AND partner_id=%s RETURNING id",
                       (acc.id, gen, pid))
            moved = [r[0] for r in cr.fetchall()]
            extra = []
            if moved:
                # contrapartidas conciliadas que quedaron en la genérica → misma cuenta + partner
                cr.execute("""
                    SELECT DISTINCT l.id FROM account_partial_reconcile pr
                    JOIN account_move_line l ON l.id = CASE WHEN pr.debit_move_id = ANY(%(mv)s)
                                                            THEN pr.credit_move_id ELSE pr.debit_move_id END
                    WHERE (pr.debit_move_id = ANY(%(mv)s) OR pr.credit_move_id = ANY(%(mv)s))
                      AND l.account_id = %(gen)s
                """, {"mv": moved, "gen": gen})
                extra = [r[0] for r in cr.fetchall()]
                if extra:
                    cr.execute("UPDATE account_move_line SET account_id=%s, partner_id=%s WHERE id=ANY(%s)",
                               (acc.id, pid, extra))
            result[bucket].append({"partner": partner.name, "cuenta": acc.code,
                                   "movidos": len(moved), "contrapartidas": len(extra)})
        # estado final de la genérica
        cr.execute("SELECT count(*), coalesce(sum(balance),0) FROM account_move_line WHERE account_id=%s", (gen,))
        n, s = cr.fetchone()
        result[bucket + "_genérica_restante"] = {"cuenta": gen_code, "apuntes": n, "saldo": float(s)}

    fix("430000", "asset_receivable", "property_account_receivable_id", "4300", "clientes")
    fix("410000", "liability_payable", "property_account_payable_id", "4100", "proveedores")
    cr.commit()

print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
