# MAPA_EMPRESAS.md — empresa ↔ Odoo ↔ pipeline ↔ web ↔ Drive

> **Para qué sirve.** Cada empresa que contabilizamos vive a la vez en varias capas que
> TIENEN que apuntar a lo mismo. Si una se desalinea (la web mira la BD equivocada, un
> pipeline escribe en otra company), los documentos "desaparecen", se contabilizan dos
> veces, o se mezcla información entre empresas.
>
> **Manténlo actualizado en el mismo turno en que toques cualquiera de las capas**, igual
> que `RECOVERY.md`.
>
> Reescrito el **17/09/2026**. La versión anterior era del 12/06/2026 y llevaba tres meses
> diciendo que la contabilidad de AUSTRAL estaba en `cararjfam_test` company 4, cuando
> migró a Odoo 18 Enterprise en agosto (`RECOVERY.md §56`). Esa desactualización propagó
> la regla equivocada "esta app es solo AUSTRAL company 4" al `CLAUDE.md` de otro repo,
> donde sobrevivió meses apuntando a un registro con `activa = 0`.

---

## La regla de oro

**Un `company_id` no significa nada por sí solo: solo junto a su base de datos.**

La company 1 es Wiemspro SL, CARARJFAM2019 o Medical Cables según dónde mires. Cualquier
instrucción que diga "company N" sin nombrar la BD es ambigua, y en contabilidad ambiguo
es peligroso. Confirma siempre el par **(BD, company_id)**.

## Tabla maestra

| Empresa | VAT | BD Odoo | company | Odoo | Web | Pipeline |
|---|---|---|---|---|---|---|
| **Wiemspro SL** | ESB72305832 | `wiems_v18_prod` | 1 | 18 Ent. | contab.wiemspro.com | `automation_wiemspro` |
| **Wiemspro CORP** | — | `wiems_v18_prod` | 3 | 18 Ent. | contab.wiemspro.com | `automation_wiemspro` |
| **Bio Sensors Suit Group SL** | ESB56771520 | `wiems_v18_prod` | 9 | 18 Ent. | contab.wiemspro.com | `automation_wiemspro` |
| **International Austral Sport SA** | ESA39100573 | `wiems_v18_prod` | 12 | 18 Ent. | pruebas-ca.medicalcables.eu | `automation_austral_e18` |
| **Medical Cables SL** | ESB93092690 | `medical_v18` | 1 | 18 CE | contab.medicalcables.eu | `automation_medicalcables` |
| **CARARJFAM2019, SL** | B93653392 | `cararjfam` | 1 | 17 | austral.carajfam.com | `automation_cf` + `automation` |
| **Best Training Rincón de la Victoria SL** | B72349137 | `round_facturacion` | 3 | 17 | austral.carajfam.com | `automation_bt` + `automation_bt_round` |

⚠️ **Trampa de nombres:** `austral.carajfam.com` **no es la web de Austral**, es la de
CARARJFAM y Best Training. La de Austral es `pruebas-ca.medicalcables.eu`.

### Dónde vive cada Odoo

| Instancia | Dónde | Acceso |
|---|---|---|
| `wiems_v18_prod` (18 Enterprise) | **Alojado, fuera del VPS** | Solo XML-RPC |
| `medical_v18` (18 Community) | **Alojado, fuera del VPS** | Solo XML-RPC |
| `cararjfam`, `round_facturacion` (17) | En el VPS `round-vps` | Postgres directo |

**El backup diario del VPS no incluye las dos primeras.** Si se pierde Austral, Wiemspro o
Medical, se recuperan desde los backups de su Odoo alojado, no de aquí.

### Bajas y restos

| Registro | Situación |
|---|---|
| `austral` en `cararjfam_test`/4 | **`activa = 0`** desde el 02/08/2026. Archivo de consulta: conserva el histórico hasta el 30/06/2026 y **no se escribe más en él** |
| `SoloCarlos`, `wiems_v18_prod`/11 | Fue una prueba. Sin pipeline, sin usuarios, sin carpeta de cola. `activa = 0` desde el 17/09/2026 |
| `cararjfam`/2 (BT legacy) | Restos anteriores al split. BT se migró a `round_facturacion`/3. No debe recibir documentos nuevos |
| `round_facturacion`/1 y /2 | `BEST TRAINING (legacy USA) - NO USAR` y `ES Company (vacía) - NO USAR` |
| `round_facturacion`/5 a /15 | `ZZZ_TEST*_DELETE_ME` y "Pruebas Noofit SL". Basura de pruebas |

---

## ⚠️ CARARJFAM y BT tienen DOS pipelines a la vez

Esto es lo más delicado del sistema ahora mismo y conviene entenderlo antes de tocar nada.

Desde el **09/09/2026** conviven dos generaciones de pipeline para las mismas empresas, y
**cada una atiende una vía de entrada distinta**:

| Vía de entrada | Quién la procesa | Cómo se dispara |
|---|---|---|
| **Subida por la web** | `automation_cf` / `automation_bt` (**nuevos**) | `cron_cola_vps.py` lee `empresa.pipeline_dir` y ejecuta `<pipeline_dir>/webproc.py` |
| **Cola de Drive** | `automation` / `automation_bt_round` (**viejos**) | Crons propios del pipeline (37 y 22 líneas de crontab) |

