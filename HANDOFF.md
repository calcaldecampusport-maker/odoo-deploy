# Handoff — Automatización contable nocturna (Odoo → portable a otro ERP)

Documento de traspaso para retomar este sistema en otro Claude Code con **otro ERP, otro servidor, otras carpetas de Drive**. Resume arquitectura, lógica contable, pipeline, contratos de datos y los puntos críticos aprendidos en producción.

---

## 1. Qué hace el sistema

Cada noche, sin intervención humana:

1. **Recoge** documentos (PDFs, imágenes, CSV/XLS bancarios, XML SEPA) de carpetas `Pendientes/` en Google Drive (una por empresa).
2. **Clasifica** cada documento con un LLM (Claude headless) en uno de varios `document_type`: `invoice`, `nomina`, `irpf_payment`, `ss_payment`, `other_official`, `bank_statement`, `sepa`.
3. **Extrae** campos estructurados (proveedor, CIF, fechas, importes, líneas con tax rates, retenciones por empleado, etc.).
4. **Crea asientos** en el ERP con las cuentas correctas según tipo (PGC español):
   - Facturas → `in_invoice`/`in_refund` (rectificativa si total<0).
   - Nóminas → asiento manual DR 640+642 / CR 4751+476+465.
   - IRPF/SS/AEAT → asiento manual DR 4751/476 / CR 572.
5. **Auto-publica** a partir de un umbral de confianza (0.9 facturas; nóminas/impuestos siempre).
6. **Concilia banco** en 3 pasadas: 1:1 score≥90 → 1:N subset-sum (IRPF trimestral) → near-match para revisión humana.
7. **Aprende reglas**: cada decisión humana en el xlsx de dudas crea/actualiza un registro `learned.rule` y se propaga a todas las líneas bancarias similares.
8. **Genera dudas xlsx** en la raíz de la carpeta Drive de cada empresa. El usuario rellena la columna `tu_decision` con texto libre; al día siguiente el bot lo interpreta y ejecuta vía ORM.
9. **Mueve** archivos procesados a `Pendientes/Procesados/`.
10. **Envía email diario** con resumen, xlsx adjunto y descuadres pendientes de instrucción.

Todo idempotente: una segunda pasada con "already reconciled" se trata como éxito, no error.

---

## 2. Arquitectura

```
                    Google Drive (por empresa)
                   ┌─────────────────────────┐
                   │ Pendientes/             │   <-- usuario sube docs aquí
                   │   ├── (PDFs, CSV, XLS)  │
                   │   ├── dudas.xlsx        │   <-- round-trip user-in-the-loop
                   │   ├── aprendizajes.csv  │   <-- reglas que enseña el usuario
                   │   └── Procesados/       │   <-- bot mueve aquí tras procesar
                   └────────────┬────────────┘
                                │ Service Account (Drive API)
                                │
        ┌───────────────────────┴────────────────────────┐
        │  VPS Ubuntu 24.04 — IONOS                      │
        │                                                 │
        │  ┌──────────────────────┐                       │
        │  │ /opt/automation/venv │  Drive ops, parsers   │
        │  │  (Google libs,       │  bank_matcher, dudas, │
        │  │   pandas, openpyxl,  │  email_summary        │
        │  │   xlrd, csb43)       │                       │
        │  └──────────┬───────────┘                       │
        │             │ JSON via /tmp                      │
        │  ┌──────────┴───────────┐                       │
        │  │ /opt/odoo17/venv     │  ORM ops (vía         │
        │  │  (Odoo + psycopg2 +  │  subprocess)          │
        │  │   pyOpenSSL 21)      │                       │
        │  └──────────────────────┘                       │
        │             │                                    │
        │  ┌──────────┴───────────┐                       │
        │  │ Postgres (peer auth) │  DB: cararjfam        │
        │  │ Odoo 17 Community    │  multi-company        │
        │  └──────────────────────┘                       │
        │                                                 │
        │  cron (usuario `odoo`) — secuencia 23:23-23:40  │
        │  Claude Code headless (extractor) en /opt/odoo17│
        └─────────────────────────────────────────────────┘
                                │
                       SMTP (Gmail app password)
                                │
                          Email diario al usuario
```

### Componentes clave

