# CLAUDE.md — odoo-deploy

> Instrucciones para Claude Code al trabajar en este proyecto.
> Creado el 17/09/2026 en la auditoría del ecosistema; este repo no tenía ninguno.

## Qué es

El repositorio de **infraestructura** del VPS `round-vps` (212.227.40.122). No es una
aplicación: aquí viven los pipelines de contabilización, los addons propios de Odoo, el
instalador, el crontab y —lo más valioso— la **documentación operativa del sistema**.

| Carpeta / fichero | Qué es |
|---|---|
| `RECOVERY.md` | **El documento vivo del sistema.** 151 KB, 56 secciones, 61 commits. Registro cronológico de cada incidente, migración y decisión de infraestructura |
| `MAPA_EMPRESAS.md` | Cruce empresa ↔ Odoo ↔ pipeline ↔ web ↔ Drive. ⚠️ desfasado, ver abajo |
| `BLUEPRINT.md`, `HANDOFF.md`, `HANDOFF_AUSTRAL.md`, `ARQUITECTURA_CARAJFAM.md` | Documentos de mayo/junio, un solo commit cada uno. Contexto histórico |
| `automation*/` | Los cinco pipelines de contabilización (ver tabla) |
| `addons/` | Addons propios de Odoo: `round_autologin`, `round_facturacion`, `learned_rules` |
| `install_odoo.sh`, `crontab_odoo.txt`, `odoo_modules/` | Provisión del servidor |

## Los pipelines

Cada uno contabiliza los documentos de UNA empresa en SU base de datos. Están aislados a
propósito: tienen su propio `companies.py`, sus carpetas de Drive y sus logs.

| Pipeline | BD Odoo | company | Estado |
|---|---|---|---|
| `automation` | `cararjfam` | 1 | Generación antigua, **en producción** (crons 23:23–23:40) |
| `automation_austral` | `cararjfam_test` | 4 | Generación antigua, **crons desactivados** salvo el backup |
| `automation_bt_round` | `round_facturacion` | 3 | Generación antigua, **en producción** (crons 00:23–00:40) |
| `automation_austral_e18` | `wiems_v18_prod` | 12 | **Nueva** (09/09/2026), en producción: cron de PrestaShop L-V 02:00 + subidas web |
| `automation_bt` | `round_facturacion` | 3 | **Nueva** (09/09/2026), en producción **para las subidas web** |
| `automation_cf` | `cararjfam` | 1 | **Nueva** (09/09/2026), en producción **para las subidas web** |

Los tres nuevos son el port de la arquitectura de `automation/wiemspro` (con
`poller_tarjetas`, `process_asiento`, `expense_router`, `webproc`, `orderproc`,
`deliveryproc`) a cada empresa.

### ⚠️ CARARJFAM y BT corren DOS pipelines a la vez

No es que los nuevos estén sin usar: **cada generación atiende una vía de entrada
distinta**, y conviene saberlo antes de tocar nada.

| Vía de entrada | Quién la procesa | Cómo se dispara |
|---|---|---|
| Subida por la web | `automation_cf` / `automation_bt` (**nuevos**) | `cron_cola_vps.py` lee `empresa.pipeline_dir` y ejecuta `<pipeline_dir>/webproc.py` |
| Cola de Drive | `automation` / `automation_bt_round` (**viejos**) | Crons propios (37 y 22 líneas de crontab) |

Los nuevos no tienen crons propios, pero la web los invoca en cada subida. La tabla
`empresa` de `carajfam-contab` ya apunta a ellos.

**Decisión del 17/09/2026: se retoma la migración.** El paso pendiente es pasar también el
flujo de Drive a los pipelines nuevos, **apagando los crons antiguos en la misma
operación** — si no, los mismos documentos se contabilizarían dos veces.

📌 `automation_austral_e18/CRON_PENDIENTE.txt` documenta los crons preparados y **no
activados**, y avisa de lo importante: activarlos sin desactivar antes los del pipeline
viejo contabilizaría los mismos documentos **en dos contabilidades**.

### La regla que más caro ha salido

**Cada pipeline debe usar SUS propios scripts, nunca los de otro.** El `DB_NAME` está
hardcodeado en cada fichero. En junio de 2026 el `extractor.py` de `bt_round` apuntaba a
scripts de `/opt/automation` (CARARJFAM): los extractos bancarios de Best Training se
importaron en la empresa equivocada y el log decía "imported" igualmente, porque el guard
de idempotencia los veía ya metidos. Las 5 constantes a revisar al clonar un pipeline:
`BANK_IMPORTER`, `SEPA_IMPORTER`, `PROCESS_SCRIPT`, `NOMINA_SCRIPT`,
`TAX_PAYMENT_SCRIPT`. Detalle en `MAPA_EMPRESAS.md`.

