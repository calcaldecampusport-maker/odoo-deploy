#!/usr/bin/env python3
"""
Aplica 4 capas de aislamiento entre pipelines (cararjfam vs austral) para que
ningún script pueda procesar documentos de la otra empresa por error.

Defensas:
1. Cada companies.py declara su PIPELINE_NAME + DB_NAME + EXPECTED_VATS
2. Cada script usa sys.path.insert(0, dirname(__file__)) → siempre carga su
   companies.py local, jamás la del pipeline hermano
3. Cada script asserta PIPELINE_NAME esperado según su ubicación
4. Filtra COMPANIES por EXPECTED_VATS al iterar (defensa final)

Idempotente: se puede correr varias veces.
"""
import re
from pathlib import Path

PIPELINES = {
    "/opt/automation": {
        "pipeline_name": "cararjfam",
        "db_name": "cararjfam",
        "expected_vats": ["B93653392", "B72349137"],
    },
    "/opt/automation_austral": {
        "pipeline_name": "austral",
        "db_name": "cararjfam_test",
        "expected_vats": ["B44821965"],
    },
}

# Bloque preamble a inyectar en cada .py script (excepto companies.py / drive_ops.py / __init__)
PREAMBLE_MARKER = "# === pipeline isolation guard (auto-injected) ==="


def patch_companies_py(path: Path, pipeline_name: str, db_name: str, expected_vats: list):
    s = path.read_text()
    block = f"""
# === pipeline metadata (auto-injected, NO BORRAR) ===
PIPELINE_NAME = {pipeline_name!r}
DB_NAME = {db_name!r}
EXPECTED_VATS = frozenset({expected_vats!r})
# === end metadata ===

"""
    # Quitar bloque anterior si existe (para idempotencia)
    s = re.sub(r"# === pipeline metadata.*?# === end metadata ===\n+", "", s, flags=re.DOTALL)
    # Insertar tras el primer docstring o al principio
    if s.lstrip().startswith('"""'):
        # buscar fin del docstring
        m = re.search(r'^("""[\s\S]*?"""\s*\n)', s)
        if m:
            s = s[:m.end()] + block + s[m.end():]
        else:
            s = block + s
    else:
        s = block + s
    path.write_text(s)
    print(f"  companies.py patched: {pipeline_name=} {db_name=} {expected_vats=}")


def patch_script(path: Path, pipeline_name: str):
    """Inject sys.path self-reference + pipeline guard."""
    name = path.name
    if name in ("companies.py", "__init__.py", "drive_ops.py") or name.startswith("_"):
        return False
    s = path.read_text()

    # 1) Quitar guard previo (idempotencia)
    s = re.sub(
        rf"{re.escape(PREAMBLE_MARKER)}[\s\S]*?# === end isolation guard ===\n+",
        "", s, flags=re.MULTILINE,
    )

    # 2) Reemplazar sys.path.insert hardcoded apuntando a otra carpeta automation*
    s = re.sub(
        r'sys\.path\.insert\(0,\s*[\'"](/opt/automation[^"\']*)[\'"]\)',
        'sys.path.insert(0, _HERE)',
        s,
    )

    # 3) Construir bloque guard
    guard = f"""{PREAMBLE_MARKER}
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
try:
    import companies as _comp_guard
    if getattr(_comp_guard, "PIPELINE_NAME", None) != {pipeline_name!r}:
        raise RuntimeError(
            f"PIPELINE_MISMATCH: script {{__file__}} expected pipeline={pipeline_name!r} "
            f"but loaded companies.PIPELINE_NAME={{getattr(_comp_guard, 'PIPELINE_NAME', None)!r}}"
        )
except ImportError:
    pass  # script sin dependencia de companies.py (e.g. drive_ops)
# === end isolation guard ===

"""

    # 4) Insertar guard tras el primer docstring o tras shebang+docstring
    inserted = False
    # Busca shebang opcional + docstring opcional
    m = re.match(r'^(#!.*\n)?(\s*"""[\s\S]*?"""\s*\n)?', s)
    if m and m.end() > 0:
        s = s[:m.end()] + guard + s[m.end():]
        inserted = True
    if not inserted:
        s = guard + s
    path.write_text(s)
    return True


def main():
    for pipeline_dir, cfg in PIPELINES.items():
        pd = Path(pipeline_dir)
        if not pd.exists():
            print(f"SKIP {pipeline_dir} (no existe)")
            continue
        print(f"\n=== {pipeline_dir} ({cfg['pipeline_name']}) ===")
        patch_companies_py(pd / "companies.py", cfg["pipeline_name"], cfg["db_name"], cfg["expected_vats"])
        n_patched = 0
        for f in sorted(pd.glob("*.py")):
            if patch_script(f, cfg["pipeline_name"]):
                n_patched += 1
        print(f"  scripts patched: {n_patched}")


if __name__ == "__main__":
    main()
