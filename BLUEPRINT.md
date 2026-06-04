# Blueprint — Pipeline de automatización con LLM + ERP + Drive + user-in-the-loop

Documento de patrones extraídos del proyecto **carajfam** para reutilizar la arquitectura en otra aplicación, sin acarrear lo específico de contabilidad española / Odoo / facturas.

> **Cuándo usarlo**: cuando tienes documentos heterogéneos llegando por carpeta compartida (Drive/Dropbox/S3), un sistema de registro (ERP, CRM, base propia), un usuario que quiere que se procesen autónomamente con supervisión opcional, y reglas que se enseñan/aprenden con el tiempo.
>
> **Cuándo NO**: si el dominio admite OCR/parsing 100% determinista sin necesidad de LLM, o si no hay un humano dispuesto a corregir lo que el bot duda.

---

## 1. La forma del problema (genérica)

```
                     ┌─────────────────────────┐
                     │ Carpeta compartida       │   ← humano sube documentos
                     │ "Pendientes/"            │
                     │  + subcarpetas estado    │
                     └────────────┬────────────┘
                                  │
                ┌─────────────────▼──────────────────┐
                │  EXTRACTOR  (clasificador + LLM)   │
                │  doc → {tipo, campos, confianza}   │
                └─────────────────┬──────────────────┘
                                  │
                ┌─────────────────▼──────────────────┐
                │  ROUTER  (despacho por tipo)       │
                │  ├─ tipo A → handler_A             │
                │  ├─ tipo B → handler_B             │
                │  └─ tipo C → handler_C             │
                │  cada handler escribe en el ERP    │
                └─────────────────┬──────────────────┘
                                  │
                ┌─────────────────▼──────────────────┐
                │  AUTO-PROCESS si confianza ≥ X     │
                │  DRAFT  si confianza < X           │
                │  FAIL   si validación falla        │
                └─────────────────┬──────────────────┘
                                  │
                ┌─────────────────▼──────────────────┐
                │  USER-IN-THE-LOOP (xlsx round-trip)│
                │  bot escribe dudas → user rellena  │
                │  → bot ejecuta acciones via ORM    │
                └─────────────────┬──────────────────┘
                                  │
                ┌─────────────────▼──────────────────┐
                │  AUTO-APRENDIZAJE                   │
                │  cada decisión humana crea regla    │
                │  que se aplica a futuros docs       │
                └─────────────────┬──────────────────┘
                                  │
                ┌─────────────────▼──────────────────┐
                │  EMAIL DIARIO + xlsx adjunto        │
                └────────────────────────────────────┘
```

Cualquier proyecto que encaje en este shape puede reutilizar estos patrones aunque el dominio cambie totalmente.

---

## 2. Stack y por qué cada componente

| Capa | Tecnología (carajfam) | Patrón general | Alternativas |
|---|---|---|---|
| Server | VPS Ubuntu 24.04 (IONOS) | Cualquier máquina con cron + Python | Hetzner, DigitalOcean, AWS Lightsail |
| ERP/DB | Odoo 17 Community | Cualquier sistema con API/ORM accesible desde Python | Postgres puro, Salesforce, Notion API, Airtable |
| LLM | Claude headless CLI (`claude -p ...`) | Subprocess que recibe prompt+archivo y devuelve JSON | OpenAI/Anthropic API, Llama local, Gemini |
| Storage docs | Google Drive + Service Account | Carpeta compartida con bot | Dropbox, OneDrive, S3, NAS |
| Round-trip humano | xlsx en Drive (openpyxl) | Hoja editable con columna decisión | Google Sheets nativo, formulario web, Slack bot |
| Email | Gmail SMTP + app password | Notificación con HTML + adjuntos | Resend, Postmark, SES |
| Cron | crontab user | Scheduler simple | systemd timers, k8s cronjobs |
| Reglas aprendidas | Tabla custom `learned.rule` | Pattern → action storage con auto-learn | Cualquier KV con pattern matching |
| Backup | rolling 7 días → Drive vía SA | Snapshot diario sobreescribe slot del día | rclone + B2/S3, restic, borg |

---

## 3. Patrones reutilizables

### 3.1 Two-venv pattern (CRÍTICO)

Separa **estrictamente** los entornos Python según qué libs cargan:

