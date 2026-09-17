"""Prueba de alta de factura de proveedor: crea un borrador de prueba en BIO,
lo verifica, comprueba el dedup y lo BORRA (no deja rastro)."""
import time

import odoo
import companies
import process_invoice as pi


def main():
    rpc = odoo.get_rpc()
    comp = companies.by_company_id(12)  # AUSTRAL (PGC)
    cid = comp["odoo_company_id"]
    ts = int(time.time())
    data = {
        "document_type": "invoice",
        "supplier_name": f"ZZ Proveedor Prueba {ts}",
        "supplier_vat": None,
        "invoice_ref": f"ZZTEST-{ts}",
        "invoice_date": "2026-06-15",
        "subtotal": 100.0, "tax_total": 21.0, "total": 121.0,
        "lines": [{"description": "Servicio de prueba", "amount": 100.0, "tax_rate": 21}],
        "extraction_notes": "prueba automatizada (se borra)",
    }
    r = pi.process(rpc, comp, data)
    print("process ->", r)
    mid = r.get("move_id")
    if r.get("status") == "created" and mid:
        m = rpc.read("account.move", [mid],
                     ["state", "move_type", "amount_untaxed", "amount_tax", "amount_total",
                      "partner_id", "invoice_line_ids", "ref", "narration"], company_id=cid)[0]
        print("MOVE:", {k: m.get(k) for k in ("state", "move_type", "amount_untaxed",
                                              "amount_tax", "amount_total", "ref")})
        print("   partner:", m.get("partner_id"), "| líneas:", len(m.get("invoice_line_ids") or []))
        print("   narration:", (m.get("narration") or "")[:160])
        r2 = pi.process(rpc, comp, data)
        print("DEDUP ->", r2.get("status"), "move_id", r2.get("move_id"), "(== original?", r2.get("move_id") == mid, ")")
        rpc.unlink("account.move", [mid], company_id=cid)
        if r.get("partner_created") and r.get("partner_id"):
            try:
                rpc.unlink("res.partner", [r["partner_id"]], company_id=cid)
            except Exception as e:  # noqa: BLE001
                print("   (no se pudo borrar partner de prueba, no crítico:", str(e)[:80], ")")
        print("CLEANUP: borrador y proveedor de prueba eliminados")


if __name__ == "__main__":
    main()
