# MAPA_EMPRESAS.md — Fuente de verdad: empresa ↔ Odoo ↔ pipeline ↔ web ↔ Drive

> **Para qué sirve este documento.** Cada empresa que contabilizamos vive
> simultáneamente en **4 capas** que TIENEN que apuntar a lo mismo. Si una se
> desalinea (p.ej. la web mira la BD equivocada, o un pipeline escribe en otra
> company), los documentos "desaparecen", se contabilizan dos veces, o se
> mezcla información entre empresas. Este es el único sitio donde se ve el
> cruce completo de un vistazo. **Manténlo actualizado en el mismo turno en que
> toques cualquiera de las 4 capas** (igual que RECOVERY.md).
>
> Ubicaciones (mantener las 3 sincronizadas):
> - Local: `C:/Users/pc/Documents/odoo-deploy/MAPA_EMPRESAS.md`
> - VPS:   `/opt/automation/MAPA_EMPRESAS.md`
> - GitHub: repo `odoo-deploy` (commit + push)

---

## Las 4 capas que SIEMPRE deben coincidir

Para una misma empresa (identificada por su **VAT**), estas cuatro cosas deben
referirse exactamente a la misma BD Odoo + company_id:

1. **Odoo** — `res_company` (BD + `company_id`) donde vive su contabilidad real.
2. **Pipeline / cron** — carpeta `/opt/automation*` con su `companies.py`
   (`DB_NAME` + `odoo_company_id` + `vat`) y sus líneas en el crontab del
   usuario `odoo`. Es quien CONTABILIZA los documentos.
3. **Web** (`austral.carajfam.com`) — fila en `data/app.db` tabla `empresa`
   (`odoo_db` + `odoo_company_id`) a la que se enganchan los usuarios. Es quien
   LEE/MUESTRA los datos.
4. **Drive** — carpeta `Cola_VPS` (queue_folder) donde se sueltan los PDFs de
   esa empresa, que el pipeline procesa.

Regla de oro: **VAT → (BD, company_id)** debe dar el MISMO resultado en las 4.

---

## Tabla maestra (estado a 2026-06-07)

| Empresa | VAT | BD Odoo | company_id | Pipeline (carpeta) | Cron (hora odoo) | Web `empresa.id` | Drive cola |
|---|---|---|---|---|---|---|---|
| **CARARJFAM2019, SL** | B93653392 | `cararjfam` | **1** | `/opt/automation` | 23:23–23:40 + `automation.service` :8080 | 2 | `1dIQ0IKGGk-3oJc9129pmA5IDepVzp71-` |
| **INTERNATIONAL AUSTRAL SPORT, SA** | A39100573 ⚠️ | `cararjfam_test` | **4** | `/opt/automation_austral` | 02:00–03:10 | 1 | `15kI9YEpo-Z1OngKAud1X2ZPnQgH4jI85` |
| **BEST TRAINING RINCÓN DE LA VICTORIA, SL** | B72349137 | `round_facturacion` | **3** | `/opt/automation_bt_round` | 00:23–00:40 | 3 | `13vIwkLLrZ8mTYn0tG_bp-tDshpOuepOE` |

⚠️ **AUSTRAL — discrepancia de VAT pendiente:** el `companies.py` del pipeline
declara `vat='B44821965'` (y `EXPECTED_VATS`/`DEFAULT_VAT` igual), pero la
company 4 real en Odoo tiene VAT **A39100573**. Funciona por *fallback* a
`DEFAULT_VAT`, pero hay que cuadrarlo (decidir cuál es el correcto y alinear
pipeline + Odoo). Anotado el 2026-06-07.

### Usuarios web por empresa

| `empresa.id` | Empresa | Usuarios (`users.email`) | Rol |
|---|---|---|---|
| 1 | AUSTRAL | `rdpablo@austral.es`, `c.alcalde.campusport@gmail.com`, `c@x.com` | accountant / admin / admin |
| 2 | CARARJFAM | `carloscararjfam@cararjfam.com` | accountant |
| 3 | BEST TRAINING | `besttraining@cararjfam.com` | accountant |

---

## Detalle por base de datos Odoo

### `cararjfam` (PRODUCCIÓN del grupo CARAJFAM)
- **company 1** — CARARJFAM2019,SL (B93653392) → la única que debe contabilizar
  aquí el pipeline `/opt/automation`.