```
/opt/proyecto/venv_external   # libs cliente: Google API, Drive, parsers, requests, OCR
/opt/erp/venv                  # libs cliente del ERP/DB: psycopg2, ORM, pyOpenSSL específico
```

**Por qué**: instalar `google-api-python-client` en el venv del ERP rompe cosas tipo `pyOpenSSL.X509StoreFlags` y tumba el servicio entero. Los pin de versiones de Google chocan con los del ERP.

**Cómo se comunican los scripts entre venvs**:

```python
# Script en venv_external escribe a /tmp
data = {"company_id": 2, "actions": [...]}
Path("/tmp/proyecto/actions.json").write_text(json.dumps(data))

# Llama al script en venv_erp por subprocess
subprocess.run(
    ["/opt/erp/venv/bin/python", "/opt/proyecto/erp_helper.py",
     "--input", "/tmp/proyecto/actions.json", "--output", "/tmp/proyecto/result.json"],
    capture_output=True, timeout=600,
)

# Lee resultado
result = json.loads(Path("/tmp/proyecto/result.json").read_text())
```

JSON sobre /tmp es feo pero **a prueba de balas**: 0 dependencias compartidas, debug fácil (ver el JSON), reintentos triviales.

---

### 3.2 LLM headless via subprocess

No uses la API directa si tienes Claude Code o equivalente CLI — el coste/llamada es menor y la integración más simple.

```python
PROMPT = """You extract fields from {tipo_doc}. Output ONLY a JSON object with:
- field_a (string)
- field_b (number)
- confidence (0..1)
- notes (string with any doubt)
"""

def extract_with_llm(file_path: Path) -> dict:
    res = subprocess.run(
        ["claude", "-p", PROMPT + f"\nThe file is at: {file_path.name}",
         "--output-format", "text",
         "--permission-mode", "bypassPermissions",
         "--add-dir", str(file_path.parent)],
        cwd=str(file_path.parent),         # CRÍTICO: contexto de directorio
        env={**os.environ, "HOME": "/opt/erp"},  # CRÍTICO: home con .claude.json autenticado
        capture_output=True, text=True, timeout=120,
    )
    if res.returncode != 0:
        raise RuntimeError(f"llm failed: {res.stderr[-500:]}")
    return json.loads(res.stdout.strip())
```

**Pitfall**: Claude CLI necesita `HOME` apuntando al user que tiene `.claude.json` autenticado. Si lo lanzas como `odoo` user vía cron, autentica una vez con `sudo -u odoo -H claude /login` y luego en el script pon `HOME=/opt/odoo17`.

**Validación post-LLM**: el modelo a veces inventa campos. Siempre validar matemáticamente:

```python
def validate_payload(p):
    sub = round(float(p["subtotal"]), 2)
    tax = round(float(p["tax_total"]), 2)
    tot = round(float(p["total"]), 2)
    if abs(sub + tax - tot) > 0.05:
        return f"math mismatch: {sub}+{tax}!={tot}"
    return None
```

Si validación falla → mover a carpeta `revision/` (no a `Procesados/`).

---

### 3.3 Confidence threshold + auto-action

```python
AUTO_PROCESS_THRESHOLD = 0.9   # ajusta por dominio

if extraction_confidence >= AUTO_PROCESS_THRESHOLD:
    create_record_in_erp(state="posted")
else:
    create_record_in_erp(state="draft")  # entra al sistema pero como borrador
    add_to_dudas_queue(record_id, reason=f"confidence {extraction_confidence:.2f}<{AUTO_PROCESS_THRESHOLD}")
```

**Excepción**: tipos de documento donde la matemática es verificable (nóminas con identidad `bruto - retenciones = liquido`) → siempre auto-process aunque confidence baje. La matemática es el segundo verificador.

---

### 3.4 User-in-the-loop xlsx round-trip

El usuario quiere supervisar pero **odia** una UI custom o entrar al ERP. La solución gana cuando:

1. El bot escribe **un xlsx** con dudas en una carpeta compartida (Drive/OneDrive)
2. Columnas: `id_documento | descripcion | sugerencia_actual | tu_decision | notas | estado`
3. El user abre el xlsx en su Excel/Sheets, rellena `tu_decision` con texto libre
4. Cron diario lee el xlsx, **clasifica el texto libre** en acciones programáticas, ejecuta vía ORM
5. Vuelve a publicar el xlsx con las filas resueltas removidas y nuevas dudas añadidas

