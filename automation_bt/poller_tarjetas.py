"""Cron de TARJETAS de crédito: contabiliza lo subido a /Tarjetas banco/<tarjeta>/.

Cada carpeta de tarjeta (tarjetas.json, 13 tarjetas) recibe dos tipos de documento:
  - ticket/factura de una compra pagada con esa tarjeta → factura de proveedor en
    borrador (process_invoice) + asiento de PAGO en el diario de la tarjeta
    (DEBE 4xx proveedor / HABER 5200xx) — process_tarjeta.pago_tarjeta.
  - recibo/extracto MENSUAL de la propia tarjeta → asiento de CARGO en el banco
    histórico (DEBE 5200xx / HABER 572) — process_tarjeta.cargo_tarjeta.

TODO en BORRADOR (regla 13 jul 2026: la publicación es siempre humana). El fichero
se mueve a Contabilizado / Revisión / Rechazadas dentro de su carpeta de tarjeta.

Uso:
  python poller_tarjetas.py [--card 520022] [--sigla BIO] [--limit N] [--dry-run]
Pensado para cron como usuario `odoo` (CLI `claude` vía CLAUDE_CODE_OAUTH_TOKEN).
"""
import argparse
import json
import logging
import tempfile
from pathlib import Path

import drive_ops
import companies
import extractor
import process_invoice
import process_tarjeta
from odoo import get_rpc
from webproc import cargar_reglas

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("poller_tarjetas")

TMP = Path(tempfile.gettempdir()) / "medicalcables_tarjetas"
CONFIG = Path(__file__).resolve().parent / "tarjetas.json"
LOW_CONFIDENCE = 0.7

HINT = (
    "Este documento viene de la carpeta de la TARJETA DE CRÉDITO '{card}' (cuenta {code}, "
    "empresa {emp}). Clasifícalo así: (a) si es un ticket o factura de una COMPRA pagada con "
    "esta tarjeta → document_type='invoice' normal (extrae proveedor, fecha, base, IVA, total). "
    "(b) si es el RECIBO / EXTRACTO / LIQUIDACIÓN MENSUAL de la propia tarjeta (lo emite el "
    "banco, lista los movimientos del período y un TOTAL que el banco carga en la cuenta "
    "corriente) → usa document_type='card_statement' con: total = importe total del cargo, "
    "invoice_date = fecha de cargo en cuenta (si no consta, el fin del período), "
    "invoice_ref = período (p.ej. '2026-06'), supplier_name = banco emisor, "
    "subtotal = total, tax_total = 0, lines = []."
)


def _cargar_tarjetas():
    with open(CONFIG, encoding="utf-8") as fh:
        return json.load(fh)["tarjetas"]


