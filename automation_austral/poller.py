#!/usr/bin/env python3
"""
Multi-company poller.

For each company in companies.COMPANIES:
  - list JSON files in its queue_folder
  - POST each to /automation/invoice with target_company_vat
  - move processed JSON to processed_folder (auto-create if missing)
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

import requests

sys.path.insert(0, _HERE)
import drive_ops  # noqa: E402
import companies as comp  # noqa: E402

ENDPOINT = "http://127.0.0.1:8080/automation/invoice"
ENV_FILE = "/etc/automation.env"
TIMEOUT_PER_INVOICE = 150

log = logging.getLogger("poller")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _load_secret() -> str:
    with open(ENV_FILE) as f:
        for line in f:
            if line.startswith("AUTOMATION_SECRET="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError(f"AUTOMATION_SECRET not found in {ENV_FILE}")


BINARY_MAGIC_BYTES = (b"%PDF", b"\xff\xd8\xff", b"\x89PNG", b"\x00\x00\x00\x18ftyp", b"\x00\x00\x00 ftyp")


def _is_binary_content(raw: bytes) -> bool:
    if not isinstance(raw, bytes) or not raw:
        return False
    if any(raw.startswith(m) for m in BINARY_MAGIC_BYTES):
        return True
    sample = raw[:512]
    nontext = sum(1 for b in sample if b < 9 or (13 < b < 32 and b not in (10,)))
    return nontext > len(sample) * 0.10


def process_company(svc, secret: str, cfg: dict) -> tuple[int, int]:
    queue = cfg["queue_folder"]
    processed = cfg.get("processed_folder")
    if not processed:
        processed = drive_ops.ensure_processed_folder(queue, name="Procesados", svc=svc)
        cfg["processed_folder"] = processed
        log.info(f"[{cfg['name']}] auto-created Procesados folder id={processed}")
    invalidos = drive_ops.ensure_processed_folder(queue, name="JSONs_Invalidos", svc=svc)

    items = drive_ops.list_jsons_in_folder(queue, svc=svc)
    if not items:
        return 0, 0

    log.info(f"[{cfg['name']}] queue has {len(items)} item(s)")
    ok = 0
    fail = 0
    for f in items:
        fid, fname = f["id"], f["name"]
        log.info(f"[{cfg['name']}] processing {fname}")
        try:
            raw = svc.files().get_media(fileId=fid, supportsAllDrives=True).execute()
            if isinstance(raw, bytes) and _is_binary_content(raw):
                log.error(f"[{cfg['name']}] {fname} is binary (PDF/image) not JSON — quarantining")
                try:
                    svc.files().update(fileId=fid, addParents=invalidos, removeParents=queue,
                                       fields="id,parents", supportsAllDrives=True).execute()
                except Exception:
                    log.exception("could not quarantine binary file")
                fail += 1
                continue
            if isinstance(raw, bytes):
                text = None
                for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
                    try:
                        text = raw.decode(enc)
                        break
                    except UnicodeDecodeError:
                        continue
                if text is None:
                    text = raw.decode("utf-8", errors="replace")
            else:
                text = str(raw)
            text = "".join(c for c in text if c >= " " or c in "\n\r\t")
            text = text.replace("\ufffd", "?")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                decoder = json.JSONDecoder(strict=False)
                payload, _ = decoder.raw_decode(text)
        except Exception as e:
            log.error(f"[{cfg['name']}] could not parse {fname}: {e} — quarantining")
            try:
                svc.files().update(fileId=fid, addParents=invalidos, removeParents=queue,
                                   fields="id,parents", supportsAllDrives=True).execute()
            except Exception:
                log.exception("could not quarantine malformed file")
            fail += 1
            continue

        if "drive_file_id" not in payload:
            log.warning(f"[{cfg['name']}] {fname} missing drive_file_id, skipping")
            fail += 1
            continue

        payload["target_company_vat"] = cfg["vat"]

        try:
            resp = requests.post(
                ENDPOINT,
                json=payload,
                headers={"X-Automation-Secret": secret},
                timeout=TIMEOUT_PER_INVOICE,
            )
        except requests.RequestException as e:
            log.exception(f"[{cfg['name']}] endpoint call failed for {fname}: {e}")
            fail += 1
            continue

        try:
            body = resp.json()
        except ValueError:
            body = {"raw": resp.text[:500]}

        success = resp.status_code == 200 and (body.get("ok") or body.get("destination") == "revision")
        if success:
            log.info(
                f"[{cfg['name']}] {fname} -> ok={body.get('ok')} dest={body.get('destination')} "
                f"invoice_id={body.get('invoice_id')}"
            )
            try:
                svc.files().update(
                    fileId=fid, addParents=processed, removeParents=queue,
                    fields="id,parents", supportsAllDrives=True,
                ).execute()
                log.info(f"[{cfg['name']}] moved {fname} -> Procesados")
            except Exception:
                log.exception(f"[{cfg['name']}] failed to move {fname} to Procesados")
            ok += 1
        else:
            log.error(f"[{cfg['name']}] {fname} -> http {resp.status_code} body={body}")
            fail += 1

    return ok, fail


def main():
    secret = _load_secret()
    svc = drive_ops._service()
    total_ok = 0
    total_fail = 0
    for cfg in comp.COMPANIES:
        ok, fail = process_company(svc, secret, cfg)
        total_ok += ok
        total_fail += fail

    if total_ok == 0 and total_fail == 0:
        log.info("all queues empty")
    else:
        log.info(f"done across all companies: ok={total_ok} fail={total_fail}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