Patrón de clasificación de texto libre (pragmático, no LLM):

```python
def classify_decision(text: str) -> dict:
    d = (text or "").lower().strip()
    if not d: return {"action": "skip"}
    if "borrar" in d or "no quiero" in d:
        return {"action": "delete"}
    if "ok" in d or "confirmo" in d or "si" in d:
        return {"action": "confirm_proposal"}
    if any(k in d for k in ["mover a", "asignar a"]):
        return {"action": "move", "target": extract_target(d)}
    # ... más patrones
    return {"action": "human", "label": "PENDIENTE_HUMANO"}
```

**Aprendizaje crítico**: el xlsx publish debe **preservar `tu_decision` previa** (mergear por `id_documento`) — si lo regeneras desde cero pisas las decisiones del usuario y se enfada.

```python
def merge_xlsx(existing_rows, fresh_rows):
    by_key = {r["id_documento"]: r for r in existing_rows}
    out = []
    for fr in fresh_rows:
        ex = by_key.get(fr["id_documento"])
        if ex and ex.get("tu_decision"):
            fr["tu_decision"] = ex["tu_decision"]  # preservar
            fr["notas"] = ex.get("notas", "") or fr["notas"]
        out.append(fr)
    return out
```

---

### 3.5 Auto-learning rules

Cada decisión humana se convierte en una regla reutilizable:

```python
class LearnedRule:
    pattern: str        # ej "TGSS COTIZACION"
    rule_type: str      # "bank" / "invoice"
    action: str         # "route_to_account_476"
    target: str         # "476000"
    partner_id: int     # opcional
    confidence: float   # 0.95 si es decisión humana
    source: str         # "active" (manual) | "passive" (autoaprendido) | "system"
    times_applied: int  # contador
```

Al ejecutar `confirm_proposal` para una línea con concepto "TGSS COTIZACION 12345", el sistema:
1. Aplica la acción a esa línea
2. Deriva un **patrón de 2 palabras** de stop-words filtradas: `TGSS COTIZACION`
3. Crea o actualiza `LearnedRule(pattern="TGSS COTIZACION", action="route_to_476", source="passive")`
4. **Walks** todas las demás líneas no procesadas que matcheen el patrón → aplica acción

```python
def derive_pattern(concept: str) -> str:
    parts = [p for p in concept.split()
             if len(p) >= 3 and p.lower() not in ("del","los","las","con","por","para","que")]
    return " ".join(parts[:2]).upper()
```

**Matching = todas las palabras presentes** (no substring estricto):

```python
def rule_matches(rule_pattern: str, concept: str) -> bool:
    words = rule_pattern.upper().split()
    concept_upper = concept.upper()
    return all(w in concept_upper for w in words)
```

Esto permite que la regla "LIQUIDACION EMISION" matchee "Liquidacion **Por** Emision".

---

### 3.6 Multi-tenant / multi-empresa

Un solo deployment sirviendo N clientes/empresas. Patrón:

```python
# /opt/proyecto/clients.py
CLIENTS = [
    {
        "name": "Cliente A",
        "vat": "...",
        "id_in_erp": 1,
        "drive_folder": "1abc...",   # ID Drive de su carpeta Pendientes
        "email_to": "contabilidad@clienteA.com",
    },
    {
        "name": "Cliente B",
        "vat": "...",
        "id_in_erp": 2,
        "drive_folder": "1xyz...",
        "email_to": "contabilidad@clienteB.com",
    },
]

# Cada script itera y hace per-client
for cfg in CLIENTS:
    process_client(cfg)
```

ORM operations usan `with_company(cfg["id_in_erp"])` o equivalente para aislar.

**Anti-pattern**: NO mezclar IDs entre clientes. Si una `LearnedRule` se crea para Cliente A, NUNCA debe aplicarse a Cliente B. Filtrar siempre por `company_id`.

---

### 3.7 Drive Service Account con cuota=0 (workaround)

Las Service Accounts de Google **no tienen cuota** en Drive personal. Pueden:
- ✅ READ archivos compartidos
- ✅ UPDATE archivos existentes (no consume cuota nueva)
- ✅ MOVE archivos
- ❌ CREATE archivos nuevos (necesita cuota → falla con `storageQuotaExceeded`)

