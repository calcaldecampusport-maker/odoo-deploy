#!/usr/bin/env python3
"""
Backup diario rolling 7-días → Google Drive.

Para cada noche calcula el día de la semana y sobrescribe el archivo
correspondiente vía Service Account `update()` (la SA no puede crear archivos
en Drive personal pero sí puede actualizar archivos pre-creados).

Empaqueta en un único tar.gz:
- db.dump            (pg_dump custom-format de cararjfam)
- filestore.tar.gz   (adjuntos de Odoo)
- custom-addons.tar.gz
- automation.tar.gz  (este pipeline, sin venv)
- configs.tar.gz     (/etc/odoo17.conf, /etc/nginx/sites-*, /etc/letsencrypt)
- secrets.tar.gz     (/etc/automation_sa.json, email_config.py)
- crontab_odoo.txt
- RECOVERY.md        (manual de desastre, incluido)

Cron: 04:00 diario (no choca con pipeline 23:23-23:40).
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

import json
import logging
import subprocess
import sys
import tarfile
import tempfile
from datetime import date
from pathlib import Path

sys.path.insert(0, _HERE)
from drive_ops import _service
from googleapiclient.http import MediaFileUpload

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("backup")

# Mapping día semana → file_id en Drive (carpeta "Mi Odoo CARARJFAM")
DAY_FILE_IDS = {
    0: "1mcCawkjFoQAp6R5qhoqyv22ePiaSZQdn",   # LUNES (Monday=0)
    1: "1WeQPnq8j2k_IFSkncWtA_SUZxGSRPf53",   # MARTES
    2: "1iKYytoY2vUYEN3SRtKo7dWS-a87xBVWK",   # MIERCOLES
    3: "1yDXVk0SxIcGllxF2WREE4WDStpUDNfk6",   # JUEVES
    4: "1YboEOzG33JGWzJ9-Mm-ROTP2T3z-z_kO",   # VIERNES
    5: "1h5hFUzuwUXFTVbsErt_oDjzzpLX6pLkh",   # SABADO
    6: "1EMYEHPQOuIIcaBAGK2Xm3Wp8aTAik7m_",   # DOMINGO
}
DAY_NAMES = ["LUNES", "MARTES", "MIERCOLES", "JUEVES", "VIERNES", "SABADO", "DOMINGO"]

DB_NAME = "cararjfam"
FILESTORE = "/opt/odoo17/.local/share/Odoo/filestore/cararjfam"
CUSTOM_ADDONS = "/opt/odoo17/custom-addons"
AUTOMATION_DIR = "/opt/automation"
RECOVERY_MD = "/opt/automation/RECOVERY.md"


def _run(cmd: list[str], cwd: str | None = None) -> str:
    """Run command, return stdout, raise on error."""
    log.info(f"  $ {' '.join(cmd)}")
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        log.error(f"  stderr: {res.stderr[-500:]}")
        raise RuntimeError(f"command failed rc={res.returncode}: {' '.join(cmd)}")
    return res.stdout


def _make_pgdump(out_path: Path):
    log.info(f"pg_dump {DB_NAME}")
    # Output a stdout via sudo y escribirlo nosotros (postgres no puede escribir
    # al tmpdir creado por odoo)
    with open(out_path, "wb") as f:
        res = subprocess.run(
            ["sudo", "-u", "postgres", "pg_dump", "-Fc", "-d", DB_NAME],
            stdout=f, stderr=subprocess.PIPE, check=False,
        )
    if res.returncode != 0:
        log.error(f"  stderr: {res.stderr.decode()[-500:]}")
        raise RuntimeError(f"pg_dump failed rc={res.returncode}")


def _make_tar(target: Path, source_dir: str, exclude: list[str] | None = None):
    log.info(f"tar {source_dir} → {target.name}")
    cmd = ["tar", "czf", str(target)]
    for pat in (exclude or []):
        cmd += ["--exclude", pat]
    cmd += ["-C", str(Path(source_dir).parent), Path(source_dir).name]
    _run(cmd)


def _dump_crontab(out_path: Path):
    log.info(f"dump crontab → {out_path.name}")
    res = subprocess.run(["crontab", "-u", "odoo", "-l"], capture_output=True, text=True, check=False)
    out_path.write_text(res.stdout if res.returncode == 0 else "# (no crontab found)\n")


def _build_secrets_tar(target: Path):
    """Tar limitado solo a archivos de secretos críticos."""
    log.info(f"secrets tar → {target.name}")
    files = [
        "/etc/automation_sa.json",
        "/opt/automation/email_config.py",
    ]
    with tarfile.open(target, "w:gz") as t:
        for f in files:
            p = Path(f)
            if p.exists():
                t.add(f, arcname=str(p.relative_to("/")))


def _build_configs_tar(target: Path):
    """Tar con configs editables del sistema."""
    log.info(f"configs tar → {target.name}")
    paths = [
        "/etc/odoo17.conf",
        "/etc/nginx/sites-available",
        "/etc/letsencrypt/live",
        "/etc/letsencrypt/renewal",
        "/etc/letsencrypt/options-ssl-nginx.conf",
        "/etc/letsencrypt/ssl-dhparams.pem",
    ]
    cmd = ["tar", "czf", str(target), "--ignore-failed-read"]
    for p in paths:
        if Path(p).exists():
            cmd += ["-C", "/", str(Path(p).relative_to("/"))]
    _run(cmd)


def build_full_backup(work_dir: Path) -> Path:
    """Construye el tar.gz consolidado, devuelve la ruta."""
    db_dump = work_dir / "db.dump"
    filestore_tar = work_dir / "filestore.tar.gz"
    addons_tar = work_dir / "custom-addons.tar.gz"
    automation_tar = work_dir / "automation.tar.gz"
    configs_tar = work_dir / "configs.tar.gz"
    secrets_tar = work_dir / "secrets.tar.gz"
    crontab_txt = work_dir / "crontab_odoo.txt"

    _make_pgdump(db_dump)
    _make_tar(filestore_tar, FILESTORE)
    _make_tar(addons_tar, CUSTOM_ADDONS)
    _make_tar(automation_tar, AUTOMATION_DIR, exclude=["venv", "__pycache__", "*.pyc", "*.tar.gz", "db.dump"])
    _build_configs_tar(configs_tar)
    _build_secrets_tar(secrets_tar)
    _dump_crontab(crontab_txt)

    if Path(RECOVERY_MD).exists():
        (work_dir / "RECOVERY.md").write_bytes(Path(RECOVERY_MD).read_bytes())

    # Empaqueta todo en un único tar.gz
    final = work_dir / "backup.tar.gz"
    members = [p for p in work_dir.iterdir() if p.is_file() and p.name != "backup.tar.gz"]
    with tarfile.open(final, "w:gz") as t:
        for m in members:
            t.add(m, arcname=m.name)
    log.info(f"final backup: {final} ({final.stat().st_size / 1e6:.1f} MB)")
    return final


def upload_to_drive(local_path: Path, file_id: str) -> dict:
    log.info(f"upload to Drive file_id={file_id}")
    svc = _service()
    media = MediaFileUpload(str(local_path), mimetype="application/gzip", resumable=True, chunksize=8 * 1024 * 1024)
    res = svc.files().update(
        fileId=file_id, media_body=media,
        fields="id,name,size,modifiedTime",
        supportsAllDrives=True,
    ).execute()
    return res


def main():
    today = date.today()
    weekday = today.weekday()  # Monday=0..Sunday=6
    file_id = DAY_FILE_IDS[weekday]
    day_name = DAY_NAMES[weekday]
    log.info(f"=== Backup {today} ({day_name}) → file_id={file_id} ===")

    with tempfile.TemporaryDirectory(prefix="backup_", dir="/var/tmp") as tmp:
        work = Path(tmp)
        try:
            final = build_full_backup(work)
            res = upload_to_drive(final, file_id)
            log.info(f"OK uploaded: name={res['name']} size={int(res['size'])/1e6:.1f}MB modified={res.get('modifiedTime')}")
            print(json.dumps({"status": "ok", "day": day_name, "size_bytes": int(res["size"]),
                              "drive_modified": res.get("modifiedTime")}, ensure_ascii=False))
        except Exception as e:
            log.exception("backup failed")
            print(json.dumps({"status": "error", "error": str(e)[:300]}, ensure_ascii=False))
            sys.exit(1)


if __name__ == "__main__":
    main()
