# Handoff — Automatización contable AUSTRAL

Documento para retomar en otro chat de Claude Code el setup de la automation pipeline para la empresa **AUSTRAL** sin mezclarla con el resto del proyecto (CARARJFAM / BT).

---

## 0. Objetivo

Montar un pipeline de automation **aislado** para AUSTRAL que:

- Lea documentos de la carpeta Drive `Mi Odoo AUSTRAL`
- Cree facturas/nóminas/asientos en la BD Odoo `cararjfam_test` bajo `company_id=3`
- No interfiera con `/opt/automation/` (que apunta a la BD productiva `cararjfam`, empresas 1 y 2)
- Pueda probarse a mano antes de meterlo en cron

El pipeline existente para CARARJFAM/BT se queda **intacto**; este es un segundo pipeline paralelo.

---

## 1. Servidor

| Dato | Valor |
|---|---|
| Host | `erp.carajfam.com` (también `demo.carajfam.com`) |
| IP | `212.227.40.122` (IONOS, Ubuntu 24.04) |
| SSH | `ssh -i ~/.ssh/odoo_carajfam root@212.227.40.122` (ed25519, password disabled) |
| Subdomain demo | `https://demo.carajfam.com` ya configurado con HTTPS Let's Encrypt + nginx |

Existe un único servicio `odoo17` corriendo, sirve **3 BDs**:

| BD | Para qué |
|---|---|
| `cararjfam` | Producción (CARARJFAM2019,SL + BEST TRAINING) — **NO TOCAR** |
| `cararjfam_test` | Sandbox + AUSTRAL (este pipeline trabaja aquí) |
| `demo` | BD vacía (sin uso real) |

- Odoo 17 Community en `/opt/odoo17/odoo`
- Config: `/etc/odoo17.conf`
- Venv Odoo: `/opt/odoo17/venv`
- Custom addons: `/opt/odoo17/custom-addons/` (incluye `learned_rules`)
- Master password (Odoo db manager): `9c0391G2bGCmO6LSwUmgnMtECFZH`

---

## 2. AUSTRAL — datos concretos

### Empresa en Odoo

```
DB:           cararjfam_test
company_id:   3
name:         AUSTRAL
vat:          ESB44821965
```

### Usuario que la usará

```
login:    contabilidad@austral.es
nombre:   ELENA AUSTRAL
id:       6
empresas: solo AUSTRAL (no ve CARARJFAM ni BT)
```

### Carpeta Drive

```
folder_id:  1SNDTko-SgeYNjyJ-_635ObprBDVWm-Jd
nombre:     "Mi Odoo AUSTRAL"
url:        https://drive.google.com/drive/folders/1SNDTko-SgeYNjyJ-_635ObprBDVWm-Jd
```

Subcarpetas ya creadas:
- `Contabilizado odoo` — destino de PDFs procesados OK
- `revision` — destino de fallos
- `Aprendizajes_aplicados` — CSVs de reglas que ELENA pueda enseñar manualmente
- `Cola_VPS` — legacy, sin uso
- `csv odoo` — sin uso para el pipeline

La raíz **"Mi Odoo AUSTRAL"** actúa como **Pendientes** (donde ELENA sube documentos).

Service Account ya tiene acceso a la carpeta:
```
SA email: vps-odoo-automation@carajfam-automation.iam.gserviceaccount.com
SA creds: /etc/automation_sa.json
```

---

## 3. Pipeline existente (referencia, no tocar)

`/opt/automation/` contiene el pipeline original para CARARJFAM/BT:

| Script | Función |
|---|---|
| `extractor.py` | Clasifica PDFs con Claude headless → enruta al processor adecuado |
| `process_invoice.py` | Crea facturas (in_invoice / in_refund) |
| `nomina_processor.py` | Asientos de nómina (DR 640+642 / CR 4751+476+465) |
| `tax_payment_processor.py` | IRPF / SS / impuestos (DR 4751/476 / CR 572) |
| `bank_importer.py` | Importa CSV/XLS/N43 → `account.bank.statement.line` |
| `bank_matcher.py` | Propone matches 1:1 + AML abiertos |
| `bank_reconciler.py` | Concilia 1:1 score≥90 |
| `bank_multi_reconciler.py` | 1:N subset-sum |
| `apply_rules_to_bank.py` | Aplica `learned.rule` a todas líneas no conciliadas |
| `dudas_apply.py` + `dudas_apply_odoo.py` | User-in-the-loop xlsx → ORM |
| `dudas_xlsx_collect.py` + `dudas_xlsx_publish.py` | Genera/publica xlsx en Drive |
| `email_summary.py` | Email diario con HTML + xlsx adjunto |
| `learning_drive.py` + `learning.py` | Importa reglas desde CSV de Drive + auto-aprende |
| `companies.py` | Config multi-empresa (CARARJFAM + BT) |

