"""Operaciones Google Drive con el service account de /etc/automation_sa.json.
Vendorizado para el pipeline wiemspro (mismo SA que el resto de pipelines)."""
import io
import logging
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SA_PATH = "/etc/automation_sa.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]

log = logging.getLogger("drive_ops")

# Tipos MIME de documentos de factura/gasto (fase 1). Los bancarios se tratan aparte.
INVOICE_MIMES = ("application/pdf", "image/jpeg", "image/jpg", "image/png", "image/heic", "image/heif")


def _service():
    creds = service_account.Credentials.from_service_account_file(SA_PATH, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_by_mimes(folder_id: str, mimes=INVOICE_MIMES, svc=None):
    svc = svc or _service()
    mime_q = " or ".join(f"mimeType='{m}'" for m in mimes)
    q = f"'{folder_id}' in parents and trashed=false and ({mime_q})"
    files, page_token = [], None
    while True:
        resp = svc.files().list(
            q=q, fields="nextPageToken, files(id,name,mimeType,size,modifiedTime)",
            pageSize=100, pageToken=page_token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def download_to(file_id: str, dest_path: Path, svc=None) -> Path:
    svc = svc or _service()
    request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request, chunksize=1024 * 1024)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    dest_path.write_bytes(buf.getvalue())
    return dest_path


def move_file(file_id: str, dest_folder: str, svc=None):
    svc = svc or _service()
    f = svc.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    prev_parents = ",".join(f.get("parents", []))
    return svc.files().update(
        fileId=file_id, addParents=dest_folder, removeParents=prev_parents,
        fields="id,parents", supportsAllDrives=True,
    ).execute()


def get_metadata(file_id: str, svc=None):
    svc = svc or _service()
    return svc.files().get(
        fileId=file_id, fields="id,name,mimeType,parents,size,modifiedTime",
        supportsAllDrives=True,
    ).execute()
