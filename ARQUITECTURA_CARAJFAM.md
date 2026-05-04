# Sistema de Contabilidad Automatizada — CARARJFAM2019,SL + Best Training

Documento explicativo del sistema completo. Pensado para entender qué hace, dónde vive cada cosa, cuándo y cómo intervenir.

---

## 1. Visión general

Cada noche, sin tocar un botón, el sistema:

1. **Lee** documentos (facturas, nóminas, recibos AEAT/TGSS, extractos bancarios) que has subido a Drive durante el día
2. **Clasifica** cada uno con IA (Claude) en uno de varios tipos: factura, nómina, IRPF, SS, otro oficial, extracto banco
3. **Extrae** los campos (proveedor, CIF, fechas, importes, líneas con IVA, retenciones por empleado, etc.)
4. **Crea** los asientos contables correctos en Odoo (factura proveedor / asiento de nómina / asiento manual de impuesto)
5. **Concilia** automáticamente los movimientos bancarios contra facturas y otros apuntes abiertos
6. **Aprende** reglas de tus decisiones manuales y las aplica al resto del extracto
7. **Genera** una hoja Excel `dudas_para_revisar.xlsx` con lo que necesita tu criterio
8. **Mueve** los archivos procesados a `Contabilizado odoo/`
9. **Envía** un email a primera hora con el resumen del día y la xlsx adjunta

Ventana de ejecución: **23:23 → 23:40** (hora de Madrid).

---

## 2. Empresas y datos

| id | Empresa | CIF |
|---|---|---|
| 1 | CARARJFAM2019,SL | B93653392 |
| 2 | BEST TRAINING RINCON DE LA VICTORIA SL. | B72349137 |

Ambas conviven en una **sola base de datos** Odoo (`cararjfam`). Cada apunte/factura/banco lleva el campo `company_id` que las separa internamente.

---

## 3. Servidor y arquitectura

```
                    ┌──────────────────────────┐
                    │   Tú subes documentos    │
                    │   a Google Drive         │
                    └────────────┬─────────────┘
                                 │
                                 ▼
                  ┌─────────────────────────────┐
                  │  Service Account (Drive API) │
                  │  /etc/automation_sa.json     │
                  └────────────┬─────────────────┘
                               │
        ┌──────────────────────┴──────────────────────┐
        │  Servidor IONOS — 212.227.40.122            │
        │  Ubuntu 24.04                               │
        │                                             │
        │  ┌─────────────────────┐                    │
        │  │ /opt/automation/    │  Pipeline Python   │
        │  │   venv/             │  + Claude headless │
        │  │   *.py scripts      │                    │
        │  └────────┬────────────┘                    │
        │           │ JSON via /tmp                    │
        │  ┌────────┴────────────┐                    │
        │  │ /opt/odoo17/        │  Odoo 17 Community │
        │  │   venv/             │  + custom addons   │
        │  │   custom-addons/    │  (learned_rules)   │
        │  │     learned_rules/  │                    │
        │  └────────┬────────────┘                    │
        │           │                                  │
        │  ┌────────┴────────────┐                    │
        │  │ Postgres            │  BD cararjfam      │
        │  └─────────────────────┘                    │
        │                                             │
        │  nginx + Let's Encrypt                      │
        │     erp.carajfam.com → Odoo                 │
        │     demo.carajfam.com → Odoo (BD demo/test) │
        └─────────────────────────────────────────────┘
                               │
                          SMTP Gmail
                               │
                               ▼
                       Email diario a ti
                       c.alcalde.campusport@gmail.com
```

### Acceso web

- **Producción**: `https://erp.carajfam.com` (BD `cararjfam`)
- **Sandbox**: `https://demo.carajfam.com/web/database/selector` (BDs `cararjfam_test`, `demo`)

### Acceso SSH (admin)

```
ssh -i ~/.ssh/odoo_carajfam root@212.227.40.122
```

