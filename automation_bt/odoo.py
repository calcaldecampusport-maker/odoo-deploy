"""Acceso a Odoo 18 (wiems_v18_prod) por XML-RPC para el pipeline.

Reutiliza el cliente `OdooRPC` del backend web (app/services/odoo_rpc.py) sin
importar el paquete `app` entero: se carga el módulo por ruta con importlib para
mantener el pipeline desacoplado del Flask. Lee las credenciales del .env del
backend (mismas que usa la web).
"""
import importlib.util
import os

# Ruta del backend (contiene .env + app/services/odoo_rpc.py). Override por env.
BACKEND_DIR = os.getenv("WIEMSPRO_BACKEND", "/opt/carajfam-contab/backend")


def _load_env():
    """Carga el .env del backend sin depender de python-dotenv (así el pipeline
    corre con cualquier venv, p.ej. el de automation con las libs de Google)."""
    envf = os.path.join(BACKEND_DIR, ".env")
    try:
        with open(envf, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


def _load_odoo_rpc_module():
    path = os.path.join(BACKEND_DIR, "app", "services", "odoo_rpc.py")
    spec = importlib.util.spec_from_file_location("wiemspro_odoo_rpc", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _companies_mod():
    """companies.py del propio pipeline (define la BD Odoo de esta contabilidad)."""
    try:
        import companies
        return companies
    except Exception:  # noqa: BLE001 - sin companies.py se usa el .env
        return None

_rpc_singleton = None


def get_rpc():
    """Cliente XML-RPC cacheado contra wiems_v18_prod."""
    global _rpc_singleton
    if _rpc_singleton is None:
        _load_env()
        mod = _load_odoo_rpc_module()
        _rpc_singleton = mod.OdooRPC(
            url=os.getenv("ODOO_RPC_URL", ""),
            # la BD la manda el companies.py de ESTE pipeline: un mismo backend sirve a
            # varias contabilidades (cararjfam / round_facturacion), asi que el .env solo
            # vale de fallback. Sin esto, el pipeline de una empresa escribia en la BD de otra.
            db=(getattr(_companies_mod(), "DB_NAME", None)
                or os.getenv("ODOO_RPC_DB", "") or "wiems_v18_prod"),
            user=os.getenv("ODOO_RPC_USER", ""),
            password=os.getenv("ODOO_RPC_PASSWORD", ""),
            timeout=float(os.getenv("ODOO_RPC_TIMEOUT", "60")),
        )
    return _rpc_singleton
