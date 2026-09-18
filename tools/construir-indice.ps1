# Extrae el respaldo de chats y construye un indice navegable en HTML.
# Uso: powershell -ExecutionPolicy Bypass -File construir-indice.ps1

$ErrorActionPreference = 'Stop'

$base     = "$env:USERPROFILE\Documents\Respaldo-Servidor-Carajfam"
$destino  = "$base\chats"
$log      = "$base\indice.log"
$maxPrev  = 300

function Escribe($texto) {
    $linea = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] $texto"
    Add-Content -Path $log -Value $linea -Encoding utf8
    Write-Output $linea
}

function TextoDeContenido($contenido) {
    if ($null -eq $contenido) { return "" }
    if ($contenido -is [string]) { return $contenido }
    $trozos = @()
    foreach ($bloque in $contenido) {
        if ($bloque -is [string]) { $trozos += $bloque; continue }
        if ($bloque.type -eq 'text' -and $bloque.text) { $trozos += $bloque.text }
    }
    return ($trozos -join " ")
}

try {
    Escribe "=== INICIO ==="

    # ---------- 1. Extraer ----------
    $tgz = Get-ChildItem "$base\claude-chats-*.tgz" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $tgz) { throw "No encuentro ningun claude-chats-*.tgz en $base" }

    if (Test-Path "$destino\projects") {
        Escribe "Ya existe $destino\projects, omito la extraccion"
    } else {
        Escribe "Extrayendo $($tgz.Name) ($([math]::Round($tgz.Length/1GB,2)) GB)..."
        New-Item -ItemType Directory -Force $destino | Out-Null
        tar -xzf $tgz.FullName -C $destino
        if ($LASTEXITCODE -ne 0) { throw "tar devolvio codigo $LASTEXITCODE" }
        Escribe "Extraccion completada"
    }

    # ---------- 2. Recorrer sesiones ----------
    # Las sesiones viven en el primer nivel de cada proyecto. No bajamos a
    # subcarpetas como tool-results, que generan rutas de mas de 260 caracteres
    # y que Windows no sabe recorrer.
    $carpetas = Get-ChildItem "$destino\projects" -Directory -ErrorAction SilentlyContinue
    $archivos = foreach ($c in $carpetas) {
        Get-ChildItem -Path $c.FullName -Filter *.jsonl -File -ErrorAction SilentlyContinue
    }
    $archivos = @($archivos)
    Escribe "Encontradas $($archivos.Count) sesiones en $($carpetas.Count) proyectos. Indexando..."

    $registros = New-Object System.Collections.Generic.List[object]
    $n = 0

    foreach ($f in $archivos) {
        $n++
        if ($n % 250 -eq 0) { Escribe "  procesadas $n de $($archivos.Count)..." }

        $cwd = ""
        $fecha = $f.LastWriteTime.ToString("s")
        $resumen = ""

        # Cabecera: primeras lineas, para cwd / fecha / primer mensaje de usuario
        try {
            $cabecera = Get-Content $f.FullName -TotalCount 80 -ErrorAction Stop
        } catch { $cabecera = @() }

        foreach ($linea in $cabecera) {
            if ([string]::IsNullOrWhiteSpace($linea)) { continue }
            try { $o = $linea | ConvertFrom-Json -ErrorAction Stop } catch { continue }

            if (-not $cwd -and $o.cwd) { $cwd = [string]$o.cwd }
            if ($o.timestamp) { $fecha = [string]$o.timestamp }

            if (-not $resumen -and $o.type -eq 'user' -and $o.message) {
                $t = TextoDeContenido $o.message.content
                $t = ($t -replace '\s+', ' ').Trim()
                # descartar ordenes internas y recordatorios del sistema
                if ($t -and $t -notmatch '^<(command|local-command|system-reminder)') {
                    if ($t.Length -gt $maxPrev) { $t = $t.Substring(0, $maxPrev) + "..." }
                    $resumen = $t
                }
            }
            if ($cwd -and $resumen) { break }
        }

        # Numero de mensajes = lineas del archivo (lectura en streaming)
        $mensajes = 0
        try {
            foreach ($l in [System.IO.File]::ReadLines($f.FullName)) { $mensajes++ }
        } catch { $mensajes = -1 }

        if (-not $cwd) { $cwd = $f.Directory.Name }
        if (-not $resumen) { $resumen = "(sin mensaje de usuario legible)" }

        $registros.Add([pscustomobject]@{
            proyecto = $cwd
            sesion   = $f.BaseName
            archivo  = $f.FullName
            fecha    = $fecha
            bytes    = $f.Length
            mensajes = $mensajes
            resumen  = $resumen
        })
    }

    Escribe "Indexadas $($registros.Count) sesiones"

    # ---------- 3. Volcar JSON ----------
    $ordenados = $registros | Sort-Object fecha -Descending
    $json = $ordenados | ConvertTo-Json -Depth 4 -Compress
    Set-Content -Path "$base\indice.json" -Value $json -Encoding utf8
    Escribe "Escrito indice.json"

    # ---------- 4. Generar HTML ----------
    $totalGB   = [math]::Round((($registros | Measure-Object bytes -Sum).Sum) / 1GB, 2)
    $proyectos = ($registros | Select-Object -ExpandProperty proyecto -Unique).Count
    $generado  = Get-Date -Format 'dd/MM/yyyy HH:mm'

    # Escapar '</' para que ningun mensaje pueda cerrar la etiqueta <script>
    $jsonHtml = $json.Replace('</', '<\/')

    $plantilla = Get-Content "$base\plantilla-indice.html" -Raw -Encoding utf8
    $html = $plantilla.
        Replace('__DATOS__', $jsonHtml).
        Replace('__TOTAL__', $registros.Count).
        Replace('__PROYECTOS__', $proyectos).
        Replace('__GB__', $totalGB).
        Replace('__GENERADO__', $generado)

    Set-Content -Path "$base\indice.html" -Value $html -Encoding utf8
    Escribe "Escrito indice.html"
    Escribe "=== FIN OK: $($registros.Count) sesiones, $proyectos proyectos, $totalGB GB ==="
}
catch {
    Escribe "ERROR: $($_.Exception.Message)"
    exit 1
}
