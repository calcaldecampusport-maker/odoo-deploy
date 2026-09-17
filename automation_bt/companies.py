"""Config del pipeline WEB de BEST TRAINING RINCÓN DE LA VICTORIA, S.L.

Odoo 17 **Community** local (BD `round_facturacion`, company 3) accedido SOLO por
XML-RPC con el usuario `apiweb` (credenciales en el .env de /opt/carajfam-contab/backend).
Plan español (PGC) de 6 dígitos: gasto 6xx, proveedor 400/410, IVA soportado 472.

OJO: esta BD la comparte con otras companies (Pruebas Noofit y la legacy). El guard de
EXPECTED_VATS y el company_id de cada llamada evitan cruzar datos.

Este pipeline sirve al FLUJO WEB (webproc.py). El histórico /opt/automation_bt_round,
con su cron nocturno desde Drive, sigue funcionando aparte y no se toca.
"""

DB_NAME = "round_facturacion"
EXPECTED_VATS = frozenset(["B72349137"])

COMPANIES = [
    {
        "name": "BEST TRAINING RINCON DE LA VICTORIA SL.",
        "vat": "B72349137",
        "odoo_company_id": 3,
        "chart": "pgc",
        # el flujo web no usa carpetas de Drive (el documento llega por la web)
        "pending_folder": "",
        "contabilizado_folder": "",
        "revision_folder": "",
        "rechazadas_folder": "",
    },
]

BY_COMPANY_ID = {c["odoo_company_id"]: c for c in COMPANIES}
BY_PENDING = {c["pending_folder"]: c for c in COMPANIES if c["pending_folder"]}

# Siglas con las que el extractor identifica al RECEPTOR del documento.
SIGLAS = {"BT": 3, "BESTTRAINING": 3, "BEST TRAINING": 3}
SIGLA_BY_CID = {3: "BT"}


def by_sigla(s) -> dict | None:
    """Empresa por sigla del extractor (BT). None si no cuadra."""
    return BY_COMPANY_ID.get(SIGLAS.get(str(s or "").strip().upper()))


def sigla_of(company: dict) -> str:
    return SIGLA_BY_CID.get(company["odoo_company_id"], "?")


# Cuenta por defecto cuando el proveedor NO tiene histórico. 600000 (compras de
# mercaderías) era una mala elección: la mayoría de lo que entra son SERVICIOS (hoteles,
# hosting, telefonía, suministros) y quedaba todo como mercancía. 629 «otros servicios»
# es el cajón genérico correcto; el revisor lo afina y el pipeline lo aprende.
DEFAULT_EXPENSE_CODE = "629000"


def by_company_id(cid: int) -> dict | None:
    return BY_COMPANY_ID.get(int(cid))