### Dos venvs Python (importante saberlo)

| Venv | Uso |
|---|---|
| `/opt/automation/venv` | Drive, parsers (csb43/pandas/openpyxl), email |
| `/opt/odoo17/venv` | ORM Odoo (psycopg2 + pyOpenSSL) |

NUNCA se mezclan: instalar `google-api-python-client` en el venv de Odoo rompe pyOpenSSL y tumba Odoo.

---

## 4. Estructura Drive — qué hay en cada carpeta

Cada empresa tiene una carpeta principal en Drive con subcarpetas estandarizadas:

```
📁 [Carpeta principal de la empresa]    ← TU PUNTO DE ENTRADA
   │
   │  Aquí subes lo que quieras procesar:
   │   - Facturas proveedor (PDF/JPG/HEIC)
   │   - Nóminas mensuales (PDF)
   │   - Recibos IRPF / SS / AEAT (PDF)
   │   - Extractos bancarios (CSV Caixa, XLS Santander, AEB43)
   │   - Imágenes de tickets (gasolinera, comida, ferretería…)
   │
   ├── 📂 Contabilizado odoo/    ← Donde el bot mueve lo procesado OK
   │     (con todos los meses anteriores)
   │
   ├── 📂 revision/              ← Fallos que necesitan tu atención
   │     (PDF que el bot no entendió, errores de extracción,
   │      formatos extraños — abrirlos y decidir qué hacer)
   │
   ├── 📂 Aprendizajes_aplicados/  ← CSVs de reglas que enseñas
   │     (subes un CSV con patron→cuenta y lo aprende)
   │
   ├── 📄 dudas_para_revisar.xlsx  ← Archivo viviente que el bot
   │     actualiza cada noche con casos que requieren tu criterio
   │     (RELLENAS la columna `tu_decision` con texto libre)
   │
   ├── 📂 Cola_VPS/   (legacy, sin uso actual)
   └── 📂 csv odoo/   (sin uso para el pipeline)
```

**Reglas de oro al subir documentos**:

- **Un PDF por documento** (no fusionar varias facturas en un PDF)
- **Nombres descriptivos** ayudan al log pero no son obligatorios
- **HEIC del iPhone**: a veces da problemas, mejor conviértelos a JPG
- **No subas duplicados**: el bot detecta `(proveedor + ref + fecha + total)` y los rechaza silenciosamente — el email te lo avisará a partir del 2026-04-30 con la sección "Documentos rechazados como duplicado"

---

## 5. Pipeline diario completo

Cron del usuario `odoo` (hora de Madrid):

| Hora | Script | Función | Venv |
|---|---|---|---|
| 23:23 | `learning_drive.py` | Descarga CSVs de `Aprendizajes_aplicados/` | automation |
| 23:25 | `learning.py --mode both` | Importa CSVs → `learned.rule` + escanea facturas posted últimos 7d para auto-aprender | odoo17 |
| 23:30 | `extractor.py` | Recorre carpetas Pendientes, clasifica con Claude, ruta al processor adecuado | automation (lanza Claude headless) |
| 23:35 | `poller.py` | Cleanup queue legacy (idle) | automation |
| 23:36 | `dudas_apply.py` | Lee xlsx, clasifica `tu_decision`, ejecuta acciones | automation + invoca odoo17 |
| 23:37 | `apply_rules_to_bank.py` | Aplica TODAS las reglas activas a TODAS las líneas no conciliadas (red de seguridad) | odoo17 |
| 23:37 | `bank_reconciler.py --threshold 90` | 1:1 matching score≥90 entre banco y facturas | odoo17 |
| 23:38 | `bank_multi_reconciler.py` | 1:N subset-sum (ej. AEAT paga IRPF trimestral agregando 3 mensualidades) | odoo17 |
| 23:38 | `dudas_xlsx_collect.py` | Recoge dudas actuales → JSON | odoo17 |
| 23:39 | `dudas_xlsx_publish.py` | Sube xlsx actualizada a Drive (preserva tus decisiones de ayer) | automation |
| 23:40 | `email_summary.py` | Genera HTML + xlsx adjunta y envía por SMTP | odoo17 |

