#!/usr/bin/env python3
"""
Reads /tmp/dudas/<vat>_dudas.json (produced by dudas_xlsx_collect.py),
downloads the existing dudas_para_revisar.xlsx in each company root via SA,
merges new rows preserving user-filled `tu_decision`, uploads back.

Runs in /opt/automation/venv (has google + openpyxl).
"""
import io
import json
import logging
import sys
from datetime import date
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment

sys.path.insert(0, "/opt/automation")
import drive_ops  # noqa: E402

INPUT_DIR = Path("/tmp/dudas")
XLSX_NAME = "dudas_para_revisar.xlsx"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEADER = [
    "empresa", "tipo", "id_odoo", "ref_o_concepto", "fecha",
    "importe", "descripcion_corta", "motivo_duda", "sugerencia_actual",
    "tu_decision", "notas", "estado_actual", "primer_visto", "ultimo_visto",
]

log = logging.getLogger("dudas_publish")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _row_key(r: dict) -> str:
    return f"{r.get('tipo','')}::{r.get('id_odoo','')}::{r.get('ref_o_concepto','')[:40]}"


def _row_to_list(r: dict, today: str, primer_visto: str = "") -> list:
    return [
        r.get("empresa", ""), r.get("tipo", ""), r.get("id_odoo", ""),
        r.get("ref_o_concepto", ""), r.get("fecha", ""), r.get("importe", ""),
        r.get("descripcion_corta", ""), r.get("motivo_duda", ""),
        r.get("sugerencia_actual", ""), r.get("tu_decision", ""),
        r.get("notas", ""), r.get("estado_actual", ""),
        primer_visto or today, today,
    ]


def _list_to_dict(values) -> dict:
    return {HEADER[i]: values[i] if i < len(values) else "" for i in range(len(HEADER))}


def _build_workbook(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Dudas"
    ws.append(HEADER)
    hdr_fill = PatternFill(start_color="1f4e79", end_color="1f4e79", fill_type="solid")
    hdr_font = Font(bold=True, color="FFFFFF")
    for cell in ws[1]:
        cell.fill = hdr_fill; cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center")
    widths = {"A":25, "B":14, "C":9, "D":25, "E":12, "F":12, "G":30, "H":35, "I":35, "J":25, "K":40, "L":16, "M":12, "N":12}
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    review_fill = PatternFill(start_color="fff2cc", end_color="fff2cc", fill_type="solid")
    descuadre_fill = PatternFill(start_color="fce4d6", end_color="fce4d6", fill_type="solid")
    for r in rows:
        ws.append(r)
        last = ws.max_row
        if (r[1] if len(r) > 1 else "") == "banco_descuadre":
            for c in ws[last]: c.fill = descuadre_fill
        elif (r[9] if len(r) > 9 else ""):
            for c in ws[last]: c.fill = review_fill
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def find_xlsx(svc, folder_id: str):
    q = f"'{folder_id}' in parents and trashed=false and name='{XLSX_NAME}'"
    files = svc.files().list(q=q, fields="files(id,name)", supportsAllDrives=True).execute().get("files", [])
    return files[0] if files else None


def publish(svc, payload: dict) -> dict:
    folder = payload.get("pending_folder")
    if not folder:
        return {"company": payload["company"], "skipped": "no pending_folder"}
    file_meta = find_xlsx(svc, folder)
    if not file_meta:
        return {
            "company": payload["company"],
            "skipped": (
                f"file '{XLSX_NAME}' not found in {payload['company']} root. "
                "Crealo una vez (vacio) y el bot lo poblará."
            ),
        }

    existing_by_key = {}
    try:
        raw = svc.files().get_media(fileId=file_meta["id"], supportsAllDrives=True).execute()
        wb_existing = load_workbook(io.BytesIO(raw))
        ws_existing = wb_existing.active
        for row_cells in ws_existing.iter_rows(min_row=2, values_only=True):
            row_dict = _list_to_dict(list(row_cells))
            existing_by_key[_row_key(row_dict)] = row_dict
    except Exception as e:
        log.warning(f"could not read existing xlsx for {payload['company']}: {e}")

    today = payload["today"]
    fresh = payload["rows"]

    final_rows = []
    seen = set()
    new_count = 0
    preserved = 0
    for fr in fresh:
        key = _row_key(fr)
        seen.add(key)
        existing = existing_by_key.get(key)
        if existing:
            merged = dict(fr)
            decision = (existing.get("tu_decision") or "").strip()
            merged["tu_decision"] = decision
            primer = existing.get("primer_visto") or today
            user_notas = existing.get("notas") or ""
            if user_notas and len(user_notas) > len(fr.get("notas", "")):
                merged["notas"] = user_notas
            if decision: preserved += 1
            final_rows.append(_row_to_list(merged, today, str(primer)))
        else:
            final_rows.append(_row_to_list(fr, today))
            new_count += 1

    closed = 0
    for k, ex in existing_by_key.items():
        if k in seen: continue
        if (ex.get("tu_decision") or "").strip():
            ex["estado_actual"] = "RESUELTO"
            final_rows.append(_row_to_list(ex, today, str(ex.get("primer_visto") or "")))
            closed += 1

    final_rows.sort(key=lambda r: (r[1] or "", str(r[2] or "")))
    xlsx_bytes = _build_workbook(final_rows)

    from googleapiclient.http import MediaIoBaseUpload
    media = MediaIoBaseUpload(io.BytesIO(xlsx_bytes), mimetype=XLSX_MIME, resumable=False)
    svc.files().update(fileId=file_meta["id"], media_body=media, supportsAllDrives=True).execute()

    return {
        "company": payload["company"],
        "file_id": file_meta["id"],
        "rows_written": len(final_rows),
        "new": new_count,
        "preserved_decisions": preserved,
        "resolved": closed,
    }


def main():
    if not INPUT_DIR.exists():
        log.warning(f"no input dir at {INPUT_DIR}")
        return

    svc = drive_ops._service()
    out = []
    for f in sorted(INPUT_DIR.glob("*_dudas.json")):
        try:
            payload = json.loads(f.read_text())
            stats = publish(svc, payload)
        except Exception as e:
            log.exception(f"failed for {f.name}")
            stats = {"file": f.name, "error": str(e)}
        out.append(stats)
        log.info(f"{f.name}: {stats}")

    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