- **company 2** — BEST TRAINING RINCÓN DE LA VICTORIA SL (B72349137) →
  **⚠️ LEGACY / A LIMPIAR.** BT se migró a `round_facturacion/3`. No debe
  recibir documentos nuevos. Quedan restos (statements + apuntes de bank) de
  antes del split que hay que migrar/borrar (ver "Pendientes").

### `cararjfam_test` (BD de AUSTRAL — pese al nombre "test")
- **company 4** — INTERNATIONAL AUSTRAL SPORT SA (A39100573) → contabiliza el
  pipeline `/opt/automation_austral`.
- companies 1/2/3 son copias de test, **no se usan**.

### `round_facturacion` (Odoo del SaaS Round / NoofitPro)
- **company 3** — BEST TRAINING RINCÓN DE LA VICTORIA SL (B72349137) → la BUENA
  para BT. Es la empresa del manager NoofitPro `roundgestion` (17677) y sus
  trainers `roundmalagacentro` (17675) / `roundanoreta` (17674). Contabiliza el
  pipeline `/opt/automation_bt_round`.
- **company 1** — "BEST TRAINING (legacy USA) - NO USAR" → lista negra.
- **company 2** — "ES Company (vacía) - NO USAR".
- **companies 5–15** — `ZZZ_TESTn_DELETE_ME` / "Pruebas Noofit SL" → basura de pruebas.

---

## Cómo arranca cada automatización

- **Server HTTP** (`automation.service`, gunicorn :8080, WorkingDir
  `/opt/automation`): recibe los JSON que le postea el `poller.py` y contabiliza
  en Odoo resolviendo la company por `target_company_vat`.
- **Crons** (crontab del usuario `odoo`, NO de root):
  - cararjfam: `extractor.py`, `poller.py`, reconciliadores, dudas… 23:23–23:40.
  - austral: `extractor.py --company B44821965`, liquidación PS, dudas… 02:00–03:10.
  - bt_round: `extractor.py`, `apply_rules_to_bank.py`, `bank_reconciler.py`,
    `bank_multi_reconciler.py`, dudas, learning… 00:23–00:40.
- Cada pipeline está **aislado**: su propio `companies.py`, sus carpetas Drive y
  sus logs en `/var/log/automation*`. NUNCA comparten company entre sí.

---

## Checklist al DAR DE ALTA una empresa nueva (no saltarse ningún paso)

1. **Odoo**: crear/identificar la `res_company` en su BD. Apuntar BD + company_id + VAT.
2. **Pipeline**: crear carpeta `/opt/automation_<slug>` con su `companies.py`
   (`DB_NAME`, `odoo_company_id`, `vat`, `EXPECTED_VATS`, carpetas Drive) y añadir
   sus líneas al crontab de `odoo`. Verificar que el `vat` del `companies.py`
   == VAT real de la company Odoo.
3. **Drive**: crear las carpetas (Cola_VPS, Procesados, Contabilizado, Revisión,
   Rechazadas, Informes) y pegar sus IDs en `companies.py`.
4. **Web**: insertar fila en `data/app.db` tabla `empresa` (`odoo_db`,
   `odoo_company_id`, nombre, carpetas, logo) y crear el/los `users` con
   `empresa_id` correcto.
5. **Verificar coincidencia** con el bloque de queries de abajo.
6. **Actualizar este documento** (tabla maestra + detalle) y RECOVERY.md.

---

## Queries de verificación (detectan desalineaciones)

```bash
# 1) Qué empresa/BD apunta cada usuario web
ssh round-vps "cd /opt/austral-contab-web/backend && sudo -u odoo /opt/odoo17/venv/bin/python -c \"
import sqlite3; c=sqlite3.connect('data/app.db'); c.row_factory=sqlite3.Row
for r in c.execute('SELECT e.id,e.nombre,e.odoo_db,e.odoo_company_id FROM empresa e'): print(dict(r))\""

# 2) VAT real de cada company en cada BD (debe coincidir con companies.py de su pipeline)
for DB in cararjfam cararjfam_test round_facturacion; do
  ssh round-vps "sudo -u postgres psql $DB -c \"SELECT c.id,c.name,p.vat FROM res_company c JOIN res_partner p ON p.id=c.partner_id ORDER BY c.id;\""
done

# 3) VAT declarado por cada pipeline
ssh round-vps "for d in /opt/automation /opt/automation_austral /opt/automation_bt_round; do
  echo \$d; grep -E 'DB_NAME|EXPECTED_VATS|odoo_company_id' \$d/companies.py; done"

# 4) Documentos contabilizados en los últimos días por BD/company (¿van donde deben?)
ssh round-vps "sudo -u postgres psql round_facturacion -c \"SELECT company_id,create_date::date,count(*) FROM account_move WHERE create_date>=now()-interval '3 days' GROUP BY 1,2 ORDER BY 1,2;\""
```

