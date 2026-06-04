#!/usr/bin/env python3
"""
Drive-side helper for the learning pipeline.

Runs in the automation venv (has google-api-python-client).
Downloads any aprendizajes.csv (or .xlsx, or Google Sheet) found in each
company's Pendientes folder to /tmp/learning/<company_vat>/<filename>
and moves the source file to a 'Aprendizajes_aplicados' subfolder so the
Odoo-side script can pick it up without needing Google libs.

Pairs with `learning.py --mode active --staging /tmp/learning`.
"""
# === pipeline isolation guard (auto-injected) ===
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
try:
    import companies as _comp_guard
    if getattr(_comp_guard, "PIPELINE_NAME", None) != 'austral':
        raise RuntimeError(
            f"PIPELINE_MISMATCH: script {__file__} expected pipeline='austral' "
            f"but loaded companies.PIPELINE_NAME={getattr(_comp_guard, 'PIPELINE_NAME', None)!r}"
        )
except ImportError:
    pass  # script sin dependencia de companies.py (e.g. drive_ops)
# === end isolation guard ===

import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, _HERE)
import drive_ops  # noqa: E402
import companies as comp  # noqa: E402

STAGING = Path("/tmp/learning")
log = logging.getLogger("learning_drive")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def run():
    svc = drive_ops._service()
    out = []
    STAGING.mkdir(parents=True, exist_ok=True)

    for cfg in comp.COMPANIES:
        pending = cfg.get("pending_folder")
        if not pending:
            continue
        q = f"'{pending}' in parents and trashed=false and name contains 'aprendizaje'"
        files = svc.files().list(
            q=q, fields="files(id,name,mimeType)", pageSize=20,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute().get("files", [])
        if not files:
            log.info(f"[{cfg['name']}] no aprendizajes file in Pendientes")
            continue

        applied_folder = drive_ops.ensure_processed_folder(pending, name="Aprendizajes_aplicados", svc=svc)
        company_dir = STAGING / cfg["vat"]
        company_dir.mkdir(parents=True, exist_ok=True)

        for f in files:
            fid, fname = f["id"], f["name"]
            mime = (f.get("mimeType") or "").lower()
            try:
                if mime.startswith("application/vnd.google-apps.spreadsheet"):
                    raw = svc.files().export(fileId=fid, mimeType="text/csv").execute()
                    if not isinstance(raw, bytes):
                        raw = raw.encode()
                    target = company_dir / (Path(fname).stem + ".csv")
                else:
                    raw = svc.files().get_media(fileId=fid, supportsAllDrives=True).execute()
                    target = company_dir / fname
                target.write_bytes(raw)
                svc.files().update(
                    fileId=fid, addParents=applied_folder, removeParents=pending,
                    fields="id,parents", supportsAllDrives=True,
                ).execute()
                out.append({"company": cfg["name"], "file": fname, "staged": str(target)})
                log.info(f"[{cfg['name']}] staged {fname} -> {target}")
            except Exception as e:
                log.exception(f"[{cfg['name']}] failed: {e}")
                out.append({"company": cfg["name"], "file": fname, "error": str(e)})

    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    run()
