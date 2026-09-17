#!/bin/bash
# Ejecutar histórico mensual PS para enero-abril 2026 en una sola noche.
# Después de procesar, se auto-elimina del cron.
# Programado para 2026-05-29 22:00 (única ejecución).

LOG=/var/log/automation_austral/ps_historico_init.log
SCRIPT=/opt/automation_austral/ps_historico_mensual.py
VENV=/opt/automation/venv/bin/python

{
  echo "============================================================"
  echo "[$(date)] Iniciando histórico inicial enero-abril 2026"
  echo "============================================================"

  for MONTH in 1 2 3 4; do
    NAME="$(date -d "2026-${MONTH}-01" '+%B %Y' | tr 'a-z' 'A-Z')"
    echo ""
    echo "[$(date)] --- Procesando $NAME ---"
    HOME=/opt/odoo17 $VENV $SCRIPT --month $MONTH --year 2026
    rc=$?
    echo "[$(date)] $NAME terminó con rc=$rc"
  done

  echo ""
  echo "[$(date)] Auto-cleanup: eliminando línea cron run_historico_init.sh"
  crontab -u odoo -l | grep -v 'run_historico_init.sh' | crontab -u odoo -

  echo ""
  echo "[$(date)] ✅ Histórico inicial COMPLETADO. Hojas creadas en:"
  echo "https://docs.google.com/spreadsheets/d/1ZX_KXMMfiQKhQdvEVIYjDhLGh7EciugOHKr8MtHa25Q"
} >> $LOG 2>&1
