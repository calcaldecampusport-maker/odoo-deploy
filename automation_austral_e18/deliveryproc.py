"""Extrae UN documento de ENTREGA de proveedor (albarán / delivery note / packing
list) con `claude -p` — CLI para la Recepción de pedidos de la web.

Uso:
  python deliveryproc.py --company <VAT|id|clave> --local-file <ruta> [--pos-abiertos "P01824,P01791"]

Imprime UNA línea JSON:
  {status: done|failed, supplier_name, delivery_ref, order_ref, date,
   lines: [{code, description, qty, price_unit}], confidence, notes, reason}
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
log = logging.getLogger("deliveryproc")

PROMPT = """You are reading a SUPPLIER DELIVERY DOCUMENT (albaran de entrega / delivery note /
packing list / shipping list) addressed to company "{company_name}".

Read the file at: {file_path}

Extract and output ONLY a valid JSON object — no markdown, no prose:
- document_type: "delivery_note" if it is a delivery/packing document; "invoice" if it is
  actually a vendor INVOICE (factura); "not_a_document" otherwise.
- supplier_name (string): the supplier/shipper company name.
- delivery_ref (string or null): the supplier's delivery note / packing list number.
- order_ref (string or null): OUR purchase order number referenced in the document
  (customer order / pedido / PO). {pos_hint}
- date (string YYYY-MM-DD or null): delivery/document date.
- lines (array): ONE entry per product line: {{"code": "<product/article code or null>",
  "description": "<product description as printed>", "qty": <units delivered, number>,
  "price_unit": <unit price if printed, else null>}}.
  Do NOT include totals, transport, packaging or note lines.
- extraction_confidence (number 0..1)
- extraction_notes (string): doubts, illegible parts, anything the reviewer should know.

Rules:
- qty = units actually DELIVERED (columns like "cantidad", "qty", "shipped", "servido").
- If a line shows ordered vs shipped, use the SHIPPED amount and mention it in notes.
- Keep descriptions EXACT as printed (sizes, models, colors matter for matching).
Output: a SINGLE JSON object. Nothing else.
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", required=True)
    ap.add_argument("--local-file", required=True)
    ap.add_argument("--pos-abiertos", default="")
    args = ap.parse_args()

    company = resolve_company(args.company)
    if not company:
        print(json.dumps({"status": "failed", "reason": f"no company config para '{args.company}'"}))
        return
    p = Path(args.local_file)
    if not p.exists():
        print(json.dumps({"status": "failed", "reason": f"archivo no encontrado: {p}"}))
        return

    pos_hint = ""
    if args.pos_abiertos.strip():
        pos_hint = ("Our OPEN purchase order numbers (the reference should be one of these "
                    f"if legible): {args.pos_abiertos.strip()}.")
    prompt = PROMPT.format(company_name=company.get("name", ""), file_path=p.name,
                           pos_hint=pos_hint)
    log.info(f"[{company['name']}] extrayendo albarán {p.name}")
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
                          "reason": data.get("error") or "no parece un documento de entrega"},
                         ensure_ascii=False))
        return
    lines = [l for l in (data.get("lines") or [])
             if l and float(l.get("qty") or 0) > 0 and (l.get("description") or "").strip()]
    print(json.dumps({
        "status": "done",
        "document_type": data.get("document_type") or "delivery_note",
        "supplier_name": data.get("supplier_name"),
        "delivery_ref": data.get("delivery_ref"),
        "order_ref": data.get("order_ref"),
        "date": data.get("date"),
        "lines": lines,
        "confidence": float(data.get("extraction_confidence") or 0),
        "notes": data.get("extraction_notes"),
    }, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
