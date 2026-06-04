"""Inyecta 4 reglas anti-duplicado en la hoja Facturas de build_rules_xlsx.py."""
from pathlib import Path

p = Path("/opt/automation/build_rules_xlsx.py")
s = p.read_text()

anchor = '''        {"empresa": "GLOBAL", "id": "FAC07", "regla": "Cuenta default por doc_type",
         "formula": "invoice→600000, nomina→640000, irpf_payment→475100, ss_payment→642000, other_official→629000",
         "severidad": "info", "fuente": "PGC PYMES",
         "script": "process_invoice.py:DOC_TYPE_DEFAULT_ACCOUNT", "notas": ""},'''

new_block = '''        {"empresa": "GLOBAL", "id": "FAC07", "regla": "Cuenta default por doc_type",
         "formula": "invoice→600000, nomina→640000, irpf_payment→475100, ss_payment→642000, other_official→629000",
         "severidad": "info", "fuente": "PGC PYMES",
         "script": "process_invoice.py:DOC_TYPE_DEFAULT_ACCOUNT", "notas": ""},
        {"empresa": "GLOBAL", "id": "FAC08", "regla": "Búsqueda partner por VARIANTES de VAT",
         "formula": "Antes de crear partner, search por: VAT raw, VAT canonical (con ES), VAT sin ES. Si cualquiera matchea → reutilizar partner existente (no crear duplicado).",
         "severidad": "critical", "fuente": "post-mortem GANESHA (B86002318 vs ESB86002318 → 2 partners)",
         "script": "process_invoice.py:find_or_create_supplier",
         "notas": "normalize_vat puede producir formas distintas según el input; buscar solo por la canonical pierde matches."},
        {"empresa": "GLOBAL", "id": "FAC09", "regla": "Fallback por nombre normalizado",
         "formula": "Si no encuentra por VAT (variantes), buscar partner por nombre normalizado (upper + collapse spaces + strip SL/SA suffix). Si matchea → reutilizar y actualizar VAT.",
         "severidad": "critical", "fuente": "post-mortem MATEO MOTOR (mismo VAT pero 2 partners por race)",
         "script": "process_invoice.py:find_or_create_supplier + _norm_partner_name",
         "notas": "Detecta duplicados causados por race / typo VAT / VAT ausente. Solo crea nuevo partner si nada matchea."},
        {"empresa": "GLOBAL", "id": "FAC10", "regla": "NUNCA SQL crudo para campos validados (vat, email)",
         "formula": "SIEMPRE usar partner.write({'vat': X}). Raw SQL UPDATE res_partner SET vat=... salta normalize_vat de base_vat y produce duplicados.",
         "severidad": "critical", "fuente": "post-mortem GANESHA (yo causé el duplicado con SQL crudo)",
         "script": "dudas_apply_odoo._create_vat_correction (corregido)",
         "notas": "Si base_vat rechaza checksum → with_context(no_vat_validation=True).write() como fallback."},
        {"empresa": "GLOBAL", "id": "FAC11", "regla": "Cron diario detector duplicados",
         "formula": "Cron 06:00 diario ejecuta detect_duplicate_partners.py: 3 SQL detectan VAT-idéntico, nombre-normalizado-idéntico, VAT-igual-salvo-prefijo-ES. Si encuentra → email alerta.",
         "severidad": "warning", "fuente": "safety net post-mortem",
         "script": "detect_duplicate_partners.py + cron 0 6 * * *",
         "notas": "Detecta cualquier duplicado que se cuele a pesar de FAC08+FAC09 (race, manual UI input, restore)."},'''

assert anchor in s, "anchor not found"
p.write_text(s.replace(anchor, new_block))
print("4 anti-duplicate rules injected")
