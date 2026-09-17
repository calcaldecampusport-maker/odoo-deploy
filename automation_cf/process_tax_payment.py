"""Pagos a AEAT/TGSS (portado de Austral a XML-RPC, en BORRADOR — regla wiemspro).

  irpf_payment (modelos 111/115/130/190/216):
      DEBE 4751x (111/190→nóminas · 115→alquileres · resto→primera 4751) | HABER 572 banco
  ss_payment (TGSS):    DEBE 476000  | HABER 572 banco
  other_official:       DEBE 629xxx  | HABER 572 banco

El banco es el diario bank principal de la empresa; queda en la narración para
que el revisor lo cambie si el cargo fue en otra cuenta (el asiento es borrador).
"""
import base64
import logging

log = logging.getLogger("tax_payment")


def _acc_4751(rpc, cid, modelo):
    pats = {"111": "nomina", "190": "nomina", "115": "alquiler"}
    p = pats.get(str(modelo or "").strip())
    dom = [("code", "=like", "4751%"), ("deprecated", "=", False)]
    if p:
        r = rpc.search_read("account.account", dom + [("name", "ilike", p)],
                            ["id", "code", "name"], limit=1, company_id=cid)
        if r:
            return r[0]
        if p == "alquiler":
            r = rpc.search_read("account.account", dom + [("name", "ilike", "arrendam")],
                                ["id", "code", "name"], limit=1, company_id=cid)
            if r:
                return r[0]
    r = rpc.search_read("account.account", dom, ["id", "code", "name"],
                        limit=1, order="code", company_id=cid)
    return r[0] if r else None


def _acc(rpc, cid, code, pref=None):
    r = rpc.search_read("account.account", [("code", "=", code), ("deprecated", "=", False)],
                        ["id", "code", "name"], limit=1, company_id=cid)
    if r:
        return r[0]
    r = rpc.search_read("account.account", [("code", "=like", f"{pref or code[:3]}%"),
                                            ("deprecated", "=", False)],
                        ["id", "code", "name"], limit=1, order="code", company_id=cid)
    return r[0] if r else None


def _banco(rpc, cid):
    """(cuenta 572 del banco principal, nombre del diario)."""
    js = rpc.search_read("account.journal", [("type", "=", "bank"), ("company_id", "=", cid)],
                         ["id", "name", "default_account_id"], order="id", company_id=cid)
    for j in js:
        if j.get("default_account_id"):
            return j["default_account_id"][0], j["name"]
    r = rpc.search_read("account.account", [("code", "=like", "572%"), ("deprecated", "=", False)],
                        ["id", "name"], limit=1, order="code", company_id=cid)
    return (r[0]["id"], r[0]["name"]) if r else (None, None)


def process(rpc, company, data, pdf_bytes=None, pdf_name=None):
    """Asiento de pago IRPF/SS/otro oficial en BORRADOR."""
    cid = company["odoo_company_id"]
    if company.get("chart") != "pgc":
        return {"status": "error", "error": "pagos AEAT/TGSS solo en plan español (WSL/BIO)"}
    dt = (data.get("document_type") or "").lower()
    total = round(float(data.get("total") or 0), 2)
    fecha = data.get("invoice_date")
    if total <= 0 or not fecha:
        return {"status": "error", "error": "sin total o sin fecha"}
    extra = data.get("extra") or {}

    if dt == "irpf_payment":
        acc = _acc_4751(rpc, cid, extra.get("modelo"))
        ref = f"IRPF mod {extra.get('modelo', '?')} {extra.get('ejercicio', '')}-{extra.get('periodo', '')}".strip().rstrip("-")
        concepto = f"Pago IRPF modelo {extra.get('modelo', '?')} ({extra.get('ejercicio', '')} {extra.get('periodo', '')})"
    elif dt == "ss_payment":
        acc = _acc(rpc, cid, "476000", "476")
        ref = f"Pago SS {extra.get('periodo', '')}".strip()
        concepto = f"Pago Seguridad Social {extra.get('periodo', '')}"
    else:
        acc = _acc(rpc, cid, "629000", "629")
        ref = data.get("invoice_ref") or f"Pago oficial {fecha}"
        concepto = f"Otro documento oficial: {data.get('supplier_name', '')} — {ref}"
    if not acc:
        return {"status": "error", "error": f"sin cuenta de cargo para {dt}"}
    bank_id, bank_name = _banco(rpc, cid)
    if not bank_id:
        return {"status": "error", "error": "sin cuenta de banco (572)"}
    jr = rpc.search_read("account.journal", [("type", "=", "general"), ("company_id", "=", cid)],
                         ["id"], limit=1, company_id=cid)
    if not jr:
        return {"status": "error", "error": "sin diario general"}

    dup = rpc.search_read("account.move", [("ref", "=", ref), ("company_id", "=", cid),
                                           ("move_type", "=", "entry"), ("state", "!=", "cancel")],
                          ["id"], limit=1, company_id=cid)
    if dup:
        return {"status": "duplicate", "move_id": dup[0]["id"]}

    move_id = rpc.create("account.move", {
        "move_type": "entry", "journal_id": jr[0]["id"], "company_id": cid,
        "date": fecha, "ref": ref,
        "narration": (f"{concepto} (BORRADOR — validar banco y publicar).\n"
                      f"Cargo {acc['code']} {acc['name']} · Abono banco {bank_name}.\n"
                      f"Total: {total} €"),
        "line_ids": [
            (0, 0, {"name": ref, "account_id": acc["id"], "debit": total, "credit": 0.0}),
            (0, 0, {"name": ref, "account_id": bank_id, "debit": 0.0, "credit": total}),
        ],
    }, company_id=cid)
    if pdf_bytes:
        try:
            rpc.create("ir.attachment", {"name": pdf_name or "pago.pdf", "type": "binary",
                                         "res_model": "account.move", "res_id": move_id,
                                         "datas": base64.b64encode(pdf_bytes).decode()},
                       company_id=cid)
        except Exception as e:  # noqa: BLE001
            log.warning(f"adjunto pago falló: {e}")
    return {"status": "created", "move_id": move_id, "needs_review": False,
            "expense_account": acc["code"]}