Cada paso escribe a su log en `/var/log/automation/<nombre>.log` (rotación automática).

---

## 6. Clasificación: cómo decide qué tipo de documento es cada PDF

`extractor.py` envía el PDF a Claude headless con un prompt en español que devuelve un JSON. Tipos posibles:

| `document_type` | Qué procesador lo trata | Asiento generado |
|---|---|---|
| `invoice` | `process_invoice.py` | Factura proveedor `in_invoice` (o `in_refund` si total<0) |
| `nomina` | `nomina_processor.py` | Asiento manual DR 640+642 / CR 4751+476+465 |
| `irpf_payment` | `tax_payment_processor.py` | Asiento DR 475100 / CR 572 |
| `ss_payment` | `tax_payment_processor.py` | Asiento DR 476000 / CR 572 |
| `other_official` | `tax_payment_processor.py` | Asiento DR 629000 / CR 572 (placeholder) |
| `bank_statement` | `bank_importer.py` | Crea `account.bank.statement` con sus líneas |
| `not_a_document` | (skip, mueve a `revision/`) | — |

Si la confianza de la extracción es ≥ **0.9** → factura se publica directa. Si < 0.9 → queda como borrador para que la revises tú.

Las nóminas, IRPF, SS y otros oficiales **siempre se publican** (son documentos autoritativos, la matemática es verificable).

---

## 7. Cómo se contabiliza cada tipo

### 7.1 Factura proveedor

```
DR 600/626/629/...   = base imponible (cuenta según patrón aprendido)
DR 472xxx (IVA sopo) = importe IVA
                       CR 410000 = total (proveedor)
```

Si el total es **negativo** → se crea como `in_refund` (factura rectificativa) con líneas en absoluto. Nunca como `in_invoice` con importe negativo (Odoo rechaza).

### 7.2 Nómina (devengo del mes)

Identidad matemática: `bruto = sueldo_cash + salario_especie`, y `bruto - irpf - ss_emp - especie = liquido`.

```
DR 640000 = sueldo_cash    (= bruto - especie, una linea por empleado)
DR 642000 = especie_total   (suma autonomos socios — solo CARARJFAM)
            CR 475100 = irpf_total
            CR 476000 = ss_empleado + especie_total   (ambos van a TGSS)
            CR 465000 = liquido (una linea por empleado)
```

**Salario en especie** (CARARJFAM): los socios autónomos (Carlos Alcalde, Concepción Arjona) llevan en su nómina un concepto "salario en especie" que representa el autónomo que la empresa les paga. Esa cuota ya se reconoce aquí en 642 — cuando luego TGSS cobra del banco "TGSS AUTONOMOS", se cancela contra el saldo en 476 (no genera gasto nuevo).

### 7.3 Impuestos / TGSS / AEAT

```
IRPF (mod 111/115):  DR 475100 / CR 572     ref: "IRPF mod 111 2025-4T"
SS mensual:          DR 476000 / CR 572
Otro oficial:        DR 629000 / CR 572     (placeholder — manual reroute si toca)
```

### 7.4 Extracto bancario

Cada línea del extracto se importa como `account.bank.statement.line`. Inicialmente el contraapunte va a la **cuenta puente (572 suspense)** con `is_reconciled=false`. Después la pipeline de conciliación (sección 8) las cuadra.

---

## 8. Conciliación bancaria — las 3 pasadas

### Pasada 1: 1:1 matching (`bank_reconciler.py`)

Para cada línea bancaria sin conciliar, busca un único apunte abierto (en 410 proveedores, 430 clientes, 465 líquidos pendientes, 475 IRPF, 476 SS) cuyo importe coincide exactamente y cuyo nombre/concepto puntúa ≥ **90%**. Si encuentra → re-rutea la línea suspense al partner correcto y reconcilia.

