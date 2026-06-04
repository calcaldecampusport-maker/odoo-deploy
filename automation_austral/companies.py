"""Per-company configuration para AUSTRAL (pipeline aislado, BD cararjfam_test).
NO contiene CARARJFAM ni BT — ese pipeline vive en /opt/automation_austral/ aparte.
"""


# === pipeline metadata (auto-injected, NO BORRAR) ===
PIPELINE_NAME = 'austral'
DB_NAME = 'cararjfam_test'
EXPECTED_VATS = frozenset(['B44821965'])
# === end metadata ===

COMPANIES = [
    {
        "name": "AUSTRAL",
        "vat": "B44821965",
        "odoo_company_id": 4,
        "pending_folder": "1SNDTko-SgeYNjyJ-_635ObprBDVWm-Jd",   # Mi Odoo AUSTRAL (raiz)
        "queue_folder": "15kI9YEpo-Z1OngKAud1X2ZPnQgH4jI85",      # Cola_VPS
        "processed_folder": None,
        "contabilizado_folder": "1g5bpK1VBmaVtt5CN9lOZBYTcXUIEigvJ",
        "revision_folder": "1KKFSc0-ph8chNjKkj58K8tK4bVpru2IH",
        "aprendizajes_folder": "1DozZoV0grBbvjhEhCU1fMvujNvSWYoFN",
        "dudas_file_id": "1cloiyMvqTHbnGELXwIFOVsKSKkIlsMP_",
        "informes_folder": "166MYzuWjLNpb9CrvLjzL19EbZnLvghMc",   # xlsx generados por scripts internos (no entran al extractor)
        "rechazadas_folder": "1Y6WRDOti_2xvKS3uCArBJGvfd0D27_rL",
    },
]

DEFAULT_VAT = "B44821965"

COMPANIES_BY_VAT = {c["vat"]: c for c in COMPANIES}
COMPANIES_BY_QUEUE = {c["queue_folder"]: c for c in COMPANIES}


def resolve_by_vat(vat: str) -> dict:
    if not vat:
        return COMPANIES_BY_VAT[DEFAULT_VAT]
    cleaned = vat.replace(" ", "").upper().lstrip("E").lstrip("S") if vat.upper().startswith("ES") else vat
    return COMPANIES_BY_VAT.get(cleaned) or COMPANIES_BY_VAT.get(vat) or COMPANIES_BY_VAT[DEFAULT_VAT]
