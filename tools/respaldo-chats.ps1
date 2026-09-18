# Respaldo cruzado del historial de Claude.
#
#   A) Servidor -> PC : el historial de /opt/odoo17/.claude/projects (el del bot que
#      procesa facturas), comprimido en el servidor y descargado aqui.
#
#   B) PC -> Servidor : el historial local de este ordenador (tus conversaciones de
#      verdad), comprimido aqui y subido al servidor.
#
# La parte B no es un capricho: guardar la copia en la misma maquina que la genera no
# protege de nada. El 16/09/2026 se perdio el PC anterior con todas las conversaciones
# de desarrollo dentro, porque solo existian alli.
#
# Programado en el Programador de tareas. Tambien se puede lanzar a mano.

$ErrorActionPreference = 'Stop'

# DOS destinos a proposito. La carpeta Proyectos del usuario se sincroniza con la nube,
# asi que lo que cae ahi queda protegido fuera de esta maquina; Documents no se sincroniza.
#
#   $destinoNube  -> lo irremplazable y pequeño (~30 MB/dia): el codigo del VPS que no
#                    tiene repositorio, las BD que no salen del servidor, y el historial
#                    de Claude de este PC.
#   $destino      -> el historial de Claude del SERVIDOR (1,6 GB/dia). Son en su inmensa
#                    mayoria ejecuciones del bot extractor de facturas, no conversaciones:
#                    2.347 de 2.461 en la auditoria del 17/09/2026. No compensa
#                    sincronizar eso a la nube todos los dias.
$destino     = "$env:USERPROFILE\Documents\Respaldo-Servidor-Carajfam"
$destinoNube = "$env:USERPROFILE\Proyectos\_respaldos"
$conservar   = 4
$fecha     = Get-Date -Format 'yyyyMMdd'
$log       = "$destino\respaldo.log"

$nombreVps = "claude-chats-$fecha.tgz"
$remotoVps = "/tmp/$nombreVps"

$nombrePc  = "claude-local-$fecha.tgz"
$remotoPc  = "/root/backups_pc_claude"

function Escribe($texto) {
    $linea = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $texto"
    Add-Content -Path $log -Value $linea -Encoding utf8
    Write-Output $linea
}

