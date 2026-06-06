# Disaster Recovery — CARARJFAM Odoo VPS

Pasos exactos para reconstruir el servidor desde un backup en caso de pérdida total. Ordenados, sin omisiones.

---

## 0. Antes de empezar

Necesitas:

- Un VPS nuevo Ubuntu 24.04 (recomendado IONOS o similar, mismo proveedor para que la IP sea fácil de cambiar en DNS)
- Acceso SSH como root con clave ed25519
- El backup más reciente descargado de Drive: `backup_<DIA>.tar.gz` (de la carpeta `Mi Odoo CARARJFAM`)
- Acceso al panel Cloudflare para apuntar `erp.carajfam.com` (y `demo.carajfam.com` si lo usabas) a la IP nueva
- Acceso a la cuenta Google Cloud Console del proyecto `carajfam-automation` (por si hay que regenerar Service Account credentials)
- App password Gmail vigente (si no, generar una nueva en Security → App passwords)

Tiempo total: **2–3 horas** en condiciones normales.

---

## 1. Provisión del VPS y SSH

### 1.1 Crea el VPS
- Ubuntu Server 24.04 LTS
- Mínimo 2 vCPU, 4 GB RAM, 40 GB SSD
- Asegúrate de tener IPv4 pública

### 1.2 Configura SSH
```bash
ssh root@<NEW_IP>
mkdir -p /root/.ssh && chmod 700 /root/.ssh
echo "<TU CLAVE PUBLICA ed25519>" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys
# Deshabilitar password auth
sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh
```

### 1.3 Hardening básico
```bash
apt update && apt upgrade -y
apt install -y ufw fail2ban
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp
ufw --force enable
systemctl enable --now fail2ban
```

---

## 2. Apunta el DNS al servidor nuevo

En Cloudflare:
- `erp.carajfam.com` (A) → nueva IP, **DNS only (gris)** mientras certbot trabaja
- `demo.carajfam.com` (A) → nueva IP, **DNS only**

Espera 1–2 minutos de propagación. Verifica: `dig +short erp.carajfam.com` desde el VPS nuevo debe devolver tu IP.

---

## 3. Instala dependencias del sistema

```bash
apt install -y \
  postgresql postgresql-contrib \
  nginx certbot python3-certbot-nginx \
  python3.11 python3.11-venv python3.11-dev \
  build-essential libpq-dev libxml2-dev libxslt1-dev \
  libldap2-dev libsasl2-dev libssl-dev libffi-dev \
  libjpeg-dev zlib1g-dev wkhtmltopdf \
  git curl wget unzip tar
```

Verifica:
```bash
python3.11 --version    # 3.11.x
psql --version          # 14+ o 16+
nginx -v
wkhtmltopdf --version   # 0.12.6+
```

### 3.1 Instala Claude Code CLI (para extractor LLM)

```bash
curl -fsSL https://claude.ai/install.sh | bash
# o sigue las instrucciones de https://claude.ai/code-installer
which claude    # debe estar en /usr/local/bin/claude o similar
```

Tras instalar, se autenticará luego como user `odoo` (paso 8).

---

## 4. Crea usuario sistema `odoo` y postgres user

```bash
adduser --system --group --home /opt/odoo17 --shell /bin/bash odoo
sudo -u postgres psql -c "CREATE USER odoo WITH CREATEDB SUPERUSER;"
```

---

## 5. Instala Odoo 17 Community

```bash
sudo -u odoo bash << 'EOF'
cd /opt/odoo17
git clone --depth=1 -b 17.0 https://github.com/odoo/odoo.git
python3.11 -m venv venv
venv/bin/pip install --upgrade pip wheel
venv/bin/pip install -r odoo/requirements.txt
mkdir -p /opt/odoo17/custom-addons /opt/odoo17/.local/share/Odoo
EOF
```

---

## 6. Restaura datos del backup

### 6.1 Descarga y descomprime
```bash
mkdir -p /tmp/restore
# Sube backup_<DIA>.tar.gz a /tmp/restore/ (vía scp desde tu PC)
cd /tmp/restore
tar xzf backup_<DIA>.tar.gz
ls -la
# Deberías ver:
#   db.dump
#   filestore.tar.gz
#   custom-addons.tar.gz
#   automation.tar.gz
#   configs.tar.gz
#   secrets.tar.gz
#   crontab_odoo.txt
#   RECOVERY.md          (este mismo archivo)
```

### 6.2 Restaura BD Postgres

```bash
sudo -u postgres createdb -O odoo cararjfam
sudo -u postgres pg_restore -d cararjfam --no-owner --no-acl /tmp/restore/db.dump
# Reasigna ownership (importante o Odoo no podrá leer las tablas)
sudo -u postgres psql -d cararjfam -c "
DO \$\$
DECLARE r record;
BEGIN
  FOR r IN SELECT schemaname, tablename FROM pg_tables WHERE schemaname='public' LOOP
    EXECUTE format('ALTER TABLE %I.%I OWNER TO odoo', r.schemaname, r.tablename);
  END LOOP;
  FOR r IN SELECT schemaname, sequencename FROM pg_sequences WHERE schemaname='public' LOOP
    EXECUTE format('ALTER SEQUENCE %I.%I OWNER TO odoo', r.schemaname, r.sequencename);
  END LOOP;
  FOR r IN SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='v' AND n.nspname='public' LOOP
    EXECUTE format('ALTER VIEW %I.%I OWNER TO odoo', r.nspname, r.relname);
  END LOOP;
END\$\$;
"
```

### 6.3 Restaura filestore

```bash
sudo tar xzf /tmp/restore/filestore.tar.gz -C /opt/odoo17/.local/share/Odoo/
sudo chown -R odoo:odoo /opt/odoo17/.local/share/Odoo/filestore
```

### 6.4 Restaura custom addons + automation scripts

```bash
sudo tar xzf /tmp/restore/custom-addons.tar.gz -C /opt/odoo17/custom-addons/
sudo chown -R odoo:odoo /opt/odoo17/custom-addons

sudo tar xzf /tmp/restore/automation.tar.gz -C /opt/
sudo chown -R odoo:odoo /opt/automation
```

### 6.5 Restaura configs y secretos

```bash
sudo tar xzf /tmp/restore/configs.tar.gz -C /
sudo tar xzf /tmp/restore/secrets.tar.gz -C /
# Permisos correctos en secretos
chmod 600 /etc/automation_sa.json
chmod 600 /opt/automation/email_config.py
```

---

## 7. Crea venvs y dependencias Python

### 7.1 Venv Odoo (ya creado en paso 5, solo verifica)

```bash
sudo -u odoo /opt/odoo17/venv/bin/python -c "import odoo; print('OK')"
```

### 7.2 Venv automation (Drive + parsers)

```bash
sudo -u odoo bash << 'EOF'
python3.11 -m venv /opt/automation/venv
/opt/automation/venv/bin/pip install --upgrade pip wheel
/opt/automation/venv/bin/pip install \
  google-api-python-client google-auth google-auth-httplib2 \
  pandas openpyxl xlrd csb43 \
  requests flask
EOF
```

---

## 8. Autentica Claude Code como user `odoo`

```bash
sudo -u odoo -H claude /login
# Sigue el flujo: copia URL, abre en navegador, pega token
# El token queda en /opt/odoo17/.claude.json
```

Verifica:
```bash
sudo -u odoo bash -c "HOME=/opt/odoo17 claude --version"
```

---

## 9. Configura systemd para Odoo

Crea `/etc/systemd/system/odoo17.service`:

```ini
[Unit]
Description=Odoo 17
After=network.target postgresql.service

[Service]
Type=simple
User=odoo
Group=odoo
ExecStart=/opt/odoo17/venv/bin/python /opt/odoo17/odoo/odoo-bin -c /etc/odoo17.conf
StandardOutput=append:/var/log/odoo/odoo17.log
StandardError=append:/var/log/odoo/odoo17.log
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
mkdir -p /var/log/odoo && chown odoo:odoo /var/log/odoo
systemctl daemon-reload
systemctl enable --now odoo17
sleep 5
systemctl status odoo17    # debe estar active (running)
```

---

## 10. Configura nginx y certificados

### 10.1 Restaura los vhosts

Los archivos en `/etc/nginx/sites-available/odoo` y `demo` deben venir del backup (paso 6.5). Si no, recrearlos según `ARQUITECTURA_CARAJFAM.md`.

```bash
ln -sf /etc/nginx/sites-available/odoo /etc/nginx/sites-enabled/odoo
ln -sf /etc/nginx/sites-available/demo /etc/nginx/sites-enabled/demo
nginx -t
```

### 10.2 Re-emite certificados Let's Encrypt

Las claves del backup pueden estar caducadas. Lo más limpio es regenerar:

```bash
# Borra los del backup (que vienen con la IP vieja)
rm -rf /etc/letsencrypt/live/erp.carajfam.com /etc/letsencrypt/archive/erp.carajfam.com
rm -rf /etc/letsencrypt/live/demo.carajfam.com /etc/letsencrypt/archive/demo.carajfam.com

# Re-emite
certbot --nginx -d erp.carajfam.com -d demo.carajfam.com \
  --non-interactive --agree-tos --email c.alcalde.campusport@gmail.com --redirect
systemctl reload nginx
```

Verifica:
```bash
curl -I https://erp.carajfam.com    # debe responder 200/302/303
curl -I https://demo.carajfam.com
```

---

## 11. Restaura el cron

```bash
crontab -u odoo /tmp/restore/crontab_odoo.txt
crontab -u odoo -l    # verifica que las 12 líneas están (11 pipeline + 1 backup)
```

### 11.1 Sudoers para que el backup haga `pg_dump`

```bash
echo 'odoo ALL=(postgres) NOPASSWD: /usr/bin/pg_dump' > /etc/sudoers.d/odoo_pgdump
chmod 440 /etc/sudoers.d/odoo_pgdump
visudo -c    # debe pasar sin errores
```

Verifica que el backup nocturno funciona:
```bash
sudo -u odoo /opt/automation/venv/bin/python /opt/automation/backup_to_drive.py 2>&1 | tail -5
# Debe acabar con: {"status": "ok", "day": "<DIA>", "size_bytes": ...}
```

### 11.2.0 Hojas extra en `dudas_para_revisar.xlsx`

Además de la hoja "Dudas" original, el publish añade:

| Hoja | Origen de datos | Edición usuario |
|---|---|---|
| **Duplicados** | `/tmp/extractor_runs/<fecha>.json` (últimos 4 días) — duplicates del extractor | solo lectura |
| **Rechazados** | mismo JSON — errors (orm rc=10/30/40) | columna `tu_decision` editable |
| **Gastos_periodicos** | `/tmp/periodic/<vat>_periodic.json` | solo lectura |

#### Verbos reconocidos en `tu_decision` de la hoja **Rechazados**

| Texto en `tu_decision` | Acción |
|---|---|
| `borrar` / `eliminar` / `tirar` | Mueve PDF a papelera de Drive |
| `ignorar` / `omitir` / `saltar` / `dejar` | Mueve a subcarpeta `ignorados/` |
| `reprocesar` / `volver a procesar` / `intentar de nuevo` | Mueve de vuelta a `Pendientes/` para que el extractor lo intente otra vez |
| `cif <vat>` o texto con un CIF/NIF español detectable (regex `[A-Z]?\d{7,8}[A-Z]?`) | **APRENDE**: crea `learned.rule(rule_type="vat_correction")` con `pattern=<partner_name>` y `notes=<vat>`, actualiza el partner si existe, y mueve el PDF a Pendientes/. La próxima vez que el extractor vea ese mismo proveedor, sustituye el VAT incorrecto por el guardado |

