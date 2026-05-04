#!/usr/bin/env bash
# Odoo 17 Community installer — Ubuntu 24.04 LTS
# Designed for non-interactive execution: pass config via env vars.
#
#   DOMAIN=erp.example.com EMAIL=you@example.com MASTER_PASSWORD=xxx \
#     ENABLE_HTTPS=yes bash install_odoo.sh
#
set -euo pipefail

log()  { printf '\033[1;34m[*]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[OK]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[ERR]\033[0m %s\n' "$*" >&2; }

[[ $EUID -eq 0 ]] || { err "Run as root"; exit 1; }
. /etc/os-release
[[ "$ID" == "ubuntu" ]] || { err "Ubuntu only"; exit 1; }

: "${DOMAIN:?DOMAIN env var required (e.g. erp.example.com)}"
: "${EMAIL:?EMAIL env var required for Lets Encrypt notices}"
: "${MASTER_PASSWORD:?MASTER_PASSWORD env var required}"
ENABLE_HTTPS="${ENABLE_HTTPS:-yes}"
ODOO_VERSION="17.0"
ODOO_USER="odoo"
ODOO_HOME="/opt/odoo17"
ODOO_CONF="/etc/odoo17.conf"
PY_VER="3.11"

export DEBIAN_FRONTEND=noninteractive

log "1/12 System update"
apt-get update -y
apt-get upgrade -y -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold"
timedatectl set-timezone Europe/Madrid || true

log "2/12 Firewall (UFW) + fail2ban"
apt-get install -y ufw fail2ban
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
systemctl enable --now fail2ban

log "3/12 Base build dependencies"
apt-get install -y \
  build-essential wget curl git ca-certificates gnupg lsb-release \
  software-properties-common \
  libxml2-dev libxslt1-dev libldap2-dev libsasl2-dev libssl-dev \
  libjpeg-dev zlib1g-dev libpq-dev libffi-dev libxmlsec1-dev \
  libtiff5-dev libopenjp2-7-dev liblcms2-dev libwebp-dev \
  libfreetype6-dev libharfbuzz-dev libfribidi-dev \
  node-less npm xz-utils

log "4/12 Python ${PY_VER} from deadsnakes"
add-apt-repository -y ppa:deadsnakes/ppa
apt-get update -y
apt-get install -y python${PY_VER} python${PY_VER}-venv python${PY_VER}-dev

log "5/12 PostgreSQL"
apt-get install -y postgresql postgresql-contrib
systemctl enable --now postgresql
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${ODOO_USER}'" | grep -q 1; then
  sudo -u postgres createuser --createdb --no-superuser --no-createrole "${ODOO_USER}"
  ok "Postgres role ${ODOO_USER} created"
fi

log "6/12 wkhtmltopdf 0.12.6 (jammy package, compatible with 24.04)"
if ! command -v wkhtmltopdf >/dev/null; then
  WKHTML_DEB="/tmp/wkhtmltox.deb"
  wget -q -O "$WKHTML_DEB" \
    "https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-3/wkhtmltox_0.12.6.1-3.jammy_amd64.deb"
  apt-get install -y "$WKHTML_DEB" || apt-get install -y -f
  rm -f "$WKHTML_DEB"
fi
wkhtmltopdf --version

log "7/12 Odoo system user + clone"
if ! id -u "$ODOO_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$ODOO_HOME" --shell /bin/bash "$ODOO_USER"
fi
if [[ ! -d "$ODOO_HOME/odoo" ]]; then
  install -d -o "$ODOO_USER" -g "$ODOO_USER" "$ODOO_HOME"
  sudo -u "$ODOO_USER" git clone --depth=1 --branch "$ODOO_VERSION" \
    https://github.com/odoo/odoo.git "$ODOO_HOME/odoo"
fi
install -d -o "$ODOO_USER" -g "$ODOO_USER" "$ODOO_HOME/custom-addons" /var/log/odoo
chown -R "$ODOO_USER:$ODOO_USER" /var/log/odoo

log "8/12 Python venv + pip install"
if [[ ! -d "$ODOO_HOME/venv" ]]; then
  sudo -u "$ODOO_USER" python${PY_VER} -m venv "$ODOO_HOME/venv"
fi
sudo -u "$ODOO_USER" "$ODOO_HOME/venv/bin/pip" install --upgrade pip wheel setuptools
sudo -u "$ODOO_USER" "$ODOO_HOME/venv/bin/pip" install -r "$ODOO_HOME/odoo/requirements.txt"

log "9/12 odoo17.conf"
cat > "$ODOO_CONF" <<EOF
[options]
admin_passwd = ${MASTER_PASSWORD}
db_host = False
db_port = False
db_user = ${ODOO_USER}
db_password = False
addons_path = ${ODOO_HOME}/odoo/addons,${ODOO_HOME}/custom-addons
data_dir = ${ODOO_HOME}/.local/share/Odoo
logfile = /var/log/odoo/odoo17.log
log_level = info
proxy_mode = True
xmlrpc_port = 8069
gevent_port = 8072
workers = 2
max_cron_threads = 1
limit_memory_soft = 671088640
limit_memory_hard = 805306368
limit_request = 8192
limit_time_cpu = 600
limit_time_real = 1200
EOF
chown "$ODOO_USER:$ODOO_USER" "$ODOO_CONF"
chmod 640 "$ODOO_CONF"
install -d -o "$ODOO_USER" -g "$ODOO_USER" "$ODOO_HOME/.local/share/Odoo"