| Componente | Tecnología | Por qué |
|---|---|---|
| ERP | Odoo 17 Community + l10n_es | Gratis, multi-empresa, ORM Python potente |
| LLM clasificador/extractor | Claude Code headless (`claude -p ... --permission-mode bypassPermissions`) | Más barato que API directa, autenticado como user `odoo` |
| Drive | Service Account + carpetas compartidas con la SA | API estable, sin OAuth interactivo |
| Reglas aprendidas | Custom Odoo addon `learned_rules` | Persistencia transaccional, multi-empresa nativa |
| Bancos | csb43 (N43), pandas+xlrd (.xls Santander), pandas+openpyxl (.xlsx Caixa) | Cubre los formatos vivos en España |
| Email | Gmail SMTP con app password | Sin servidor SMTP propio |

---

## 3. Pipeline diario (cron usuario `odoo`, hora Madrid)

```
23:23  learning_drive.py        Descarga aprendizajes/dudas CSV de raíz Drive
23:25  learning.py --mode both   Importa CSV → learned.rule + scan facturas posted
23:30  extractor.py              Recorre Pendientes/, clasifica, ruta al processor
23:35  poller.py                 Cleanup queue legacy (idle)
23:36  dudas_apply.py            Lee xlsx, clasifica `tu_decision`, ejecuta vía ORM
23:37  apply_rules_to_bank.py    Aplica TODAS las reglas activas a TODAS las líneas
23:37  bank_reconciler.py        1:1 matching score>=90
23:38  bank_multi_reconciler.py  1:N subset-sum
23:38  dudas_xlsx_collect.py     Recoge dudas → JSON /tmp/dudas/<vat>.json
23:39  dudas_xlsx_publish.py     Sube xlsx actualizado a Drive (preserva tu_decision)
23:40  email_summary.py          HTML + xlsx adjunto al usuario
```

**Patrón**: scripts que tocan Drive corren con `automation/venv`; scripts que tocan ORM con `odoo/venv`. Se comunican por JSON en `/tmp` (collect→publish, classify→apply).

---

## 4. Clasificación y enrutado de documentos

`extractor.py` ejecuta Claude headless con un PROMPT que devuelve JSON con campos comunes + extras por tipo. Luego despacha al processor:

| `document_type` | Processor | Tipo asiento | Líneas (cuentas españolas) |
|---|---|---|---|
| invoice | `process_invoice.py` | `in_invoice` (o `in_refund` si total<0) | DR 600/626/629... + CR 410 + tax lines |
| nomina | `nomina_processor.py` | `entry` | DR 640 cash + DR 642 especie / CR 4751 IRPF / CR 476 SS+autónomo / CR 465 líquido |
| irpf_payment | `tax_payment_processor.py` | `entry` | DR 475100 / CR 572 |
| ss_payment | `tax_payment_processor.py` | `entry` | DR 476000 / CR 572 |
| other_official | `tax_payment_processor.py` | `entry` | DR 629000 / CR 572 |
| bank_statement | `bank_importer.py` | statement | crea `account.bank.statement.line` por movimiento |
| sepa | (ignorar — son instrucciones, no asientos) | — | — |

**Reglas críticas grabadas a fuego:**

- **Total negativo en factura proveedor → es rectificativa**: usar `in_refund` con líneas en absoluto. Si se crea `in_invoice` con importe negativo, Odoo no permite postear.
- **Filtro de impuestos domésticos**: el chart l10n_es trae 12 impuestos al 10% (G/S/EX/EU/IG/RC/ND). El default no debe coger el "EX/EU/IG/RC/ND" (extranjero/inversión sujeto pasivo) porque neutraliza IVA. Filtrar por nombre prefijo: `SPECIAL_TAX_PREFIXES = (" EX", " EU", " IG", " RC", " ND")`.
- **TGSS company SS-payment** parece nómina al LLM (lista empleados) → revisar: si hay empleados pero **no hay IRPF retenido por empleado**, es `ss_payment`, no `nomina`.
- **Recibos AEAT mod 111/115** parecen facturas → el prompt los clasifica explícitamente como `irpf_payment`.

---

## 5. Lógica contable (PGC español PYMES)

### Cuentas usadas (reverificar en chart de la nueva empresa)

```
410000  Proveedores
430000  Clientes
465000  Remuneraciones pendientes de pago
475100  HP, acreedora retenciones IRPF       (6 dígitos, NO 4751)
476000  Organismos SS, acreedores             (6 dígitos, NO 4760)
520000  Préstamos cp con entidades
572xxx  Banco (default account del journal)
600000  Compras de mercaderías
625000  Primas de seguros
626000  Servicios bancarios
629000  Otros servicios (placeholder no deducible)
640000  Sueldos y salarios
642000  Seguridad Social a cargo empresa
```

