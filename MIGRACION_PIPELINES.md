# Migración de CARARJFAM y BT al modelo nuevo

> Estado: **planificada, no ejecutada.** Escrito el 17/09/2026 tras verificar el terreno.
> Decisión del usuario: se retoma. Este documento existe para que quien la aborde no tenga
> que redescubrir lo de abajo.

## Qué se creía y qué es en realidad

Se planteó como «apagar los crons viejos y encender los nuevos». **No lo es.** Los
pipelines nuevos (`automation_cf`, `automation_bt`) no son una versión mejorada de los
viejos: hacen **menos cosas a propósito**, porque el resto se mudó a la web.

| Función | Modelo viejo (CARARJFAM, BT) | Modelo nuevo (Wiemspro, Medical, Austral) |
|---|---|---|
| Extraer y contabilizar documentos | pipeline | pipeline |
| Importar extractos bancarios | pipeline (`bank_importer.py`) | web (`services/bank_import.py`) |
| Conciliar | pipeline, de madrugada | **web, una persona** |
| Reglas de conciliación | Odoo, addon `learned_rules` | web, tabla `concil_regla` |
| Dudas | pipeline (xlsx a Drive) | web |
| Aprendizaje | pipeline (`learning.py`) | web |
| Backup a Drive | pipeline (`backup_to_drive.py`) | **no tiene equivalente** |

**Por qué difieren:** CARARJFAM y BT están en un Odoo 17 propio, donde se puede instalar
un addon (`learned_rules`) y guardar ahí las reglas. Wiemspro, Medical y Austral están en
Odoo alojado, donde no se puede — por eso sus reglas viven en la web.

Migrar no es cambiar unos crons: es **cambiar cómo se trabaja**. Hoy la conciliación de
esas dos empresas se hace sola por la noche; después la haría una persona desde la web.
Eso es una decisión de negocio, no de infraestructura.

## Lo verificado (17/09/2026)

### Los pipelines nuevos ya están en producción, para una vía

`cron_cola_vps.py` lee `empresa.pipeline_dir` y ejecuta `<pipeline_dir>/webproc.py`. La
tabla `empresa` de `carajfam-contab` ya apunta a `/opt/automation_cf` y
`/opt/automation_bt`. Es decir:

- **Subidas por la web** → pipelines **nuevos** (ya)
- **Cola de Drive** → pipelines **viejos** (crons)

### Lo que los crons viejos hacen y los nuevos no tienen

| | CARARJFAM | Best Training |
|---|---|---|
| Scripts distintos que invocan sus crons | 15 | 11 |
| De esos, presentes en el pipeline nuevo | 1 (`poller_tarjetas.py`) | 0 |

Ausentes en los nuevos: `bank_reconciler`, `bank_multi_reconciler`, `apply_rules_to_bank`,
`dudas_apply`, `dudas_xlsx_collect`, `dudas_xlsx_publish`, `learning`, `learning_drive`,
`detect_duplicate_partners`, `periodic_expenses_check`, `email_summary`, `backup_to_drive`.

### Las reglas: 313 en Odoo, 0 en la web

Módulo `learned_rules` **instalado** en las dos BD. Contenido real:

| BD | `bank` | `invoice` | `vat_correction` | Total |
|---|---|---|---|---|
| `cararjfam` | 38 | 46 | 1 | **85** |
| `round_facturacion` | 85 | 143 | — | **228** |

Confianza media: 0.91 en las de banco, 0.85 en las de factura.

Y en la web de esas empresas:

| Tabla | `carajfam-contab` | `wiemspro-contab` | `medicalcables-contab` |
|---|---|---|---|
| `concil_regla` | **0** | 299 | 57 |
| `regla_asiento` | 145 | 145 | 82 |
| `reconcile_dismissed` | 7 | 11 | 4 |

**Apagar los crons hoy dejaría a las dos empresas con cero reglas de conciliación.**

### Funciona, no está roto

`apply_rules_to_bank.py` corre cada noche (23:37 CARARJFAM, 00:37 BT) y registra:

```
company 1: 37 rules, 10 unreconciled lines
company 3: 76 rules, 37 unreconciled lines
```

## El trabajo real

### 1. Escribir el migrador de reglas

De `learned.rule` (Odoo 17) a `concil_regla` (SQLite de la web). El mapeo:

| `learned_rule` | `concil_regla` | Dificultad |
|---|---|---|
| `pattern` | `patron` | directo |
| `confidence` | `confianza` | directo |
| `times_applied` | `veces` | directo |
| `active` | `activa` | directo |
| `account_id` | `cuenta_code` + `cuenta_name` | resolver el id contra `account_account` |
| `partner_id` | `partner_name` | resolver el id contra `res_partner` |
| `company_id` | `empresa_id` | **traducir**: company de Odoo → `empresa.id` de la web |

**Tres huecos que hay que decidir:**

1. **`signo`** existe en `concil_regla` y **no tiene origen** en `learned_rule`. Hay que
   deducirlo (¿del tipo de cuenta? ¿del histórico de aplicación?) o dejarlo nulo y ver qué
   hace el motor.
2. **Solo migran las reglas `bank`** (123 de 313). `concil_regla` no tiene `rule_type`.
   Las 189 de `invoice` no tienen destino claro — puede que sean `regla_asiento`, que ya
   tiene 145 filas en las tres webs (sospechosamente iguales: conviene mirar si es una
   semilla compartida o datos reales).
3. Se pierden `source`, `notes`, `tax_id`, `sequence` y `last_applied`. Comprobar si el
   motor de la web los necesita.

### 2. Validar en paralelo, sin apagar nada

Con las reglas importadas y los crons **encendidos**, comparar durante varios días lo que
concilia el pipeline con lo que propondría la web. Si no coinciden, el migrador está mal y
se ve antes de romper nada.

### 3. Cambiar una empresa primero

**Best Training**, que tiene menos volumen. Solo cuando lleve semanas estable, CARARJFAM.

### 4. Lo que no se apaga nunca

- `backup_to_drive.py` — no tiene equivalente en la web. Se queda como está.
- `email_summary.py`, `periodic_expenses_check.py`, `detect_duplicate_partners.py` —
  decidir uno a uno; no son parte del modelo nuevo pero tampoco estorban.

## Antes de empezar, contesta esto

1. ¿Aceptas que la conciliación de CARARJFAM y BT **deje de ser automática** y pase a
   hacerse desde la web, como en Wiemspro?
2. ¿Qué pasa con las 189 reglas de `invoice`?
3. ¿Quién mira la conciliación a diario si deja de hacerse sola?

Sin respuesta a la primera, el resto no importa: la migración no es un cambio técnico, es
un cambio de rutina de trabajo.
