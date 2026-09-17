"""Acceso a Odoo 18 (wiems_v18_prod, company 12 AUSTRAL) por XML-RPC.

Reutiliza el cliente `OdooRPC` del backend web (app/services/odoo_rpc.py) sin
importar el paquete `app` entero: se carga el módulo por ruta con importlib para
mantener el pipeline desacoplado del Flask. Lee las credenciales del .env del
backend (mismas que usa la web).
"""
import importlib.util
import os

# Ruta del backend (contiene .env + app/services/odoo_rpc.py). Override por env.
BACKEND_DIR = os.getenv("AUSTRAL_BACKEND", "/opt/austral-contab/backend")


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


_rpc_singleton = None


def get_rpc():
    """Cliente XML-RPC cacheado contra wiems_v18_prod."""
    global _rpc_singleton
    if _rpc_singleton is None:
        _load_env()
        mod = _load_odoo_rpc_module()
        _rpc_singleton = mod.OdooRPC(
            url=os.getenv("ODOO_RPC_URL", ""),
            db=os.getenv("ODOO_RPC_DB", "") or "wiems_v18_prod",
            user=os.getenv("ODOO_RPC_USER", ""),
            password=os.getenv("ODOO_RPC_PASSWORD", ""),
            timeout=float(os.getenv("ODOO_RPC_TIMEOUT", "60")),
        )
    return _rpc_singleton
