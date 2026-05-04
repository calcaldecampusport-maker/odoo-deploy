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
| BD nombre | `cararjfam` |
| Empresa 1 | CARARJFAM2019,SL — CIF B93653392 |
| Empresa 2 | BEST TRAINING RINCON DE LA VICTORIA SL. — CIF B72349137 |
| Master Odoo (admin BD) | (ver `/etc/odoo17.conf` `admin_passwd`) |
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

Fin RECOVERY.md.