### Asiento de nómina (CARARJFAM, socios autónomos)

Identidad matemática: `bruto = sueldo_cash + salario_especie`, y `bruto - irpf - ss_emp - especie = liquido`.

```
DR 640000 = sueldo_cash    (= bruto - especie, una línea por empleado, partner=empleado)
DR 642000 = especie_total  (suma autónomos socios)
            CR 475100 = irpf_total
            CR 476000 = ss_empleado_total + especie_total  (ambos van a TGSS)
            CR 465000 = liquido (una línea por empleado, partner=empleado)
```

El cargo bancario "TGSS AUTONOMOS" cancela el saldo en 476 (no es gasto nuevo — el gasto se reconoció en 642 al posteo de la nómina). Hay un `learned.rule` `TGSS AUTONOMOS` (source=system, conf=0.98) en cada empresa que mapea a 476000.

### Asientos de impuestos

```
IRPF mod 111/115:   DR 475100 / CR 572     ref: "IRPF mod 111 2025-4T"
SS mensual:         DR 476000 / CR 572
Otro oficial:       DR 629000 / CR 572     (placeholder no-deducible, manual reroute si toca)
```

### Auto-post (umbrales)

- `invoice`: auto-post si `extraction_confidence >= 0.9`. Por debajo → draft → entra al xlsx de dudas.
- `nomina` / `irpf_payment` / `ss_payment` / `other_official`: **siempre** auto-post (documentos autoritativos, math verificable).

---

## 6. Conciliación bancaria (3 pasadas)

### Pasada 1 — `bank_reconciler.py` (1:1, threshold 90)
Para cada línea bancaria sin conciliar, busca un AML abierto único en cuentas operacionales (410/430/465/475/476...) cuyo importe + similitud (ref/partner/concepto) puntúa ≥90. Re-rutea la línea suspense al partner account, luego `(susp + target).reconcile()`.

### Pasada 2 — `bank_multi_reconciler.py` (1:N subset-sum)
Para líneas bancarias todavía sin conciliar, busca un subconjunto (tamaño 2..12) de AMLs abiertos en cuentas agregadoras cuya suma = importe banco ±0.10€. **Caso de uso real**: AEAT paga IRPF trimestral agregando 3 mensualidades en 475100 (1425.63 × 3 = 4276.89). Elige el subset más corto y reconcilia banco + N AMLs juntos.

### Pasada 3 — Near-match (no auto-acción)
`bank_matcher.find_near_matches_for_company()` devuelve líneas con candidato AML cuya diferencia es 0.5€ < diff ≤ 100€. Aparecen en el email diario bajo "⚠ Conciliaciones descuadradas — esperan instrucciones". El usuario decide caso por caso.

**Filtros antifalsos positivos** en near-match:
- Skip si hay una `learned.rule` con conf≥0.85 que cubra la línea (se categorizará automáticamente en otra pasada).
- Requiere similitud de partner ≥0.3 OR primera palabra concepto coincide OR ref-en-concepto. Si solo coincide importe, no es match.

### `apply_rules_to_bank.py` (aplicación agresiva de reglas)
Pasada extra: para cada línea sin conciliar, busca la primera `learned.rule` con `pattern in payment_ref` (uppercase, conf≥0.85), re-rutea suspense a `rule.account_id` y `rule.partner_id`. Idempotente. Implementado para resolver el caso "una regla aprendida debe aplicar a TODAS las líneas similares, no solo a las que dudaron".

---

## 7. Dudas xlsx round-trip (user-in-the-loop)

Artefacto stateful en raíz de carpeta Drive de cada empresa: `dudas_para_revisar.xlsx`.

**Columnas**: `empresa | tipo | id_odoo | fecha | importe | ref_o_concepto | descripcion_corta | motivo_duda | sugerencia_actual | tu_decision | estado_actual | notas`

**Flujo nocturno:**
1. `dudas_apply.py` lee xlsx, para cada fila con `tu_decision` no vacía y no clasificada, llama `classify_decision(text)` → action+label+account_code.
2. Genera `actions.json`, llama `dudas_apply_odoo.py` que ejecuta vía ORM.
3. `dudas_xlsx_collect.py` regenera lista actual de dudas → JSON.
4. `dudas_xlsx_publish.py` mergea: preserva `tu_decision` previa por clave `(tipo, id_odoo, ref)`, añade nuevas filas, reemplaza el xlsx en Drive.