Constantes hardcoded clave en TODOS los scripts:
```python
DB_NAME = "cararjfam"   # ← este pipeline está atado a la prod
ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
```

Cron actual (usuario `odoo`, hora Madrid):
```
23:23 learning_drive
23:25 learning --mode both
23:30 extractor
23:35 poller (legacy)
23:36 dudas_apply
23:37 apply_rules_to_bank + bank_reconciler --threshold 90
23:38 bank_multi_reconciler + dudas_xlsx_collect
23:39 dudas_xlsx_publish
23:40 email_summary
```

**No tocar este pipeline. AUSTRAL va aparte.**

---

## 4. Patrón de dos venvs (CRÍTICO)

```
/opt/odoo17/venv       # Odoo + psycopg2 + pyOpenSSL 21 (ORM)
/opt/automation/venv   # Google API + Flask + openpyxl + pandas + xlrd + csb43 (Drive/parsers)
```

NUNCA mezclar Google libs en el venv de Odoo: rompe `pyOpenSSL.X509StoreFlags` y tumba Odoo.

Comunicación entre scripts:
- Drive ops: `/opt/automation/venv/bin/python <script>`
- ORM ops: `/opt/odoo17/venv/bin/python <script>`
- Pasan datos por JSON en `/tmp/`

Para AUSTRAL puedes **reutilizar los mismos dos venvs** (no necesitas duplicarlos).

---

## 5. Lo que hay que hacer para AUSTRAL

### 5.1 Crear `/opt/automation_austral/`

Copia los scripts de `/opt/automation/` excluyendo `venv/`:

```bash
sudo cp -r /opt/automation /opt/automation_austral
sudo rm -rf /opt/automation_austral/venv
sudo chown -R odoo:odoo /opt/automation_austral
```

### 5.2 Cambiar `DB_NAME` en TODOS los scripts

```bash
sudo sed -i 's|DB_NAME = "cararjfam"|DB_NAME = "cararjfam_test"|g' /opt/automation_austral/*.py
```

Verifica que no quede ninguna referencia a `cararjfam` (sin `_test`):
```bash
grep -rn '"cararjfam"' /opt/automation_austral/*.py
```

### 5.3 Reescribir `companies.py`

Reemplazar el contenido por solo AUSTRAL:

```python
COMPANIES = [
    {
        "name": "AUSTRAL",
        "vat": "ESB44821965",
        "odoo_company_id": 3,
        "pending_folder": "1SNDTko-SgeYNjyJ-_635ObprBDVWm-Jd",
        "contabilizado_folder": "<id de subcarpeta 'Contabilizado odoo'>",
        "revision_folder": "<id de subcarpeta 'revision'>",
        "aprendizajes_folder": "<id de subcarpeta 'Aprendizajes_aplicados'>",
    },
]
```

Para obtener los IDs de las subcarpetas, ejecuta:
```bash
sudo -u odoo /opt/automation/venv/bin/python -c "
import sys; sys.path.insert(0, '/opt/automation')
from drive_ops import _service
svc = _service()
fid = '1SNDTko-SgeYNjyJ-_635ObprBDVWm-Jd'
res = svc.files().list(q=f\"'{fid}' in parents and trashed=false\", fields='files(id,name)', supportsAllDrives=True, includeItemsFromAllDrives=True).execute()
for f in res.get('files', []):
    print(f['name'], '=', f['id'])
"
```

### 5.4 Reusar credenciales y SMTP

- SA Drive: `/etc/automation_sa.json` (mismo)
- SMTP Gmail: el mismo (config probablemente en `/opt/automation_austral/email_config.py` — copiar tal cual)
- Email destinatario: pendiente de decidir con el usuario (¿el mismo `c.alcalde.campusport@gmail.com` o uno de AUSTRAL?)

### 5.5 Logs separados

```bash
sudo mkdir -p /var/log/automation_austral
sudo chown odoo:odoo /var/log/automation_austral
```

Edita los scripts para escribir ahí en vez de `/var/log/automation/`.

### 5.6 NO añadir al cron todavía

Ejecutar TODO manualmente las primeras veces hasta validar:
```bash
sudo -u odoo /opt/automation/venv/bin/python /opt/automation_austral/extractor.py
sudo -u odoo /opt/odoo17/venv/bin/python /opt/automation_austral/dudas_apply.py
# etc.
```

Cuando funcione bien, añadir al cron con horarios distintos (ej. 23:50–00:05) para no solapar con la pipeline productiva.