Los pipelines nuevos **no tienen crons propios**, pero **sí están en producción**: los
invoca la web en cada subida. No son un port abandonado.

**Qué NO hacer:** activar los crons de `automation_cf` o `automation_bt` sin desactivar
antes los equivalentes de `automation` y `automation_bt_round`. Los mismos documentos se
contabilizarían dos veces. El aviso está también en
`automation_austral_e18/CRON_PENDIENTE.txt`.

**Decisión tomada el 17/09/2026:** se retoma la migración a los pipelines nuevos. El paso
pendiente es mover el flujo de Drive de los viejos a los nuevos, apagando los crons
antiguos en la misma operación.

---

## Carpetas de cola en Drive

Donde se sueltan los documentos que el pipeline recoge:

| Empresa | `queue_folder` |
|---|---|
| CARARJFAM2019 | `1dIQ0IKGGk-3oJc9129pmA5IDepVzp71-` |
| Best Training | `13vIwkLLrZ8mTYn0tG_bp-tDshpOuepOE` |
| Medical Cables | `19SV1Sst6MgMGXJADOrSE2LVPOfFScolz` |
| Wiemspro SL | `1aIHzVtNRuKdQ7h5gydd_oLkFn6m0Y4cf` |
| Wiemspro CORP | `10EraOIKgXO2bDzswIPmyZa1ILjp9goX4` |
| Bio Sensors | `1Qm4euWXUjAOlCTEjzJOC8_efvw2KFtBM` |
| Austral | `15kI9YEpo-Z1OngKAud1X2ZPnQgH4jI85` |

---

## La fuente de verdad no es este documento

Este fichero es un **resumen legible**. Los datos vivos están en:

| Capa | Dónde mirarla |
|---|---|
| Empresa ↔ BD ↔ company ↔ pipeline ↔ Drive | Tabla `empresa` del `app.db` de cada web (`/opt/<web>/backend/data/app.db`) |
| Lo que contabiliza cada pipeline | `companies.py` de su carpeta |
| Qué se ejecuta y cuándo | `crontab -u odoo -l` |
| Historia y decisiones | `RECOVERY.md` |

Si este documento y la tabla `empresa` se contradicen, **manda la tabla** — y corrige esto.

### Comprobación rápida

```bash
# 1) que dice cada web
ssh round-vps 'for w in wiemspro-contab medicalcables-contab carajfam-contab austral-contab; do
  echo "-- $w"; sqlite3 -header -column /opt/$w/backend/data/app.db \
  "SELECT clave,odoo_db,odoo_company_id,vat,activa,pipeline_dir FROM empresa;"; done'

# 2) que dice cada pipeline
ssh round-vps 'for d in /opt/automation*/; do echo "-- $d";
  grep -hE "DB_NAME|odoo_company_id|vat" $d/companies.py 2>/dev/null | head -4; done'

# 3) las companies reales de las BD que estan en el VPS
ssh round-vps 'for DB in cararjfam round_facturacion cararjfam_test; do echo "-- $DB";
  sudo -u postgres psql -d $DB -Atc "SELECT id||chr(124)||name FROM res_company ORDER BY id;"; done'
```

Si (1) y (2) no coinciden para una empresa, hay desalineación: corrígela antes de seguir.

---

## Al dar de alta una empresa nueva

1. **Odoo** — identificar la `res_company`. Apuntar BD + company_id + VAT.
2. **Pipeline** — carpeta `/opt/automation_<slug>` con su `companies.py` (`DB_NAME`,
   `odoo_company_id`, `vat`, carpetas de Drive). ⚠️ Al clonar de otro pipeline hay que
   cambiar **todas** sus constantes de script, y también los `DAY_FILE_IDS` y el
   `FILESTORE` del `backup_to_drive.py`: en junio de 2026 una copia se llevó los file_ids
   de CARARJFAM y habría machacado sus backups.
3. **Drive** — crear las carpetas (cola, contabilizado, revisión, rechazadas, informes) y
   poner sus ids en `companies.py` y en la tabla `empresa`.
4. **Web** — fila en `empresa` (`odoo_db`, `odoo_company_id`, `vat`, carpetas,
   `pipeline_dir`, `activa`) y sus usuarios.
5. **Verificar** con las tres consultas de arriba.
6. **Actualizar este documento y `RECOVERY.md`.**

## La regla que más caro ha salido

**Cada pipeline usa SUS propios scripts, nunca los de otro.** El `DB_NAME` está
hardcodeado en cada fichero. En junio de 2026 el `extractor.py` de `bt_round` apuntaba a
scripts de `/opt/automation` (CARARJFAM): los extractos bancarios de Best Training se
importaron en la empresa equivocada, y el log decía "imported" igualmente porque el guard
de idempotencia los veía ya metidos.

Las 5 constantes a revisar: `BANK_IMPORTER`, `SEPA_IMPORTER`, `PROCESS_SCRIPT`,
`NOMINA_SCRIPT`, `TAX_PAYMENT_SCRIPT`.