log "10/12 systemd unit"
cat > /etc/systemd/system/odoo17.service <<EOF
[Unit]
Description=Odoo 17
Requires=postgresql.service
After=network.target postgresql.service

[Service]
Type=simple
User=${ODOO_USER}
Group=${ODOO_USER}
ExecStart=${ODOO_HOME}/venv/bin/python ${ODOO_HOME}/odoo/odoo-bin -c ${ODOO_CONF}
StandardOutput=journal+console
Restart=on-failure
RestartSec=5
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now odoo17.service
sleep 5
systemctl is-active --quiet odoo17 || { err "odoo17 failed to start"; journalctl -u odoo17 -n 60 --no-pager; exit 1; }
ok "odoo17 service running"

log "11/12 Nginx reverse proxy"
apt-get install -y nginx
cat > /etc/nginx/sites-available/odoo <<EOF
upstream odoo  { server 127.0.0.1:8069; }
upstream odoochat { server 127.0.0.1:8072; }

map \$http_upgrade \$connection_upgrade {
  default upgrade;
  ""      close;
}

server {
  listen 80;
  listen [::]:80;
  server_name ${DOMAIN};

  client_max_body_size 200M;
  proxy_read_timeout 720s;
  proxy_connect_timeout 720s;
  proxy_send_timeout 720s;

  proxy_set_header Host \$host;
  proxy_set_header X-Real-IP \$remote_addr;
  proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
  proxy_set_header X-Forwarded-Proto \$scheme;

  location /websocket {
    proxy_pass http://odoochat;
    proxy_http_version 1.1;
    proxy_set_header Upgrade \$http_upgrade;
    proxy_set_header Connection \$connection_upgrade;
  }

  location / {
    proxy_redirect off;
    proxy_pass http://odoo;
  }

  location ~* /web/static/ {
    proxy_cache_valid 200 90m;
    proxy_buffering on;
    expires 864000;
    proxy_pass http://odoo;
  }

  gzip on;
  gzip_min_length 1100;
  gzip_buffers 4 32k;
  gzip_types text/css text/less text/plain text/xml application/xml application/json application/javascript;
  gzip_vary on;
}
EOF
ln -sf /etc/nginx/sites-available/odoo /etc/nginx/sites-enabled/odoo
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

if [[ "$ENABLE_HTTPS" == "yes" ]]; then
  log "11b/12 Let's Encrypt cert for ${DOMAIN}"
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx -d "${DOMAIN}" --non-interactive --agree-tos -m "${EMAIL}" --redirect || {
    err "certbot failed (DNS not propagated yet?). Re-run later: certbot --nginx -d ${DOMAIN}"
  }
  systemctl enable --now certbot.timer || true
fi

log "12/12 Daily backups (03:30, 14d retention)"
cat > /usr/local/bin/odoo_backup.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
BACKUP_DIR="/var/backups/odoo"
RETENTION_DAYS=14
DATE=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUP_DIR"
FILESTORE_BASE="/opt/odoo17/.local/share/Odoo/filestore"
DBS=$(sudo -u postgres psql -tAc "SELECT datname FROM pg_database WHERE datdba=(SELECT oid FROM pg_roles WHERE rolname='odoo')")
for DB in $DBS; do
  sudo -u postgres pg_dump -Fc "$DB" > "${BACKUP_DIR}/${DB}-${DATE}.dump"
  if [[ -d "${FILESTORE_BASE}/${DB}" ]]; then
    tar czf "${BACKUP_DIR}/${DB}-filestore-${DATE}.tar.gz" -C "${FILESTORE_BASE}" "${DB}"
  fi
done
find "$BACKUP_DIR" -type f -mtime +${RETENTION_DAYS} -delete
EOF
chmod +x /usr/local/bin/odoo_backup.sh
( crontab -l 2>/dev/null | grep -v 'odoo_backup.sh'; echo "30 3 * * * /usr/local/bin/odoo_backup.sh >> /var/log/odoo_backup.log 2>&1" ) | crontab -

if [[ "$ENABLE_HTTPS" == "yes" ]]; then ODOO_URL="https://${DOMAIN}"; else ODOO_URL="http://${DOMAIN}"; fi
echo
echo "================================================================="
echo " [OK] Instalacion completada"
echo "================================================================="
echo " URL:       ${ODOO_URL}"
echo " Config:    ${ODOO_CONF}"
echo " Logs:      journalctl -u odoo17 -f"
echo " Backups:   /var/backups/odoo (diario 03:30, retencion 14d)"
echo " Master pw: (la que pasaste por MASTER_PASSWORD)"
echo "================================================================="
