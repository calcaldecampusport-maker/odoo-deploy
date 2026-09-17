"""Contabiliza UN documento LOCAL (subido desde la web) — CLI para services/pipeline.py.

Uso:
  python webproc.py --company <VAT|odoo_company_id|clave> --local-file <ruta> [--web-upload-id N]

Réplica del flujo por-fichero del poller (extract → validate → process_invoice en
BORRADOR), sin Drive: el archivo ya está en el VPS. Imprime al final UNA línea JSON
(contrato de pipeline._parse_extractor_output):
  {status: done|duplicate|failed, classification, odoo_move_id, odoo_move_name, reason}
Los logs van a stderr para no ensuciar el JSON de stdout.
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import companies          # noqa: E402
import extractor          # noqa: E402
import process_invoice    # noqa: E402
from odoo import get_rpc  # noqa: E402

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("webproc")

LOW_CONFIDENCE = 0.7
_CLAVES = {"austral": 12}
_CLAVE_BY_CID = {v: k for k, v in _CLAVES.items()}


def cargar_reglas(company, upload_id=None):
    """Reglas de contabilización del revisor (tabla regla_asiento del app.db de la web).

    Dos ámbitos:
      - scope='documento' (por defecto): solo se inyectan en SU subida (upload_id).
      - scope='proveedor': se inyectan SIEMPRE — su texto nombra al proveedor y el
        extractor solo las aplica si el documento es de ese proveedor."""
    import os
    import sqlite3
    db = os.path.join(os.getenv("AUSTRAL_BACKEND", "/opt/austral-contab/backend"), "data", "app.db")
    try:
        con = sqlite3.connect(db)
        if upload_id:
            rows = con.execute(
                "SELECT texto, imagen_path FROM regla_asiento "
                "WHERE activa=1 AND (upload_id=? OR scope='proveedor') ORDER BY id",
                (int(upload_id),)).fetchall()
        else:
            rows = con.execute(
                "SELECT texto, imagen_path FROM regla_asiento "
                "WHERE activa=1 AND scope='proveedor' ORDER BY id").fetchall()
        con.close()
        return [{"texto": t, "imagen": i} for t, i in rows if (t or "").strip()]
    except Exception as e:  # noqa: BLE001 — sin reglas no se bloquea el proceso
        log.warning(f"no se pudieron cargar las reglas: {e}")
        return []


def resolve_company(key):
    """Acepta VAT (con o sin prefijo ES), odoo_company_id o la clave de la web."""
    k = str(key or "").strip()
    if not k:
        return None
    k_vat = k[2:] if k.upper().startswith("ES") else k
    for c in companies.COMPANIES:
        if c.get("vat") and c["vat"].upper() == k_vat.upper():
            return c
    try:
        return companies.by_company_id(int(k))
    except (TypeError, ValueError):
        pass
    if k.lower() in _CLAVES:
        return companies.by_company_id(_CLAVES[k.lower()])
    return None


def out(d):
    print(json.dumps(d, ensure_ascii=False, default=str))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--local-file", required=True)
    ap.add_argument("--web-upload-id", default=None)   # informativo (trazas)
    ap.add_argument("--hint", default=None)            # instrucción puntual del revisor
    # PÁGINAS DE LA FACTURA: copia recortada que se manda SOLO al reconocimiento de
    # texto (documentos largos que agotaban el tiempo). Lo que se adjunta en Odoo y se
    # ve en el visor es siempre --local-file, el documento ENTERO.
    ap.add_argument("--ocr-file", default=None)
    ap.add_argument("--skip-po", action="store_true",
                    help="contabilizar como gasto normal aunque el proveedor tenga pedidos")
    args = ap.parse_args()

    company = resolve_company(args.company)
    if not company:
        out({"status": "failed", "reason": f"no company config para '{args.company}'"})
        return
    p = Path(args.local_file)
    if not p.exists():
        out({"status": "failed", "reason": f"archivo no encontrado: {p}"})
        return

    reglas = cargar_reglas(company, upload_id=args.web_upload_id)
    # el OCR puede ir sobre un recorte de páginas; el resto del proceso usa el original
    p_ocr = Path(args.ocr_file) if (args.ocr_file and Path(args.ocr_file).exists()) else p
    log.info(f"[{company['name']}] extrayendo {p.name} (upload {args.web_upload_id}, "
             f"{len(reglas)} reglas)" + (f" · OCR sobre recorte {p_ocr}" if p_ocr != p else ""))
    try:
        data = extractor.extract(p_ocr, company, hint=args.hint, rules=reglas)
    except Exception as e:  # noqa: BLE001 — el motivo viaja en el JSON
        out({"status": "failed", "reason": f"extractor: {e}"})
        return

    err = extractor.validate(data)
    if err:
        # se sale ANTES de tocar `data`: cuando el OCR devuelve una LISTA (varias facturas
        # en un mismo PDF), el `data.get` de abajo reventaba y el revisor veía el traceback
        # en vez del motivo que `validate` ya había preparado.
        out({"status": "failed", "reason": err})
        return
    dt = (data.get("document_type") or "").lower()
    if dt in ("", "not_a_document"):
        out({"status": "failed", "classification": dt or None,
             "reason": data.get("error") or "no es un documento contabilizable"})
        return

    # NÓMINAS y pagos AEAT/TGSS (portados de Austral, siempre BORRADOR)
    if dt in ("nomina", "irpf_payment", "ss_payment", "other_official"):
        rpc = get_rpc()
        det = companies.by_sigla(data.get("company_recipient"))
        comp = det if (det and det["odoo_company_id"] != company["odoo_company_id"]) else company
        try:
            if dt == "nomina":
                import process_nomina
                res = process_nomina.process(rpc, comp, data, pdf_bytes=p.read_bytes(), pdf_name=p.name)
            else:
                import process_tax_payment
                res = process_tax_payment.process(rpc, comp, data, pdf_bytes=p.read_bytes(), pdf_name=p.name)
        except Exception as e:  # noqa: BLE001
            out({"status": "failed", "classification": dt, "reason": f"proceso {dt}: {e}"})
            return
        sig = companies.sigla_of(comp)
        cid2 = comp["odoo_company_id"]
        if res.get("status") in ("created", "duplicate"):
            try:
                nm = rpc.read("account.move", [res["move_id"]], ["name"], company_id=cid2)[0].get("name")
            except Exception:  # noqa: BLE001
                nm = None
            clas = {"nomina": "nómina (borrador)", "irpf_payment": "pago IRPF (borrador)",
                    "ss_payment": "pago SS (borrador)", "other_official": "pago oficial (borrador)"}[dt]
            out({"status": "done" if res["status"] == "created" else "duplicate",
                 "classification": f"{clas} · {sig}" + (" · revisar" if res.get("needs_review") else ""),
                 "odoo_move_id": res["move_id"], "odoo_move_name": nm,
                 "odoo_company_id": cid2, "empresa": sig,
                 "review_reason": res.get("review_reason"), "publicada": False})
        else:
            out({"status": "failed", "classification": dt, "reason": res.get("error")})
        return

    if dt != "invoice":
        out({"status": "failed", "classification": dt,
             "reason": f"tipo '{dt}' aún no soportado"})
        return

    # ANTI-MEZCLA: el asiento va a la empresa a la que está DIRIGIDO el documento
    # (BILL TO/receptor), aunque la subida apuntara a otra. Nunca se mezclan.
    rerouted_from = None
    det = companies.by_sigla(data.get("company_recipient"))
    if det and det["odoo_company_id"] != company["odoo_company_id"]:
        rerouted_from = company["name"]
        log.info(f"documento dirigido a {det['name']} (subida apuntaba a {company['name']}) "
                 f"→ se contabiliza en {det['name']}")
        company = det

    rpc = get_rpc()

    # ASIENTO DIRECTO (regla del revisor): el documento es un RECIBO, no una factura de
    # proveedor → DEBE gasto / HABER la cuenta que diga la regla (normalmente la 5200xx
    # de la tarjeta), sin tercero ni desglose de IVA. Va antes del circuito de pedidos:
    # un recibo no se cotejará nunca contra un pedido de compra.
    if data.get("asiento_directo"):
        import process_asiento
        try:
            res = process_asiento.process(rpc, company, data,
                                          pdf_bytes=p.read_bytes(), pdf_name=p.name)
        except Exception as e:  # noqa: BLE001 — el motivo viaja en el JSON
            out({"status": "failed", "classification": "recibo (asiento directo)",
                 "reason": f"asiento directo: {e}"})
            return
        sig = companies.sigla_of(company)
        if res.get("status") in ("created", "duplicate"):
            out({"status": "done" if res["status"] == "created" else "duplicate",
                 "classification": f"recibo · asiento directo · {sig} · {res.get('info') or ''}"[:200],
                 "odoo_move_id": res["move_id"], "odoo_move_name": res.get("move_name"),
                 "odoo_company_id": company["odoo_company_id"], "empresa": sig,
                 "publicada": False})
        else:
            out({"status": "failed", "classification": "recibo (asiento directo)",
                 "reason": res.get("error")})
        return

    # CIRCUITO DE PEDIDOS: si el proveedor EXISTE y tiene pedidos de compra sin
    # facturar del todo, la factura NO se contabiliza como gasto normal — se manda
    # al cotejo factura↔pedido de la web (Pedidos de compra → Facturas sin cotejar).
    if not args.skip_po:
        try:
            cid_po = company["odoo_company_id"]
            pid_po = None
            vat = process_invoice.normalize_vat(data.get("supplier_vat"))
            if vat:
                # búsqueda robusta (ignora mayúsculas, espacios/guiones y prefijo país)
                pid_po = process_invoice.buscar_partner_por_vat(rpc, cid_po, vat)
            if not pid_po and (data.get("supplier_name") or "").strip():
                r = rpc.search_read("res.partner",
                                    [("name", "=ilike", data["supplier_name"].strip())],
                                    ["id"], limit=1, company_id=cid_po)
                if r:
                    pid_po = r[0]["id"]
            if pid_po:
                # incluye pedidos SIN confirmar (draft/sent): la factura de compra debe
                # quedar en "Facturas sin cotejar" tanto si el pedido está confirmado
                # como si no (el revisor confirma el pedido y coteja desde la web).
                pos = rpc.search_read("purchase.order",
                                      [("company_id", "=", cid_po), ("partner_id", "=", pid_po),
                                       ("state", "in", ["draft", "sent", "purchase", "done"]),
                                       ("invoice_status", "!=", "invoiced")],
                                      ["name"], limit=50, company_id=cid_po)
                if pos:
                    out({"status": "po_review",
                         "classification": "factura de pedido · por cotejar",
                         "odoo_company_id": cid_po, "empresa": companies.sigla_of(company),
                         "partner_id": pid_po,
                         "supplier_name": data.get("supplier_name"),
                         "po_abiertos": [x["name"] for x in pos],
                         "extract": data,
                         "reason": f"proveedor con {len(pos)} pedido(s) por facturar → cotejo factura↔pedido"})
                    return
        except Exception as e:  # noqa: BLE001 — si el chequeo falla, sigue el flujo normal
            log.warning(f"chequeo de pedidos falló (se sigue flujo normal): {e}")

    try:
        res = process_invoice.process(rpc, company, data, pdf_bytes=p.read_bytes(), pdf_name=p.name)
    except Exception as e:  # noqa: BLE001
        out({"status": "failed", "classification": "invoice", "reason": f"proceso: {e}"})
        return

    st = res.get("status")
    move_id = res.get("move_id")
    cid = company["odoo_company_id"]

    def _name():
        try:
            return rpc.read("account.move", [move_id], ["name"], company_id=cid)[0].get("name")
        except Exception:  # noqa: BLE001 — el nombre es informativo
            return None

    sigla = companies.sigla_of(company)
    if st == "duplicate":
        out({"status": "duplicate", "classification": f"invoice · {sigla}",
             "odoo_move_id": move_id, "odoo_move_name": _name(),
             "odoo_company_id": cid, "empresa": sigla,
             "reason": "ya contabilizada (mismo proveedor + ref)"})
    elif st == "created":
        conf = float(data.get("extraction_confidence") or 1.0)
        needs = bool(res.get("needs_review")) or conf < LOW_CONFIDENCE
        # REGLA (usuario, 13 jul 2026): TODO se contabiliza en BORRADOR — tanto la
        # subida web como el cron de Drive. La publicación es SIEMPRE humana, desde
        # Odoo o desde la web. (Sustituye a la regla anterior de auto-publicar >60%.)
        clas = f"invoice (borrador) · {sigla}"
        if rerouted_from:
            clas += " (redirigida)"
        if needs:
            clas += " · revisar cuenta"
        out({"status": "done", "classification": clas,
             "odoo_move_id": move_id, "odoo_move_name": _name(),
             "odoo_company_id": cid, "empresa": sigla,
             "rerouted_from": rerouted_from,
             "publicada": False,
             "expense_account": res.get("expense_account"),
             "review_reason": res.get("review_reason"),
             "confidence": conf})
    else:
        out({"status": "failed", "classification": "invoice",
             "reason": res.get("error") or "error desconocido en el proceso"})


if __name__ == "__main__":
    main()
