"""Config del pipeline de contabilización de AUSTRAL (Odoo 18 Enterprise).

UNA sola empresa: INTERNATIONAL AUSTRAL SPORT SA, company_id 12 de la BD
wiems_v18_prod, accedida SOLO por XML-RPC. Comparte base de datos con las empresas
de Wiemspro pero NUNCA se mezcla: el guard de abajo (DB_NAME + EXPECTED_VATS) aborta
si este pipeline se carga con la configuración de otro.

⚠ El plan de Austral usa códigos de cuenta de NUEVE dígitos (600000000, 472000021…),
no los de seis del plan PYMES de Wiemspro. Todos los códigos que el pipeline necesita
están en CUENTAS, y de ahí los toman los procesadores.
"""

DB_NAME = "wiems_v18_prod"
# VAT esperado en esta BD para este pipeline (guard anti-cruce con el de Wiemspro)
EXPECTED_VATS = frozenset(["A39100573"])
PIPELINE_NAME = "austral_e18"

COMPANIES = [
    {
        "name": "INTERNATIONAL AUSTRAL SPORT SA",
        "vat": "A39100573",
        "odoo_company_id": 12,
        "chart": "pgc",
        # carpetas de Drive: las mismas que venía usando Austral en Odoo 17
        "pending_folder": "15kI9YEpo-Z1OngKAud1X2ZPnQgH4jI85",       # Cola_VPS
        "contabilizado_folder": "1g5bpK1VBmaVtt5CN9lOZBYTcXUIEigvJ",  # Contabilizado odoo
        "revision_folder": "1KKFSc0-ph8chNjKkj58K8tK4bVpru2IH",       # revision
        "rechazadas_folder": "1Y6WRDOti_2xvKS3uCArBJGvfd0D27_rL",     # rechazadas
        "raiz_folder": "1SNDTko-SgeYNjyJ-_635ObprBDVWm-Jd",           # Mi Odoo AUSTRAL
        "tarjetas_folder": "1Fc2hsQi8my30i10sSVWuB385FeorQTjz",       # Tarjetas banco
    },
]

BY_COMPANY_ID = {c["odoo_company_id"]: c for c in COMPANIES}
BY_PENDING = {c["pending_folder"]: c for c in COMPANIES}

# Siglas con las que el extractor identifica al RECEPTOR del documento. Aquí solo hay
# una empresa, así que cualquiera de sus alias apunta a la 12.
SIGLAS = {"AUS": 12, "AUSTRAL": 12, "IAS": 12, "ASI": 12}
SIGLA_BY_CID = {12: "AUS"}

# Códigos de cuenta del plan de AUSTRAL que necesita el pipeline (9 dígitos)
CUENTAS = {
    "gasto_defecto": "600000000",      # COMPRAS (generica importacion)
    "proveedor": "400000000",          # PROVEEDORES (genérica)
    "cliente": "430000000",            # CLIENTES (genérica)
    "acreedor_servicios": "400000000",  # Austral no tiene grupo 41x: va al 400
    "iva_soportado_21": "472000021",
    "iva_repercutido_21": "477000021",
    "nomina_remuneraciones": "465000000",   # REMUNERACIONES PTES.PAGO
    "nomina_sueldos": "640000000",          # SUELDOS Y SALARIOS
    "nomina_ss_empresa": "642000000",       # S.SOCIAL A CARGO DE EMPRESA
    "nomina_irpf": "475100000",             # HACIENDA RETENCION ASALARIADOS
    "ss_acreedores": "476000000",           # S.SOCIAL ACREEDORES
    "otros_servicios": "629000000",         # OTROS GASTOS
    "publicidad": "627000002",              # PUBLICIDAD, REDES SOCIALES
}

# Cuenta de gasto por defecto cuando no hay histórico del proveedor (se avisa/revisa)
DEFAULT_EXPENSE_CODE = CUENTAS["gasto_defecto"]


def by_sigla(s) -> dict | None:
    """Empresa por sigla del extractor. None si no cuadra."""
    return BY_COMPANY_ID.get(SIGLAS.get(str(s or "").strip().upper()))


def sigla_of(company: dict) -> str:
    return SIGLA_BY_CID.get(company["odoo_company_id"], "?")


def by_company_id(cid: int) -> dict | None:
    return BY_COMPANY_ID.get(int(cid))


def resolve_by_vat(vat: str) -> dict:
    """Empresa por CIF; con una sola empresa siempre devuelve Austral."""
    return COMPANIES[0]


DEFAULT_VAT = "A39100573"
COMPANIES_BY_VAT = {c["vat"]: c for c in COMPANIES}
COMPANIES_BY_QUEUE = {c["pending_folder"]: c for c in COMPANIES}