Score basado en: importe exacto (+50) + dirección coherente (+10) + nombre proveedor en concepto (+25 si ≥60% similitud) + ref factura en concepto (+15) + proximidad fecha (+5). Mínimo para auto-aplicar: 90.

### Pasada 2: 1:N subset-sum (`bank_multi_reconciler.py`)

Para líneas que sobreviven la pasada 1, busca un subconjunto (2..12 apuntes) en cuentas agregadoras cuya suma cuadre con el banco ±0,10 €.

**Caso típico**: AEAT paga IRPF trimestral 4.276,89 € que cancela 3 mensualidades de 1.425,63 € en 475100. Encuentra ese subset y lo concilia todo junto.

### Pasada 3: aplicación de reglas (`apply_rules_to_bank.py`)

Red de seguridad. Recorre **todas** las líneas no conciliadas y, si el `payment_ref` matchea con una `learned.rule` (todas las palabras del patrón presentes), re-rutea la línea suspense a la cuenta de la regla.

Ejemplos de reglas aprendidas que ya tienes:
- `TGSS AUTONOMOS` → 476000
- `LIQUIDACION EMISION` → 626000
- `SEGUROS TUIO` → 625000
- `PAG NOMINAS` → 465000
- `DEVOLUCION RECIBO` → 430000

### Pasada 4 (no auto, surface): near-matches

Lo que no se concilia automáticamente y queda con un candidato cercano (diferencia entre 0,50 € y 100 €) aparece en el **email diario** bajo "⚠ Conciliaciones descuadradas — esperan instrucciones". Tú decides caso por caso.

### Reglas especiales

- **Líneas "Transaccion Contactless ..." (TPV)**: NUNCA se proponen como partial match. Solo si existe una factura **abierta del MISMO proveedor con importe exacto** se sugiere. Lo demás aparece "sin propuesta" para tu revisión.
- **Idempotencia**: una segunda pasada con "already reconciled" se trata como éxito.

---

## 9. Sistema de dudas xlsx (round-trip user-in-the-loop)

Es el centro de tu interacción con el sistema.

### Cómo funciona

1. Cada noche el bot regenera `dudas_para_revisar.xlsx` en la raíz de la carpeta Drive de cada empresa
2. La xlsx contiene casos que requieren TU criterio (facturas en borrador, líneas bancarias sin matchear, descuadres)
3. **Tú rellenas la columna `tu_decision`** con texto libre en español
4. La noche siguiente, `dudas_apply.py` lee la columna y clasifica tu texto
5. Ejecuta la acción correspondiente vía ORM
6. Si la decisión genera una regla útil, la propaga a líneas similares y la **graba como `learned.rule`**

### Columnas del xlsx

| Columna | Significado |
|---|---|
| empresa, tipo, id_odoo | identificadores |
| ref_o_concepto | el concepto bancario o referencia factura |
| fecha, importe | datos del movimiento |
| descripcion_corta | partner / cuenta sugerida |
| motivo_duda | por qué está aquí (multiple candidatos / sin propuesta / descuadrado / confianza baja) |
| sugerencia_actual | la mejor candidata del bot (no rellenes aquí — hazlo en `tu_decision`) |
| **tu_decision** | ← AQUÍ ESCRIBES TÚ |
| notas | metadatos del bot |
| estado_actual, primer_visto, ultimo_visto | tracking |

### Patrones que el bot entiende en `tu_decision` (no es exhaustivo)