**Acciones que entiende `classify_decision`** (texto libre español, case-insensitive, tolera tildes):

```
"apertura" / "asiento de apertura"  → skip   APERTURA
"neteado"/"neteo"/"siguiente"/"anterior"/"compensado" → skip NETEADO
"comisión banc" / "tarifa plana" / "emision sepa"     → DR 626 COMISION_BANCARIA
"hipoteca" / "préstamo" / "prestamo"                  → DR 520 HIPOTECA
"no deducible"                                        → DR 629 GASTO_NO_DEDUCIBLE + nota
"pago factura ingres" / "cobro factura"               → match contra 430 abierta
"pago seguro" / "seguro anual"                        → DR 625 SEGURO
"falta fact" / "queda como duda" / "mientras se sube" → skip PENDIENTE_FACTURA
"pago nomina"                                         → partial reconcile contra 465
"saldo pendiente" / "diferencia se queda"             → partial reconcile genérico
"liquidacion efectuada"                               → DR 430 COBRO_TPV
"gympass"                                             → DR 430 COBRO_GYMPASS partner=Gympass US LLC
"devolucion de recibo"                                → DR 430 DEVOLUCION_CLIENTE
"emision remesa" / "abona la cuenta de clientes"      → DR 430 COBRO_REMESA_SEPA
otro texto                                            → PENDIENTE_HUMANO (fila amarilla)
```

**Idempotencia**: `already reconciled` se trata como éxito ("ya aplicado en pasada anterior"), no error.

---

## 8. Propagación de reglas (yellow rules apply broadly)

Cada decisión `direct_entry` en `DIRECT_ENTRY_LABELS_TO_PROPAGATE` ejecuta `_propagate_to_similar()`:

1. Deriva un patrón corto de 2 palabras del concepto (`_derive_pattern`): primeras 2 palabras de ≥3 chars que no sean stopwords.
2. Crea `learned.rule` (rule_type=bank, source=passive, conf=0.9) con ese patrón apuntando a la cuenta destino.
3. Walks **todas** las líneas bancarias sin conciliar de la misma empresa cuyo `payment_ref` contiene el patrón → re-rutea suspense a la cuenta destino.

`apply_rules_to_bank.py` (cron 23:37) corre por encima como red de seguridad: aplica TODAS las reglas activas a TODAS las líneas sin conciliar.

**Importante**: la propagación corre **fuera del check de error**, así que aunque la línea fuente esté ya conciliada (segunda pasada), la regla se sigue creando y propagando.

---

## 9. Multi-empresa

Dos empresas en una sola DB Odoo, `company_id` discrimina:

```python
# /opt/automation/companies.py
COMPANIES = [
    {"name": "CARARJFAM2019", "vat": "B93653392", "odoo_company_id": 1,
     "pending_folder": "<drive_folder_id>"},
    {"name": "Best Training Rincón", "vat": "B72349137", "odoo_company_id": 2,
     "pending_folder": "<drive_folder_id>"},
]
```

Cada cron itera sobre `COMPANIES`. Las facturas se crean con `with_company(cid)` para que se asigne al diario contable correcto.

---

## 10. Patrón de dos venvs (CRÍTICO)

`/opt/odoo17/venv` (ORM) y `/opt/automation/venv` (Drive, parsers, email) **NO se mezclan**.

**Por qué**: instalar `google-api-python-client` en el venv de Odoo rompe `pyOpenSSL.X509StoreFlags.NOTIFY_POLICY` (cryptography 3.4.8 vs ≥41 incompatibilidad), y tumba Odoo entero al arrancar.

**Patrón**: cualquier script que toca Drive escribe JSON en `/tmp/<algo>/...` y lanza `subprocess.run(["/opt/odoo17/venv/bin/python", "<script>_odoo.py", "--input", json_in, "--output", json_out])`. Vuelve a leer el resultado.

Ejemplos: `learning_drive.py` + `learning.py`, `dudas_xlsx_collect.py` + `dudas_xlsx_publish.py`, `dudas_apply.py` + `dudas_apply_odoo.py`.

---

## 11. Autenticación / secretos

