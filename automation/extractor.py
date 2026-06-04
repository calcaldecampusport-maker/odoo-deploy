#!/usr/bin/env python3
"""
Daily extractor — replaces Cowork.

For each company:
  1. List PDFs/JPG/PNG/HEIC files in Pendientes folder.
  2. Skip those already in Cola_VPS as {drive_file_id}.json.
  3. Download each new file. If HEIC, convert to JPG.
  4. Run `claude -p ...` headless to extract invoice fields.
  5. Validate the JSON.
  6. Upload to Cola_VPS as {drive_file_id}.json so the existing poller picks it up.

Designed to run from cron as user `odoo` (same user that ran `claude /login`).
"""
# === pipeline isolation guard (auto-injected) ===
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
try:
    import companies as _comp_guard
    if getattr(_comp_guard, "PIPELINE_NAME", None) != 'cararjfam':
        raise RuntimeError(
            f"PIPELINE_MISMATCH: script {__file__} expected pipeline='cararjfam' "
            f"but loaded companies.PIPELINE_NAME={getattr(_comp_guard, 'PIPELINE_NAME', None)!r}"
        )
except ImportError:
    pass  # script sin dependencia de companies.py (e.g. drive_ops)
# === end isolation guard ===

import argparse
import base64
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, _HERE)  # FIX: era _austral, leía companies equivocado
import drive_ops  # noqa: E402
import companies as comp  # noqa: E402

CLAUDE_BIN = "/usr/local/bin/claude"
CLAUDE_TIMEOUT = 180
TMP_DIR = Path("/var/automation/extractor_tmp")
SUPPORTED_INVOICE_MIMES = {
    "application/pdf": ".pdf",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/heif": ".heic",
    "image/heic": ".heic",
}
SUPPORTED_BANK_MIMES = {
    "text/plain": ".n43",
    "text/csv": ".csv",
    "application/octet-stream": ".n43",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.google-apps.spreadsheet": ".csv",
}
SUPPORTED_SEPA_MIMES = {"application/xml": ".xml", "text/xml": ".xml"}
BANK_FILENAME_HINTS = (".n43", ".csb", "n43", "csb43", "extracto", "movimientos", "movimentos")
SEPA_FILENAME_HINTS = (".xml",)
SUPPORTED_MIMES = {**SUPPORTED_INVOICE_MIMES, **SUPPORTED_BANK_MIMES, **SUPPORTED_SEPA_MIMES}
BANK_IMPORTER = "/opt/automation/bank_importer.py"
SEPA_IMPORTER = "/opt/automation/sepa_xml_importer.py"
GOOGLE_SHEETS_MIME = "application/vnd.google-apps.spreadsheet"
TOLERANCE = 0.05