| Si escribes... | Hace |
|---|---|
| `apertura` / `asiento de apertura` | skip (saldo pre-fundacional) |
| `neteado` / `siguiente` / `compensado` | skip (par cancelado) |
| `comisión bancaria` / `tarifa plana` / `liquidación por emisión` | DR 626 |
| `hipoteca` / `préstamo` | DR 520 |
| `no deducible` | DR 629 + nota en narration |
| `pago factura` / `pago fra` / `agrupación de facturas` | match contra factura abierta en 410 |
| `pago seguro` | DR 625 |
| `falta factura` / `queda como duda` | skip (esperando factura) |
| `pago nómina` | partial reconcile contra 465 |
| `saldo pendiente` / `diferencia se queda` | partial reconcile genérico |
| `liquidacion efectuada` | DR 430 (cobro TPV) |
| `gympass` | DR 430 con partner Gympass US LLC |
| `devolucion de recibo` | DR 430 (devolución cliente) |
| `pago seguridad social` / `pago SS` | DR 476 |
| `pago IVA` / `liquidación IVA` | DR 477 |
| `retenciones IRPF` | DR 475100 |
| `pago alquiler` | match factura 410 |
| `pago contra proveedor` | match factura abierta del proveedor |
| `subida factura X` / `subida fra X` | busca factura recién subida y reconcilia (auto-match inteligente por partner+importe) |
| `no hacer, buscaré la contrapartida` | skip (te ocupas tú manualmente) |
| `ok` / `sí` / `confirmo` (en filas con sugerencia) | acepta la sugerencia y reconcilia con ese apunte |
| Cualquier otro texto | queda como `PENDIENTE_HUMANO` (fila amarilla) — el bot no lo entendió, reformula |

### Importante

- **Las decisiones que tomas se convierten en reglas**: si escribes "comisión bancaria" en una línea con concepto "TARIFA PLANA SEPA", el bot crea una regla `TARIFA PLANA → 626` y la aplica a todas las líneas similares de los próximos meses.
- **Por seguridad**, las líneas resueltas se eliminan del xlsx (no se acumulan filas RESUELTO). Si quieres ver el histórico de decisiones, está en `narration` de cada movimiento Odoo.

---

## 10. Sistema de reglas aprendidas (`learned.rule`)

Es un **modelo Odoo custom** que vive en la pestaña Configuración → Reglas Aprendidas (si has activado modo desarrollador).

Cada regla:
- Patrón (texto a buscar en `payment_ref` del banco — match por "todas las palabras presentes")
- Tipo (banco / factura)
- Cuenta destino
- Partner asociado (opcional)
- Confianza (0..1)
- Origen: `active` (manual), `passive` (auto-aprendida de facturas posted), `system` (built-in, ej. TGSS AUTONOMOS)
- `times_applied` (contador)

### Cómo se crean

1. **Auto desde tus decisiones xlsx** (la mayoría): cuando rellenas "comisión bancaria" en una fila → el script extrae las 2 palabras más significativas del concepto bancario, crea regla con esas palabras → cuenta destino, confianza 0.9
2. **Auto desde facturas posted recientes** (`learning.py --mode passive`): escanea facturas creadas últimos 7 días, extrae partner+ref habituales y propone reglas
3. **Manual via CSV en Drive**: subes un CSV a `Aprendizajes_aplicados/` con columnas `pattern;account_code;rule_type;partner_name` y se importa

### Cómo verlas / editarlas en Odoo

URL directa: `https://erp.carajfam.com/odoo/action-learned_rules.action_learned_rule`

O via menú (con modo desarrollador activado): Configuración → Técnico → Learned Rules.

Puedes desactivar reglas que dan falsos positivos (campo `active = false`) sin borrarlas.

---

## 11. Email diario

Llega a `c.alcalde.campusport@gmail.com` cada día tras 23:40.

Secciones:

1. **Resumen por empresa** — tabla con todas las facturas creadas hoy: id, proveedor, CIF, ref, fecha, base, IVA, total, estado (publicada / borrador), notas de extracción
2. **📋 Documentos rechazados como duplicado** (cuando aplique) — PDFs que ya tenían factura existente (ref+partner+fecha coincidente), con link a la factura ya en Odoo
3. **Conciliación bancaria — propuestas** — líneas bancarias con candidatos por debajo del umbral 90, para que decidas
4. **⚠ Conciliaciones descuadradas — esperan instrucciones** — líneas con un candidato cercano pero diferencia entre 0,5 € y 100 €
5. **Adjuntos**: `dudas_para_revisar.xlsx` de cada empresa

