#!/usr/bin/env python3
"""
Flask endpoint for invoice automation. Multi-company aware.

Routes:
  GET  /automation/health        -> liveness
  POST /automation/invoice       -> create draft vendor bill; auto-move Drive file
  GET  /automation/summary       -> today's invoices, JSON
  GET  /automation/pendientes    -> list files in pending folder of given company

Auth: header `X-Automation-Secret: <SECRET>` must match env AUTOMATION_SECRET.

The invoice payload may include `target_company_vat`. If absent, defaults to
the company at companies.DEFAULT_VAT.
"""
import json
import logging
import os
import secrets
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path

from flask import Flask, request, jsonify

sys.path.insert(0, "/opt/automation")
import drive_ops  # noqa: E402
import companies as comp  # noqa: E402

QUEUE_DIR = Path("/var/automation/queue")
LOG_DIR = Path("/var/log/automation")
PROCESS_SCRIPT = "/opt/automation/process_invoice.py"
SUMMARY_SCRIPT = "/opt/automation/summary.py"
PYTHON = "/opt/odoo17/venv/bin/python"
SUBPROCESS_TIMEOUT = 120
SECRET = os.environ.get("AUTOMATION_SECRET", "")

app = Flask(__name__)
log = logging.getLogger("automation_server")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _check_auth():
    provided = request.headers.get("X-Automation-Secret", "")
    if not SECRET or not secrets.compare_digest(provided, SECRET):
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


@app.route("/automation/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "service": "automation", "ts": datetime.utcnow().isoformat() + "Z"})


@app.route("/automation/companies", methods=["GET"])
def companies_list():
    auth_err = _check_auth()
    if auth_err:
        return auth_err
    return jsonify({"ok": True, "companies": comp.COMPANIES})


@app.route("/automation/invoice", methods=["POST"])
def invoice():
    auth_err = _check_auth()
    if auth_err:
        return auth_err

    if not request.is_json:
        return jsonify({"ok": False, "error": "expected application/json"}), 400

    data = request.get_json(silent=True) or {}
    drive_file_id = data.pop("drive_file_id", None)
    target_vat = data.pop("target_company_vat", None)
    cfg = comp.resolve_by_vat(target_vat)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(data.get("invoice_ref", "noref")))[:60]
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    json_path = QUEUE_DIR / f"{ts}-{safe}.json"
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    pdf_path = None
    download_error = None
    if drive_file_id:
        try:
            ext = ".pdf"
            mime = (data.get("source_mime_type") or "").lower()
            if "jpeg" in mime or "jpg" in mime:
                ext = ".jpg"
            elif "png" in mime:
                ext = ".png"
            elif "heif" in mime or "heic" in mime:
                ext = ".heic"
            pdf_path = QUEUE_DIR / f"{ts}-{safe}{ext}"
            drive_ops.download_to(drive_file_id, pdf_path)
            log.info(f"downloaded {drive_file_id} -> {pdf_path} ({pdf_path.stat().st_size} bytes)")
        except Exception as e:
            download_error = f"drive download failed: {e}"
            log.exception(download_error)
            pdf_path = None

    cmd = [PYTHON, PROCESS_SCRIPT, "--json", str(json_path), "--company-id", str(cfg["odoo_company_id"])]
    if pdf_path:
        cmd += ["--pdf", str(pdf_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=SUBPROCESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        log.error("process_invoice.py timed out")
        return jsonify({"ok": False, "error": "timeout", "destination": "revision"}), 504

    out = result.stdout
    rc = result.returncode

    parsed = {}
    for line in out.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            if k in ("INVOICE_ID", "AMOUNT_TOTAL", "STATE", "DUPLICATE", "VALIDATION_ERRORS", "ERROR", "COMPANY_ID"):
                parsed[k] = v

    response = {
        "rc": rc,
        "company": cfg["name"],
        "company_id": cfg["odoo_company_id"],
        "parsed": parsed,
        "stdout_tail": out[-400:],
        "stderr_tail": result.stderr[-400:],
    }
    if download_error:
        response["download_error"] = download_error

    if rc in (0, 20):
        response["ok"] = True
        if rc == 20:
            response["duplicate"] = True
        response["destination"] = "contabilizados"
        if "INVOICE_ID" in parsed:
            try:
                response["invoice_id"] = int(parsed["INVOICE_ID"])
            except ValueError:
                pass
        if drive_file_id:
            try:
                drive_ops.move_file(drive_file_id, cfg["contabilizado_folder"])
                response["drive_moved"] = True
            except Exception as e:
                log.exception("drive move failed")
                response["drive_move_error"] = str(e)
        return jsonify(response), 200

    if rc == 10:
        response["ok"] = False
        response["destination"] = "revision"
        response["reason"] = parsed.get("VALIDATION_ERRORS", "validation failed")
        if drive_file_id:
            try:
                drive_ops.move_file(drive_file_id, cfg["revision_folder"])
                response["drive_moved"] = True
            except Exception as e:
                log.exception("drive move failed")
                response["drive_move_error"] = str(e)
        return jsonify(response), 200

    response["ok"] = False
    response["destination"] = "revision"
    response["error"] = parsed.get("ERROR", f"rc={rc}")
    if drive_file_id:
        try:
            drive_ops.move_file(drive_file_id, cfg["revision_folder"])
            response["drive_moved"] = True
        except Exception as e:
            log.exception("drive move failed")
            response["drive_move_error"] = str(e)
    return jsonify(response), 500


@app.route("/automation/summary", methods=["GET"])
def summary():
    auth_err = _check_auth()
    if auth_err:
        return auth_err

    requested_date = request.args.get("date") or date.today().isoformat()
    cmd = [PYTHON, SUMMARY_SCRIPT, "--date", requested_date]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "timeout"}), 504
    if result.returncode != 0:
        return jsonify({"ok": False, "error": result.stderr[-400:], "rc": result.returncode}), 500
    try:
        return jsonify(json.loads(result.stdout)), 200
    except json.JSONDecodeError:
        return jsonify({"ok": False, "error": "invalid summary JSON", "stdout_tail": result.stdout[-400:]}), 500


if __name__ == "__main__":
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    app.run(host="127.0.0.1", port=8080)