| Servicio | Cómo |
|---|---|
| SSH al VPS | ed25519 key (`~/.ssh/odoo_carajfam`); password disabled; fail2ban |
| Postgres | peer auth; `process_invoice.py` debe correr como user `odoo` (no root) |
| Claude Code (LLM) | `sudo -u odoo -H claude /login` interactivo una vez; auth queda en `/opt/odoo17/.claude.json`; subprocess con `HOME=/opt/odoo17` |
| Google Drive | Service Account JSON en `/opt/automation/sa_credentials.json`; carpetas compartidas con email de la SA |
| Gmail SMTP | App password 16-char (Gmail → Security → App Passwords); usuario+pass en `/opt/automation/email_config.py` |

**Limitación SA Drive**: no puede CREAR archivos nuevos en Drive personal (cuota 0). Solo puede UPDATE/MOVE archivos existentes. Workaround: el primer xlsx se sube manualmente (o vía MCP del usuario), luego `files().update(media_body=...)` lo refresca.

---

## 12. Configuración a cambiar al migrar

Lista de constantes hardcoded que deberás sustituir en el nuevo entorno:

```python
# Servidor / ERP
ODOO_PATH = "/opt/odoo17/odoo"               # ruta install ERP
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"                        # nombre de la BD

# Drive
DRIVE_SA_JSON = "/opt/automation/sa_credentials.json"
COMPANIES = [
    {"name": "...", "vat": "...", "odoo_company_id": N,
     "pending_folder": "<drive_folder_id>"},  # ID de la carpeta Pendientes/ por empresa
]

# Email
SMTP_HOST = "smtp.gmail.com"; SMTP_PORT = 587
SMTP_USER = "..."; SMTP_PASS = "..."         # app password
EMAIL_TO = "..."

# LLM headless
CLAUDE_HOME = "/opt/odoo17"                  # donde vive .claude.json
CLAUDE_CMD = ["claude", "-p", PROMPT, "--output-format", "text",
              "--permission-mode", "bypassPermissions", "--add-dir", str(folder)]

# Cuentas (verificar en chart del país/empresa)
DOC_TYPE_DEFAULT_ACCOUNT = {
    "invoice": "600000", "nomina": "640000",
    "irpf_payment": "475100", "ss_payment": "642000",  # NB: ss_payment Lo procesa tax_payment con CR 476
    "other_official": "629000",
}
SPECIAL_TAX_PREFIXES = (" EX", " EU", " IG", " RC", " ND")  # filtro IVA doméstico

# Umbrales
AUTO_POST_INVOICE_THRESHOLD = 0.9
NEAR_MATCH_RANGE = (0.5, 100.0)              # diferencia €
RECONCILE_THRESHOLD = 90                     # score 1:1
RULE_CONFIDENCE_MIN = 0.85
SUBSET_SUM_TOLERANCE = 0.10                  # €
SUBSET_SUM_MAX_SIZE = 12
```

**Si el ERP nuevo NO es Odoo**, los puntos de fricción a resolver:

- Reemplazar `Environment(cr, SUPERUSER_ID, ...)` por la API equivalente del ERP nuevo (REST / SDK).
- Reemplazar `learned.rule` por una tabla equivalente (cualquier ERP la admite, o tabla SQL externa).
- Reemplazar `account.bank.statement.line` + suspense account pattern por el equivalente del nuevo ERP. La idea de **re-rutear una línea suspense** y luego reconciliar es genérica, pero la API cambia.
- Reemplazar `account.move` type=entry por el equivalente (asiento manual de diario).
- Si el ERP nuevo no es español, reemplazar todo el plan de cuentas (475100, 476000, 465000, 642 etc) por el del país. La estructura del asiento de nómina sigue siendo válida (gross-cash + autónomo / retención IRPF / SS+autónomo / líquido), solo cambian los códigos.

---

## 13. Pitfalls / lecciones aprendidas en producción

