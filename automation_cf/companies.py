"""Config del pipeline WEB de CARARJFAM2019, S.L.

Odoo 17 **Community** local (BD `cararjfam`) accedido SOLO por XML-RPC con el usuario
`apiweb` (credenciales en el .env de /opt/carajfam-contab/backend). Empresa ÚNICA con
plan español (PGC) de 6 dígitos: gasto 6xx, proveedor 400/410, IVA soportado 472.

Este pipeline sirve al FLUJO WEB (webproc.py). El pipeline histórico /opt/automation,
con su cron nocturno desde Drive, sigue funcionando aparte y no se toca.
"""

DB_NAME = "cararjfam"
# VATs esperados en esta BD (guard anti-cruce con otros pipelines).
EXPECTED_VATS = frozenset(["B93653392"])

COMPANIES = [
    {
        "name": "CARARJFAM2019,SL",
        "vat": "B93653392",
        "odoo_company_id": 1,
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
SIGLAS = {"CF": 1, "CARARJFAM": 1, "CARAJFAM": 1, "CARARJFAM2019": 1}
SIGLA_BY_CID = {1: "CF"}


def by_sigla(s) -> dict | None:
    """Empresa por sigla del extractor (CF). None si no cuadra."""
    return BY_COMPANY_ID.get(SIGLAS.get(str(s or "").strip().upper()))


def sigla_of(company: dict) -> str:
    return SIGLA_BY_CID.get(company["odoo_company_id"], "?")


# Cuenta de gasto por defecto cuando no hay histórico del proveedor (se avisa/revisa).
# Cuenta por defecto cuando el proveedor NO tiene histórico. 600000 (compras de
# mercaderías) era una mala elección: la mayoría de lo que entra son SERVICIOS (hoteles,
# hosting, telefonía, suministros) y quedaba todo como mercancía. 629 «otros servicios»
# es el cajón genérico correcto; el revisor lo afina y el pipeline lo aprende.
DEFAULT_EXPENSE_CODE = "629000"


def by_company_id(cid: int) -> dict | None:
    return BY_COMPANY_ID.get(int(cid))