---

## 12. Cómo accedes a Odoo y dónde encontrar las cosas

### Login

`https://erp.carajfam.com` → tu usuario habitual.

### Dashboard contable

Contabilidad → **Tablero**. Verás tarjetas para cada banco (Banco La Caixa, Banco Santander), facturas de cliente, facturas de proveedor.

### Ver movimientos bancarios pendientes

Contabilidad → Apuntes → Líneas de extractos bancarios → filtra por:
- Diario: tu banco
- ☐ Conciliado: false

(Si no encuentras el menú: `https://erp.carajfam.com/odoo/action-account.action_bank_statement_line`)

### Ver el plan de cuentas

Contabilidad → Configuración → Plan Contable. Filtra por código (ej. `476000`) o por empresa.

### Ver el libro mayor

Contabilidad → Informes → Libro Mayor (módulo `account_financial_report`). Selecciona empresa, rango de fechas, cuenta.

### Modo desarrollador (para ver opciones avanzadas)

Tu nombre arriba a la derecha → Mis Preferencias → activa "Modo desarrollador". O directamente: `https://erp.carajfam.com/web?debug=1`.

---

## 13. Cuándo necesitas intervenir manualmente

El sistema te avisa solo. Tu trabajo se reduce a:

1. **Subir documentos** a la carpeta de Drive de la empresa correspondiente cuando lleguen (correo, WhatsApp del gestor, etc.)
2. **Rellenar la columna `tu_decision`** del xlsx cuando algo te aparezca dudoso (típicamente: 5–10 líneas/día durante los primeros meses, luego va bajando porque las reglas se acumulan)
3. **Revisar la sección descuadres** del email cuando aparezca (1–2 casos/semana)
4. **Mirar `revision/` en Drive** semanalmente — ahí caen documentos que el bot no entendió. Decide qué hacer (a veces es un PDF mal escaneado, a veces el extractor falló y hay que reintentar moviéndolo de vuelta a la carpeta principal)

---

## 14. Logs y diagnóstico

Si algo va mal, los logs están en `/var/log/automation/`:

| Log | Qué contiene |
|---|---|
| `extractor.log` | Lo que clasificó / creó / movió a revisión cada noche |
| `dudas.log` | Decisiones aplicadas y errores de la pipeline xlsx |
| `reconciler.log` | Conciliaciones automáticas (1:1 + 1:N) |
| `apply_rules.log` | Reglas aplicadas en bulk |
| `email.log` | Si el email se envió OK |
| `learning.log` | Reglas auto-aprendidas |

Acceso vía SSH (root): `tail -50 /var/log/automation/extractor.log`

---

## 15. Glosario contable rápido (l10n_es PYMES)

| Cuenta | Significado |
|---|---|
| 410000 | Proveedores |
| 430000 | Clientes |
| 465000 | Remuneraciones pendientes de pago (líquido nóminas) |
| 475100 | HP, retenciones IRPF (mod 111/115) — siempre 6 dígitos, NO 4751 |
| 476000 | Org Seg Social acreedores (cuota empleado + autónomo) |
| 477000 | HP, IVA repercutido |
| 520000 | Préstamos a corto plazo |
| 572xxx | Cuenta de banco (default journal) |
| 600000 | Compras de mercaderías |
| 622000 | Comunidades / propiedad horizontal |
| 625000 | Primas de seguros |
| 626000 | Servicios bancarios (comisiones, transferencias, devoluciones) |
| 629000 | Otros servicios — placeholder para "no deducible" |
| 640000 | Sueldos y salarios |
| 642000 | Seguridad Social a cargo de la empresa (cuota empresa + autónomo socios) |

