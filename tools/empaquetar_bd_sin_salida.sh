#!/bin/bash
# Empaqueta el dump MAS RECIENTE de las BD que no salen del VPS por ningun otro medio.
#
# odoo_backup.sh las vuelca cada noche a /var/backups/odoo, pero esas copias se quedan
# aqui. Solo salen a Drive las tres que tienen pipeline propio con su backup_to_drive.py
# (cararjfam, cararjfam_test, round_facturacion). Estas dos no tienen pipeline, asi que
# nadie las recogia: se volcaban a diario y morian con la maquina.
#
# Lo llama el respaldo nocturno del PC (respaldo-chats.ps1, parte D).
set -euo pipefail
BASES="gestionnoofit round_config"
DESTINO="${1:?falta la ruta del tgz a crear}"
cd /var/backups/odoo
FICHEROS=""
for db in $BASES; do
  ultimo=$(ls -t "${db}"-*.dump 2>/dev/null | head -1 || true)
  [ -n "$ultimo" ] && FICHEROS="$FICHEROS $ultimo"
done
[ -n "$FICHEROS" ] || { echo "no hay dumps que empaquetar" >&2; exit 1; }
tar -czf "$DESTINO" $FICHEROS
echo "empaquetados:$FICHEROS"