1. **Total negativo de factura proveedor = rectificativa**: nunca crear `in_invoice` con importe negativo. Detectar `total<0` y volcar a `in_refund` con líneas en absoluto.
2. **Charts l10n_es traen 12 impuestos al mismo tipo**: filtrar prefijos especiales o el cálculo de IVA es incorrecto.
3. **Dos venvs siempre**: mezclar Google libs con Odoo rompe pyOpenSSL.
4. **Service Account NO puede crear archivos en Drive personal**: solo update/move. Crear el primer xlsx manualmente o vía OAuth de usuario.
5. **`already reconciled` = éxito, no error**: cualquier pipeline idempotente debe tratarlo así.
6. **Cursor cerrado tras commit**: nunca acceder a campos de records ORM después de `cr.commit()` fuera del `with`. Capturar a variables locales antes.
7. **Propagación fuera del err-check**: si solo propagas en éxito, una segunda pasada con "already reconciled" no aprende. Mover propagación fuera del else.
8. **Validador de nómina**: NO usar `subtotal+tax=total` (eso es factura). Usar `bruto - irpf - ss - especie = liquido`.
9. **TGSS autónomos NO es gasto nuevo**: el cargo bancario cancela 476 acumulado en la nómina, no genera 642 fresh. Reglas mapean a 476.
10. **Cuentas españolas son 6 dígitos**: 475100 / 476000 — buscar exact match con 4 dígitos no encuentra nada.
11. **Auto-publicar nóminas siempre**: el usuario las quiere posted aunque la confianza sea baja, porque son verificables matemáticamente.
12. **Edit con caracteres ñ/tildes a veces falla en Edit tool**: si una replace silenciosa no aplica, usar otro anchor sin acentos.
13. **El xlsx del usuario se pisa fácilmente**: leer ANTES de regenerar, mergear preservando `tu_decision` por clave `(tipo, id_odoo, ref)`.
14. **Pregunta una a una**: este usuario odia checklists numerados. Una pregunta, esperar respuesta, siguiente.
15. **Near-match con solo importe es falso positivo**: requerir similitud partner/concepto además de importe-cercano.

---

## 14. Inventario de archivos en `/opt/automation/`

```
extractor.py              Orquestador principal: scan Drive → clasificar → ruta processor
process_invoice.py        Crea facturas in_invoice / in_refund con líneas y taxes
nomina_processor.py       Asiento de nómina DR 640+642 / CR 4751+476+465
tax_payment_processor.py  Asiento IRPF/SS/oficial DR 4751/476 / CR 572
bank_importer.py          Parsea CSV (Caixa) / .xls (Santander) / N43 → bank.statement.line
bank_matcher.py           Propone matches 1:1, near-matches, AML abiertos
bank_reconciler.py        Aplica matches 1:1 score>=90
bank_multi_reconciler.py  1:N subset-sum (IRPF trimestral)
apply_rules_to_bank.py    Aplica TODAS las learned.rule a TODAS líneas (red de seguridad)
dudas_xlsx_collect.py     Recoge dudas actuales → JSON /tmp
dudas_xlsx_publish.py     Sube xlsx a Drive (preserva tu_decision)
dudas_apply.py            Lee xlsx, classify_decision, build actions.json
dudas_apply_odoo.py       Ejecuta actions vía ORM (con propagación)
learning_drive.py         Descarga aprendizajes/dudas CSV de raíz Drive
learning.py               Importa CSV → learned.rule + scan facturas posted
poller.py                 Cleanup queue legacy
email_summary.py          HTML email diario + xlsx adjunto
companies.py              Config multi-empresa (cid, vat, drive_folder)
sa_credentials.json       Service Account JSON (no commit)
email_config.py           Credenciales SMTP (no commit)
```

```
/opt/odoo17/custom-addons/learned_rules/
  models/learned_rule.py  Modelo Odoo learned.rule
  views/learned_rule.xml  Vista admin
  __manifest__.py
```

---

## 15. Plantilla de prompt para retomar en otro Claude Code

> Estoy migrando una automatización contable nocturna de Odoo a **<NUEVO ERP>**, en VPS **<NUEVO HOST>**, con carpetas Drive **<NUEVOS IDS>**. Adjunto el HANDOFF.md del sistema actual.
>
> El sistema actual hace: clasificación de PDFs con LLM → asientos correctos por tipo (factura/nómina/IRPF/SS) → conciliación bancaria 3 pasadas → dudas xlsx round-trip con el usuario → propagación de reglas aprendidas. Detalles completos en el HANDOFF.
>
> Quiero replicar la misma lógica funcional en el nuevo entorno. Empieza por:
>
> 1. Confirmar el plan de cuentas del nuevo ERP (qué cuentas reemplazan 410/430/465/475100/476000/520/572/600/625/626/629/640/642).
> 2. Decidir cómo se modela `learned.rule` en el nuevo ERP (tabla custom, módulo, o tabla externa).
> 3. Mapear la API del nuevo ERP a las operaciones que usamos: crear factura, crear asiento manual, importar extracto, reconciliar par, search AML abiertos por cuenta.
> 4. Confirmar credenciales Drive (Service Account o OAuth de usuario) y carpetas Pendientes/ por empresa.
>
> Pregúntame **una cosa cada vez**, sin checklists numerados.

---

Fin handoff.
