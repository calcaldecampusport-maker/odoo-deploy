"""Prueba (solo lectura) del routing de cuenta de gasto contra Odoo 18.

Para cada empresa: coge un proveedor con facturas ya contabilizadas y comprueba
que el router devuelve su cuenta histórica; y comprueba el caso 'sin histórico'
(debe caer a la cuenta por defecto con from_history=False = avisar/revisar).
"""
import odoo
import companies
import expense_router as er


def main():
    rpc = odoo.get_rpc()
    print("Odoo:", rpc.version().get("server_version"))
    for c in companies.COMPANIES:
        cid = c["odoo_company_id"]
        mv = rpc.search_read(
            "account.move",
            [("move_type", "=", "in_invoice"), ("state", "=", "posted"),
             ("company_id", "=", cid), ("partner_id", "!=", False)],
            ["partner_id"], limit=1, order="id desc", company_id=cid)
        print(f"\n=== {c['name']} (company {cid}, chart={c['chart']}) ===")
        if mv:
            pid, pname = mv[0]["partner_id"][0], mv[0]["partner_id"][1]
            r = er.route_expense_account(rpc, cid, pid, chart=c["chart"])
            print(f"  proveedor con histórico: {pname!r}")
            print(f"     -> {r['account_code']} {r['account_name']!r} | from_history={r['from_history']} | {r['info']}")
        else:
            print("  (sin facturas de proveedor posteadas)")
        r2 = er.route_expense_account(rpc, cid, 999999999, chart=c["chart"])
        print(f"  caso SIN histórico -> {r2['account_code']} | from_history={r2['from_history']} | {r2['info']}")


if __name__ == "__main__":
    main()