function Rotar($patron, $carpeta = $destino) {
    Get-ChildItem "$carpeta\$patron" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending |
        Select-Object -Skip $conservar |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

$fallos = 0
New-Item -ItemType Directory -Force $destino | Out-Null
New-Item -ItemType Directory -Force $destinoNube | Out-Null
Escribe "===== inicio ====="

# ---------------------------------------------------------------- A) servidor -> PC
try {
    ssh -o BatchMode=yes carajfam "cd /opt/odoo17/.claude && tar -czf $remotoVps projects"
    if ($LASTEXITCODE -ne 0) { throw "fallo al comprimir en el servidor (codigo $LASTEXITCODE)" }

    scp -o BatchMode=yes "carajfam:$remotoVps" "$destino\$nombreVps"
    if ($LASTEXITCODE -ne 0) { throw "fallo la descarga (codigo $LASTEXITCODE)" }

    $hashLocal  = (Get-FileHash "$destino\$nombreVps" -Algorithm SHA256).Hash.ToLower()
    $hashRemoto = (ssh -o BatchMode=yes carajfam "sha256sum $remotoVps").Split(' ')[0]
    if ($hashLocal -ne $hashRemoto) { throw "la huella no coincide: el archivo llego corrupto" }

    ssh -o BatchMode=yes carajfam "rm -f $remotoVps"
    Rotar "claude-chats-*.tgz"

    $tam = [math]::Round((Get-Item "$destino\$nombreVps").Length / 1GB, 2)
    Escribe "A) servidor -> PC  OK: $nombreVps ($tam GB) - huella verificada"
}
catch {
    Escribe "A) servidor -> PC  ERROR: $($_.Exception.Message)"
    $fallos++
}

# ---------------------------------------------------------------- B) PC -> servidor
try {
    # Se guardan DOS cosas, y la segunda esta fuera de la carpeta .claude:
    #   .claude\       -> conversaciones (projects\), historial de ficheros, sesiones
    #   .claude.json   -> configuracion: lista de proyectos, servidores MCP, preferencias
    # Copiar solo .claude\projects deja la instalacion sin configuracion al restaurar.
    if (-not (Test-Path "$env:USERPROFILE\.claude\projects")) {
        throw "no existe $env:USERPROFILE\.claude\projects"
    }

    $aGuardar = @('.claude')
    if (Test-Path "$env:USERPROFILE\.claude.json") { $aGuardar += '.claude.json' }

    if (Test-Path "$destinoNube\$nombrePc") { Remove-Item "$destinoNube\$nombrePc" -Force }

    # Ruta explicita al tar de Windows (bsdtar). Si se deja en "tar" a secas y el PATH
    # trae antes el Git Bash, su tar GNU interpreta "C:\..." como host remoto y falla
    # con "Cannot connect to C: resolve failed". Ademas bsdtar aguanta rutas de mas de
    # 260 caracteres, cosa que los cmdlets de PowerShell no.
    $tarExe = Join-Path $env:SystemRoot 'System32\tar.exe'
    if (-not (Test-Path $tarExe)) { throw "no encuentro el tar de Windows en $tarExe" }

    & $tarExe -czf "$destinoNube\$nombrePc" -C $env:USERPROFILE @aGuardar
    if (-not (Test-Path "$destinoNube\$nombrePc")) { throw "no se pudo crear el paquete local" }

    ssh -o BatchMode=yes carajfam "mkdir -p $remotoPc"
    scp -o BatchMode=yes "$destinoNube\$nombrePc" "carajfam:$remotoPc/$nombrePc"
    if ($LASTEXITCODE -ne 0) { throw "no se pudo subir el paquete al servidor" }

    $hashLocal  = (Get-FileHash "$destinoNube\$nombrePc" -Algorithm SHA256).Hash.ToLower()
    $hashRemoto = (ssh -o BatchMode=yes carajfam "sha256sum $remotoPc/$nombrePc").Split(' ')[0]
    if ($hashLocal -ne $hashRemoto) { throw "la huella no coincide: el archivo subio corrupto" }

    # rotacion en los dos lados
    Rotar "claude-local-*.tgz" $destinoNube
    ssh -o BatchMode=yes carajfam "ls -1t $remotoPc/claude-local-*.tgz 2>/dev/null | tail -n +$($conservar + 1) | xargs -r rm -f"

    $tamMb = [math]::Round((Get-Item "$destinoNube\$nombrePc").Length / 1MB, 1)
    Escribe "B) PC -> servidor  OK: $nombrePc ($tamMb MB) - huella verificada"
}
catch {
    Escribe "B) PC -> servidor  ERROR: $($_.Exception.Message)"
    $fallos++
}

# ------------------------------------------- C) codigo del VPS sin repo -> PC
# La auditoria del 17/09/2026 encontro ~380 ficheros de codigo en produccion cuya unica
# copia era el disco del VPS: round_config_api (210 .py) y round_mcp, ambos servicios
# activos; austral-contab, que es el backend de la web de Austral; y gn_scripts.
# Ninguno esta en un repo ni lleva backup_to_drive.py propio, al contrario que los
# pipelines de automation*.
#
# OJO: este paquete SI lleva los .env con credenciales — es una copia de seguridad, no
# un repositorio. No lo subas a ningun sitio publico.
try {
    $nombreCod = "codigo-vps-$fecha.tgz"
    $remotoCod = "/tmp/$nombreCod"
    $dirs = 'round_config_api round_mcp gn_scripts austral-contab austral-contab-web carajfam-contab'

    ssh -o BatchMode=yes carajfam "cd /opt && tar -czf $remotoCod --exclude=venv --exclude=node_modules --exclude=__pycache__ --exclude='*.pyc' --exclude=.git $dirs"
    if ($LASTEXITCODE -ne 0) { throw "fallo al empaquetar el codigo en el servidor" }

    scp -o BatchMode=yes "carajfam:$remotoCod" "$destinoNube\$nombreCod"
    if ($LASTEXITCODE -ne 0) { throw "fallo la descarga del codigo" }

    $hashLocal  = (Get-FileHash "$destinoNube\$nombreCod" -Algorithm SHA256).Hash.ToLower()
    $hashRemoto = (ssh -o BatchMode=yes carajfam "sha256sum $remotoCod").Split(' ')[0]
    if ($hashLocal -ne $hashRemoto) { throw "la huella no coincide: el archivo llego corrupto" }

    ssh -o BatchMode=yes carajfam "rm -f $remotoCod"
    Rotar "codigo-vps-*.tgz" $destinoNube

    $tamMb = [math]::Round((Get-Item "$destinoNube\$nombreCod").Length / 1MB, 1)
    Escribe "C) codigo VPS -> PC  OK: $nombreCod ($tamMb MB) - huella verificada"
}
catch {
    Escribe "C) codigo VPS -> PC  ERROR: $($_.Exception.Message)"
    $fallos++
}

# ------------------------------------ D) bases de datos que no salen del VPS -> PC
# odoo_backup.sh vuelca cada noche TODAS las BD del usuario odoo a /var/backups/odoo,
# pero esas copias se quedan en la maquina. Solo salen a Drive las tres que tienen
# pipeline propio con su backup_to_drive.py: cararjfam, cararjfam_test y round_facturacion.
#
# gestionnoofit y round_config no tienen pipeline, asi que nadie las recogia: se volcaban
# a diario y morian con el servidor. Detectado el 18/09/2026. Son ~3 MB cada una y no
# tienen filestore.
try {
    $nombreBd = "bd-vps-$fecha.tgz"
    $remotoBd = "/tmp/$nombreBd"
    $bases = 'gestionnoofit round_config'

    # La seleccion del dump mas reciente la hace un script EN EL SERVIDOR
    # (/usr/local/bin/empaquetar_bd_sin_salida.sh). Intentar montar ese bucle aqui dentro
    # como una linea de shell no funciona: PowerShell expande las variables antes de
    # enviarlas y el servidor recibe los nombres de fichero como si fueran comandos.
    ssh -o BatchMode=yes carajfam "/usr/local/bin/empaquetar_bd_sin_salida.sh $remotoBd"
    if ($LASTEXITCODE -ne 0) { throw "fallo al empaquetar los dumps en el servidor" }

    scp -o BatchMode=yes "carajfam:$remotoBd" "$destinoNube\$nombreBd"
    if ($LASTEXITCODE -ne 0) { throw "fallo la descarga de los dumps" }

    $hashLocal  = (Get-FileHash "$destinoNube\$nombreBd" -Algorithm SHA256).Hash.ToLower()
    $hashRemoto = (ssh -o BatchMode=yes carajfam "sha256sum $remotoBd").Split(' ')[0]
    if ($hashLocal -ne $hashRemoto) { throw "la huella no coincide: el archivo llego corrupto" }

    ssh -o BatchMode=yes carajfam "rm -f $remotoBd"
    Rotar "bd-vps-*.tgz" $destinoNube

    $tamMb = [math]::Round((Get-Item "$destinoNube\$nombreBd").Length / 1MB, 1)
    Escribe "D) BD sin salida -> PC  OK: $nombreBd ($tamMb MB) - huella verificada"
}
catch {
    Escribe "D) BD sin salida -> PC  ERROR: $($_.Exception.Message)"
    $fallos++
}

if ($fallos -gt 0) { Escribe "===== terminado con $fallos problema(s) ====="; exit 1 }
Escribe "===== terminado sin incidencias ====="
