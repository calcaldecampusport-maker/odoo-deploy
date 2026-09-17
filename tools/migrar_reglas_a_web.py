#!/usr/bin/env python3
"""Lleva las reglas de banco de Odoo (learned.rule) a la web (tabla concil_regla).

POR QUE EXISTE
──────────────
CARARJFAM y Best Training guardan sus reglas de conciliacion en Odoo 17, en el addon
`learned_rules`. Wiemspro, Medical y Austral las guardan en la web, en `concil_regla`,
porque su Odoo es alojado y no admite addons propios.

La web de CARARJFAM/BT tiene el motor nuevo pero con CERO reglas: si algun dia se quiere
conciliar desde ella, o simplemente ver que reglas hay, no tiene nada. Este script rellena
ese hueco.

QUE NO HACE
───────────
NO cambia como se trabaja. Los crons del pipeline siguen conciliando de madrugada igual
que hasta ahora; la conciliacion de la web es a peticion (no hay pase automatico ni cron),
asi que cargar reglas no dispara nada por si solo. Verificado el 17/09/2026.

SOLO migra las reglas de tipo `bank` (123 de las 313). Las de `invoice` no tienen destino
claro en `concil_regla`, que no tiene columna de tipo.

USO
───
    python3 migrar_reglas_a_web.py                 # simulacro: dice que haria, no escribe
    python3 migrar_reglas_a_web.py --aplicar       # escribe, tras copia de seguridad
    python3 migrar_reglas_a_web.py --empresa bt    # solo una

Se ejecuta EN EL VPS y como root (lee Postgres via `sudo -u postgres psql`).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime

APP_DB = "/opt/carajfam-contab/backend/data/app.db"

# clave en la web -> (BD de Odoo, company_id de Odoo)
# Ojo: los company_id NO son globales. Aqui la 1 es CARARJFAM en `cararjfam`, y la 3 es
# Best Training en `round_facturacion`. Ver MAPA_EMPRESAS.md.
EMPRESAS = {
    "cararjfam": ("cararjfam", 1),
    "bt": ("round_facturacion", 3),
}

CREADO_POR = "migracion-learned-rule"


def psql(db: str, sql: str):
    """Ejecuta una consulta y devuelve la lista de dicts. Usa json_agg para no romperse
    con los separadores: los patrones llevan espacios, comas y a veces barras."""
    envuelto = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql}) t;"
    r = subprocess.run(
        ["sudo", "-u", "postgres", "psql", "-d", db, "-Atc", envuelto],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"psql fallo en {db}: {r.stderr.strip()}")
    return json.loads(r.stdout.strip() or "[]")


def reglas_de_odoo(db: str, company_id: int):
    """Las reglas de banco activas, con la cuenta y el tercero ya resueltos.

    `account_account.name` es jsonb en Odoo 17 (traducible), de ahi el ->>. El nombre del
    tercero no lo es, pero se protege igual por si cambia en una version futura.
    """
    sql = f"""
        SELECT lr.id,
               lr.pattern,
               lr.confidence,
               lr.times_applied,
               lr.active,
               lr.create_date,
               aa.code AS cuenta_code,
               coalesce(aa.name->>'en_US', aa.name->>'es_ES', aa.name::text) AS cuenta_name,
               rp.name AS partner_name
          FROM learned_rule lr
          LEFT JOIN account_account aa ON aa.id = lr.account_id
          LEFT JOIN res_partner     rp ON rp.id = lr.partner_id
         WHERE lr.rule_type = 'bank'
           AND lr.company_id = {company_id}
           AND lr.active IS TRUE
         ORDER BY lr.times_applied DESC NULLS LAST, lr.id
    """
    return psql(db, sql)


def empresa_id_de(con: sqlite3.Connection, clave: str) -> int:
    fila = con.execute("SELECT id FROM empresa WHERE clave = ?", (clave,)).fetchone()
    if not fila:
        raise RuntimeError(f"no existe la empresa '{clave}' en {APP_DB}")
    return fila[0]


def ya_existe(con: sqlite3.Connection, empresa_id: int, patron: str, cuenta: str) -> bool:
    """Idempotencia: el trio (empresa, patron, cuenta) identifica la regla. Permite
    relanzar el script sin duplicar."""
    q = ("SELECT 1 FROM concil_regla "
         "WHERE empresa_id = ? AND patron = ? AND coalesce(cuenta_code,'') = ?")
    return con.execute(q, (empresa_id, patron, cuenta or "")).fetchone() is not None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--aplicar", action="store_true",
                    help="escribe de verdad (por defecto solo simula)")
    ap.add_argument("--empresa", choices=sorted(EMPRESAS),
                    help="migrar solo esta empresa")
    args = ap.parse_args()

    claves = [args.empresa] if args.empresa else sorted(EMPRESAS)
    con = sqlite3.connect(APP_DB)

    if args.aplicar:
        copia = f"{APP_DB}.bak-migrar-reglas-{datetime.now():%Y%m%d-%H%M%S}"
        shutil.copy2(APP_DB, copia)
        print(f"copia de seguridad: {copia}\n")
    else:
        print("SIMULACRO — no se escribe nada. Usa --aplicar para hacerlo de verdad.\n")

    total_nuevas = total_repetidas = total_sin_cuenta = 0

    for clave in claves:
        db, company_id = EMPRESAS[clave]
        empresa_id = empresa_id_de(con, clave)
        reglas = reglas_de_odoo(db, company_id)
        print(f"── {clave}: {len(reglas)} reglas de banco en {db}/company {company_id} "
              f"→ empresa_id {empresa_id}")

        nuevas = repetidas = sin_cuenta = 0
        for r in reglas:
            patron = (r.get("pattern") or "").strip()
            cuenta = (r.get("cuenta_code") or "").strip()
            if not patron:
                continue
            if not cuenta:
                # sin cuenta destino la regla no sirve para conciliar: se avisa y se salta
                sin_cuenta += 1
                print(f"     ⚠ sin cuenta, se salta: {patron[:48]}")
                continue
            if ya_existe(con, empresa_id, patron, cuenta):
                repetidas += 1
                continue

            nuevas += 1
            if args.aplicar:
                con.execute(
                    "INSERT INTO concil_regla "
                    "(empresa_id, patron, cuenta_code, cuenta_name, partner_name, "
                    " confianza, veces, activa, creado_por, created_at, signo) "
                    "VALUES (?,?,?,?,?,?,?,1,?,?,NULL)",
                    (empresa_id, patron, cuenta, r.get("cuenta_name"),
                     r.get("partner_name"), r.get("confidence"),
                     r.get("times_applied") or 0, CREADO_POR, r.get("create_date")),
                )
            else:
                print(f"     + {patron[:44]:<44} → {cuenta}  "
                      f"conf={r.get('confidence')} veces={r.get('times_applied')}")

        print(f"   nuevas={nuevas}  ya estaban={repetidas}  sin cuenta={sin_cuenta}\n")
        total_nuevas += nuevas
        total_repetidas += repetidas
        total_sin_cuenta += sin_cuenta

    if args.aplicar:
        con.commit()
        print(f"ESCRITO: {total_nuevas} reglas nuevas "
              f"({total_repetidas} ya estaban, {total_sin_cuenta} sin cuenta)")
        print("\nOJO: el campo `signo` queda a NULL. learned.rule no lo tiene y deducirlo "
              "(cargo/abono) a partir de la cuenta seria adivinar. Se rellena a mano desde "
              "la web si hace falta; 13 de las reglas de Wiemspro tambien lo tienen nulo.")
    else:
        print(f"SIMULACRO: se insertarian {total_nuevas} reglas "
              f"({total_repetidas} ya estaban, {total_sin_cuenta} sin cuenta)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
