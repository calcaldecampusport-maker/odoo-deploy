"""Extrae UN documento de PEDIDO de proveedor (confirmación de pedido / albarán /
order confirmation / quote) con `claude -p` — CLI para "Pedidos de proveedor" de
la web: el documento se convierte en un purchase.order de Odoo.

Uso:
  python orderproc.py --company <VAT|id|clave> --local-file <ruta>

Imprime UNA línea JSON:
  {status: done|failed, supplier_name, order_ref, supplier_ref, order_date,
   delivery_date, currency, lines: [{code, mfr_code, description, qty, price_unit}],
   confidence, notes, reason}
Los logs van a stderr.
"""
import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from webproc import resolve_company          # noqa: E402
from extractor import (CLAUDE_BIN, CLAUDE_TIMEOUT,  # noqa: E402
                       _strip_code_fences, _parse_first_json)

logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("orderproc")

PROMPT = """You are reading a SUPPLIER PURCHASE ORDER DOCUMENT addressed to company
"{company_name}" — it can be an order confirmation, a sales order acknowledgement,
a quote, a proforma or a delivery/packing document that reflects OUR purchase order
to the supplier. It will be converted into an Odoo purchase order.

Read the file at: {file_path}

Extract and output ONLY a valid JSON object — no markdown, no prose:
- document_type: "order" if it reflects a purchase/sales order (confirmation, quote,
  proforma, albaran); "invoice" if it is actually a vendor INVOICE (factura);
  "not_a_document" otherwise.
- supplier_name (string): the SUPPLIER company name.
- order_ref (string or null): OUR purchase order reference printed on the document
  (customer reference / pedido / PO number, e.g. "P01823").
- supplier_ref (string or null): the SUPPLIER's own order/confirmation/quote number.
- order_date (string YYYY-MM-DD or null): date the order was placed / document date.
- delivery_date (string YYYY-MM-DD or null): expected/committed DELIVERY or ship date
  if printed ("fecha de entrega", "delivery date", "ship date", "ETA"). null if absent.
- currency (string): ISO-4217 code AS PRINTED ("EUR", "USD"...). Default "EUR" only
  if clearly euros.
- lines (array): ONE entry per product line:
  {{"code": "<supplier part number / article code or null>",
    "mfr_code": "<manufacturer part number if a second code is printed, else null>",
    "cust_code": "<the CUST / customer reference / 'your ref' code printed on the line —
       this is OUR INTERNAL Odoo product reference (short alphanumeric like CSMD39,
       ICSM21, RSMD72, XTSM05). It is the KEY used to match the product in Odoo:
       read it CHARACTER BY CHARACTER, do not guess. null if absent>",
    "description": "<product description as printed>",
    "qty": <units ORDERED, number>,
    "price_unit": <unit price WITHOUT VAT if printed, else null>}}.
  Do NOT include totals, shipping, packaging, fees or note lines.
- extraction_confidence (number 0..1)
- extraction_notes (string): doubts, illegible parts, anything the reviewer should know.

Rules:
- qty = units ORDERED (columns like "cantidad", "qty", "ordered", "pedido"). If the
  document shows ordered vs shipped, use ORDERED and mention it in notes.
- Keep codes EXACT as printed (they are used to match Odoo products).
- unit price: if only a line total is printed, compute total/qty.
Output: a SINGLE JSON object. Nothing else.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--local-file", required=True)
    args = ap.parse_args()

    company = resolve_company(args.company)
    if not company:
        print(json.dumps({"status": "failed", "reason": f"no company config para '{args.company}'"}))
        return
    p = Path(args.local_file)
    if not p.exists():
        print(json.dumps({"status": "failed", "reason": f"archivo no encontrado: {p}"}))
        return

    prompt = PROMPT.format(company_name=company.get("name", ""), file_path=p.name)
    log.info(f"[{company['name']}] extrayendo pedido de proveedor {p.name}")
    try:
        result = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--output-format", "text",
             "--permission-mode", "bypassPermissions", "--add-dir", str(p.parent)],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT, cwd=str(p.parent),
            env={**os.environ, "HOME": os.environ.get("HOME", "/opt/odoo17")},
        )
    except subprocess.TimeoutExpired:
        print(json.dumps({"status": "failed", "reason": "claude timed out"}))
        return
    if result.returncode != 0:
        print(json.dumps({"status": "failed", "reason": f"claude exit {result.returncode}: {result.stderr[:300]}"}))
        return
    data = _parse_first_json(_strip_code_fences(result.stdout.strip()))
    if data.get("error") or (data.get("document_type") or "") == "not_a_document":
        print(json.dumps({"status": "failed",
                          "reason": data.get("error") or "no parece un documento de pedido"},
                         ensure_ascii=False))
        return
    lines = [l for l in (data.get("lines") or [])
             if l and float(l.get("qty") or 0) > 0 and (l.get("description") or "").strip()]
    print(json.dumps({
        "status": "done",
        "document_type": data.get("document_type") or "order",
        "supplier_name": data.get("supplier_name"),
        "order_ref": data.get("order_ref"),
        "supplier_ref": data.get("supplier_ref"),
        "order_date": data.get("order_date"),
        "delivery_date": data.get("delivery_date"),
        "currency": data.get("currency"),
        "lines": lines,
        "confidence": float(data.get("extraction_confidence") or 0),
        "notes": data.get("extraction_notes"),
    }, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