El procesamiento corre en `dudas_apply.py` (Drive ops in-place) + `dudas_apply_odoo.py` (`_create_vat_correction` para el ORM).

#### Importante para restore

- Tras restaurar custom-addons, asegúrate de que `learned_rules/__manifest__.py` tiene `version >= "17.0.1.1.0"` (versión que añadió `vat_correction` al Selection del modelo).
- Si la BD viene de un dump anterior, levanta Odoo con `-u learned_rules --stop-after-init` para que registre el nuevo rule_type en el Selection.
- `process_invoice.py` consulta `learned.rule(vat_correction)` ANTES de la validación VAT (`base_vat`); si encuentra match por `pattern in supplier_name`, sustituye el VAT extraído por el guardado.
- `dudas_apply.py` ya **NO** sobrescribe el xlsx — eso lo hace `dudas_xlsx_publish.py` para preservar todas las hojas y las `tu_decision` que el usuario haya escrito.

### 11.2.0.-4 Migración BT cararjfam → round_facturacion (jun 2026)

**Contexto**: BT (Best Training Rincón de la Victoria, CIF B72349137) operaba duplicado:
- COMPRAS (in_invoice + nóminas + IRPF/SS + bancos) en BD `cararjfam` (company_id=2)
- VENTAS (out_invoice de la facturación Round) en BD `round_facturacion` (company_id=3)

**Consolidación realizada**: todo BT vive ahora en `round_facturacion`.

#### Estado tras migración

| Sistema | Antes | Después |
|---|---|---|
| BT en `cararjfam` (company_id=2) | activa | **archivada** (`active=False`) |
| BT en `round_facturacion` (company_id=3) | solo ventas | **completa**: ventas + 708 moves migrados (97 in_invoice/refund + 611 entries: nóminas, bancos, impuestos) |
| `/opt/automation/` pipeline | CARARJFAM + BT | **solo CARARJFAM** |
| `/opt/automation_bt_round/` pipeline | no existía | **nuevo**, apunta a `round_facturacion` company_id=3 |
| Cron BT | 23:30 con cararjfam | **00:23–00:40** apuntando a round_facturacion |
| `austral-contab-web` app.db empresa id=3 | `odoo_db=cararjfam, company_id=2` | **`odoo_db=round_facturacion, company_id=3, pipeline_dir=/opt/automation_bt_round`** |

#### Restore de la migración

Si tras restore las BDs no incluyen los moves migrados:

1. Verifica que `/opt/odoo17/custom-addons/learned_rules/` está instalado en `round_facturacion`:
   ```bash
   sudo -u odoo /opt/odoo17/venv/bin/python /opt/odoo17/odoo/odoo-bin -c /etc/odoo17.conf -d round_facturacion -i learned_rules --stop-after-init --no-http
   ```
2. Re-ejecuta el migrador (idempotente — saltará lo ya migrado):
   ```bash
   sudo -u odoo /opt/odoo17/venv/bin/python /opt/automation/_migrate_bt_to_round.py
   ```
3. Fix de move_type (convierte las facturas migradas como `entry` → `in_invoice`/`in_refund` original):
   ```bash
   sudo -u odoo /opt/odoo17/venv/bin/python /opt/automation/_fix_invoice_move_type.py
   ```
4. Archiva BT en cararjfam:
   ```python
   env["res.company"].browse(2).write({"active": False})
   ```
5. Actualiza `austral-contab-web` app.db:
   ```sql
   UPDATE empresa SET odoo_db='round_facturacion', odoo_company_id=3, pipeline_dir='/opt/automation_bt_round'
   WHERE id=3 AND clave='bt';
   ```
6. Aplica isolation guard incluyendo `/opt/automation_bt_round/`:
   ```bash
   sudo -u odoo /opt/automation/_isolate_pipelines.py
   ```

#### Pipeline `/opt/automation_bt_round/` — config

- `companies.py`: `PIPELINE_NAME='bt_round'`, `DB_NAME='round_facturacion'`, `EXPECTED_VATS={'B72349137'}`
- Mismas folder_ids Drive que tenía BT en `/opt/automation/companies.py` antes
- `DB_NAME = "round_facturacion"` sed-replaced en todos los scripts del pipeline
- Logs en `/var/log/automation_bt_round/`
- Cron entries: 00:23 learning_drive → 00:25 learning → 00:30 extractor → 00:36 dudas_apply → 00:37 apply_rules + bank_reconciler → 00:38 multi_reconciler + xlsx_collect → 00:39 xlsx_publish → 00:40 email_summary

### 11.2.0.-3.5 Aplicación web Austral Contab (austral.carajfam.com)

Proyecto separado en `/opt/austral-contab-web/` que ofrece dashboards de contabilidad para AUSTRAL + CARARJFAM + BT. Es un SaaS multi-tenant:

| Path | Contenido |
|---|---|
| `/opt/austral-contab-web/backend/` | Flask app (puerto 5000) |
| `/opt/austral-contab-web/backend/data/app.db` | SQLite con users, sessions, empresa registry |
| `/var/www/austral/` | SPA React build |
| `/etc/nginx/sites-enabled/austral.carajfam.com` | nginx vhost (HTTPS Let's Encrypt) |
| `austral-contab-web.service` | systemd unit |

**Tabla `empresa` en app.db** mapea cada cliente a su BD Odoo:
```
id | clave     | nombre                            | odoo_db           | odoo_company_id | pipeline_dir
---+-----------+-----------------------------------+-------------------+-----------------+----------------
 1 | austral   | International Austral Sport, S.A. | cararjfam_test    | 4               | /opt/automation_austral
 2 | cararjfam | CARARJFAM2019,SL                  | cararjfam         | 1               | /opt/automation
 3 | bt        | Best Training Rincon...           | round_facturacion | 3               | /opt/automation_bt_round
```

Si cambia el mapeo Odoo de cualquier empresa: hay que actualizar la fila correspondiente en `app.db`. Backend cachea — `systemctl restart austral-contab-web` tras el UPDATE.

### 11.2.0.-3 Aislamiento entre pipelines multi-empresa (defensa final)

Tras el bug del 1-jun-2026 (sys.path cruzado), se aplican **4 capas defensivas** para que ningún script pueda procesar documentos de una empresa que no le corresponde:

| Capa | Implementación |
|---|---|
| **1. Metadata en `companies.py`** | Cada pipeline (`/opt/automation/` y `/opt/automation_austral/`) declara en su `companies.py`: `PIPELINE_NAME`, `DB_NAME`, `EXPECTED_VATS` (frozenset). Inyectado por `/opt/automation/_isolate_pipelines.py` (idempotente). |
| **2. `sys.path` auto-referencial** | Cada script: `_HERE = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, _HERE)`. Garantiza que `import companies` carga el `companies.py` DE SU MISMA carpeta, no del pipeline hermano. |
| **3. Guard pipeline en runtime** | Cada script comprueba `comp.PIPELINE_NAME == EXPECTED` (literal hardcoded por pipeline). Si difiere → `RuntimeError(PIPELINE_MISMATCH: ...)` ABORTA antes de tocar Drive/BD. |
| **4. Cron con `cd` explícito** | Toda entrada del crontab del usuario `odoo` empieza con `cd /opt/automation && ...` o `cd /opt/automation_austral && ...`. Defensa de runtime context. |

**Para aplicar tras restore** o tras añadir un pipeline nuevo:
```bash
sudo -u odoo python3 /opt/automation/_isolate_pipelines.py
```
Idempotente — borra el guard previo y lo re-inyecta. Re-aplicarlo tras editar manualmente cualquier script.

**Para añadir una empresa nueva** (ej. Cliente Z):
1. Crear `/opt/automation_clientez/` con copia de scripts
2. Editar `companies.py` con `PIPELINE_NAME='clientez'`, `DB_NAME='...'`, `EXPECTED_VATS={...}`
3. Editar el diccionario `PIPELINES` en `_isolate_pipelines.py` añadiendo la nueva ruta
4. Re-aplicar `_isolate_pipelines.py`
5. Añadir entradas al cron con `cd /opt/automation_clientez && ...`

### 11.2.0.-2 Bug post-mortem: extractor saltó CARARJFAM/BT durante 3 días

**Fecha**: detectado 1-jun-2026 (bug introducido ~28/29-may-2026 al desplegar AUSTRAL).

**Síntoma**: `Foto.pdf` (y otros archivos) acumulados en Pendientes/ de CARARJFAM sin procesar. Logs de cron 23:30 solo mostraban AUSTRAL.

**Causa raíz**: línea 26 de `/opt/automation/extractor.py` contenía
```python
sys.path.insert(0, "/opt/automation_austral")  # ← apunta a la carpeta hermana
import companies as comp
```
Al ejecutarse `/opt/automation/extractor.py`, Python encontraba PRIMERO `/opt/automation_austral/companies.py` (que solo contiene AUSTRAL) y nunca el de CARARJFAM/BT.

**Arreglo**: `sys.path.insert(0, "/opt/automation")` (línea 26) + `out_dir = Path("/tmp/extractor_runs")` (línea 472) + añadidos `"backup_"` y `"recovery"` a `SKIP_FILENAME_HINTS`.

**Reglas EXT09 + EXT10 en `REGLAS_SISTEMA.xlsx`** documentan la lección.

**Limpieza Drive**: 8 archivos basura (7 backups rolling + RECOVERY.md) movidos de `Pendientes/` (raíz CARARJFAM) a subcarpeta `Sistema (no procesar)/`. Los file_ids fijos de los backups y RECOVERY.md no cambian, así que `backup_to_drive.py` sigue funcionando sin tocar nada.

### 11.2.0.-1 Anti-duplicado de partners (defensa multi-capa)

Post-mortem de 2 duplicados (GANESHA y MATEO MOTOR) → 4 reglas FAC08-FAC11 en `REGLAS_SISTEMA.xlsx`:

1. **FAC08** — `find_or_create_supplier` busca por VARIANTES de VAT (raw + canonical con ES + sin ES) antes de crear. Si cualquiera matchea → reutiliza partner.
2. **FAC09** — Fallback por **nombre normalizado** (upper + collapse spaces + strip SL/SA suffix) si VAT no matchea. Captura duplicados por VAT ausente/típo/race.
3. **FAC10** — **NUNCA SQL crudo** para `vat` o `email`. Siempre `partner.write({"vat": X})` → pasa por `base_vat` que normaliza prefijo ES. Si base_vat rechaza checksum, fallback `with_context(no_vat_validation=True).write()`.
4. **FAC11** — **Cron 06:00 diario** `/opt/automation/detect_duplicate_partners.py`. Detecta:
   - Mismo VAT con varios ids
   - Mismo nombre normalizado con varios ids
   - VAT idéntico salvo prefijo ES (ej. `B86002318` vs `ESB86002318`)

   Si encuentra → email HTML al usuario con tabla de duplicados. Log en `/var/log/automation/duplicates.log`.

Cron entry:
```
0 6 * * * /opt/odoo17/venv/bin/python /opt/automation/detect_duplicate_partners.py >> /var/log/automation/duplicates.log 2>&1
```

Para fusionar duplicados existentes manualmente:
```python
env["base.partner.merge.automatic.wizard"].create({"dst_partner_id": <id_a_conservar>})._merge([<id_dup>, <id_a_conservar>])
```

### 11.2.0.0 Adjuntos PDF en chatter (no solo en clip 📎)

Cada vez que un processor (`process_invoice.py`, `nomina_processor.py`, `tax_payment_processor.py`) adjunta el PDF/imagen original al `account.move`, además de crear el `ir.attachment`, llama a `move.message_post(body=..., attachment_ids=[att.id], subtype_xmlid='mail.mt_note')`.

Esto **vincula el adjunto al chatter** del documento (sección "Notas / Mensajes" en la UI de Odoo) — sin el `message_post`, el PDF solo aparecería en el botón clip 📎 del header pero el usuario lo buscaría en vano en "notas".

Si se hace un restore o se posteen documentos sin `message_post` por algún bug, ejecutar el backfill: `sudo -u odoo /opt/odoo17/venv/bin/python /tmp/_backfill_chatter.py` (script en `automation/_backfill_chatter.py` del repo). Idempotente — solo crea mensajes para adjuntos sin vínculo previo.

### 11.2.0.1 Biblioteca de reglas — `REGLAS_SISTEMA.xlsx`

Archivo único con TODAS las reglas de negocio del pipeline, dirigido a agentes Claude (o humanos) que retomen el sistema.

- **Path local generador**: `/opt/automation/build_rules_xlsx.py`
- **Drive file_id**: `1lX032XqZ63u73jM6wDlIZsekqeQ-pzkG` (en carpeta "Mi Odoo CARARJFAM")
- **Hojas**: README, Nominas, Facturas, Bancos, Dudas, Aprendizaje, Extractor, Backup, Gastos_periodicos
- **Convención**: cada fila ID|Regla|Lógica|Severidad|Fuente|Script|Notas. Severidades: CRITICAL/ERROR (rojo), WARNING (amarillo), INFO (azul)

Para regenerar tras añadir reglas nuevas:
```bash
sudo -u odoo /opt/automation/venv/bin/python /opt/automation/build_rules_xlsx.py
# Sobrescribe el archivo en Drive vía SA UPDATE (no CREATE — cuota=0)
```

El archivo Drive ya está pre-creado (vía OAuth user del propietario; SA solo lo actualiza). Si se pierde habría que recrearlo desde MCP user (ver workaround sección 16).

### 11.2.0.2 Validaciones SS adicionales (10 reglas) en nómina

Añadidas a `nomina_processor.py:_run_nomina_ss_validations()`. Detectan errores típicos del gestor (caso real: BCCC empleado ≠ BCCC empresa para trabajador parcial → sobrepago empresa).

Cuotas oficiales 2026 (Orden ISM/118/2026):
- CC empresa: 24,35% — CC trabajador: 4,85%
- Desempleo empresa indef: 5,50% — temporal: 6,70%
- Desempleo trabajador: 1,55%
- FP empresa: 0,60% — trabajador: 0,10%
- FOGASA: 0,20%
- MEI empresa: 0,67% — trabajador: 0,13%

Cuando cambien (anualmente en Orden de cotización), actualizar las constantes en cabecera de `nomina_processor.py` (`TIPO_CC_EMPRESA`, `TIPO_DESEMPLEO_INDEF`, etc).

Las reglas escriben sus hallazgos en la `narration` del asiento de nómina (❌ para errores, ⚠ para avisos). NO abortan el posteo — el asiento se postea con la `aport_empresa_total` declarada y el usuario revisa la narración para detectar discrepancias.

### 11.2.1 Asiento de nómina — fórmula y validaciones

`nomina_processor.py` aplica el esquema español PGC:

```
DR 640   = Σ bruto                                  (total devengo, agregado)
DR 642   = Σ ss_empresa (aportación total: CC empresa + AT + FOGASA + formación + desempleo)
       CR 4751 = Σ irpf
       CR 476  = Σ ss_empresa + Σ ss_trabajador + Σ especie_socio (CARARJFAM)
       CR 465.NNN per trabajador = liquido_cash (= bruto_i − ss_trab_i − irpf_i − especie_i)
```

**Subcuenta 465 por trabajador**: el processor busca/crea `465NNN` (NNN incremental empezando en `001`) por cada empleado. Reutiliza por NIF — el nombre de la cuenta incluye el NIF para localización futura.

**Validaciones**:
- Por trabajador: `liquido == bruto − ss_trab − irpf − especie` (ERROR → rc=10)
- Por trabajador: `bruto ≈ base_contingencias_comunes` (AVISO en narración, no aborta)
- Asiento global: `Σ DR == Σ CR` (ERROR → rc=10, NO postea)
- Duplicado por `ref = "Nomina YYYY-MM (company_id)"` (rc=20 idempotent)

**Campos requeridos en extra del JSON** (extractor PROMPT actualizado):
- `irpf_total`, `ss_empleado_total`, `aportaciones_empresa_total`, `base_contingencias_comunes_total`, `salario_especie_total`, `liquido_total`, `period`, `employees[]`
- Cada `employee`: `name`, `nif`, `bruto`, `irpf`, `ss`, `salario_especie`, `liquido`, `base_contingencias_comunes`

**`base_accidente`**: NO es un importe, es una base de cálculo. NUNCA se suma al 642 ni a ningún total.

### 11.2 Análisis trimestral de gastos periódicos

Script `periodic_expenses_check.py` corre 4 veces al año (10 ene/abr/jul/oct a las 08:00) y detecta automáticamente patrones recurrentes (alquiler, nóminas, suministros, TGSS, IRPF...) y los que faltan respecto a su cadencia esperada.

Salida JSON en `/tmp/periodic/<vat>_periodic.json` que `dudas_xlsx_publish.py` lee y añade como hoja extra `Gastos_periodicos` al xlsx diario.

Cron entry:
```
0 8 10 1,4,7,10 * /opt/odoo17/venv/bin/python /opt/automation/periodic_expenses_check.py >> /var/log/automation/periodic.log 2>&1
```

Verifica:
```bash
sudo -u odoo /opt/odoo17/venv/bin/python /opt/automation/periodic_expenses_check.py 2>&1 | tail -3
# Debe imprimir summary con patrones detectados y faltantes por empresa
```

El sistema **aprende automáticamente** del histórico — no necesita lista predefinida. Cuanto más histórico tengas en BD, más patrones detecta. Parámetros (en el script):
- `LOOKBACK_MONTHS = 18` — ventana de análisis
- `MIN_OCCURRENCES = 3` — mínimo de repeticiones para considerar patrón
- `GAP_CV_MAX = 0.35` — variación máxima entre intervalos (rechaza patrones irregulares)
- `GRACE_PCT = 0.5` — gracia 50% del avg_gap antes de marcar como faltante

---

## 12. Smoke test del pipeline

### 12.1 Verifica acceso a Odoo
```bash
curl -s https://erp.carajfam.com/web/login | grep -i 'odoo'
```

Login web: `c.alcalde.campusport@gmail.com` / `<tu password>`. Cargar tablero contable y comprobar que las 2 empresas aparecen y los datos están.

### 12.2 Test pipeline manual

```bash
# 1) Test extractor (sin documentos pendientes solo logea ok=0)
sudo -u odoo /opt/automation/venv/bin/python /opt/automation/extractor.py 2>&1 | tail -5

# 2) Test reconciler
sudo -u odoo /opt/odoo17/venv/bin/python /opt/automation/bank_reconciler.py --threshold 90 2>&1 | tail -5

# 3) Test email
sudo -u odoo /opt/odoo17/venv/bin/python /opt/automation/email_summary.py 2>&1 | tail -3
```

Si el email llega → todo OK.

---

## 13. Activa snapshot diario en el proveedor

Vuelve al panel del nuevo VPS y activa snapshots automáticos diarios (IONOS lo ofrece como add-on). Esto cubre el caso "rompe disco entero" y complementa los backups app-level a Drive.

---

## 14. Si algo no funciona — orden de diagnóstico

1. Logs Odoo: `journalctl -u odoo17 -n 100 --no-pager` o `tail -100 /var/log/odoo/odoo17.log`
2. Logs nginx: `tail -50 /var/log/nginx/error.log`
3. Logs automation: `ls -la /var/log/automation/ && tail -50 /var/log/automation/extractor.log`
4. Permisos: `ls -la /opt/odoo17/.local/share/Odoo/filestore/cararjfam` debe ser `odoo:odoo`
5. Ownership tablas: `sudo -u postgres psql -d cararjfam -c "SELECT tableowner, count(*) FROM pg_tables WHERE schemaname='public' GROUP BY tableowner;"` → todas deben pertenecer a `odoo`
6. SA Drive: `sudo -u odoo /opt/automation/venv/bin/python -c "from drive_ops import _service; print(_service().about().get(fields='user').execute())"`

---

## 15. Datos de configuración clave

(Por si necesitas referencias rápidas)

| Item | Valor |
|---|---|
| Odoo path | `/opt/odoo17/odoo` |
| Odoo conf | `/etc/odoo17.conf` |
| Odoo venv | `/opt/odoo17/venv` |
| Odoo data_dir | `/opt/odoo17/.local/share/Odoo` |
| Custom addons | `/opt/odoo17/custom-addons` (incluye `learned_rules`) |
| Automation | `/opt/automation` |
| Automation venv | `/opt/automation/venv` |
| Logs Odoo | `/var/log/odoo/odoo17.log` |
| Logs automation | `/var/log/automation/*.log` |
| Service Account JSON | `/etc/automation_sa.json` |
| BD nombre (prod) | `cararjfam` |
| Empresa 1 (prod) | CARARJFAM2019,SL — CIF B93653392 |
| Empresa 2 (prod) | BEST TRAINING RINCON DE LA VICTORIA SL. — CIF B72349137 |
| BD nombre (sandbox + AUSTRAL) | `cararjfam_test` |
| Empresa 3 (AUSTRAL legacy/migración, archivar) | `AUSTRAL (en migración)` — CIF ESB44821965, plan híbrido l10n_es + 9-dig |
| Empresa 4 (AUSTRAL actual, plan 9-dig limpio) | `AUSTRAL (nueva)` — CIF ESB44821965, plan Sage 9-dig puro (sin l10n_es chart) |
| Usuario AUSTRAL | `contabilidad@austral.es` (uid=7) → company_ids=[4] |
| Master Odoo (admin BD) | (ver `/etc/odoo17.conf` `admin_passwd`) |
| limit_memory_soft / hard (odoo17.conf, ajustado 2026-05-28) | 1,5 GB / 2 GB (antes 640 MB / 768 MB — reventaba Libro Mayor de AUSTRAL por volumen 22k+ moves) |
| Wizard Libro Mayor (override 2026-05-28) | Botón "Exportar XLSX (recomendado)" ahora default_focus, en lugar de "View" (HTML qweb). Vista heredada `general_ledger_wizard_xlsx_default` (priority 99). XLSX para todo 2026 AUSTRAL: ~15s, 4 MB. HTML rendering peta con 11k+ partners. |
| SMTP | Gmail, app password 16-char |
| Email destinatario | c.alcalde.campusport@gmail.com |

---

## 16. Sobre el backup mismo

El esquema de backup usa **rolling 7 días** (lunes-domingo). Cada noche se sobrescribe el archivo del día actual:
- `backup_LUNES.tar.gz` … `backup_DOMINGO.tar.gz`

Los IDs de Drive son fijos; el script los conoce y hace `update()` vía Service Account. La carpeta es:

> `https://drive.google.com/drive/folders/1RZjKO1GqJuPURl6WTsl2R9egwm7cyYFQ` (Mi Odoo CARARJFAM)

El script de backup vive en `/opt/automation/backup_to_drive.py` (debería estar en `automation.tar.gz`). Si no, recrearlo según el repo `odoo-deploy`.

Para histórico más largo: descarga manualmente uno de los backups una vez al mes y guárdalo en otro lado (NAS, otra cuenta cloud, etc.).

---

## 17. AUSTRAL — empresa en BD `cararjfam_test` (rebuild 2026-05-03)

AUSTRAL vive en la BD **`cararjfam_test`** (NO en `cararjfam`). Es una empresa independiente con su propio plan contable Sage 9-dígitos puro, sin compartir nada con CARARJFAM/BT.

### 17.1 Estado actual

| Item | Valor |
|---|---|
| BD | `cararjfam_test` |
| Company id activa | **4** (`AUSTRAL (nueva)`, plan 9-dig limpio) |
| Company id archivada | **3** (`AUSTRAL (en migración)`, plan híbrido del primer intento — backup vivo, no borrar) |
| VAT | ESB44821965 |
| Plan contable | **Sage 9-dig puro** (5.557 cuentas), sin `l10n_es` chart template |
| Partners cliente | 3.586 (430xxxxxxx con NIF) |
| Partners proveedor | 783 (400xxxxxxx con NIF) |
| Taxes | 14 (IVA 21/10/4/0 intracom/0 export/IGIC3/Portugal23/Bélgica21 + compras) — todos a cuentas 9-dig |
| Diarios | 14 (INV, FACTU, OV, NOM, APE, CIE, LIQ + bancarios CRU, ABA, CAI, BSA, BBVA, UNI06, UNI22) |
| Reconcile models | 7 auto_match (uno por banco) |
| Usuario | `contabilidad@austral.es` (uid=7, Elena Austral 2) → company_ids=[4] |

### 17.2 Fuente del plan contable

El plan se construyó desde el xlsx que entregó el contable Sage de AUSTRAL:

- Original: `Listado de cuentas.xls` en Drive folder `Mi Odoo AUSTRAL` (`1SNDTko-SgeYNjyJ-_635ObprBDVWm-Jd`)
- Backup local en VPS: `/tmp/austral_input/Listado de cuentas.xls` + JSON pre-procesado `/tmp/austral_input/plan_cuentas.json` (5.554 entradas — len>=8 dígitos con nombre no vacío)

### 17.3 Mapeo prefijo PGC → account_type Odoo v17

**Importante**: Odoo solo permite **UNA** cuenta de tipo `equity_unaffected` por compañía. Si hay 0, los informes "Libro Mayor / Balance Sheet" fallan con "solo se puede calcular si la empresa tiene una cuenta de resultados no afectados". Si hay 2+, el `_check_account_type_unique_current_year_earning` rechaza la creación.

**Convención AUSTRAL**: `129000000 PERDIDAS Y GANANCIAS` → `equity_unaffected` (única). El resto de 12x → `equity`.

```python
1xx → equity (10x, 11x, 12x, 13x), liability_non_current (14x-19x)
   ⚠ 129000000 manualmente -> equity_unaffected (ÚNICA, post-load)
2xx → asset_non_current
3xx → asset_current
4xx → 400-419 liability_payable / 430-449 asset_receivable
      465-466 liability_current, 460/464 asset_current
      470-474 asset_current (IVA soportado, HP deudor)
      475-477,479 liability_current (HP acreedor, IVA repercutido)
      480 asset_prepayments, 485 liability_current
      490/493 asset_current
5xx → 52-56 liability_current (préstamos, líneas crédito CP)
      54 asset_current, 57 asset_cash (bancos/caja), 58-59 liability_current
6xx → expense
7xx → income (770/771/778 income_other)
8/9xx → off_balance
```

### 17.4 Scripts de reconstrucción

Los scripts están en `/tmp/` del VPS y deberían replicarse en `odoo-deploy/austral/` para versionarlos:

| Script | Hace |
|---|---|
| `/tmp/austral_phase_a.py` | Renombra company 3 a "AUSTRAL (en migración)", crea company 4 "AUSTRAL (nueva)", importa 5554 cuentas |
| `/tmp/austral_phase_b.py` | Crea partners (3586 cli + 783 prov) con NIF + property_account_*_id vinculado |
| `/tmp/austral_phase_cd.py` | Crea 4 cuentas IVA faltantes, 14 taxes (con repartition lines apuntando a cuenta 9-dig), 14 diarios + IBANs |
| `/tmp/finish_phase_ef.py` | Cambia ELENA a company 4, crea 7 reconcile.models auto-match |

**Importante**: si reconstruyes desde cero, ejecuta en orden A→B→CD→EF. Cada uno commitea por chunks → idempotentes (re-ejecutables sin doblar datos).

### 17.5 Diarios bancarios y cuentas (oficial, según `RESUMEN CUENTAS BANCOS.xlsx`)

| Code | Banco | Cuenta default | IBAN |
|---|---|---|---|
| `ABA` | ABANCA *0632 | 520100006 | ES32 2080 1202 5155 0000 0632 |
| `BBVA` | BBVA C. CDTO C19 | 520100022 | ES89 0182 2355 2101 0151 7954 |
| `BBVATPV` | BBVA TPV (liquidaciones B2C) | 572000039 | ES53 0182 2355 2802 0155 0576 |
| `BSA` | Banco Santander *9790 | 572000035 | ES98 0049 6254 3129 1608 9790 |
| `CAI` | CaixaBank *5742 | 572000004 | ES68 2100 8608 9302 0004 5742 |
| `CRU` | Caja Rural *8729 | 520100032 | ES49 3060 0066 3023 4784 8729 |
| `LIB0602` | Liberbank *0602 | 572000017 | ES91 2103 7151 6200 3002 0602 |
| `LIB2252` | Liberbank PIGNORADO *2252 | 572000047 | ES66 2103 7151 6900 3003 2252 |
| `LIB5056` | Liberbank *5056 | 572000038 | ES56 2103 7151 6605 5000 5056 |

Nota: cuentas 520xxx (líneas crédito) las cambiamos a `account_type='asset_cash'` para que Odoo las acepte como default_account en diarios bancarios. En primera pasada del rebuild puse "Unicaja 0602/2252" — eran realmente Liberbank, corregido el 2026-05-03.

### 17.6 Taxes IVA — cuentas 477/472 a las que apuntan

| Tax | % | type_tax_use | Cuenta destino |
|---|---|---|---|
| IVA 21% G / S | 21 | sale | 477000021 |
| IVA 10% G | 10 | sale | 477000010 (creada en rebuild) |
| IVA 4% G | 4 | sale | 477000004 |
| IVA 0% Intracomunitario | 0 | sale | 477000022 |
| IVA 0% Exportación | 0 | sale | 477000002 |
| IGIC 3% | 3 | sale | 477000003 |
| IVA 23% Portugal | 23 | sale | 477000029 |
| IVA 21% Bélgica | 21 | sale | 477000031 (creada en rebuild) |
| IVA 21% G Compras | 21 | purchase | 472000021 |
| IVA 10% G Compras | 10 | purchase | 472000010 |
| IVA 4% G Compras | 4 | purchase | 472000004 (creada en rebuild) |
| IVA 0% Intracom Compras | 0 | purchase | 472800000 |
| IGIC 3% Soportado | 3 | purchase | 472000003 |

### 17.7 Pendiente cuando se reanude (no parte del rebuild)

- Re-importar histórico ene/feb/mar (xlsx `Asientos Enero y Febrero 2026.xlsx` + `Asientos Marzo 2026.xlsx` ya en VPS)
- Re-importar facturas abril (xlsx `Facturas emitidas Abril 2026.xlsx`)
- Re-importar liquidaciones B2C abril (xlsx `Liquidaciones B2C abril 2026_430098000.xlsx`)
- Re-procesar 84 PDFs proveedor (carpeta Drive AUSTRAL)
- Importar 7 extractos bancos abril (CRU, ABA, UNI06, UNI22, CAI, BSA + BBVA cuando aparezca)
- Una vez todo verificado, archivar company id=3 (active=False)

### 17.8 Pipeline de automation AUSTRAL — `/opt/automation_austral/` (montado 2026-05-27)

Clon aislado de `/opt/automation/` para AUSTRAL. **NO toca CARARJFAM/BT**. Pasos del montaje (idempotentes):

1. `cp -r /opt/automation /opt/automation_austral` + `rm -rf venv` (reusa venvs `/opt/odoo17/venv` y `/opt/automation/venv`).
2. `sed DB_NAME "cararjfam"→"cararjfam_test"` en todos los `.py`.
3. **CRÍTICO**: `sed "/opt/automation/"→"/opt/automation_austral/"` en todos los `.py` — los scripts tenían `sys.path.insert` y constantes `PROCESS_SCRIPT/NOMINA_SCRIPT/TAX_PAYMENT_SCRIPT/BANK_IMPORTER` apuntando a `/opt/automation/` (CARARJFAM). Sin esto el extractor importa companies.py de CARARJFAM y bookea en BD prod.
4. `companies.py` reescrito → solo AUSTRAL (company 4 + carpetas + `dudas_file_id`). DEFAULT_VAT=B44821965.
5. `process_invoice.py`: `DEFAULT_EXPENSE_ACCOUNT_CODE="600000000"` + `DOC_TYPE_DEFAULT_ACCOUNT` a 9-dig (invoice 600000000, nomina 640000000, irpf 475100002, ss 642000000, other 629000000). Creadas cuentas placeholder `600000000`/`629000000`.
6. `email_summary.py`: `ENV_FILE="/etc/automation_austral.env"` (copia de automation.env con `SUMMARY_TO=contabilidad@austral.es, c.alcalde.campusport@gmail.com`).
7. `backup_to_drive.py` → renombrado a `.DISABLED` (los backups son responsabilidad del pipeline CARARJFAM; cubren el cluster pg entero).
8. Logs: `/var/log/automation_austral/`.
9. `extractor.py`: añadido soporte `EXTRACTOR_LIMIT` env (para tests).
10. dudas file renombrado en Drive a `dudas_para_revisar.xlsx` (el extractor lo skip-ea por `SKIP_FILENAME_HINTS=("dudas",...)`).

**Probado 2026-05-27**: extractor sobre 2 PDFs → 2 facturas `in_invoice` posted en company 4, PDF adjunto, diario FACTU, 0 toques a CARARJFAM. ✓

**Cron AUSTRAL (instalado 2026-05-28, sin tocar las 14 entradas existentes de CARARJFAM)**:

```cron
0 3 * * * cd /opt/automation_austral && HOME=/opt/odoo17 /opt/automation/venv/bin/python /opt/automation_austral/extractor.py --company B44821965 >> /var/log/automation_austral/extractor.log 2>&1
5 3 * * * HOME=/opt/odoo17 /opt/odoo17/venv/bin/python /opt/automation_austral/dudas_xlsx_collect.py >> /var/log/automation_austral/dudas_collect.log 2>&1
7 3 * * * HOME=/opt/odoo17 /opt/automation/venv/bin/python /opt/automation_austral/dudas_xlsx_publish.py >> /var/log/automation_austral/dudas_publish.log 2>&1
10 3 * * * HOME=/opt/odoo17 /opt/odoo17/venv/bin/python /opt/automation_austral/email_summary.py >> /var/log/automation_austral/email.log 2>&1
```

**Importante sobre los venvs por script**:
- `extractor.py`, `dudas_xlsx_publish.py`, `build_rules_xlsx.py` → venv `/opt/automation/venv` (Google API + pandas + openpyxl, sin Odoo).
- `dudas_xlsx_collect.py`, `email_summary.py`, `process_invoice.py`, `nomina_processor.py`, `tax_payment_processor.py`, `bank_*.py`, `learning*.py` → venv `/opt/odoo17/venv` (Odoo + psycopg2).
- Mezclar venvs = ModuleNotFoundError. NUNCA instalar Google API en venv Odoo (rompe pyOpenSSL).

**Aislamiento /tmp y env (CRÍTICO)**:
- `/tmp/dudas/` (CARARJFAM) vs `/tmp/dudas_austral/` (AUSTRAL) — directorios separados.
- `/tmp/periodic/` → `/tmp/periodic_austral/`.
- `/tmp/extractor_runs/` → `/tmp/extractor_runs_austral/`.
- `/etc/automation.env` (CARARJFAM) vs `/etc/automation_austral.env` (AUSTRAL) — chmod 640 root:odoo.

**PENDIENTE**:
- `nomina_processor.py` y `tax_payment_processor.py` aún tienen códigos de cuenta 6-dig hardcodeados → fallarán en nóminas/IRPF hasta adaptarlos a 9-dig (igual que se hizo con process_invoice).
- Backup `crontab_before_austral.txt` en `/tmp/` (para roll-back).
- Verificar en el primer email de mañana que el adjunto dudas xlsx llega (hoy reportó "0 dudas attachments"; puede ser que busque el xlsx en una ruta cambiada — investigar `email_summary.py` attachments_dir).

**Config `companies.py` para AUSTRAL (cada empresa tiene su carpeta raíz Drive)**:

```python
COMPANIES = [{
    "name": "AUSTRAL",
    "vat": "ESB44821965",
    "odoo_company_id": 4,                                   # company nueva limpia
    "pending_folder":      "1SNDTko-SgeYNjyJ-_635ObprBDVWm-Jd",  # "Mi Odoo AUSTRAL" RAÍZ = donde llegan docs Y donde va el dudas xlsx
    "contabilizado_folder":"1g5bpK1VBmaVtt5CN9lOZBYTcXUIEigvJ",  # Contabilizado odoo
    "revision_folder":     "1KKFSc0-ph8chNjKkj58K8tK4bVpru2IH",  # revision
    "aprendizajes_folder": "1DozZoV0grBbvjhEhCU1fMvujNvSWYoFN",  # Aprendizajes_aplicados
}]
```

Subcarpetas de "Mi Odoo AUSTRAL": Aprendizajes_aplicados, Cola_VPS, revision, Contabilizado odoo, csv odoo (+ FRAS MAYO/ABRIL 2026 temporales).

**Regla folders (confirmada por usuario 2026-05-27)**: la carpeta raíz de cada empresa ("Mi Odoo AUSTRAL" para AUSTRAL) es donde (a) llegan los documentos pendientes y (b) se deja el `dudas_para_revisar.xlsx` de revisión.

**Blocker SA quota=0**: el `dudas_para_revisar.xlsx` de AUSTRAL debe **pre-crearse** una vez con la cuenta OAuth del usuario en la raíz "Mi Odoo AUSTRAL" (la SA solo puede UPDATE, no CREATE). Igual patrón que CARARJFAM.

**Config pipeline AUSTRAL (decidida 2026-05-27)**:
- dudas xlsx pre-creado: `dudas_para_revisar:Austral.xlsx` file_id **`1cloiyMvqTHbnGELXwIFOVsKSKkIlsMP_`** (raíz Mi Odoo AUSTRAL, estructura idéntica a CARARJFAM, hojas Dudas/Rechazados/Gastos_periodicos vacías). El pipeline lo UPDATE-a por file_id (el `:` en el nombre es solo display).
- Email destinatario AUSTRAL: **AMBOS** `contabilidad@austral.es` + `c.alcalde.campusport@gmail.com`.
- Backup dudas file_id en `companies.py:dudas_file_id`.

### 17.9 Convención cuentas Prestashop B2C (clientes web) — regla 2026-05-03

**Origen**: la tienda online (Prestashop) de AUSTRAL genera facturas B2C que en Odoo se contabilizan individualmente. Cada cliente PS necesita su propia cuenta 9-dig y su propio `res.partner` vinculado.

**Convención del código de cuenta**:

```
4308XXXXX   (9 dígitos total)
│  │└──┘
│  │  └──── ID cliente Prestashop (zero-padded a 5 dígitos)
│  └─────── "8" marca que es Prestashop
└────────── prefijo "430" clientes (PGC)
```

- **Rango actualmente disponible**: `430800000` a `430899999` (cubre IDs PS de 1 a 99.999). Verificado en company 4 (BD `cararjfam_test`): 0 cuentas con prefijo `4308` existen al momento de definir la regla.
- **Si se llena (cliente PS 100.000+)**: pasamos al siguiente bloque `4304XXXXX` con la misma estrategia.

**Por cada cliente PS importado se debe crear**:

1. `account.account`:
   - `code` = `4308` + `str(ps_customer_id).zfill(5)`
   - `name` = nombre del cliente PS
   - `account_type` = `asset_receivable`
   - `reconcile` = True
   - `company_id` = 4 (AUSTRAL nueva)

2. `res.partner`:
   - `ref` = mismo código que la cuenta (`4308XXXXX`)
   - `name`, `vat` (si tiene), email, dirección, etc.
   - `customer_rank` = 1
   - `company_id` = 4
   - `property_account_receivable_id` = la cuenta creada arriba

3. Por cada factura PS de ese cliente:
   - `account.move` tipo `out_invoice` (o `out_refund` si total < 0)
   - `partner_id` = el res.partner
   - Las líneas usan los taxes IVA configurados (21/10/4 G, intracom, exportación, IGIC 3%, etc.)
   - `ref` = número factura PS para trazabilidad

**Acceso API Prestashop** (probado y funcional desde VPS 2026-05-03):

- URL: `https://tienda.austral.es/api`
- Auth: query string `?ws_key=<KEY>` (Basic Auth devuelve 401 — PHP CGI mode no propaga `Authorization` header)
- Key almacenada en VPS: `/etc/austral_prestashop.env` (chmod 600, root-only). Contiene `PS_AUSTRAL_API_URL` y `PS_AUSTRAL_WS_KEY`. **NO commitear nunca la key**.
- Output: `&output_format=JSON` (más fácil que XML default)
- Tienda: PrestaShop 1.7.7.5, nginx + Plesk, IP 135.181.220.100

**Conteos PS al momento de definir (2026-05-03)**:

| Endpoint | Total | id_max | Observaciones |
|---|---|---|---|
| customers | 409 | 60.534 | IDs sparse — GDPR deletes |
| order_invoices | ~100k+ | 136.748 | Última 26/05/2026 nº 124.181 |
| order_slip | ~16k | 16.077 | Abonos |
| addresses | ~67k | 68.919 | Por cliente puede haber varias |
| orders (LIST) | 6 | 6 | **Filtrado por webservice** — listado solo devuelve órdenes de test |
| orders (GET por id) | N/A | – | Funciona — pivote: invoice → id_order → GET /orders/{id} → id_customer → customer |

**Aviso importante (docx)**: el informe diario PS filtra por `date_add` de la operación, no del pedido original — los abonos aparecen el día emitidos, no de la factura. Las filas negativas en días posteriores son `order_slip` sobre facturas previas.

Documento fuente: `/tmp/austral_input/Acceso_API_PrestaShop_Austral.docx` en VPS (SA no puede subirlo a Drive — cuota=0; usuario sube manualmente si quiere copia en Drive).

---

Fin RECOVERY.md.

---

## 18. Regla global: facturas rechazadas → carpeta `rechazadas/` (todas las empresas)

Fecha: **2026-05-28** — establecida por el usuario tras revisar el primer xlsx de dudas AUSTRAL.

### 18.1 Definición de la regla

Cuando una factura llega al xlsx de revisión (hoja "Rechazados") y el usuario decide en `tu_decision` que NO se contabiliza, el archivo se mueve a una **carpeta `rechazadas/`** en la raíz de Drive de cada empresa (hermana de `Pendientes/`, `Cola_VPS/`, `contabilizado/`, `revision/`, `aprendizajes/`).

**No se borra ni se ignora** — queda archivado por si hay que recuperarlo o auditar.

### 18.2 Palabras clave reconocidas en `tu_decision`

El parser `classify_rechazo_decision()` de `dudas_apply.py` mapea estas frases a `action = rechazo_archivar`:

- `rechazar`, `rechazada`, `rechazado`
- `archivar`
- `a rechazadas`, `carpeta rechazadas`
- `no contabiliza`

Estado final en xlsx: `ARCHIVADO_RECHAZADAS`.

### 18.3 Implementación técnica

- **`companies.py` (ambos pipelines)**: nueva clave `rechazadas_folder` (folder_id Drive).
- **`dudas_apply.py` `_do_drive_action()`**: handler `rechazo_archivar` → mueve file a `rechazadas_folder`. Si la clave es `None` (empresa sin carpeta creada), busca/crea `rechazadas` como hermana de la actual.
- **Resolución multi-company**: por `root` (= `pending_folder`), busca la company match en `COMPANIES` y lee su `rechazadas_folder`.

### 18.4 Estado de carpetas `rechazadas/` por empresa

| Empresa | Drive folder_id | Estado |
|---|---|---|
| AUSTRAL | `1Y6WRDOti_2xvKS3uCArBJGvfd0D27_rL` | ✅ creada 2026-05-28 |
| CARARJFAM2019,SL | — | ⏳ pendiente crear en Drive raíz de "Mi Odoo" |
| Best Training Rincon de la Victoria | — | ⏳ pendiente crear en Drive raíz |

Cuando se creen, actualizar `/opt/automation/companies.py` con el folder_id.

### 18.5 Aprendizajes contables generados al aplicar la regla AUSTRAL (2026-05-28)

3 facturas reprocesadas con decisión del usuario + 3 reglas `learned.rule` creadas:

| Regla id | Partner | Pattern | Cuenta | Tax |
|---|---|---|---|---|
| 35 | SUMINISTROS SERIGRAFICOS KIMA (8402) | `DTF\|DIGISTAR\|TINTA` | 602000017 COMPRA TRANSFER TINTAS | IVA 21% G |
| 36 | SUMINISTROS SERIGRAFICOS KIMA (8402) | `PAPEL\|FILM\|VINILO\|STICKY` | 602000018 COMPRA TRANSFER PAPEL | IVA 21% G |
| 37 | BANCO BILBAO VIZCAYA (8429) | `ARRENDAMIENTO\|RENTING\|CUOTA\|0182` | 621000007 RENTING BBVA LLORENTE | IVA 21% G |

Asientos publicados:
- `FACTU/2026/05/0022` KIMA SF 26001701 13.471,85€ (mix tintas+papel)
- `FACTU/2026/05/0023` BBVA 260992A00235770 123,54€ (arrendamiento)
- `FACTU/2026/04/0001` Mª Dolores 525 491,99€ (camisas R.Club Polo Barcelona)

### 18.6 Nota OCR — alias proveedor

Para SUMINISTROS SERIGRAFICOS KIMA, el extractor anterior leyó **"Internacional"** (castellano) en lugar del nombre real **"International"** (inglés). El usuario indica: ignorar ese error de OCR únicamente en esta factura; no introducir alias hardcoded por una sola ocurrencia.

---

Fin sección 18.

---

## 19. Reglas obligatorias en cualquier contabilización (manual o automática)

Fecha: **2026-05-28** — establecidas tras review usuario de las 3 facturas reprocesadas.

### 19.1 Adjuntar PDF al `account.move` SIEMPRE

Cuando se crea una `account.move` desde un documento (factura, ticket, recibo, nómina, etc.) — sea por el extractor automático, sea por intervención manual vía ORM — el archivo fuente **DEBE quedar adjunto al asiento**. Procedimiento:

```python
att = env['ir.attachment'].create({
    'name': filename,                  # nombre original del PDF
    'res_model': 'account.move',
    'res_id': move.id,
    'type': 'binary',
    'datas': base64.b64encode(open(path,'rb').read()).decode(),
    'mimetype': 'application/pdf',
    'company_id': move.company_id.id,
})
move.message_main_attachment_id = att.id
move.message_post(body=f'PDF original adjuntado: {filename}', attachment_ids=[att.id])
```

El `extractor.py` / `process_invoice.py` del pipeline ya lo hacen automáticamente. Las contabilizaciones manuales vía script ad-hoc deben replicar este patrón.

### 19.2 De-duplicar partners por VAT antes de contabilizar

Antes de crear o publicar una factura, verificar que **solo hay UN partner activo por VAT en la misma company**. Si hay duplicados vacíos (`moves=0 AND amls=0`), archivarlos (`active=False`) — no borrarlos por riesgo de FKs orfanas. Si tienen moves, se deben mergear con `base.partner.merge.automatic.wizard` o reasignación manual.

**Síntoma del bug**: la vista "Proveedores" no muestra el histórico facturado porque está repartido entre duplicados que comparten VAT. El correcto suele ser el que tiene `property_account_payable_id` informado.

Caso resuelto 2026-05-28: KIMA tenía partners 7447 (company 4, vacío), 3425 (company 3, vacío) y 8402 (company 4, con 1 factura + cuenta 400048101). Archivados 7447 y 3425 → KIMA aparece consolidado en 8402 con histórico completo. Misma operación con Mª Dolores 5915 (vacío en company 4).

---

Fin sección 19.

---

## 20. Carpeta `informes/` por empresa — xlsx generados por scripts internos

Fecha: **2026-05-29** — creada tras detectar que el cron procesaba xlsx de informes propios como si fueran facturas.

### 20.1 Problema detectado

En el cron del 2026-05-29 03:00 el extractor procesó 8 xlsx de informes (Pendientes_cobro, Liquidaciones_*, Duplicados_pendientes_merge, Matcheo_*, BBVA Histórico, Revision_*) como facturas y los rechazó por `bank rc=30`. Estos xlsx no son ni facturas ni extractos bancarios — son informes que generan los scripts ORM para auditoría humana.

### 20.2 Solución

Cada empresa tiene una **subcarpeta `informes/`** en la raíz de su carpeta operativa Drive, hermana de `pending`, `contabilizado`, `revision`, `rechazadas`, `aprendizajes`.

Cuando un script genera un xlsx de auditoría/informe, debe colocarlo (o el usuario debe colocarlo) en `informes/`, NO en `pending/`. El extractor jamás escanea `informes/`.

### 20.3 Folder IDs Drive

| Empresa | informes_folder | rechazadas_folder |
|---|---|---|
| AUSTRAL | `166MYzuWjLNpb9CrvLjzL19EbZnLvghMc` | `1Y6WRDOti_2xvKS3uCArBJGvfd0D27_rL` |
| CARARJFAM2019,SL | `1raE4-0_q4QP8dELHy4NY5QxPiIecE2UU` | `1vJwd3LpShitDb5ERFt3oMhbo0fDjYkVm` |
| Best Training | `1RBMYkC74cdYCIyeDD116msNjlUvIMctm` | `1_OzPVOWJqmgauvKQbjMucPGcEkLqdG4v` |

Las 6 carpetas (3 `informes/` + 3 `rechazadas/` antes faltaban 2 de CARARJFAM/BT) fueron creadas el 2026-05-29 vía SA (las carpetas Drive no consumen quota de storage, solo los archivos sí).

### 20.4 Defensa adicional: SKIP_FILENAME_HINTS

Aunque la regla principal es colocar los xlsx en `informes/`, en `extractor.py` se ha ampliado el SKIP por nombre como segunda barrera:

```python
SKIP_FILENAME_HINTS = (
    "dudas", "aprendizaje", "_aplicado", "_procesado",
    "pendientes_", "liquidaciones_", "matcheo_", "duplicados_",
    "revision_", "152_liquidaciones", "asientos_", "resumen_",
    "libro_mayor_", "informe_", "reporte_",
)
```

Si por error un xlsx de informe acaba en `pending/`, el extractor lo verá pero hará `skip` y no consumirá créditos Claude.

### 20.5 Enrutado de rechazos del extractor

Función nueva `_route_to_rejected(svc, fid, cfg)` en `extractor.py`. Mueve a `rechazadas_folder`; si la clave está vacía, fallback a `revision_folder`. Llamada desde los 5 puntos donde antes había `drive_ops.move_file(fid, cfg["revision_folder"], ...)`.

### 20.6 Operación del usuario

A partir de ahora, cuando Claude genere un xlsx de informe, hay 2 opciones:

- **Vía sync local**: Claude lo deja en `G:\Mi unidad\Mi Odoo AUSTRAL\informes\<nombre>.xlsx` (o equivalente para CARARJFAM/BT) usando scp + ruta directa.
- **Vía API Drive (SA)**: el script lo sube vía `drive.files().create` con `parents=[informes_folder]`. Falla por quota si es CREATE — funciona si es UPDATE de un fichero existente.

Recomendación: dejar siempre los informes en `informes/` desde el momento de generación, sin pasar por `pending/` nunca.

---

Fin sección 20.

---

## 21. Reversión importación PS B2C — borrado masivo 4303xxxxx (2026-05-29)

Fecha: **2026-05-29 09:04 UTC** — el usuario decidió retroceder la contabilización de las 6.711 facturas + 570 abonos PS B2C importados el 2026-05-03 con la convención `4303 + id_customer_PS`.

### 21.1 Motivo

Tras importar las 7.281 facturas individuales PS B2C (sección 17.9) y verificar la diferencia frente a las 152 liquidaciones BBVA, se decidió revertir la importación para rehacerla más adelante con un enfoque diferente (probablemente conciliando primero contra TPV y/o usando partner único + cuenta agregada por canal).

### 21.2 Alcance del borrado

| Concepto | Resultado |
|---|---:|
| account.move borrados (out_invoice + out_refund) | **7.281** |
| Importe facturas revertido | 486.355,47 € |
| Importe abonos revertido | 24.146,47 € |
| Cuentas 4303xxxxx archivadas (`deprecated=true`) | 6.887 |
| Moves Sage legacy 430098xxxx (intactos) | 19 |
| res.partner (NO se tocaron) | 6.887 PS |
| learned.rule (NO se tocaron) | sigue activo |
| Conciliaciones bancarias (NO había) | 0 |
| account.payment (NO había) | 0 |

### 21.3 Procedimiento ejecutado

1. **Backup** previo: `/root/backup_pre_delete_4303_20260529_090439.sql.gz` (7,1 MB gzipped)
2. **Script** `/tmp/delete_4303_moves.py`:
   - Fase 1: `button_draft()` + `unlink()` en lotes de 200 (145 segundos)
   - Fase 2: archivado de cuentas (fallida por nombre de campo)
3. **Corrección Fase 2**: `UPDATE account_account SET deprecated=true WHERE code LIKE '4303%' AND company_id=4` (en Odoo 17, `account.account` NO tiene campo `active`, usa `deprecated`)

### 21.4 NOTA TÉCNICA — Odoo 17 `account.account.deprecated`

A diferencia de otros modelos, `account.account` en Odoo 17 NO usa `active` (sí lo usa `res.partner`, `account.journal`, etc.). Para archivar cuentas:

```python
# WRONG (lanzará "Invalid field account.account.active")
env['account.account'].search([('active','=',False)])

# CORRECT
env['account.account'].search([('deprecated','=',True)])
# O via SQL: UPDATE account_account SET deprecated=true WHERE ...
```

Campos disponibles en `account.account`: account_type, centralized, code, company_id, create_*, currency_id, **deprecated**, group_id, include_initial_balance, internal_group, name, non_trade, note, reconcile, root_id, write_*.

### 21.5 Re-ejecución futura de la importación PS

Si se decide volver a importar, los artefactos siguen disponibles:

- **PS dump JSON** (49 MB) en VPS `/tmp/austral_input/PS_dump_2026.json` (y en `G:\Mi unidad\Mi Odoo AUSTRAL\`) — 6.711 invoices + 570 slips + 9.465 orders + 6.886 customers + 7.206 addresses
- **Cuentas 4303xxxxx** deprecadas pero existentes (basta `UPDATE ... SET deprecated=false`)
- **Partners 4303** mappings intactos
- **Convención** en sección 17.9 sigue válida

Alternativa recomendada antes de re-importar:
1. Conciliar primero las 152 liquidaciones BBVA TPV contra un cliente agregado `B2C TPV Redsys`
2. Las ventas no TPV (transferencia, contado) se contabilizan por partner real
3. Reduce el ruido de 7.281 facturas individuales a ~152 asientos diarios agregados

### 21.6 Restauración del backup en caso necesario

```bash
ssh round-vps
systemctl stop odoo17
sudo -u postgres dropdb cararjfam_test
sudo -u postgres createdb -O odoo cararjfam_test
gunzip -c /root/backup_pre_delete_4303_20260529_090439.sql.gz | sudo -u postgres psql cararjfam_test
systemctl start odoo17
```

> Restaurar destruye TODO lo posterior al 2026-05-29 09:04 (también los 9 asientos del cron de anoche, y el resto de cambios). Si se quiere restaurar solo la parte 4303, sería necesario importar selectivamente del dump.

---

Fin sección 21.

---

## 22. Import Sage → Odoo: normalización de signos + dedupe robusto (2026-05-29)

Aprendizajes obtenidos al re-importar los 265 asientos Sage de la cuenta 430098000 en AUSTRAL.

### 22.1 Sage usa importes con signo; Odoo no

**Sage** representa las anulaciones/regularizaciones con valores negativos en la misma columna Debe o Haber:

| Apunte | Cuenta | Debe Sage | Haber Sage | Significado |
|---|---|---|---|---|
| 1 | 430098000 | −119,42 | 0 | "deshago un cargo de 119,42" |
| 4 | 430098000 | 0 | −119,42 | "deshago un abono de 119,42" |

**Odoo** sólo acepta `debit ≥ 0` y `credit ≥ 0`. La equivalencia correcta para que el saldo se mantenga es:

| Sage | → | Odoo |
|---|---|---|
| `D = −X, H = 0` | → | `D = 0, C = X` |
| `D = 0, H = −X` | → | `D = X, C = 0` |
| `D = X, H = 0` (positivo) | → | `D = X, C = 0` (mantener) |
| `D = 0, H = X` (positivo) | → | `D = 0, C = X` (mantener) |

### 22.2 Snippet de normalización (copiable)

```python
def normalize_sage_line(d_raw: float, h_raw: float) -> tuple[float, float]:
    """Convierte (debit_sage, credit_sage) que pueden ser negativos
    a (debit_odoo, credit_odoo) siempre positivos."""
    if d_raw < 0 and h_raw == 0:
        return 0, abs(d_raw)            # swap a credit
    if h_raw < 0 and d_raw == 0:
        return abs(h_raw), 0            # swap a debit
    if d_raw >= 0 and h_raw >= 0:
        return d_raw, h_raw              # ya correcto
    raise ValueError(f"Línea Sage con D={d_raw} H={h_raw} ambos no nulos o ambos negativos")
```

### 22.3 Pequeño efecto colateral: el "Total Debe" agregado es distinto pero el saldo neto coincide

Una vez normalizado, el **saldo neto (D−H) por cuenta coincide al céntimo** entre Sage y Odoo. Pero los **totales Debe/Haber agregados** difieren (Odoo es mayor) porque Sage compensa con valores negativos mientras Odoo suma siempre positivos en ambas columnas. Esto NO es un error — es diferencia de representación.

Ejemplo cuenta 430098000:
- Saldo Sage: −77,43 (acreedor)
- Saldo Odoo: −77,43 (acreedor) ✅ match exacto
- Debe Sage agregado: 452.889,36 / Debe Odoo: 453.397,20 (+507,84 = diferencia formal)
- Haber Sage agregado: 452.966,79 / Haber Odoo: 453.474,63 (+507,84 = misma diferencia)

Al cliente le importa el saldo y los apuntes individuales, no la suma agregada Debe/Haber.

### 22.4 Dedupe robusto: cuidado con `Doc nan` vs `Doc ` (vacío)

En la importación original (sección 17.x), cuando el campo `Documento` del xlsx estaba vacío, se usaba la cadena literal `'nan'` (probablemente de pandas). En la re-importación, el mismo campo se convertía a `''` (vacío). Resultado:

- Asiento preexistente: `ref = "SAGE-4248 / Doc nan"`
- Asiento re-importado: `ref = "SAGE-4248 / Doc "` (con espacio final)

Distintos como string → el dedupe no los detectó → **se creó duplicado del asiento de apertura de 883 líneas**.

**Fix de norma en futuros scripts**:

```python
def normalize_doc(doc_raw):
    """Construye sufijo Doc para ref del move, robusto frente a None/NaN/vacío."""
    if doc_raw is None or doc_raw == '' or str(doc_raw).lower() in ('nan', 'none'):
        return 'nan'  # convención fija — todos los vacíos se mapean a 'nan'
    return str(doc_raw).strip()

ref = f"SAGE-{asiento_num} / Doc {normalize_doc(documento)}"
```

Y antes de crear, **buscar también con variantes** del ref:

```python
existing = env['account.move'].search([
    ('company_id','=', cid),
    '|', '|',
        ('ref','=', ref),
        ('ref','=', ref.replace(' / Doc nan', ' / Doc ')),
        ('ref','=', ref.replace(' / Doc ', ' / Doc nan')),
], limit=1)
```

### 22.5 Procedimiento limpio recomendado

Para futuras importaciones de asientos Sage:

1. **Pre-verificación**: ¿existen ya asientos `SAGE-N / Doc X` o variantes en company N? Reportar count.
2. **Backup BD** antes de cualquier insert masivo.
3. **Lectura xlsx**: aplicar `normalize_sage_line()` y `normalize_doc()` siempre.
4. **Dedupe**: buscar ref con variantes `Doc nan` / `Doc ` / `Doc <num>`.
5. **Importar en lotes** con commit cada 50–100 moves.
6. **Verificación post**: comparar saldo neto (D−H) por cuenta xlsx vs Odoo. Si no coincide al céntimo → investigar.

### 22.6 Archivos de referencia

- Script de importación inicial 430098000: `/tmp/import_430098000_sage.py` (en VPS)
- Script de fix de los 6 negativos: `/tmp/fix_6_asientos_negativos.py`
- Backup pre-borrado 4303: `/root/backup_pre_delete_4303_20260529_090439.sql.gz`
- Xlsx fuente: `G:\Mi unidad\Mi Odoo AUSTRAL\Contabilizado odoo\ASIENTOS DEL 1 DE ENERO AL 30 DE ABRIL.xlsx` + `Asientos MAYO 2026 (Hasta 25).xlsx`

---

Fin sección 22.

---

## 23. Liquidaciones PS B2C diarias en Odoo (auto-cron) — 2026-05-29

Sistema automático que descarga facturas/abonos PS B2C y crea asientos en Odoo
siguiendo el patrón Sage de la cuenta `430098000`.

### 23.1 Patrón contable Sage replicado

Para cada día con facturas PS, se crea **un asiento de facturación**:

```
DEBE                                                HABER
─────────────────────────────────────────────────  ────────────────────────────────
430098000  LIQUIDACION PS DIA YYYY-MM-DD             700000001  VENTAS WEB (base)
                                                     477000021  IVA Repercutido 21%
                                                     477000003  HP IGIC 3% Canarias
                                                     (477000022 intracom 0% → no línea, base 0)
                                                     430098000  LIQUIDACIÓN FECHA YYYY-MM-DD
572000039  C/C BBVA TPV cobro YYYY-MM-DD
```

Y un **asiento de abono** invertido por cada día con abonos (`order_slip`).

**Refs únicos para idempotencia**:
- Facturación: `PS-LIQ-FAC-YYYY-MM-DD`
- Abono: `PS-LIQ-ABO-YYYY-MM-DD`

### 23.2 Clasificación automática del IVA

El script clasifica cada factura/abono según el **ratio `total/base`**:

| Ratio | Tipo | Cuenta destino |
|---|---|---|
| 1,21 | 21% peninsular | `477000021` |
| 1,03 | IGIC 3% Canarias | `477000003` |
| 1,00 | Intracom 0% / exporto / exento | `477000022` (sin línea IVA, base va a ventas) |
| Otro | → 21% por defecto (con descuadre menor) | `477000021` |

Cuentas IVA adicionales disponibles si se necesitan:
- `477000027` IVA 20% Francia (OSS)
- `477000029` IVA 23% Portugal (OSS)
- `477000002` IVA 0% extranjero

### 23.3 Cuentas usadas

| Code | Nombre | Rol |
|---|---|---|
| `430098000` | CLIENTES WEB - DETAIL | Cuenta puente (en y sale) |
| `700000001` | VENTAS TIENDAS | Ingresos (base + envíos) |
| `477000021` | IVA Repercutido 21% | IVA peninsular |
| `477000003` | HP IGIC 3% | IGIC Canarias |
| `477000022` | IVA Repercutido Intracom 0% | Intracom (línea solo si importe > 0) |
| `572000039` | C/C BBVA TPV (0182-2355-2802-0155-0576) | Banco TPV Redsys |

Journal: `34 (LIQ)` "Liquidaciones B2C".

### 23.4 Cron L-V 02:00 — liquidación diaria

```cron
0 2 * * 1-5 HOME=/opt/odoo17 /opt/odoo17/venv/bin/python /opt/automation_austral/ps_liquidacion_diaria.py >> /var/log/automation_austral/ps_liquidacion.log 2>&1
```

**Lógica de fechas**:
- Si hoy es **martes-viernes**: procesa el **día anterior** (lunes a las 02:00 → procesa viernes/sábado/domingo)
- Si hoy es **lunes**: procesa **viernes + sábado + domingo** (3 días)
- Argumentos manuales:
  - `--date YYYY-MM-DD` → procesa solo ese día
  - `--from YYYY-MM-DD --to YYYY-MM-DD` → rango

**Idempotencia**: detecta `account.move` con ref `PS-LIQ-FAC-<fecha>` / `PS-LIQ-ABO-<fecha>` y hace SKIP si ya existe. Seguro re-ejecutar.

**Salida**: JSON en `/tmp/ps_liquidacion_<HOY>.json` con detalle de cada asiento creado, leído por `email_summary.py` para el email diario.

### 23.5 Email summary modificado

`email_summary.py` añade automáticamente una sección **💳 Liquidaciones PS B2C contabilizadas** después del bloque de facturas de proveedor. Lee `/tmp/ps_liquidacion_*.json` (últimos 3 días) y muestra:

| Tipo | Ref | Asiento Odoo | Nº fac/abo | Base € | Detalle IVA | Total € | Estado |
|---|---|---|---:|---:|---|---:|---|
| FAC | PS-LIQ-FAC-2026-05-22 | LIQ/2026/05/0043 | 24 | 2.188,29 | iva_21=337,55; iva_3=22,54 | 2.548,38 | OK |
| ABO | PS-LIQ-ABO-2026-05-22 | LIQ/2026/05/0044 | 8 | 444,71 | iva_21=93,39 | 538,10 | OK |

### 23.6 Cron mensual día 2 — xlsx histórico

```cron
30 2 2 * * HOME=/opt/odoo17 /opt/automation/venv/bin/python /opt/automation_austral/ps_historico_mensual.py >> /var/log/automation_austral/ps_historico.log 2>&1
```

El día 2 de cada mes a las 02:30, descarga TODAS las facturas+abonos del mes anterior individualizadas por cliente y las añade como hoja al xlsx `PS_historico_mensual_AUSTRAL.xlsx` en Drive `informes/`.

**Nombre de hoja**: `<MES_ES> <AÑO>` (ej. `MAYO 2026`).

**Contenido de cada hoja**:

1. **Detalle por factura/abono**: Tipo, Nº PS, Fecha, Cliente, Email, Base, Tipo IVA, IVA, Total
2. **Totales PS por tipo IVA**: agrupado 21% / 3% / intracom / otros
3. **Contabilizado en Odoo** (mes): saldo D/H de cuentas 430098000, 477000021/3/22, 700000001, 572000039
4. **Diferencia PS vs Odoo**: comparativa IVA 21%, IGIC 3%, Total facturado

### 23.7 Limitación SA — primera subida manual

La cuenta de servicio (`/etc/automation_sa.json`) **no puede CREAR archivos en Drive personal** (`storageQuotaExceeded`). Solo puede `UPDATE` archivos existentes.

**Primera vez**: el script generará el xlsx en `/tmp/PS_historico_mensual_AUSTRAL.xlsx` y el operador deberá subirlo manualmente a `Mi Odoo AUSTRAL/informes/`. Después, todas las actualizaciones mensuales se hacen vía SA automáticamente.

### 23.8 Argumentos del script mensual

- `--month N --year YYYY` → procesa mes específico
- Sin argumentos → procesa el mes anterior al actual

### 23.9 Manejo de credenciales PS

`/etc/austral_prestashop.env` (chmod 640 root:odoo, legible por usuario `odoo`):

```env
PS_AUSTRAL_API_URL=https://tienda.austral.es/api
PS_AUSTRAL_WS_KEY=<32 chars>
```

El script carga estas variables con un `load_env_file()` que no requiere bash sourcing.

### 23.10 Importación retroactiva 22-29 mayo 2026 (datos iniciales)

Ejecutado el 2026-05-29 manualmente con `/tmp/ps_create_asientos.py`:

| Día | Fac creada | Total Fac € | Abo creada | Total Abo € |
|---|---|---:|---|---:|
| 2026-05-22 | LIQ/2026/05/0043 | 2.548,38 | LIQ/2026/05/0044 | 538,10 |
| 2026-05-23 | LIQ/2026/05/0045 | 1.351,84 | — | — |
| 2026-05-24 | LIQ/2026/05/0046 | 1.784,29 | — | — |
| 2026-05-25 | LIQ/2026/05/0047 | 12.905,52 | LIQ/2026/05/0048 | 155,75 |
| 2026-05-26 | LIQ/2026/05/0049 | 11.751,60 | LIQ/2026/05/0050 | 107,28 |
| 2026-05-27 | LIQ/2026/05/0051 | 7.177,40 | LIQ/2026/05/0052 | 24,20 |
| 2026-05-28 | LIQ/2026/05/0053 | 3.549,57 | LIQ/2026/05/0054 | 253,05 |
| 2026-05-29 | LIQ/2026/05/0055 | 1.003,43 | LIQ/2026/05/0056 | 358,33 |
| **Total** | **8 FAC** | **42.072,03** | **6 ABO** | **1.436,71** |

Neto: **40.635,33 €** (= ingreso TPV BBVA esperado de estos días).

---

Fin sección 23.

## 24. AUSTRAL — Web de contabilidad + parches pipeline (2026-06-03)

### 24.1 Web `austral.carajfam.com` (app austral-contab-web)
App propia (no Odoo) para que el gestor suba documentos, revise asientos y vea reports.

- **DNS**: `austral.carajfam.com` → `212.227.40.122` (mismo VPS).
- **Frontend** (React/Vite build estático) en `/var/www/austral/`. Código fuente en
  PC: `C:\Users\pc\Documents\austral-contab-web\frontend`. Deploy:
  `npm run build` → `scp -r dist/. round-vps:/var/www/austral/` (borrar antes
  `assets/*` e `index.html` viejos). El logo está en `frontend/public/austral-logo.jpg`.
- **Backend Flask** en `/opt/austral-contab-web/backend`, servicio systemd
  `austral-contab-web.service` (user `odoo`, `flask --app app run --host 127.0.0.1 --port 5000`).
  Venv propio `/opt/austral-contab-web/backend/venv`.
  - BD app (usuarios/2FA/audit) = **SQLite** `backend/data/app.db` (bcrypt + JWT pyjwt + TOTP pyotp).
  - BD Odoo = solo lectura `cararjfam_test` (SQLAlchemy); escrituras vía subprocess ORM.
  - `.env` clave: `APP_DB_URL`, `ODOO_DB_URL`, `ODOO_COMPANY_ID=4`,
    `ODOO_BASE_URL=https://erp.carajfam.com`, **`ODOO_DB=cararjfam_test`**,
    `ODOO_WRITEBACK_SCRIPT`, `ODOO_PYTHON`.
- **nginx**: site `/etc/nginx/sites-available/austral.carajfam.com` (HTTPS por certbot).
  - `location /api/` → `proxy_pass http://127.0.0.1:5000` (timeout 300s, `client_max_body_size 50M`).
  - `location / { try_files $uri $uri/ /index.html; }` (SPA).
  - **Caché (importante para no servir bundle viejo tras deploy)**:
    `location = /index.html` → `Cache-Control: no-store`;
    `location /assets/` → `Cache-Control: public, max-age=31536000, immutable`.
- **HTTPS**: Let's Encrypt `austral.carajfam.com` (certbot --nginx, renovación auto).
- **Enlaces a Odoo**: como `erp.carajfam.com` tiene varias BD y NO hay `dbfilter`,
  los enlaces deben llevar `?db=cararjfam_test` para saltar el selector:
  `https://erp.carajfam.com/web?db=cararjfam_test#id=<id>&model=account.move&view_type=form&cids=4`.
- **Usuarios web** (tabla `users` en app.db, roles viewer|accountant|admin):
  `c.alcalde.campusport@gmail.com` (admin), `rdpablo@austral.es` (accountant). 2FA desactivado.

### 24.2 Impuesto de RETENCIÓN IRPF (company 4)
Creado `account.tax` de compra reutilizable (regla **FAC13**):
- **Retención IRPF 19% arrendamientos** → cuenta `475100003` (H.P.ACREEDOR I.R.P.F. ARREND.)
- **Retención IRPF 15%/7% profesionales** → cuenta `475100002` (HP RETENCIONES PROFESIONALES)
- amount = −rate%, repartition base 100% + tax 100% a la cuenta 4751.
- En factura con retención: `total = base + IVA − IRPF`. Cuenta gasto alquiler típica `621000003`.

### 24.3 Parches del pipeline austral (`/opt/automation_austral/`)
Backups `.bak*` junto a cada archivo. **Solo el pipeline austral; carajfam/BT intactos.**
- `process_invoice.py`:
  - `_normalize_iva_included_lines()` — tickets con IVA incluido: reescala líneas a base neta (regla **FAC14**).
  - `find_or_create_irpf_tax()` + `validate_payload` contempla `irpf_amount` (regla **FAC13**).
- `extractor.py` PROMPT: campos `irpf_rate`/`irpf_amount`; regla de cuadre `base+IVA−IRPF=total`.
- `bank_importer.py` (regla **BNK08/BNK09**):
  - `_read_xls_robusto` (xlrd DIRECTO bypass versión pandas, fallback HTML/openpyxl);
    cabecera hasta fila 25; fecha con hora recortada; nº cuenta CCC desnudo (16-24 díg);
    detección `NO_ES_BANCO` (exports de facturas).
  - `_find_journal` filtra `company_id = _AUSTRAL_COMPANY_ID (4)` — un IBAN duplicado en
    company 3 ("AUSTRAL en migración") NO captura el statement.
  - idempotencia: statement existente por (company, journal, nombre, nº líneas) → `duplicate`.
- **Cuenta transitoria bancos** `572999000` creada y asignada como `suspense_account_id`
  a los 9 diarios bancarios de company 4 (sin ella el import de extractos da 405/UserError).

### 24.4 REGLA ANTI-DUPLICADO (FAC12, GLOBAL)
**Antes de contabilizar CUALQUIER documento, comprobar si ya está contabilizado**
(por nº/ref en toda la company; cross-diario incl. OV/Sage `Doc <nº>`; extractos por
company+journal+periodo+nº líneas; o partner+importe+fecha). Origen: el import
factura-a-factura de "Facturas emitidas" de mayo duplicó 327 ventas ya presentes en
los asientos de Sage (diario OV); hubo que borrarlas. Detalle en `REGLAS_SISTEMA.xlsx`
(FAC12 global; FAC13/14/15 + BNK08/09 con etiqueta AUSTRAL).

### 24.5 Diarios company 4 (referencia clasificación)
`28 INV` ventas (B2B) · `29 FACTU` compras · `31 NOM` nóminas · `34 LIQ` + `42 BBVAT`
ventas B2C · `30 OV` operaciones varias (migración Sage) · `35/36/39/40/41/42/43/44/45` bancos.

### 24.6 Conciliación bancaria automática + aprendizaje (2026-06-04)
**Regla: todo extracto importado se concilia.**
- `bank_importer.py:import_file` → tras crear líneas llama `conciliacion_ops.cmd_auto_reconcile(env, company_id)`
  = aplica `learned.rule(bank, conf>=0.85)` a las líneas pendientes (reasigna la línea
  suspense `572999000` a la cuenta/partner de la regla). Devuelve `auto_reconciled` en el resultado.
- **Puente ORM** `/opt/automation_austral/conciliacion_ops.py` (venv odoo17):
  - `--propose --company-id 4` → JSON de líneas sin conciliar + propuestas (`bank_matcher.propose_for_company`) + fiabilidad (score 0-100) + razones. Sanea nombres jsonb.
  - `--resolve --line-id N --action account|move [--account CODE|--move-id M] [--partner] [--learn 1] [--pattern]`
    → concilia (account: reasigna suspense; move: reconcilia contra la factura AR/AP) y si `learn=1` crea/actualiza `learned.rule(bank)`.
  - `--auto-reconcile --company-id 4` → aplica reglas aprendidas a demanda.
- **Web** (`austral-contab-web`): blueprint `app/api/conciliacion.py` (`/api/conciliacion/pendientes|resolver|auto-reconcile`), llama al puente por subprocess (`ODOO_PYTHON`).
  Pestaña **Revisión → Conciliaciones pendientes** (`ConciliacionPendiente.jsx`): info banco + propuesta + fiabilidad coloreada; conciliar por propuesta o manual a cuenta; checkbox "Aprender".
- **Aprendizaje**: cada decisión con `learn=1` crea `learned.rule(bank, pattern=concepto, account/partner, conf 0.9, source=active)` → auto-concilia movimientos parecidos en el próximo import.
- **Gestión de reglas (CRUD web)**: botón **"Ver reglas"** en la pestaña → modal `RulesModal`.
  - Bridge: `conciliacion_ops.py --list-rules | --save-rule [--rule-id] | --delete-rule` (delete = desactiva, no borra).
  - API: `GET/POST/PATCH/DELETE /api/conciliacion/reglas[/<id>]`.
  - `--list-rules` usa `with_context(active_test=False)` para mostrar también las inactivas; filtra `rule_type=bank` y `company in (4, False)` (las globales 🌐 salen pero las de carajfam/BT no).
  - Permite ver (patrón, cuenta, partner, confianza, nº aplicaciones, estado), crear y editar reglas a mano, además de las que se aprenden solas.
- Regla documentada: **BNK10** (AUSTRAL) en `REGLAS_SISTEMA.xlsx`.

---

Fin sección 24.

## 25. SII (Suministro Inmediato de Información) — AUSTRAL company 4 (2026-06-05)

Austral está obligada a SII. Montado en el Odoo de Austral (BD `cararjfam_test`); la
producción carajfam/BT (BD `cararjfam`) NO lo tiene.

- **Módulo OCA**: `l10n_es_aeat_sii_oca` v17.0.1.6.2 (repo `l10n-spain`). Depende de
  `l10n_es_aeat` (ya instalado) y de **`account_invoice_refund_link`** (repo OCA
  `account-invoicing`, **clonado nuevo** en `/opt/odoo17/custom-addons/account-invoicing`
  rama 17.0; añadido al `addons_path` de `/etc/odoo17.conf` al principio).
- Deps Python: `zeep` + `requests` (ya presentes). NO requiere `requests_pkcs12`.
- Instalación: `systemctl stop odoo17 && sudo -u odoo .../odoo-bin -c /etc/odoo17.conf -d cararjfam_test -i l10n_es_aeat_sii_oca --stop-after-init --no-http && systemctl start odoo17`.
- **Config company 4** (res.company): `sii_test=True` (preproducción AEAT),
  `tax_agency_id`=Agencia Tributaria española, `sii_start_date=2026-06-05`,
  `sii_enabled=False` (pendiente de certificado).
- **PENDIENTE**: cargar el **certificado digital AEAT** de Austral (`.p12`/`.pfx` + contraseña)
  en `l10n.es.aeat.certificate` (company 4) → luego `sii_enabled=True` → prueba de envío.
- Backup previo: `/root/backup_cararjfam_test_pre_sii_2026-06-05.sql.gz`.
- Al activar: las facturas de Austral (cron + web) con fecha ≥ `sii_start_date` se envían
  a SII automáticamente (cron del módulo). El histórico anterior NO se envía salvo que se
  baje `sii_start_date`.

---

Fin sección 25.

## 26. Limpieza duplicados banco CARARJFAM + idempotencia import extractos (2026-06-06)

**Incidente:** en CARARJFAM (company 1, BD `cararjfam`) la cuenta La Caixa `572001` tenía
124 apuntes = 62 reales (con `statement_line`) + **62 duplicados** (`BNK1/2026/00001..00062`,
sin `statement_line`), de una importación previa del extracto contabilizada a mano
(572001↔572998), dejando 47.532,19 € atascados en la transitoria `572998`. No conciliados.

- **Fix aplicado:** backup (`/root/backup_cararjfam_pre_dedup_*.sql.gz`) + borrado de los 62
  asientos sin línea de extracto (`button_draft()` + `unlink()`). Resultado: 572001 → 62 apuntes,
  572998 → 5 apuntes (−1.450,39 €). Best Training no tenía duplicación (300 reales + 1 ajuste manual).
- **Prevención (regla BNK11, GLOBAL):** se añadió el **guard de idempotencia** a
  `/opt/automation/bank_importer.py` (CARARJFAM/BT), igual que el de `/opt/automation_austral`:
  antes de crear el `account.bank.statement` busca uno existente con mismo
  `(company_id, journal_id, nombre)` y, si coincide el nº de líneas, devuelve `duplicate=True`
  sin crear nada. Backup del fichero: `bank_importer.py.bak_idem`.
- Documentado en `REGLAS_SISTEMA.xlsx` como **BNK11 (GLOBAL)**.

---

Fin sección 26.
