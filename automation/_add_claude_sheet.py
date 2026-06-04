"""Inyecta hoja Para_Claude en build_rules_xlsx.py — onboarding completo."""
from pathlib import Path

p = Path("/opt/automation/build_rules_xlsx.py")
s = p.read_text()

anchor = "    # ====== Nóminas ======"
assert anchor in s, "anchor not found"

# Bloque a inyectar: usa raw strings y comillas dobles internas
claude_block = '''    # ====== Para Claude (onboarding completo del sistema) ======
    ws_c = wb.create_sheet("Para_Claude")
    ws_c.column_dimensions["A"].width = 28
    ws_c.column_dimensions["B"].width = 130
    titulo = ws_c.cell(row=1, column=1, value="Onboarding para otro agente Claude - leeme primero")
    titulo.font = Font(bold=True, size=14, color="1f4e79")
    ws_c.merge_cells("A1:B1")
    ws_c.row_dimensions[1].height = 28

    onboarding = [
        ("Que hace este sistema",
         "Pipeline contable nocturno autonomo: cada noche el cron del VPS recoge documentos (facturas, nominas, IRPF, SS, extractos bancarios) de carpetas Drive de las empresas, los clasifica con un LLM (Claude headless), los procesa y crea asientos en Odoo 17 Community via ORM. Bancos se concilian en 3 pasadas (1:1, subset-sum 1:N, near-match). Lo que el sistema no resuelve solo lo escribe en un xlsx dudas_para_revisar.xlsx en Drive; el usuario rellena la columna tu_decision con texto libre, el bot lee, clasifica y ejecuta via ORM. Cada decision humana se convierte en learned.rule auto-aplicable a lineas futuras. Email diario con resumen + xlsx adjunto."),

        ("Empresas en produccion",
         "BD cararjfam: empresa 1 = CARARJFAM2019,SL (CIF B93653392, tiene socios autonomos: Carlos Alcalde, Concepcion Arjona, con salario_especie). Empresa 2 = BEST TRAINING RINCON DE LA VICTORIA SL (CIF B72349137, trabajadores normales). BD cararjfam_test: sandbox + empresa 3 = AUSTRAL (CIF ESB44821965, usuario contabilidad@austral.es). BD demo: vacia, sin uso."),

        ("Servidor",
         "IONOS Ubuntu 24.04, IP 212.227.40.122, dominios erp.carajfam.com (prod) y demo.carajfam.com (test/austral). SSH como root con clave ed25519 en ~/.ssh/odoo_carajfam (PasswordAuthentication off, fail2ban activo). Odoo 17 en /opt/odoo17/odoo, config /etc/odoo17.conf, data_dir /opt/odoo17/.local/share/Odoo. Custom addon learned_rules en /opt/odoo17/custom-addons/. Master Odoo BD: ver admin_passwd en odoo17.conf."),

        ("Patron critico - dos venvs",
         "NUNCA mezclar libs en el mismo venv: /opt/odoo17/venv (Odoo + psycopg2 + pyOpenSSL 21) vs /opt/automation/venv (Google API + pandas + openpyxl + xlrd + csb43). Instalar google-api-python-client en el venv de Odoo rompe pyOpenSSL.X509StoreFlags y tumba Odoo. Los scripts se comunican entre venvs por JSON en /tmp + subprocess (dudas_apply.py llama a dudas_apply_odoo.py)."),

        ("Pipeline diaria (cron user odoo, hora Madrid)",
         "23:23 learning_drive (descarga reglas CSV de Drive) -> 23:25 learning (importa reglas + scan pasivo facturas posted) -> 23:30 extractor (LLM clasifica PDFs y enruta) -> 23:35 poller (legacy, idle) -> 23:36 dudas_apply (lee xlsx tu_decision, ejecuta via ORM) -> 23:37 apply_rules_to_bank (aplica TODAS las learned.rule a lineas no conciliadas) + bank_reconciler --threshold 90 -> 23:38 bank_multi_reconciler (subset-sum) + dudas_xlsx_collect (recoge dudas -> JSON) -> 23:39 dudas_xlsx_publish (sube xlsx a Drive) -> 23:40 email_summary (HTML + xlsx adjunto). Extra: 04:00 backup_to_drive (rolling 7 dias); 08:00 los dias 10 de ene/abr/jul/oct: periodic_expenses_check."),

        ("Drive - Service Account + workaround quota=0",
         "SA email: vps-odoo-automation@carajfam-automation.iam.gserviceaccount.com (creds en /etc/automation_sa.json). Tiene quota 0 en Drive personal del usuario; NO puede CREATE archivos nuevos, solo UPDATE existentes. Workaround: archivos como dudas xlsx, RECOVERY.md, REGLAS_SISTEMA.xlsx y los 7 backup_DIA.tar.gz se PRE-CREARON via OAuth del usuario humano (file_ids fijos hardcodeados en codigo). La SA solo los actualiza. Si necesitas crear un archivo nuevo, usa el MCP create_file de Drive con cuenta del usuario, no la SA. Carpetas Drive por empresa: CARARJFAM 'Mi Odoo CARARJFAM' (1RZjKO1GqJuPURl6WTsl2R9egwm7cyYFQ), BT (en companies.py:pending_folder), AUSTRAL 'Mi Odoo AUSTRAL' (1SNDTko-SgeYNjyJ-_635ObprBDVWm-Jd). Subcarpetas estandar por empresa: Pendientes (raiz, donde sube docs el usuario) + Contabilizado odoo + revision + Aprendizajes_aplicados + ignorados (creada solo cuando rechazo se marca como ignorar)."),

        ("Documentos hermanos - leelos en este orden si arrancas desde 0",
         "1) /opt/automation/RECOVERY.md (sync en Drive file_id 1cYLjkrXgkJxKS-2nBD8GX0DFugMLznZe) - runbook completo de reinstalacion + lista de datos de configuracion clave. Imprescindible. 2) BLUEPRINT.md (en repo local odoo-deploy/) - patrones reutilizables si vas a montar algo similar en otro dominio. 3) HANDOFF.md / HANDOFF_AUSTRAL.md - como portar el sistema a otro ERP / anadir una empresa nueva. 4) ARQUITECTURA_CARAJFAM.md - explicacion didactica del sistema para humanos no-tecnicos. 5) Memoria del proyecto Claude: ~/.claude/projects/.../memory/MEMORY.md y archivos referenciados (preferencias usuario + reglas operativas, como 'pregunta una a una' y 'actualiza RECOVERY.md en cada cambio estructural')."),

        ("Como regenero ESTE xlsx (REGLAS_SISTEMA.xlsx)",
         "sudo -u odoo /opt/automation/venv/bin/python /opt/automation/build_rules_xlsx.py. El script lee las reglas hardcodeadas en su propia code, construye xlsx con openpyxl en /tmp, sube via SA UPDATE al file_id fijo 1lX032XqZ63u73jM6wDlIZsekqeQ-pzkG. Si anades reglas nuevas, edita el script (seccion rules_<area>.append({...})) y vuelve a ejecutar."),

        ("Reglas operativas no negociables (memoria del proyecto)",
         "1) Pregunta UNA cosa a la vez al usuario (Carlos), no listas de 5 puntos numerados; no le gustan. 2) Cada cambio ESTRUCTURAL del VPS (nuevo script, nuevo cron, nuevo addon, nueva BD, nuevo subdomain, nuevo modulo, cambio de path) -> actualizar RECOVERY.md en local + scp al VPS + UPDATE en Drive (file_id 1cYLjkrXgkJxKS-2nBD8GX0DFugMLznZe) en el MISMO turno. Si no, en 6 meses el RECOVERY esta obsoleto y el restore no funciona cuando se necesita. 3) Si una regla aprendida (learned.rule) tiene caso edge especifico de una empresa, marcarla con company_id correcto; NO mezclar reglas entre empresas (los socios autonomos de CARARJFAM no aplican a BT). 4) Idempotencia: 'already reconciled' / 'already exists' es exito, NO error. Pipelines re-pasan sin doblar acciones."),

        ("Pitfalls universales del proyecto",
         "1) Total negativo factura proveedor = factura rectificativa (in_refund), no in_invoice negativo (Odoo rechaza). 2) Chart l10n_es trae 12 IVAs al mismo tipo (G/S/EX/EU/IG/RC/ND); filtrar prefijos especiales o el calculo IVA falla. 3) En CARARJFAM las cuentas 475100 / 476000 son 6 digitos, no 4 (4751/4760 NO existen). 4) Cuentas 465/475/476/477 requieren reconcile=True; tras restore hay que habilitarlo manualmente. 5) Bank reconciler: nunca confiar SOLO en importe; filtrar por similitud partner/concepto, sino cruza Culligan 72,88 con MediaMarkt 30E (caso real). 6) Lineas contactless TPV: solo match 100% exacto contra factura; NUNCA partial ni near-match (regla usuario tras falsos positivos). 7) Cursor cerrado tras cr.commit() fuera del 'with' -> InterfaceError; capturar valores ORM en variables locales antes. 8) HEIC images (iPhone): Claude headless no las lee bien; convertir a JPG primero. 9) Gmail SMTP requiere app-password 16 chars (Security -> App Passwords), NO la pwd normal. 10) Charts l10n_es PYMES en Odoo 17 cambio name (campo translatable) y account_type; tras restore validar que account_type sea correcto (asset_cash, liability_current, etc)."),

        ("Como entrar a debuggear sin romper nada",
         "Para QUERY/READ: sudo -u postgres psql -d cararjfam -c 'SELECT ...' o via ORM con sudo -u odoo /opt/odoo17/venv/bin/python -c '...' importando odoo.api.Environment. Para PROBAR cambios sin afectar prod: usa BD cararjfam_test (clon del estado actual + cron interno desactivado + SMTP off; no envia emails reales). Para reset rapido cararjfam_test: pg_dump cararjfam | pg_restore cararjfam_test (script en RECOVERY.md seccion 16)."),

        ("Como se notifica al usuario",
         "Email diario a c.alcalde.campusport@gmail.com via Gmail SMTP (app password 16 chars en /opt/automation/email_config.py). Contiene: lista facturas creadas hoy (link a Odoo), seccion Documentos rechazados como duplicado, seccion de conciliaciones bancarias propuestas/near-matches, xlsx adjunto por empresa. El usuario rellena la columna tu_decision del xlsx y al dia siguiente el cron procesa esa decision."),

        ("Conceptos contables espanoles esenciales",
         "PGC PYMES Espana: 400/410 proveedores, 430 clientes, 465 remuneraciones pdtes pago empleados, 475100 HP retenciones IRPF (6 digitos!), 476000 Org SS acreedores, 520 prestamos corto plazo, 572 banco, 600 compras, 625 seguros, 626 servicios bancarios, 629 otros (placeholder no deducible), 640 sueldos, 642 SS empresa, 477 HP IVA repercutido. Asiento de nomina (spec usuario): DR 640=bruto total devengo + DR 642=aport empresa SS / CR 4751=IRPF + CR 476=aport_empresa+ss_trab+especie_socio + CR 465.NNN per trabajador=liquido_cash. Tipos cotizacion SS 2026: CC empresa 24,35% / desempleo empresa indef 5,50% temp 6,70% / FP empresa 0,60% / FOGASA 0,20% / CC trabajador 4,85% / desempleo trabajador 1,55% / FP trabajador 0,10%. BCCC empleado DEBE = BCCC empresa (LGSS 147)."),

        ("Para entender ESTE xlsx",
         "9 hojas. README = metadatos. Para_Claude = esta hoja (onboarding). Resto = una hoja por area del sistema (Nominas, Facturas, Bancos, Dudas, Aprendizaje, Extractor, Backup, Gastos_periodicos). Cada fila = una regla con ID unico (NOM01, FAC02, etc), severidad coloreada (rojo=error/critical, amarillo=warning, azul=info), columna Empresa (GLOBAL/TODAS/CARARJFAM/BT/AUSTRAL/combinaciones). Si dos empresas necesitan reglas opuestas, duplicar fila con cada Empresa correspondiente."),
    ]
    row = 2
    section_fill = PatternFill(start_color="e7f0fa", end_color="e7f0fa", fill_type="solid")
    for titulo_sec, contenido in onboarding:
        c1 = ws_c.cell(row=row, column=1, value=titulo_sec)
        c1.font = Font(bold=True, color="1f4e79", size=11)
        c1.fill = section_fill
        c1.alignment = Alignment(vertical="top", wrap_text=True)
        c2 = ws_c.cell(row=row, column=2, value=contenido)
        c2.alignment = Alignment(wrap_text=True, vertical="top")
        c2.font = Font(size=10)
        ws_c.row_dimensions[row].height = max(40, min(330, 15 + len(contenido) // 130 * 14))
        row += 1
    ws_c.freeze_panes = "A2"

'''

s = s.replace(anchor, claude_block + anchor)
p.write_text(s)
print("Para_Claude sheet injected")
