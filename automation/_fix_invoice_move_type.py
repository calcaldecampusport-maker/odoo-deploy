#!/usr/bin/env python3
"""
Fix post-migración: cambia move_type='entry' → 'in_invoice'/'in_refund' vía SQL UPDATE
para los 97 moves que migré con move_type cast. Mantiene AMLs intactos.
"""
import sys, re
sys.path.insert(0, "/opt/odoo17/odoo")
import odoo

odoo.tools.config.parse_config(["-c", "/etc/odoo17.conf"])
reg_src = odoo.registry("cararjfam")
reg = odoo.registry("round_facturacion")

# Cargar map src_id → (invoice_date, move_type, journal.code) origen
src_data = {}
with reg_src.cursor() as cr_src:
    cr_src.execute(
        "SELECT m.id, m.invoice_date, m.move_type, j.code "
        "FROM account_move m JOIN account_journal j ON j.id=m.journal_id "
        "WHERE m.company_id=2 AND m.move_type IN ('in_invoice','in_refund')"
    )
    for sid, idate, mtype, jcode in cr_src.fetchall():
        src_data[sid] = (idate, mtype, jcode)
print(f"src in_invoice/in_refund: {len(src_data)}")

src_id_re = re.compile(r"move id=(\d+)")
counts = {"in_invoice": 0, "in_refund": 0, "fail": 0}

with reg.cursor() as cr:
    # Journal purchase destino (la mayoría irán aquí — código BILL)
    cr.execute(
        "SELECT id, code FROM account_journal "
        "WHERE company_id=3 AND type='purchase' ORDER BY code LIMIT 5"
    )
    purchase_journals = cr.fetchall()
    print(f"journals purchase destino: {purchase_journals}")
    default_jid = purchase_journals[0][0] if purchase_journals else None

    # Moves a fixear
    cr.execute(
        "SELECT id, ref, narration FROM account_move "
        "WHERE company_id=3 AND narration LIKE %s "
        "AND (ref LIKE %s OR ref LIKE %s)",
        ("%Migrado de cararjfam%", "[in_invoice]%", "[in_refund]%"),
    )
    rows = cr.fetchall()
    print(f"moves destino a fixear: {len(rows)}")

    for mid, ref, narr in rows:
        m = src_id_re.search(narr or "")
        if not m:
            counts["fail"] += 1
            continue
        sid = int(m.group(1))
        if sid not in src_data:
            counts["fail"] += 1
            continue
        inv_date, src_mt, src_jcode = src_data[sid]
        new_ref = re.sub(r"^\[in_(?:invoice|refund)\]\s*", "", ref or "").strip() or None

        cr.execute(
            "UPDATE account_move "
            "SET move_type=%s, invoice_date=%s, ref=%s, journal_id=%s "
            "WHERE id=%s",
            (src_mt, inv_date, new_ref, default_jid, mid),
        )
        counts[src_mt] = counts.get(src_mt, 0) + 1

    cr.commit()

print("counts:", counts)
