"""Recibos que NO son factura de proveedor: asiento contable directo.

Algunos documentos son un simple recibo o ticket (una suscripción cargada en la
tarjeta, un cargo de marketplace…) y el revisor no quiere una factura de proveedor con
su tercero y su desglose de IVA, sino un asiento directo:

    DEBE  <cuenta de gasto>   importe total
    HABER <cuenta de pago>    importe total     (p.ej. 5200xx de la tarjeta)

El extractor lo indica con el campo `asiento_directo` cuando una REGLA DEL REVISOR lo
pide («contabilízalo como asiento contable directamente contra la cuenta 520004»).
Sin regla no se usa este camino: por defecto todo sigue yendo a factura de proveedor.

El asiento se crea en BORRADOR, como el resto del pipeline: lo publica una persona.
"""
import base64
import logging

import expense_router as er
import process_invoice as pi

log = logging.getLogger("asiento")


def _journal_id(rpc, cid, cuenta_haber):
    """Diario del asiento: el de la tarjeta si el haber es una 5200xx (así el cargo
    queda en el diario de esa tarjeta, como el resto de sus movimientos); si no, el
    diario general de operaciones diversas."""
    codigo = str(cuenta_haber.get("code") or "")
    if codigo.startswith("5200"):
        # el diario de la tarjeta se localiza por los 4 últimos dígitos de su nombre
        nombre = str(cuenta_haber.get("name") or "")
        last4 = "".join(c for c in nombre if c.isdigit())[-4:]
        if last4:
            r = rpc.search_read("account.journal",
                                [("company_id", "=", cid), ("name", "ilike", last4)],
                                ["id", "name"], limit=1, company_id=cid)
            if r:
                return r[0]["id"], r[0]["name"]
    for dom in ([("company_id", "=", cid), ("type", "=", "general"), ("code", "=", "MISC")],
                [("company_id", "=", cid), ("type", "=", "general")]):
        r = rpc.search_read("account.journal", dom, ["id", "name"], limit=1, company_id=cid)
        if r:
            return r[0]["id"], r[0]["name"]
    return None, None


def _cuenta(rpc, cid, codigo):
    """{id, code, name} de una cuenta por su código, o None (el router ya lo devuelve así)."""
    if not codigo:
        return None
    return er._account_by_code(rpc, cid, str(codigo).strip())


def process(rpc, company, data, pdf_bytes=None, pdf_name=None):
    """Crea el asiento directo en BORRADOR. Devuelve dict de resultado."""
    cid = company["odoo_company_id"]
    spec = data.get("asiento_directo") or {}
    debe = _cuenta(rpc, cid, spec.get("debe"))
    haber = _cuenta(rpc, cid, spec.get("haber"))
    if not debe or not haber:
        faltan = [c for c, v in (("debe", spec.get("debe")), ("haber", spec.get("haber")))
                  if not _cuenta(rpc, cid, v)]
        return {"status": "failed",
                "error": f"la regla pide un asiento directo pero no existe(n) la(s) cuenta(s) "
                         f"{', '.join(f'{k}={spec.get(k)}' for k in faltan)} en el plan"}
    try:
        importe = round(float(spec.get("importe") or data.get("total") or 0), 2)
    except (TypeError, ValueError):
        importe = 0.0
    if importe <= 0:
        return {"status": "failed", "error": "asiento directo sin importe"}

    # el nº del documento (invoice_ref) es lo que identifica el recibo; el nombre del
    # fichero es solo el último recurso (y hace inútil el anti-duplicado)
    ref = (str(data.get("invoice_ref") or data.get("invoice_number") or "").strip()
           or (pdf_name or "recibo"))
    jid, jname = _journal_id(rpc, cid, haber)
    if not jid:
        return {"status": "failed", "error": "sin diario donde crear el asiento"}

    # ANTI-DUPLICADO: mismo diario y misma referencia (el nº del recibo)
    ya = rpc.search_read("account.move",
                         [("journal_id", "=", jid), ("ref", "=", ref), ("state", "!=", "cancel")],
                         ["id", "name"], limit=1, company_id=cid)
    if ya:
        return {"status": "duplicate", "move_id": ya[0]["id"], "move_name": ya[0]["name"],
                "info": f"ya existe {ya[0]['name']} con la referencia {ref}"}

    concepto = (str(data.get("concept") or data.get("supplier_name") or "").strip()
                or "Recibo")[:200]
    fecha = data.get("invoice_date") or data.get("date")
    notas = []
    if data.get("extraction_notes"):
        notas.append("OCR: " + str(data["extraction_notes"]))
    notas.append(f"ASIENTO DIRECTO por regla del revisor: DEBE {debe['code']} / "
                 f"HABER {haber['code']} · {importe:.2f} · diario {jname}")
    if spec.get("motivo"):
        notas.append("Regla: " + str(spec["motivo"])[:400])

    vals = {
        "move_type": "entry", "journal_id": jid, "date": fecha, "ref": ref,
        "company_id": cid,
        "narration": "<p>" + "</p><p>".join(notas) + "</p>",
        "line_ids": [
            (0, 0, {"account_id": debe["id"], "name": concepto,
                    "debit": importe, "credit": 0.0}),
            (0, 0, {"account_id": haber["id"], "name": f"{concepto} · {ref}",
                    "debit": 0.0, "credit": importe}),
        ],
    }
    move_id = rpc.create("account.move", vals, company_id=cid)
    name = rpc.read("account.move", [move_id], ["name"], company_id=cid)[0].get("name")
    if pdf_bytes:
        try:
            pi.attach_pdf(rpc, cid, move_id, pdf_bytes, pdf_name)
        except Exception as e:  # noqa: BLE001 — el adjunto es secundario
            log.warning(f"adjunto del asiento {move_id} falló: {e}")
    return {"status": "created", "move_id": move_id, "move_name": name,
            "info": f"asiento directo {debe['code']} / {haber['code']} en {jname}",
            "importe": importe}
