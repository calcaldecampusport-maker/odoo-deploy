"""Per-company configuration: maps a company VAT (or its Cola_VPS folder) to
Odoo company id and the right destination Drive folders.

The poller iterates COMPANIES_BY_QUEUE; the server resolves COMPANIES_BY_VAT
when processing a payload.
"""


# === pipeline metadata (auto-injected, NO BORRAR) ===
PIPELINE_NAME = 'cararjfam'
DB_NAME = 'cararjfam'
EXPECTED_VATS = frozenset(['B93653392', 'B72349137'])
# === end metadata ===

COMPANIES = [
    {
        "name": "CARARJFAM2019,SL",
        "vat": "B93653392",
        "odoo_company_id": 1,
        "pending_folder": "1RZjKO1GqJuPURl6WTsl2R9egwm7cyYFQ",
        "queue_folder": "1dIQ0IKGGk-3oJc9129pmA5IDepVzp71-",
        "processed_folder": "1Ua07cYk8XL1GVLuZWuV_ShdHr57AtbyW",
        "contabilizado_folder": "1JwE4yblvap2qx2JtTJW6YRgpyXEsGkMu",
        "revision_folder": "11VDbGsheAp4np155afA2TVQYaSwuD380",
        "rechazadas_folder": "1vJwd3LpShitDb5ERFt3oMhbo0fDjYkVm",
        "informes_folder": "1raE4-0_q4QP8dELHy4NY5QxPiIecE2UU",
    },
    {
        "name": "Best Training Rincon de la Victoria, S.L.",
        "vat": "B72349137",
        "odoo_company_id": 2,
        "pending_folder": "1d12YefAiP4RmDqmNQ1xTGAwrfxlyobOS",
        "queue_folder": "13vIwkLLrZ8mTYn0tG_bp-tDshpOuepOE",
        "processed_folder": None,
        "contabilizado_folder": "1gVd-6rOfCVyWkyCIUUtrcUBEg7DmWu7N",
        "revision_folder": "1Xqs_Cf_F2xss2O_GfQ8nc2xEdBKV7eGD",
        "rechazadas_folder": "1_OzPVOWJqmgauvKQbjMucPGcEkLqdG4v",
        "informes_folder": "1RBMYkC74cdYCIyeDD116msNjlUvIMctm",
    },
]

DEFAULT_VAT = "B93653392"

COMPANIES_BY_VAT = {c["vat"]: c for c in COMPANIES}
COMPANIES_BY_QUEUE = {c["queue_folder"]: c for c in COMPANIES}


def resolve_by_vat(vat: str) -> dict:
    if not vat:
        return COMPANIES_BY_VAT[DEFAULT_VAT]
    cleaned = vat.replace(" ", "").upper().lstrip("E").lstrip("S") if vat.upper().startswith("ES") else vat
    return COMPANIES_BY_VAT.get(cleaned) or COMPANIES_BY_VAT.get(vat) or COMPANIES_BY_VAT[DEFAULT_VAT]