def _procesar_fichero(rpc, company, card, f, svc, dry_run):
    """Devuelve la clave de stats: contabilizado|revision|rechazada|duplicada|None."""
    fid, fname = f["id"], f["name"]
    tag = f"[{card['account_code']} {card['sigla']}]"
    dest = drive_ops.download_to(fid, TMP / fname, svc=svc)
    reglas = cargar_reglas(company)
    hint = HINT.format(card=card["account_name"], code=card["account_code"], emp=card["sigla"])
    data = extractor.extract(dest, company, hint=hint, rules=reglas)
    dt = (data.get("document_type") or "").lower()

    def mover(folder_key):
        if not dry_run:
            drive_ops.move_file(fid, card[folder_key], svc=svc)

    # ANTI-MEZCLA: en las carpetas de tarjeta la empresa la define la TARJETA.
    # Si el documento está dirigido a OTRA empresa del grupo → Revisión (humano).
    det = companies.by_sigla(data.get("company_recipient"))
    if det and det["odoo_company_id"] != company["odoo_company_id"]:
        log.warning(f"{tag} {fname} -> REVISIÓN (documento dirigido a {det['name']}, "
                    f"pero la tarjeta es de {company['name']} — no se mezcla)")
        mover("revision_folder")
        return "revision"

    # --- extracto mensual de la tarjeta → cargo banco (DEBE 5200xx / HABER 572)
    if dt == "card_statement":
        if not (data.get("total") and data.get("invoice_date")):
            log.warning(f"{tag} {fname} -> RECHAZADA (extracto sin total/fecha)")
            mover("rechazadas_folder")
            return "rechazada"
        if dry_run:
            log.info(f"{tag} {fname} -> (dry-run) EXTRACTO tarjeta {data.get('total')}€ "
                     f"{data.get('invoice_date')}, NO se contabiliza")
            return None
        res = process_tarjeta.cargo_tarjeta(rpc, company, card, data,
                                            pdf_bytes=dest.read_bytes(), pdf_name=fname)
        st = res.get("status")
        if st == "created":
            log.info(f"{tag} {fname} -> CARGO borrador move {res['move_id']} -> Contabilizado")
            mover("contabilizado_folder")
            return "contabilizado"
        if st == "duplicate":
            log.info(f"{tag} {fname} -> cargo DUPLICADO (move {res.get('move_id')}) -> Contabilizado")
            mover("contabilizado_folder")
            return "duplicada"
        log.error(f"{tag} {fname} -> REVISIÓN (cargo: {res.get('error')})")
        mover("revision_folder")
        return "revision"

    # --- resto: mismo criterio que el poller de Cola_VPS
    err = extractor.validate(data)
    if err or dt in ("", "not_a_document"):
        log.warning(f"{tag} {fname} -> RECHAZADA ({err or dt})")
        mover("rechazadas_folder")
        return "rechazada"
    if dt != "invoice":
        log.info(f"{tag} {fname} -> REVISIÓN (tipo '{dt}' no esperado en carpeta de tarjeta)")
        mover("revision_folder")
        return "revision"
    if dry_run:
        log.info(f"{tag} {fname} -> (dry-run) FACTURA detectada, NO se contabiliza")
        return None

    # factura de proveedor (borrador) + pago con tarjeta (borrador)
    res = process_invoice.process(rpc, company, data, pdf_bytes=dest.read_bytes(), pdf_name=fname)
    st = res.get("status")
    if st not in ("created", "duplicate"):
        log.error(f"{tag} {fname} -> RECHAZADA (factura: {res.get('error')})")
        mover("rechazadas_folder")
        return "rechazada"

    pago = process_tarjeta.pago_tarjeta(rpc, company, card, data,
                                        bill_move_id=res["move_id"],
                                        partner_id=res.get("partner_id"))
    conf = float(data.get("extraction_confidence") or 1.0)
    a_revision = (res.get("needs_review") or conf < LOW_CONFIDENCE
                  or pago.get("status") == "error")
    motivo = (res.get("review_reason")
              or (pago.get("error") and f"pago tarjeta: {pago['error']}")
              or (f"confianza {conf:.2f}" if conf < LOW_CONFIDENCE else ""))
    # regla usuario 20 jul 2026: las compras con tarjeta se consideran PAGADAS al
    # contabilizarse (publicar factura + pago y casar 4xx). Solo si no va a revisión.
    pagada = None
    if not a_revision and pago.get("move_id"):
        pagada = process_tarjeta.marcar_pagada(rpc, company, res["move_id"], pago["move_id"])
        if not pagada.get("ok"):
            a_revision = True
            motivo = f"marcar pagada: {pagada.get('error')}"
    log.info(f"{tag} {fname} -> factura {st} move {res['move_id']} · "
             f"pago {pago.get('status')} move {pago.get('move_id')} · "
             f"pagada {(pagada or {}).get('ok')} "
             f"-> {'REVISIÓN ' + str(motivo) if a_revision else 'Contabilizado'}")
    mover("revision_folder" if a_revision else "contabilizado_folder")
    if a_revision:
        return "revision"
    return "duplicada" if st == "duplicate" and pago.get("status") == "duplicate" else "contabilizado"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--card", help="código de cuenta (p.ej. 520022). Por defecto: todas")
    ap.add_argument("--sigla", help="filtrar por empresa (WSL|BIO|CORP)")
    ap.add_argument("--limit", type=int, default=None, help="máx. ficheros por tarjeta")
    ap.add_argument("--dry-run", action="store_true", help="No crea asientos ni mueve ficheros")
    args = ap.parse_args()

    tarjetas = _cargar_tarjetas()
    if args.card:
        tarjetas = [t for t in tarjetas if t["account_code"] == args.card]
    if args.sigla:
        tarjetas = [t for t in tarjetas if t["sigla"].upper() == args.sigla.upper()]

    rpc = get_rpc()
    svc = drive_ops._service()
    if args.dry_run:
        log.info("== DRY-RUN: no se crea nada ni se mueven ficheros ==")
    total = {"total": 0, "contabilizado": 0, "revision": 0, "rechazada": 0, "duplicada": 0}
    for card in tarjetas:
        company = companies.by_company_id(card["company_id"])
        if not company:
            log.error(f"[{card['account_code']} {card['sigla']}] sin company config, se salta")
            continue
        files = drive_ops.list_by_mimes(card["folder_id"], svc=svc)
        if args.limit:
            files = files[: args.limit]
        if files:
            log.info(f"[{card['account_code']} {card['sigla']} {card['account_name']}] "
                     f"{len(files)} documento(s)")
        total["total"] += len(files)
        for f in files:
            try:
                k = _procesar_fichero(rpc, company, card, f, svc, args.dry_run)
                if k:
                    total[k] += 1
            except Exception:  # noqa: BLE001 — un fichero no debe tumbar el lote
                log.exception(f"[{card['account_code']} {card['sigla']}] {f['name']} -> "
                              "error inesperado (se deja en la carpeta)")
            finally:
                try:
                    (TMP / f["name"]).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
    log.info(f"TOTAL tarjetas: {total}")


if __name__ == "__main__":
    main()