---

## 6. Lógica contable española (ya implementada en los scripts)

Cuentas usadas (l10n_es PYMES, todas existen tras instalar `l10n_es` y `l10n_es_aeat`):

```
410000  Proveedores
430000  Clientes
465000  Remuneraciones pendientes pago
475100  HP, retenciones IRPF (NO 4751, 6 dígitos)
476000  Org SS acreedores (NO 4760)
477000  HP, IVA repercutido
520000  Préstamos cp
572xxx  Banco (default journal)
600000  Compras
625000  Seguros
626000  Servicios bancarios
629000  Otros (placeholder no deducible)
640000  Sueldos
642000  SS empresa
```

**Nómina** (devengo): `bruto = sueldo_cash + salario_especie`, y `bruto - irpf - ss_emp - especie = liquido`.

```
DR 640 = sueldo_cash (= bruto - especie, una linea por empleado)
DR 642 = especie_total (autónomo socios — solo aplica si hay socios autónomos)
            CR 475100 = irpf_total
            CR 476000 = ss_empleado + especie
            CR 465000 = liquido (una línea por empleado)
```

**Auto-post**:
- `invoice`: confidence ≥ 0.9
- `nomina` / `irpf_payment` / `ss_payment`: siempre

---

## 7. Decisiones pendientes con el usuario para AUSTRAL

1. **¿Diario bancario?** ¿AUSTRAL ya tiene IBAN configurado en Odoo? (Configuración → Diarios → Banco)
2. **¿Importará extractos manualmente?** (CSV/XLS de Caixa/Santander/etc.) o solo se usará reconciliación pasiva
3. **¿Recibirá email diario?** ¿A qué dirección? (¿`contabilidad@austral.es` o el mismo de los otros?)
4. **¿Tiene socios autónomos?** Esto determina si las nóminas usan `salario_especie` (afecta a `nomina_processor.py`)
5. **¿Plantilla `dudas_para_revisar.xlsx`?** Se crea en la raíz de la carpeta Drive vía script — el primer publish la genera

---

## 8. Pitfalls aprendidos del pipeline original

(Aplican igual al de AUSTRAL):

1. **Total negativo factura proveedor → es rectificativa**: usar `in_refund`, líneas en absoluto. NO `in_invoice` con importe negativo (Odoo rechaza).
2. **Charts l10n_es traen 12 impuestos al mismo tipo**: filtrar `SPECIAL_TAX_PREFIXES = (" EX", " EU", " IG", " RC", " ND")` o se cogen IVA extranjero/inversión sujeto pasivo y neutralizan IVA.
3. **`reconcile=True` en cuentas 465/475100/476000/477000**: hay que habilitar manualmente tras cargar el chart, si no las reconciliaciones fallan.
4. **`already reconciled` = éxito, no error** (idempotencia de pipeline).
5. **Cursor cerrado tras commit**: nunca acceder campos de records ORM después de `cr.commit()` — capturar valores antes.
6. **Service Account no puede CREAR archivos en Drive personal** (cuota 0). Solo update/move. Crear el primer xlsx manualmente desde la cuenta del propietario, luego SA lo refresca con `update(media_body=...)`.
7. **HEIC images**: Claude headless no lee HEIC directo bien — convertir a JPG previamente o que el usuario suba JPG.
8. **Gmail app password 16-char** (Security → App Passwords). NO la password normal.
9. **Detección duplicados** en `process_invoice.py`: clave `(partner + ref + invoice_date + company_id)`. Si re-subes la misma factura → marca duplicate y mueve a Contabilizado sin crear.

---

## 9. Plantilla de prompt para arrancar otro chat

> Tengo este HANDOFF_AUSTRAL.md (adjunto). Quiero **montar el pipeline de automation para la empresa AUSTRAL** en el servidor 212.227.40.122, BD `cararjfam_test`, company_id=3, carpeta Drive "Mi Odoo AUSTRAL".
>
> El pipeline para CARARJFAM/BT ya existe en `/opt/automation/` y funciona — **no tocar**. Necesito un pipeline gemelo en `/opt/automation_austral/` apuntando a la nueva BD/empresa/carpeta, con logs separados, sin cron de momento.
>
> Empieza por:
> 1. SSH al servidor con `~/.ssh/odoo_carajfam`.
> 2. Clonar `/opt/automation/` → `/opt/automation_austral/` (sin venv).
> 3. Ajustar `DB_NAME`, `companies.py`, log dirs.
> 4. Pedirme las decisiones pendientes (sección 7) **una a una** (preferencia del usuario, no checklists numerados).
> 5. Smoke test manual antes de cron.

Fin handoff.