---

## 16. Vista por empresa

### CARARJFAM2019,SL (id=1)

- Banco: Banco La Caixa (extractos CSV)
- Empleados: socios autónomos (Carlos Alcalde, Concepción Arjona) + asalariados
- Particular: las nóminas llevan "salario en especie" = autónomo socios → DR 642 / CR 476
- Reglas aprendidas notables: `TGSS AUTONOMOS`, `CUSTODIA.FONDOS`, `C.P.BARRIO LA LUZ`, `TGSS.COTIZACION`, `I.R.P.F. MOD.111`, `PAG NOMINAS`, `SEGUROS TUIO`, `DIGI SPAIN`, `ANTHROPIC`

### Best Training Rincón de la Victoria SL. (id=2)

- Banco: Banco Santander (extractos XLS)
- Solo asalariados (sin socios autónomos)
- Particular: muchos cargos contactless (TPV) por compras pequeñas en gasolinera, ferretería, supermercado — requieren factura subida del establecimiento para cuadrar (regla "100% exacto, no parcial")
- Reglas aprendidas notables: `TRANSFERENCIA GYMPASS`, `EMISION REMESA`, `LIQUIDACION EFECTUADA`, `COBRO TARIFA`, `GASTOS DEVOLUCIONES`, `LIQUIDACION EMISION`, `DEVOLUCION RECIBO`, `TGSS AUTONOMOS`

---

## 17. Casuísticas conocidas

### Carlos Alcalde 22,28 € recurrentes

Cada cargo `PAG NOMINAS -3.000 €` a Carlos no cuadra exactamente con su líquido de nómina (3.022,28 €). Aparece como descuadre +22,28 € recurrente en 465. **Pendiente de identificar** qué concepto es (probablemente anticipo o concepto extracontable). Cuando lo aclares, se puede automatizar el ajuste.

### Diferencia SS empresa (BT)

Las nóminas de BT solo provisionan SS empleado (~6,35% bruto). La cuota patronal (~30%) NO se contabiliza en el devengo — se reconoce al recibir el cargo TGSS, donde la regla `TGSS COTIZACION → 476` lo manda a 476. Eso deja 476 con saldo negativo (la empresa "debe" SS) durante el mes. Si quieres provisionar mensualmente, se puede modificar `nomina_processor.py` para añadir una línea estimada de SS empresa (~30% bruto) DR 642 / CR 476 al postear la nómina.

### Líneas contactless TPV

Por regla del usuario: solo cuadran con factura **100% exacta** (mismo proveedor + mismo importe). Si difiere por 0,01 € → queda en xlsx para que decidas. Esto se hizo para evitar matches falsos por importe coincidente.

---

## 18. Backups

- BD: `/tmp/odoo_backup/cararjfam_db.dump` (último: ver `ls -lh`). pg_dump custom-format. Restore con `pg_restore -d nuevo_nombre /tmp/odoo_backup/cararjfam_db.dump`
- Filestore: `/tmp/odoo_backup/filestore_cararjfam.tar.gz` (44 MB)
- Custom addon: `/tmp/odoo_backup/learned_rules_addon.tar.gz`
- Scripts: `/tmp/odoo_backup/automation_scripts.tar.gz`

Recomendación: pasarlos a Drive o un sistema externo periódicamente (no hay backup automático configurado actualmente — lo que hay es solo lo que generé manualmente cuando preparamos el servidor 18).

---

## 19. Si tienes que volver a montar todo desde cero

Existe un documento técnico aparte (`HANDOFF.md`) con instrucciones detalladas para reproducir todo el pipeline en otro ERP, otro servidor, otras carpetas Drive. Útil si:
- Migras a Odoo 18 Enterprise
- Cambias de proveedor de hosting
- Replicas para una nueva empresa cliente

---

Fin documento.
