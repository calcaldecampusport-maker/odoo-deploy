"""Poller de contabilización wiemspro (fase 1: facturas de proveedor).

Por cada empresa: lista los documentos en su carpeta de entrada (Cola_VPS),
los extrae con `claude -p`, contabiliza las FACTURAS en borrador vía XML-RPC y
mueve el fichero en Drive a Contabilizado / Revisión / Rechazadas.

  - invoice          -> process_invoice (borrador). Contabilizado, o Revisión si
                        needs_review (cuenta de gasto sin histórico) o baja confianza.
  - nomina/irpf/ss   -> Revisión (procesadores aún no portados en fase 1).
  - not_a_document / error de extracción -> Rechazadas.

Uso:
  python poller.py [--company 1|3|9] [--limit N] [--dry-run]

Pensado para cron como usuario `odoo` (con la CLI `claude` autenticada).
"""
import argparse
import logging
import tempfile
from pathlib import Path

import drive_ops
import companies
import extractor
import process_invoice
from odoo import get_rpc

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("poller")

TMP = Path(tempfile.gettempdir()) / "wiemspro_ocr"
LOW_CONFIDENCE = 0.7
NO_PROC_YET = {"nomina", "irpf_payment", "ss_payment", "other_official"}


def poll_company(rpc, company, svc, limit=None, dry_run=False):
    name = company["name"]
    files = drive_ops.list_by_mimes(company["pending_folder"], svc=svc)
    if limit:
        files = files[:limit]
    stats = {"total": len(files), "contabilizado": 0, "revision": 0, "rechazada": 0, "duplicada": 0}
    log.info(f"[{name}] {len(files)} documento(s) en Cola_VPS")

    for f in files:
        fid, fname = f["id"], f["name"]
        try:
            dest = drive_ops.download_to(fid, TMP / fname, svc=svc)
            data = extractor.extract(dest, company)
            err = extractor.validate(data)
            dt = (data.get("document_type") or "").lower()

            if err or dt in ("", "not_a_document"):
                log.warning(f"[{name}] {fname} -> RECHAZADA ({err or dt})")
                if not dry_run:
                    drive_ops.move_file(fid, company["rechazadas_folder"], svc=svc)
                stats["rechazada"] += 1
                continue

            if dt != "invoice":
                log.info(f"[{name}] {fname} -> REVISIÓN (tipo '{dt}' aún no soportado en fase 1)")
                if not dry_run:
                    drive_ops.move_file(fid, company["revision_folder"], svc=svc)
                stats["revision"] += 1
                continue

            # ANTI-MEZCLA: el asiento va a la empresa a la que está DIRIGIDO el
            # documento (BILL TO/receptor), aunque estuviera en la Cola_VPS de otra.
            comp_proc = company
            det = companies.by_sigla(data.get("company_recipient"))
            if det and det["odoo_company_id"] != company["odoo_company_id"]:
                log.info(f"[{name}] {fname} dirigido a {det['name']} → se contabiliza allí")
                comp_proc = det

            if dry_run:
                log.info(f"[{name}] {fname} -> (dry-run) FACTURA detectada, NO se contabiliza")
                continue

            pdf_bytes = dest.read_bytes()
            res = process_invoice.process(rpc, comp_proc, data, pdf_bytes=pdf_bytes, pdf_name=fname)
            st = res.get("status")
            conf = float(data.get("extraction_confidence") or 1.0)

            if st == "duplicate":
                log.info(f"[{name}] {fname} -> DUPLICADA (move {res.get('move_id')}) -> Contabilizado")
                if not dry_run:
                    drive_ops.move_file(fid, company["contabilizado_folder"], svc=svc)
                stats["duplicada"] += 1
            elif st == "created":
                a_revision = res.get("needs_review") or conf < LOW_CONFIDENCE
                target = "revision_folder" if a_revision else "contabilizado_folder"
                motivo = (res.get("review_reason") or (f"confianza {conf:.2f}" if conf < LOW_CONFIDENCE else ""))
                log.info(f"[{name}] {fname} -> BORRADOR move {res['move_id']} "
                         f"({res['expense_account']}) -> {'REVISIÓN' if a_revision else 'Contabilizado'} {motivo}")
                if not dry_run:
                    drive_ops.move_file(fid, company[target], svc=svc)
                stats["revision" if a_revision else "contabilizado"] += 1
            else:
                log.error(f"[{name}] {fname} -> RECHAZADA (proceso: {res.get('error')})")
                if not dry_run:
                    drive_ops.move_file(fid, company["rechazadas_folder"], svc=svc)
                stats["rechazada"] += 1
        except Exception:  # noqa: BLE001 — un fichero no debe tumbar el lote
            log.exception(f"[{name}] {fname} -> error inesperado (se deja en Cola_VPS)")
        finally:
            try:
                (TMP / fname).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass
    log.info(f"[{name}] resumen: {stats}")
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--company", type=int, help="odoo_company_id (1=WSL, 3=CORP, 9=BIO). Por defecto: todas")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="No crea asientos ni mueve ficheros")
    args = ap.parse_args()

    rpc = get_rpc()
    svc = drive_ops._service()
    comps = ([companies.by_company_id(args.company)] if args.company else companies.COMPANIES)
    comps = [c for c in comps if c]
    if args.dry_run:
        log.info("== DRY-RUN: no se crea nada ni se mueven ficheros ==")
    total = {"total": 0, "contabilizado": 0, "revision": 0, "rechazada": 0, "duplicada": 0}
    for c in comps:
        s = poll_company(rpc, c, svc, limit=args.limit, dry_run=args.dry_run)
        for k in total:
            total[k] += s.get(k, 0)
    log.info(f"TOTAL: {total}")


if __name__ == "__main__":
    main()
