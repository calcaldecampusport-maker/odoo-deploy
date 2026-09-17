"""Elige la cuenta de gasto (6xx / expense) de una factura de proveedor.

REGLA (definida por el usuario): analizar el gasto a partir de lo YA CONTABILIZADO.
- Se busca en el histórico del proveedor en Odoo la cuenta de gasto que ha usado
  antes (la más frecuente en sus facturas posteadas).
- Si NO hay ninguna referencia previa (proveedor/concepto nuevo), se usa una
  cuenta por defecto (600000) y se DEVUELVE UN AVISO (`from_history=False`) para
  que la factura vaya a Revisión y un humano analice y confirme la cuenta.

No depende de ningún modelo de learning en Odoo: usa datos reales de la propia BD.
"""
from collections import Counter


def route_expense_account(rpc, company_id, partner_id, chart="pgc",
                          default_code="600000"):
    """Devuelve dict: {account_id, account_code, account_name, from_history, info}.

    from_history=False  →  se usó la cuenta por defecto: AVISAR y mandar a Revisión.
    """
    hist = _partner_history_account(rpc, company_id, partner_id, chart)
    if hist:
        return {
            "account_id": hist["id"], "account_code": hist["code"],
            "account_name": hist["name"], "from_history": True,
            "info": f"cuenta del histórico del proveedor ({hist['n']} apuntes previos)",
        }
    acc = _account_by_code(rpc, company_id, default_code) or _default_expense(rpc, company_id)
    if not acc:
        return {"account_id": None, "account_code": None, "account_name": None,
                "from_history": False, "info": "sin cuenta de gasto disponible"}
    return {
        "account_id": acc["id"], "account_code": acc["code"], "account_name": acc["name"],
        "from_history": False,
        "info": f"⚠ sin histórico del proveedor → cuenta por defecto {acc['code']}; ANALIZAR",
    }


def _expense_domain(chart):
    # PGC: cuentas 6xx (gasto/compra). US: por tipo contable estándar.
    if chart == "pgc":
        return [("account_id.code", "=like", "6%")]
    return [("account_id.account_type", "in", ["expense", "expense_direct_cost"])]


def _partner_history_account(rpc, company_id, partner_id, chart):
    """Cuenta de gasto más usada por este proveedor en facturas ya contabilizadas."""
    if not partner_id:
        return None
    dom = [
        ("parent_state", "=", "posted"),
        ("move_id.move_type", "in", ["in_invoice", "in_refund"]),
        ("partner_id", "=", partner_id),
        ("company_id", "=", company_id),
    ] + _expense_domain(chart)
    lines = rpc.search_read("account.move.line", dom, ["account_id"],
                            limit=500, order="id desc", company_id=company_id)
    cnt = Counter()
    for ln in lines:
        a = ln.get("account_id")
        if a:
            cnt[(a[0], a[1])] += 1
    if not cnt:
        return None
    (aid, aname), n = cnt.most_common(1)[0]
    return {"id": aid, "name": aname,
            "code": (aname.split(" ", 1)[0] if aname else None), "n": n}


def _account_by_code(rpc, company_id, code):
    r = rpc.search_read("account.account", [("code", "=", code)],
                        ["code", "name"], limit=1, company_id=company_id)
    return {"id": r[0]["id"], "code": r[0]["code"], "name": r[0]["name"]} if r else None


def _default_expense(rpc, company_id):
    r = rpc.search_read("account.account", [("account_type", "=", "expense")],
                        ["code", "name"], limit=1, order="code", company_id=company_id)
    return {"id": r[0]["id"], "code": r[0]["code"], "name": r[0]["name"]} if r else None