**Workaround para subir archivos nuevos vía SA**:

1. Pre-crear archivos vacíos vía OAuth de un usuario humano (manual o vía MCP tool con tu propia cuenta)
2. Anotar los `file_id` de cada uno
3. SA usa `update(file_id, media_body=...)` para meter el contenido real

```python
# Pre-creado: 7 archivos backup_LUNES.tar.gz ... backup_DOMINGO.tar.gz
DAY_FILE_IDS = {0: "1abc...", 1: "1def...", ..., 6: "1xyz..."}

def upload_daily_backup(local_path):
    weekday = date.today().weekday()
    file_id = DAY_FILE_IDS[weekday]
    media = MediaFileUpload(local_path, mimetype="application/gzip", resumable=True)
    svc.files().update(fileId=file_id, media_body=media, supportsAllDrives=True).execute()
```

Resultado: rolling 7-day backup sin necesidad de OAuth de usuario en el server.

**Alternativa profesional**: usar Google Workspace con Shared Drives — la SA ahí SÍ puede crear archivos. Pero requiere licencia.

---

### 3.8 Backup rolling diario

Un único script que se ejecuta diariamente, escribiendo siempre al slot del día actual:

```python
# /opt/proyecto/backup.py
def main():
    today = date.today()
    file_id = DAY_FILE_IDS[today.weekday()]
    with tempfile.TemporaryDirectory() as tmp:
        # 1. dump DB
        run(["pg_dump", "-Fc", "-d", DB_NAME, "-f", f"{tmp}/db.dump"])
        # 2. tar filestore
        run(["tar", "czf", f"{tmp}/filestore.tar.gz", "-C", DATA_DIR, "filestore"])
        # 3. tar code + configs + secrets
        # 4. INCLUIR RECOVERY.md dentro del tar (autocontenido)
        # 5. tar final consolidado
        # 6. upload to Drive vía SA update()
```

Cron: `0 4 * * *` (madrugada, no choca con la pipeline operativa).

**Decisiones clave**:
- ✅ Incluir el RECOVERY.md DENTRO del tar (si pierdes el server, descargas el tar y ya tienes el manual)
- ✅ Subir TAMBIÉN el RECOVERY.md como archivo independiente en Drive (si no quieres ni descomprimir, lo lees directo)
- ✅ Backup a otro proveedor distinto del que aloja el server (regla 3-2-1)

---

### 3.9 Disaster recovery runbook

Un único `RECOVERY.md` con TODOS los pasos para reconstruir el server desde cero. Estructurado por secciones numeradas:

```
0. Pre-requisitos (qué tener listo antes)
1. Provisión VPS + SSH
2. DNS apunta al server nuevo
3. Dependencias sistema
4. User sistema + DB user
5. Instalar el ERP/app principal
6. RESTORE datos del backup (DB + filestore + ownership)
7. Crear venvs e instalar dependencias Python
8. Autenticar servicios externos (LLM, OAuth, etc.)
9. Configurar systemd
10. Nginx + Let's Encrypt
11. Restaurar cron + sudoers
12. Smoke test
13. Activar snapshot del proveedor (capa adicional)
14. Diagnóstico si algo falla
15. Datos de configuración clave (paths, passwords, IDs)
16. Sobre el backup mismo (estructura, file IDs)
```

**Regla operacional**: cada vez que se cambia algo estructural (nuevo cron, nuevo módulo, nueva ruta, nuevo secret), se actualiza RECOVERY.md **en el mismo turno**. Si no, en 6 meses está obsoleto y no funciona cuando se necesita.

---

### 3.10 Multi-DB en una única instancia (cuando aplique)

Si tu ERP soporta servir múltiples DBs desde el mismo proceso (Odoo, PostgreSQL via schemas), úsalo para tener:
- `prod` — producción
- `test` — sandbox / pruebas
- `demo` — clones para nuevos clientes / demos

**Cuidados**:
- Configurar un `db_filter` por subdomain para que cada URL solo vea su DB
- En las DBs no-prod: **desactivar cron interno y SMTP saliente** (`UPDATE ir_cron SET active=false`) para no disparar acciones reales
- Compartir filestore por DB-name (no cruzar)

---

### 3.11 Tres pasadas de matching (1:1, 1:N, near-match)