Si la query (2) y la (3) NO coinciden para una empresa, o la (1) apunta a una
BD/company distinta de la de su pipeline → hay desalineación, corregir antes de
seguir.

---

## Pendientes / anomalías conocidas (2026-06-07)

1. **BT — extracto bancario iba a la BD equivocada — CAUSA RAÍZ ARREGLADA.**
   `/opt/automation_bt_round/extractor.py` tenía
   `BANK_IMPORTER = "/opt/automation/bank_importer.py"` (el de **cararjfam**,
   `DB_NAME=cararjfam`) → los extractos de BT se importaban en `cararjfam/2` en
   vez de `round_facturacion/3`. El "imported" del log era falso: el guard de
   idempotencia veía el statement ya metido en cararjfam/2 y devolvía OK.
   - ✅ Corregido a `"/opt/automation_bt_round/bank_importer.py"` (`round_facturacion`).
   - ✅ El log de éxito ahora imprime el resultado real del importador (BD/statement/duplicado).
   - ✅ Colisión de IBAN resuelta: el IBAN `ES98…7577` estaba en company 1
     (legacy "NO USAR", journal 15) **y** company 3 (journal 21). Quitado de la
     legacy → routing inequívoco a company 3. (`account.journal._order` no es por
     id, así que la colisión era impredecible.)
   - ⏳ PENDIENTE: el usuario deja el fichero Santander REAL de BT en su carpeta
     Drive (queue) → el cron de las 00:30 lo importará en `round_facturacion/3`.
     Backup `extractor.py.bak_bankfix` en el VPS.
2. **`cararjfam/2` (BT legacy) — limpiar MAÑANA** (tras confirmar el banco en
   round_facturacion/3). Las 102 facturas ya están replicadas en
   round_facturacion/3 (verificado: las 102 refs ⊆ las 110 de round/3). Quedan
   por borrar: statements Santander #3 (ene–abr) y #4 (mar–jun) + sus 542
   apuntes + 6 varios. Objetivo: cararjfam/2 vacía/archivada, nunca más BT ahí.
3. **AUSTRAL — VAT del pipeline ≠ VAT de Odoo** (`B44821965` vs `A39100573`).
   Decidir cuál es el bueno y alinear `companies.py` + Odoo.

### ⚠️ Lección / regla derivada (ampliada jun 2026)
Cada pipeline DEBE usar **sus propios** scripts (importadores + procesadores),
nunca los de otro pipeline — el `DB_NAME` (y la company) está **hardcodeado en
cada script**. Si un `extractor.py` apunta a un script de otro pipeline, los
documentos se contabilizan en la BD/empresa equivocada, o fallan con
"missing accounts in company N" (esa company no existe en esa BD).

**Incidente bt_round (jun 2026):** su `extractor.py` apuntaba a varios scripts de
`/opt/automation` (cararjfam, `DB_NAME=cararjfam`). Síntomas: nóminas y facturas
de BT fallaban con `missing accounts in company 3` (no existe en cararjfam) y los
extractos bancarios se colaban en `cararjfam/2`. **Las 5 constantes a revisar en
`extractor.py`:** `BANK_IMPORTER`, `SEPA_IMPORTER`, `PROCESS_SCRIPT`,
`NOMINA_SCRIPT`, `TAX_PAYMENT_SCRIPT`. Todas corregidas a
`/opt/automation_bt_round/…`. `sepa_xml_importer.py` no existe en ningún pipeline
(SEPA-XML no implementado; las remesas SEPA entran por el backend de Round).
Backups VPS: `extractor.py.bak_bankfix`, `.bak_procfix`, `.bak_procfix2`.

**Verificación rápida** — ninguna ruta debe apuntar a `/opt/automation/` desde
otro pipeline, y cada script destino debe tener su `DB_NAME` correcto:
```
grep -noE '/opt/automation[a-z_]*/[a-zA-Z_]+\.py' /opt/automation_<X>/extractor.py | sort -u
for f in process_invoice nomina_processor tax_payment_processor bank_importer; do
  grep -m1 '^DB_NAME' /opt/automation_<X>/$f.py; done
```

---

_Última actualización: 2026-06-07 — creado tras detectar que el usuario web de
Best Training apuntaba a la BD equivocada (round_facturacion ↔ cararjfam)._