log = logging.getLogger("extractor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


PROMPT = """You are a Spanish accounting document extractor for company "{company_name}" (CIF {company_vat}).

Read the file at: {file_path}

Step 1: classify the document. document_type must be one of:
  - "invoice": vendor bill / factura de proveedor (the most common case)
  - "nomina": employee payslip / nomina
  - "irpf_payment": IRPF retention payment receipt to AEAT (modelos 111, 115, 130, 190, 216 etc.)
  - "ss_payment": Social Security cotizacion document from TGSS
  - "other_official": other tax / official document (multas, subvenciones, modelos AEAT distintos a IRPF, certificados)
  - "not_a_document": the file is not a financial document (skip)

Step 2: extract fields. Output ONLY a valid JSON object — no markdown, no prose.

Common fields (always include):
- document_type: one of the values above
- supplier_name (string): vendor / counterparty / employee name. For nomina, use the employee name. For irpf/ss, use "Hacienda Publica" or "TGSS Tesoreria General de la Seguridad Social".
- supplier_vat (string): NIF/CIF. For nomina employee, use their NIF. For irpf, use "Q2826000H" (HP). For TGSS, use "Q2827003A". Strip "ES" prefix.
- invoice_ref (string): document reference (factura number, modelo+trimestre+ejercicio for IRPF, etc.)
- invoice_date (string YYYY-MM-DD): issue date.
- due_date (string YYYY-MM-DD or null): due date if printed.
- subtotal (number): base imponible / sueldo bruto / cuota base.
- tax_total (number): IVA / 0 for nominas / 0 for irpf payments.
- total (number): total a pagar.
- lines (array): one entry per relevant section. Each: {{"description": "...", "amount": <number>, "tax_rate": 21|10|4|0}}.
- extraction_confidence (number 0..1): your honest confidence.
- extraction_notes (string): any doubt, assumption, OCR ambiguity, or relevant remark for the human reviewer.

Special fields by document_type (optional but useful):
- For "nomina": REQUIRED include "extra": {{"irpf_total": <number>, "ss_empleado_total": <number>, "aportaciones_empresa_total": <number>, "base_contingencias_comunes_total": <number>, "salario_especie_total": <number>, "liquido_total": <number>, "period": "YYYY-MM", "employees": [{{"name": "...", "nif": "...", "bruto": <number>, "irpf": <number>, "ss": <number>, "salario_especie": <number>, "liquido": <number>, "base_contingencias_comunes": <number>, "base_cc_empresa": <number>, "base_at_ep": <number>, "cuota_cc_empresa": <number>, "cuota_at_empresa": <number>, "cuota_desempleo_empresa": <number>, "cuota_fp_empresa": <number>, "cuota_fogasa_empresa": <number>, "ss_empresa_total": <number>, "tipo_contrato": "indefinido"|"temporal"}}]}}. aportaciones_empresa_total is the SUM across all payslips of the FULL company SS contributions (contingencias comunes empresa + desempleo empresa + FOGASA + formación profesional + AT/EP). Typically ~30% of bruto total. IMPORTANT: do NOT confuse with base_contingencias_comunes or base_accidente — those are BASES (calculation amounts), not contributions; never sum them. base_contingencias_comunes is per employee the base used for the CC retention (usually equals bruto but can differ slightly when there are non-cotizable concepts). salario_especie (also called "salario en especie" or "retribucion en especie") represents non-cash compensation — for socios/administradores it usually equals their autonomo cuota that the company pays. Default to 0 if not present. The arithmetic: subtotal (= sum of brutos including salario_especie) - tax_total (= irpf_total + ss_empleado_total) = total (= liquido_total). NOTE the salario_especie does NOT enter the liquido (it is not cash), but it IS included in the bruto for tax purposes. The "lines" array must contain ONE entry per employee with description="Nomina <nombre> <NIF> bruto <bruto> liquido <liquido>", amount=bruto, tax_rate=0.
- For "irpf_payment": include "extra": {{"modelo": "111"|"115"|"130"|"190"|"216", "ejercicio": "YYYY", "periodo": "1T"|"2T"|"3T"|"4T"|"01"|...}}
- For "ss_payment": include "extra": {{"periodo": "YYYY-MM", "ccc": "..."}}

Rules:
- Read tax rates AS PRINTED. Spain has 21/10/4/0. For nominas/irpf/ss, tax_rate is normally 0.
- Confidence: 1.0 = clear PDF nativo; 0.7-0.9 = minor doubts; <0.7 = ambiguous; 0.6 = had to assume IVA rate.
- subtotal + tax_total must equal total within 0.05 EUR. If not, write values as printed and note discrepancy.
- For nominas: "total" is the net paid (liquido). subtotal = bruto. tax_total = retenciones (irpf+ss empleado).
- For irpf payments: "total" is the amount paid to AEAT. subtotal = total, tax_total = 0.
- If the document is not extractable, output: {{"document_type": "not_a_document", "error": "<reason>"}}.

Output: a SINGLE JSON object. Nothing else.
"""


def _run_claude(file_path: Path, company: dict) -> dict:
    prompt = PROMPT.format(
        company_name=company["name"],
        company_vat=company["vat"],
        file_path=file_path.name,
    )
    log.info(f"  invoking claude for {file_path.name}")
    try:
        result = subprocess.run(
            [
                CLAUDE_BIN, "-p", prompt,
                "--output-format", "text",
                "--permission-mode", "bypassPermissions",
                "--add-dir", str(file_path.parent),
            ],
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
            cwd=str(file_path.parent),
            env={**os.environ, "HOME": os.environ.get("HOME", "/opt/odoo17")},
        )
    except subprocess.TimeoutExpired:
        return {"error": "claude timed out"}

    if result.returncode != 0:
        return {"error": f"claude exit {result.returncode}: {result.stderr[:300]}"}

    out = result.stdout.strip()
    out = _strip_code_fences(out)
    return _parse_first_json(out)


def _strip_code_fences(text: str) -> str:
    if text.startswith("```"):
        lines = text.split("\n")
        cleaned = [l for l in lines if not l.strip().startswith("```")]
        return "\n".join(cleaned).strip()
    return text


def _parse_first_json(text: str) -> dict:
    text = text.replace("\ufffd", "?")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder(strict=False)
    for i, c in enumerate(text):
        if c == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    return {"error": "could not parse claude output as JSON"}


def _validate(payload: dict) -> str | None:
    if "error" in payload:
        return payload["error"]
    if (payload.get("document_type") or "").lower() == "not_a_document":
        return f"not a document: {payload.get('error','')}"
    required = ["supplier_name", "supplier_vat", "invoice_ref", "invoice_date",
                "subtotal", "tax_total", "total", "lines"]
    missing = [k for k in required if k not in payload or payload[k] in (None, "")]
    if missing:
        return f"missing fields: {missing}"
    doc_type = (payload.get("document_type") or "invoice").lower()
    try:
        sub = round(float(payload["subtotal"]), 2)
        tax = round(float(payload["tax_total"]), 2)
        tot = round(float(payload["total"]), 2)
        if doc_type == "nomina":
            extra = payload.get("extra") or {}
            especie = round(float(extra.get("salario_especie_total") or 0), 2)
            if abs((sub - tax - especie) - tot) > TOLERANCE:
                return f"math mismatch nomina: bruto({sub})-tax({tax})-especie({especie})!=liquido({tot})"
        else:
            if abs(sub + tax - tot) > TOLERANCE:
                return f"math mismatch: {sub}+{tax}!={tot}"
    except (TypeError, ValueError) as e:
        return f"invalid numbers: {e}"
    return None


def _convert_heic(src: Path) -> Path:
    from PIL import Image
    import pillow_heif
    pillow_heif.register_heif_opener()
    img = Image.open(src)
    img.thumbnail((2200, 2200))
    dst = src.with_suffix(".jpg")
    img.convert("RGB").save(dst, "JPEG", quality=88)
    return dst


def _list_processed_ids_in_drive(svc, contabilizado_folder: str, revision_folder: str) -> set[str]:
    """Files already moved to Contabilizado or Revision are 'done' — skip them."""
    ids = set()
    for folder in (contabilizado_folder, revision_folder):
        resp = svc.files().list(
            q=f"'{folder}' in parents and trashed=false",
            fields="files(id)", pageSize=300,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        ids.update(f["id"] for f in resp.get("files", []))
    return ids


def _list_pending(svc, pending_folder_id: str):
    files = []
    for mime in SUPPORTED_MIMES:
        q = f"'{pending_folder_id}' in parents and trashed=false and mimeType='{mime}'"
        resp = svc.files().list(
            q=q, fields="files(id,name,mimeType,size)",
            pageSize=200, supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files.extend(resp.get("files", []))
    seen = set()
    out = []
    for f in files:
        if f["id"] in seen:
            continue
        seen.add(f["id"])
        out.append(f)
    return out



def _route_to_rejected(svc, fid, cfg):
    """Mueve un archivo a rechazadas_folder si está configurado; si no, fallback a revision_folder."""
    target = cfg.get("rechazadas_folder") or cfg.get("revision_folder")
    drive_ops.move_file(fid, target, svc=svc)

SKIP_FILENAME_HINTS = (
    "dudas", "aprendizaje", "_aplicado", "_procesado",
    # Informes / xlsx generados por scripts internos (no son facturas ni extractos, "backup_", "recovery")
    "pendientes_", "liquidaciones_", "matcheo_", "duplicados_",
    "revision_", "152_liquidaciones", "asientos_", "resumen_",
    "libro_mayor_", "informe_", "reporte_",
)


def _classify(file_meta: dict) -> str:
    """Return 'invoice', 'bank', 'sepa', 'skip' or 'unknown'."""
    mime = (file_meta.get("mimeType") or "").lower()
    name_l = (file_meta.get("name") or "").lower()
    if any(h in name_l for h in SKIP_FILENAME_HINTS):
        return "skip"
    if mime in SUPPORTED_INVOICE_MIMES:
        return "invoice"
    if mime in SUPPORTED_SEPA_MIMES or name_l.endswith(".xml"):
        return "sepa"
    if mime in SUPPORTED_BANK_MIMES:
        return "bank"
    if any(h in name_l for h in BANK_FILENAME_HINTS):
        return "bank"
    return "unknown"


def _process_bank(file_path: Path) -> tuple[int, str, str]:
    cmd = [ODOO_PYTHON, BANK_IMPORTER, "--file", str(file_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return result.returncode, result.stdout, result.stderr


def _process_sepa(file_path: Path) -> tuple[int, str, str]:
    cmd = [ODOO_PYTHON, SEPA_IMPORTER, "--file", str(file_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return result.returncode, result.stdout, result.stderr


def _download_bank_file(svc, file_id: str, mime: str, ext: str, dest: Path) -> Path:
    """Bank file download — Google Sheets need export, others get_media."""
    if mime == GOOGLE_SHEETS_MIME:
        data = svc.files().export(fileId=file_id, mimeType="text/csv").execute()
        if not isinstance(data, bytes):
            data = data.encode()
        dest = dest.with_suffix(".csv")
        dest.write_bytes(data)
        return dest
    drive_ops.download_to(file_id, dest, svc=svc)
    return dest


PROCESS_SCRIPT = "/opt/automation/process_invoice.py"
NOMINA_SCRIPT = "/opt/automation/nomina_processor.py"
TAX_PAYMENT_SCRIPT = "/opt/automation/tax_payment_processor.py"
ODOO_PYTHON = "/opt/odoo17/venv/bin/python"


def _process_with_orm(payload: dict, pdf_path: Path, company: dict) -> tuple[int, str, str]:
    """Route to the right processor based on document_type."""
    PROCESS_QUEUE_DIR = Path("/var/automation/queue")
    PROCESS_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    json_path = PROCESS_QUEUE_DIR / f"{payload['drive_file_id']}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    doc_type = (payload.get("document_type") or "invoice").lower()
    if doc_type == "nomina":
        script = NOMINA_SCRIPT
    elif doc_type in ("irpf_payment", "ss_payment", "other_official"):
        script = TAX_PAYMENT_SCRIPT
    else:
        script = PROCESS_SCRIPT
    cmd = [
        ODOO_PYTHON, script,
        "--json", str(json_path),
        "--pdf", str(pdf_path),
        "--company-id", str(company["odoo_company_id"]),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    return result.returncode, result.stdout, result.stderr


def process_company(svc, cfg: dict) -> dict:
    pending_folder = cfg.get("pending_folder")
    if not pending_folder:
        log.warning(f"[{cfg['name']}] no pending_folder configured, skip")
        return {"company": cfg["name"], "skipped": True}

    pending = _list_pending(svc, pending_folder)
    todo = pending  # files in Pendientes are by definition not yet processed (we move on success/failure)
    import os as _os
    _lim = _os.environ.get("EXTRACTOR_LIMIT")
    if _lim:
        todo = todo[:int(_lim)]

    log.info(f"[{cfg['name']}] pending={len(pending)} todo={len(todo)}")

    TMP_DIR.mkdir(parents=True, exist_ok=True)
    stats = {"company": cfg["name"], "todo": len(todo), "ok": 0, "fail": 0, "errors": [], "duplicates": [], "created": []}

    for f in todo:
        fid, fname, mime = f["id"], f["name"], f["mimeType"]
        kind = _classify(f)
        if kind == "skip":
            log.info(f"  {fname} -> skip (excluded by filename pattern)")
            continue
        ext = SUPPORTED_MIMES.get(mime, ".n43" if kind == "bank" else ".bin")
        local = TMP_DIR / f"{fid}{ext}"
        try:
            if kind == "sepa":
                drive_ops.download_to(fid, local, svc=svc)
                rc, out, err_text = _process_sepa(local)
                if rc == 0:
                    try:
                        drive_ops.move_file(fid, cfg["contabilizado_folder"], svc=svc)
                    except Exception:
                        log.exception("  could not move SEPA file to contabilizado")
                    stats["ok"] += 1
                    log.info(f"  {fname} -> SEPA payroll asiento creado")
                else:
                    try:
                        _route_to_rejected(svc, fid, cfg)
                    except Exception:
                        log.exception("  could not move SEPA file to revision")
                    stats["fail"] += 1
                    stats["errors"].append({"file": fname, "id": fid, "reason": f"sepa rc={rc}: {out[-300:]} {err_text[-300:]}"})
                    log.warning(f"  {fname} -> revision (sepa rc={rc})")
                continue

            if kind == "bank":
                local = _download_bank_file(svc, fid, mime, ext, local)
                rc, out, err_text = _process_bank(local)
                if rc == 0:
                    try:
                        drive_ops.move_file(fid, cfg["contabilizado_folder"], svc=svc)
                    except Exception:
                        log.exception("  could not move bank file to contabilizado")
                    stats["ok"] += 1
                    stats.setdefault("bank_imports", 0)
                    stats["bank_imports"] += 1
                    log.info(f"  {fname} -> bank statement imported")
                else:
                    try:
                        _route_to_rejected(svc, fid, cfg)
                    except Exception:
                        log.exception("  could not move bank file to revision")
                    stats["fail"] += 1
                    stats["errors"].append({"file": fname, "id": fid, "reason": f"bank rc={rc}: {err_text[-300:]} {out[-300:]}"})
                    log.warning(f"  {fname} -> revision (bank rc={rc})")
                continue

            drive_ops.download_to(fid, local, svc=svc)
            target = local
            if ext == ".heic":
                target = _convert_heic(local)
            payload = _run_claude(target, cfg)
            err = _validate(payload)
            if err:
                stats["fail"] += 1
                stats["errors"].append({"file": fname, "id": fid, "reason": err})
                log.warning(f"  {fname} -> revision ({err})")
                payload = {"drive_file_id": fid, "extraction_error": err}
                try:
                    _route_to_rejected(svc, fid, cfg)
                except Exception:
                    log.exception("  could not move to revision")
                continue
            payload["drive_file_id"] = fid
            payload["source_drive_url"] = f"https://drive.google.com/file/d/{fid}/view"
            payload["source_mime_type"] = mime
            rc, out, err_text = _process_with_orm(payload, target, cfg)
            invoice_id = None
            for line in out.splitlines():
                if line.startswith("INVOICE_ID="):
                    try:
                        invoice_id = int(line.split("=", 1)[1])
                    except ValueError:
                        pass
            if rc in (0, 20):
                try:
                    drive_ops.move_file(fid, cfg["contabilizado_folder"], svc=svc)
                except Exception:
                    log.exception("  could not move to contabilizado")
                stats["ok"] += 1
                marker = "duplicate" if rc == 20 else "created"
                rec = {"file": fname, "invoice_id": invoice_id, "total": payload.get("total"),
                       "supplier": payload.get("supplier_name"), "ref": payload.get("invoice_ref"),
                       "invoice_date": payload.get("invoice_date")}
                if rc == 20:
                    stats["duplicates"].append(rec)
                else:
                    stats["created"].append(rec)
                log.info(f"  {fname} -> {marker} invoice_id={invoice_id} total={payload.get('total')}")
            else:
                try:
                    _route_to_rejected(svc, fid, cfg)
                except Exception:
                    log.exception("  could not move to revision")
                stats["fail"] += 1
                stats["errors"].append({"file": fname, "id": fid, "reason": f"orm rc={rc}: {err_text[-300:]}"})
                log.warning(f"  {fname} -> revision (orm rc={rc})")
        except Exception as e:
            stats["fail"] += 1
            stats["errors"].append({"file": fname, "id": fid, "reason": f"exception: {e}"})
            log.exception(f"  {fname} -> exception")
        finally:
            for p in (local, local.with_suffix(".jpg")):
                try:
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass

    return stats


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--company", help="filter to a single VAT")
    args = p.parse_args()

    svc = drive_ops._service()
    overall = []
    for cfg in comp.COMPANIES:
        if args.company and cfg["vat"] != args.company:
            continue
        overall.append(process_company(svc, cfg))

    log.info("--- extractor done ---")
    for s in overall:
        log.info(f"  {s.get('company')}: ok={s.get('ok')} fail={s.get('fail')} todo={s.get('todo')}")
        for err in s.get("errors", []):
            log.warning(f"    error: {err['file']} ({err['reason']})")
    try:
        from datetime import date as _date
        out_dir = Path("/tmp/extractor_runs")  # FIX: era _austral
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{_date.today().isoformat()}.json").write_text(
            json.dumps({"summary": overall}, ensure_ascii=False, default=str)
        )
    except Exception:
        log.exception("could not persist extractor run summary")
    print(json.dumps({"summary": overall}, ensure_ascii=False))


if __name__ == "__main__":
    main()