Cuando hay que cuadrar items contra otros items (banco vs facturas, eventos vs registros, etc.):

**Pasada 1 — 1:1 score-based** (umbral alto):
- Para cada item, busca el mejor candidato por score multi-criterio (importe + nombre + fecha + ref)
- Si score ≥ 90 → match automático

**Pasada 2 — 1:N subset-sum**:
- Items que no matchearon 1:1 → buscar subconjunto de size 2..12 cuya suma cuadre exacto (±tolerancia)
- Útil cuando un evento agrega varios items (un pago bancario que cuadra contra varias facturas)

**Pasada 3 — near-match** (no acción, surfacear al humano):
- Diferencia 0.5..100 → mostrar en email con candidato para revisión humana
- **Importante**: filtrar por similitud de partner/concepto. Si solo coincide importe → falso positivo.

---

## 4. Pitfalls universales (no domain-specific)

1. **Idempotencia**: `already done` debe tratarse como ✅ no como ❌. Cualquier pipeline que reprocesa por error no debe doblar acciones.
2. **Cursor cerrado tras commit**: nunca acceder a campos ORM tras `cr.commit()` fuera del `with`. Capturar valores en variables locales primero.
3. **Charts/reglas con prefijos especiales**: si el ERP tiene 12 IVAs al 10% (G/S/EX/EU/IG/RC/ND), filtrar por nombre prefijo o coger el "internacional" que neutraliza por error.
4. **Total negativo en facturas → es rectificativa** (refund), no factura negativa. Los ERPs suelen rechazar facturas con importe negativo.
5. **Caracteres especiales (ñ, tildes) en `Edit` o regex**: si una replace silenciosa no aplica, usa otro anchor sin acentos.
6. **El xlsx del usuario se pisa fácil**: leer ANTES de regenerar, mergear preservando texto del user.
7. **DNS local desfasado**: certbot puede triunfar via HTTP-01 (resolución externa) pero `curl` local falla. Usa `--resolve` o IP directa para tests locales.
8. **Drive SA quota=0**: documentado arriba.
9. **Renombrar reset de filestore**: si copias filestore entre DBs, el nombre del subdir DEBE coincidir con el nombre de la DB.
10. **Pg_restore con `--no-owner`**: las tablas quedan dueñas de `postgres`, el ERP no puede leerlas. **Hay que reasignar ownership** post-restore.
11. **Pregunta una a una**: si el usuario tiene preferencia de UX (checklists vs preguntas secuenciales), respétalo. Marca en memoria.
12. **HEIC images**: muchos LLMs no leen HEIC bien — convertir a JPG antes.
13. **Gmail app password 16-char**: distinto de la pwd normal. Generar en Security → App Passwords.
14. **Cron en multi-tenant**: scripts apuntan a un `DB_NAME` hardcoded. Para servir N tenants necesitas o N copias del pipeline (cada una su `DB_NAME`) o un loop interno.

---

## 5. Adaptación a otro dominio

| Dominio nuevo | Mapping |
|---|---|
| Facturas → ERP contable | el caso original |
| Albaranes → sistema logístico | extractor extrae items + cantidades; processor crea albaran en ERP logístico |
| CVs → ATS de RRHH | extractor extrae nombre+experiencia+skills; processor crea candidato en sistema ATS; xlsx dudas para "shortlistear o descartar" |
| Recetas médicas → historial clínico | extractor extrae paciente+medicamentos+posología; processor escribe en historial; reglas aprenden interacciones permitidas |
| Tickets de soporte → Jira/Asana | extractor clasifica prioridad+tipo; processor crea issue; xlsx para reasignación humana |
| Contratos legales → CRM | extractor extrae partes+cláusulas+fechas; processor crea registro; reglas aprenden tipos de cláusulas críticas |

**Lo que cambia**: el prompt LLM, la validación matemática (cada dominio tiene su identidad), las cuentas/objetos del ERP, los patrones del classifier de decisiones libres.

**Lo que NO cambia**:
- Two-venv pattern
- JSON sobre /tmp inter-process
- Carpeta Drive con subcarpetas estado
- xlsx round-trip
- Auto-aprendizaje de reglas
- Backup rolling N-días
- Disaster recovery runbook
- Confidence threshold + auto/draft
- 3-pasadas de matching cuando aplique

---