## ⚠️ `MAPA_EMPRESAS.md` se contradice con `RECOVERY.md`

`MAPA_EMPRESAS.md` se declara *"fuente de verdad"*, pero su última actualización real es
del **12/06/2026** y dice que AUSTRAL vive en `cararjfam_test` company 4.

`RECOVERY.md §56` (15/09) dice lo contrario, y en mayúsculas:

> **LO PRIMERO SI HAY QUE RESTAURAR**: la contabilidad de AUSTRAL ya NO vive en este VPS.
> Está en `wiems_v18_prod` company 12, accesible solo por XML-RPC.

**Manda `RECOVERY.md`.** Además a `MAPA_EMPRESAS.md` le faltan dos instancias enteras de
Odoo 18 (`wiems_v18_prod` y `medical_v18`) y las webs de Wiemspro y Medical Cables.
Pendiente de reescribir.

Mapa correcto a 17/09/2026:

| Empresa | BD | company | Odoo |
|---|---|---|---|
| Wiemspro SL / CORP / Bio Sensors | `wiems_v18_prod` | 1 / 3 / 9 | 18 Enterprise |
| International Austral Sport SA | `wiems_v18_prod` | 12 | 18 Enterprise |
| Medical Cables SL | `medical_v18` | 1 | 18 Community |
| CARARJFAM2019 SL | `cararjfam` | 1 | 17 |
| Best Training Rincón de la Victoria SL | `round_facturacion` | 3 | 17 |

`cararjfam_test` quedó como **archivo de consulta**: conserva el histórico de Austral
hasta el 30/06/2026 y **no se escribe más en él**.

## Copias de seguridad — qué cubre cada una

| Qué | Quién | Cuándo | Dónde |
|---|---|---|---|
| BD + filestores de Odoo 17 | `/usr/local/bin/odoo_backup.sh` | 03:30 diario | `/var/backups/odoo`, 14 días |
| Pipeline + addons + configs + secretos | `backup_to_drive.py` de cada pipeline | 04:00 / 04:20 / 04:40 | Google Drive, rotación por día de la semana |
| `app.db` de wiemspro-contab | `backup_appdb.py` | 03:00 diario | local |

**Lo que NO cubren:** `wiems_v18_prod` y `medical_v18` no están en este VPS (son Odoo
alojados; si se pierden, se recuperan desde sus propios backups). Y solo tres pipelines
tienen `backup_to_drive.py` propio — los otros dependen de estar en este repo, que es
justo por lo que se rescataron el 17/09/2026.

## Reglas al tocar este repo

- ✅ **Escribe en `RECOVERY.md` en el mismo turno** en que hagas un cambio de infra o
  resuelvas un incidente. Es lo que ha permitido reconstruir el sistema tras perder un PC.
- ✅ Actualiza `MAPA_EMPRESAS.md` si tocas empresas, BD o pipelines. Que lleve tres meses
  desfasado es el motivo de que una regla apuntara a un registro muerto durante meses.
- ❌ No clones un pipeline copiando otro sin revisar las 5 constantes de scripts,
  `DB_NAME`, `odoo_company_id`, `vat` y los `DAY_FILE_IDS` de Drive.
- ❌ No actives los crons de `automation_bt`, `automation_cf` o `automation_austral_e18`
  sin desactivar antes los del pipeline viejo equivalente.
- ❌ No commitees secretos. Están en `/etc/automation_sa.json` y en los `.env`, fuera del
  repo a propósito.

## Relación con los demás repos

| Repo | Qué aporta |
|---|---|
| `wiemspro-contab` | Plataforma común de las webs contab + `tools/contab.sh` (turnos y despliegue) |
| `medicalcables-contab` | Web de Medical + los scripts de sync (`aplicar_desde_pc.sh`, `fixups.py`, `personalizar_carajfam.py`) |
| `austral-contab-web` | ⛔ Retirado el 17/09/2026. Fue el origen de toda la plataforma |
| `gestionnoofit` | Panel NoofitPro, producto aparte |

## Estado de la documentación

| Documento | Último commit | Fiabilidad |
|---|---|---|
| `RECOVERY.md` | 15/09/2026 (61 commits) | ✅ Al día, es la referencia |
| `MAPA_EMPRESAS.md` | 12/06/2026 (6 commits) | ⚠️ Desfasado: Austral y las dos instancias de Odoo 18 |
| `BLUEPRINT.md` | 04/06/2026 (1 commit) | Histórico |
| `HANDOFF.md`, `HANDOFF_AUSTRAL.md`, `ARQUITECTURA_CARAJFAM.md` | 04/05/2026 (1 commit) | Histórico |
