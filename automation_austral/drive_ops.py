"""Google Drive operations using the service account at /etc/automation_sa.json."""
import io
import logging
from pathlib import Path

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

SA_PATH = "/etc/automation_sa.json"
SCOPES = ["https://www.googleapis.com/auth/drive"]

log = logging.getLogger("drive_ops")


def _service():
    creds = service_account.Credentials.from_service_account_file(SA_PATH, scopes=SCOPES)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def list_pdfs_in_folder(folder_id: str, svc=None):
    svc = svc or _service()
    q = f"'{folder_id}' in parents and trashed=false and mimeType='application/pdf'"
    files = []
    page_token = None
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


def list_jsons_in_folder(folder_id: str, svc=None):
    svc = svc or _service()
    q = (
        f"'{folder_id}' in parents and trashed=false "
        f"and mimeType!='application/vnd.google-apps.folder' "
        f"and (mimeType='application/json' or name contains '.json')"
    )
    return svc.files().list(
        q=q, fields="files(id,name,mimeType,size,modifiedTime)",
        pageSize=200, supportsAllDrives=True, includeItemsFromAllDrives=True,
    ).execute().get("files", [])


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


def get_metadata(file_id: str, svc=None):
    svc = svc or _service()
    return svc.files().get(
        fileId=file_id, fields="id,name,mimeType,parents,size,modifiedTime",
        supportsAllDrives=True,
    ).execute()


def move_file(file_id: str, dest_folder: str, svc=None):
    svc = svc or _service()
    f = svc.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
    prev_parents = ",".join(f.get("parents", []))
    return svc.files().update(
        fileId=file_id, addParents=dest_folder, removeParents=prev_parents,
        fields="id,parents", supportsAllDrives=True,
    ).execute()


def ensure_processed_folder(parent_folder_id: str, name: str = "Procesados", svc=None) -> str:
    """Find or create a Procesados subfolder inside parent_folder_id. Returns its id."""
    svc = svc or _service()
    q = (
        f"'{parent_folder_id}' in parents and trashed=false "
        f"and mimeType='application/vnd.google-apps.folder' and name='{name}'"
    )
    existing = svc.files().list(q=q, fields="files(id,name)", supportsAllDrives=True).execute().get("files", [])
    if existing:
        return existing[0]["id"]
    created = svc.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_folder_id]},
        fields="id", supportsAllDrives=True,
    ).execute()
    return created["id"]