## 6. Cron típico (referencia)

Horarios separados ~5 min para evitar contención:

```
23:23  refresh_external (descarga reglas/seeds desde Drive)
23:25  learning_passive  (aprende patrones de items recientes)
23:30  extractor         (clasifica + procesa pendientes)  ← el más pesado
23:36  user_in_loop_apply (lee xlsx, ejecuta decisiones)
23:37  apply_rules       (aplica reglas a items pendientes)
23:37  matcher_1to1       (umbral alto)
23:38  matcher_subset    (1:N subset-sum)
23:38  collect_dudas     (regenera lista de dudas)
23:39  publish_xlsx      (sube al user)
23:40  email_summary     (notifica)
04:00  backup_to_drive   (backup nocturno)
```

Si el extractor es muy pesado (docs grandes), separa la pipeline en 2 ejecuciones: una para extraer, otra para procesar.

---

## 7. Inventario de scripts (referencia)

```
extractor.py              # Orquestador: scan carpeta → LLM → router
process_invoice.py        # Handler tipo A
nomina_processor.py       # Handler tipo B
tax_payment_processor.py  # Handler tipo C
bank_importer.py          # Importer de extractos bancarios (CSV/XLS/N43)
bank_matcher.py           # Propone matches 1:1 + AML abiertos
bank_reconciler.py        # Aplica matches 1:1 score>=N
bank_multi_reconciler.py  # 1:N subset-sum
apply_rules_to_bank.py    # Aplica TODAS reglas a TODAS líneas (red de seguridad)
dudas_xlsx_collect.py     # Recoge dudas → JSON
dudas_xlsx_publish.py     # Sube xlsx a Drive
dudas_apply.py            # Lee xlsx, classify_decision, build actions.json
dudas_apply_odoo.py       # Ejecuta actions vía ORM
learning_drive.py         # Descarga reglas manuales del user (CSV)
learning.py               # Importa CSV → tabla rules + scan items posted
email_summary.py          # HTML email + xlsx adjunto
backup_to_drive.py        # Backup diario rolling 7 días
companies.py              # Config multi-cliente
drive_ops.py              # Helpers Drive Service Account
sa_credentials.json       # Service Account JSON (no commit)
email_config.py           # Credenciales SMTP (no commit)
```

---

## 8. Documentos de referencia que vienen con el sistema

| Doc | Función |
|---|---|
| **BLUEPRINT.md** | Este documento — patrones reutilizables |
| **ARQUITECTURA_<proyecto>.md** | Explicación didáctica para no-técnicos |
| **HANDOFF.md** | Cómo llevar el sistema a otro ERP/server |
| **HANDOFF_<cliente>.md** | Cómo añadir un nuevo cliente al deployment existente |
| **RECOVERY.md** | Runbook desastre, paso a paso |
| **MEMORY.md / project_*.md** | Memorias de proyecto para el agente AI |

Mantén estos 6 documentos al día. La regla de oro: **cada cambio estructural actualiza RECOVERY.md en el mismo turno**.

---

## 9. Plantilla de prompt para arrancar otro proyecto similar

> Quiero montar una pipeline de automatización para **<dominio>**. El shape es: documentos llegan a una carpeta Drive, un LLM los clasifica y extrae campos, se crean registros en **<sistema destino>**, hay supervisión humana via xlsx round-trip, y el sistema aprende de las decisiones del usuario.
>
> Adjunto este BLUEPRINT.md que explica los patrones que ya tengo validados. Quiero reutilizar:
> - Two-venv pattern
> - LLM headless via subprocess
> - Confidence threshold + auto/draft
> - User-in-the-loop xlsx
> - Auto-learning rules
> - Backup rolling 7-días
> - Disaster recovery runbook
>
> Lo nuevo a definir es:
> - El prompt LLM específico para mi dominio
> - La validación matemática/lógica post-extracción
> - Los handlers por tipo de documento
> - Los patrones de classify_decision para mi UX
> - El ERP destino y su API/ORM
>
> Pregúntame **una a una** las decisiones críticas:
> 1. ¿Qué carpeta Drive y Service Account?
> 2. ¿Qué sistema destino y cómo accedo?
> 3. ¿Qué tipos de documento y prompt LLM?
> 4. ¿Qué umbral de confidence y qué validación?

---

Fin BLUEPRINT.md.
