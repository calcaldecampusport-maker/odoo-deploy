"""Phase 2 patches: add 4 actions on Rechazados sheet (borrar/ignorar/reprocesar/CIF correcto).

Modifies:
- dudas_apply.py: add Rechazados sheet reader + classify_decision_rechazo
- dudas_apply_odoo.py: handlers for borrar / ignorar / reprocesar / vat_correction
- process_invoice.py: consult vat_correction rules before VAT validation
"""
from pathlib import Path
import re

# === 1) Patch dudas_apply.py ===
p1 = Path("/opt/automation/dudas_apply.py")
s1 = p1.read_text()

# 1a) New classifier function for Rechazados decisions
new_classifier = '''
def classify_rechazo_decision(decision: str) -> dict:
    """Map free-text decision on Rechazados sheet to action."""
    d = (decision or "").lower().strip()
    if not d:
        return {"action": "skip", "label": ""}

    if any(k in d for k in ["borrar", "eliminar", "tirar", "delete", "papelera"]):
        return {"action": "rechazo_borrar", "label": "BORRADO"}

    if any(k in d for k in ["ignorar", "omitir", "saltar", "dejar"]):
        return {"action": "rechazo_ignorar", "label": "IGNORADO"}

    if any(k in d for k in ["reprocesar", "volver a procesar", "intentar de nuevo", "intenta", "vuelve a procesar", "reintentar"]):
        return {"action": "rechazo_reprocesar", "label": "REPROCESAR"}

    # Detect CIF/NIF Spanish format: optional letter + 7-8 digits + optional letter
    import re
    m = re.search(r"\\b([a-z]?\\d{7,8}[a-z]?)\\b", d)
    has_vat_keyword = any(k in d for k in ["cif", "nif", "vat", "es el"])
    if m and (has_vat_keyword or len(d) < 80):
        vat = m.group(1).upper()
        return {"action": "rechazo_cif", "label": "CIF_CORREGIDO", "vat": vat}

    return {"action": "human", "label": "PENDIENTE_HUMANO_RECHAZO"}


def collect_rechazos(svc, cfg) -> list[dict]:
    """Read the Rechazados sheet of the company xlsx. Returns list of decisions."""
    folder = cfg.get("pending_folder")
    q = f"'{folder}' in parents and trashed=false and name='{XLSX_NAME}'"
    files = svc.files().list(q=q, fields="files(id,name)", supportsAllDrives=True).execute().get("files", [])
    if not files:
        return []
    fid = files[0]["id"]
    raw = svc.files().get_media(fileId=fid, supportsAllDrives=True).execute()
    wb = load_workbook(io.BytesIO(raw))
    if "Rechazados" not in wb.sheetnames:
        return []
    ws = wb["Rechazados"]
    out = []
    for idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True)):
        if not row or not row[0]:
            continue
        archivo, file_id, motivo, tu_decision, notas = (row + (None, None, None, None, None))[:5]
        decision = (tu_decision or "").strip() if tu_decision else ""
        if not decision:
            continue
        out.append({
            "row_index": idx,
            "archivo": archivo,
            "drive_file_id": file_id,
            "motivo": motivo,
            "tu_decision": decision,
            "notas": notas or "",
        })
    return out

'''

# Insert before def main()
anchor = "def main():\n    p = argparse.ArgumentParser()"
assert anchor in s1, "main anchor"
s1 = s1.replace(anchor, new_classifier + "\n" + anchor)

# 1b) In main(), after processing dudas, also process rechazos
# Find the actions_payload loop and inject rechazos after
old_main_block = '''        log.info(f"  {len(decisions)} decisions -> classified")
        for a in actions_payload[:20]:
            log.info(f"    row#{a['row_index']} tipo={a['tipo']} id={a['id_odoo']} -> {a['label']}")'''
new_main_block = old_main_block + '''

        # ===== RECHAZOS sheet =====
        rechazos = collect_rechazos(svc, cfg)
        rechazo_actions = []
        for r in rechazos:
            cls = classify_rechazo_decision(r["tu_decision"])
            rechazo_actions.append({
                "row_index": r["row_index"],
                "archivo": r["archivo"],
                "drive_file_id": r["drive_file_id"],
                "motivo": r["motivo"],
                "decision": r["tu_decision"],
                "action": cls["action"],
                "label": cls["label"],
                "vat": cls.get("vat"),
            })
        if rechazo_actions:
            log.info(f"  {len(rechazo_actions)} rechazo decisions classified")
            for a in rechazo_actions:
                log.info(f"    rechazo {a['archivo']} -> {a['label']}{' VAT='+a['vat'] if a.get('vat') else ''}")
        actions_payload.extend(rechazo_actions)'''
assert old_main_block in s1, "main block"
s1 = s1.replace(old_main_block, new_main_block)

p1.write_text(s1)
print("1) dudas_apply.py: classifier+reader+main injected")


# === 2) Patch dudas_apply_odoo.py ===
p2 = Path("/opt/automation/dudas_apply_odoo.py")
s2 = p2.read_text()

new_handlers = '''
import re as _re
def _drive_svc():
    import sys
    sys.path.insert(0, "/opt/automation")
    from drive_ops import _service
    return _service()


def _handle_rechazo(env, action: dict) -> dict:
    """Handle 'rechazo_borrar', 'rechazo_ignorar', 'rechazo_reprocesar', 'rechazo_cif'."""
    res = {"row_index": action.get("row_index"), "archivo": action.get("archivo")}
    file_id = action.get("drive_file_id")
    if not file_id:
        res.update({"estado_actual": "ERROR_RECHAZO", "note": "drive_file_id ausente"})
        return res
    svc = _drive_svc()
    act = action["action"]
    try:
        if act == "rechazo_borrar":
            svc.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
            res.update({"estado_actual": "BORRADO", "note": "movido a papelera Drive"})
            return res

        if act == "rechazo_ignorar":
            # Find or create 'ignorados' subfolder under the same parent
            f = svc.files().get(fileId=file_id, fields="parents,name", supportsAllDrives=True).execute()
            parent = f["parents"][0]
            # Get root folder (Pendientes/) — assume parent is revision/, so go one up
            rev_meta = svc.files().get(fileId=parent, fields="name,parents", supportsAllDrives=True).execute()
            root = rev_meta["parents"][0] if rev_meta.get("parents") else parent
            # Search for 'ignorados' subfolder
            q = "'%s' in parents and name='ignorados' and mimeType='application/vnd.google-apps.folder' and trashed=false" % root
            sub = svc.files().list(q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
            if sub:
                ign_id = sub[0]["id"]
            else:
                ign_id = svc.files().create(body={"name": "ignorados", "mimeType": "application/vnd.google-apps.folder", "parents": [root]}, fields="id", supportsAllDrives=True).execute()["id"]
            svc.files().update(fileId=file_id, addParents=ign_id, removeParents=parent, supportsAllDrives=True).execute()
            res.update({"estado_actual": "IGNORADO", "note": "movido a ignorados/"})
            return res

        if act in ("rechazo_reprocesar", "rechazo_cif"):
            # Move back to root pending folder
            f = svc.files().get(fileId=file_id, fields="parents", supportsAllDrives=True).execute()
            parent = f["parents"][0]
            rev_meta = svc.files().get(fileId=parent, fields="parents", supportsAllDrives=True).execute()
            root = rev_meta["parents"][0] if rev_meta.get("parents") else parent
            svc.files().update(fileId=file_id, addParents=root, removeParents=parent, supportsAllDrives=True).execute()

            note_extra = ""
            if act == "rechazo_cif":
                vat = action.get("vat")
                # Extract partner name from motivo: "para el contacto [<NAME>]"
                motivo = action.get("motivo") or ""
                m = _re.search(r"contacto \\[([^\\]]+)\\]", motivo)
                partner_name = (m.group(1).strip() if m else "").upper()
                if not partner_name:
                    res.update({"estado_actual": "ERROR_RECHAZO", "note": "no pude extraer partner del motivo"})
                    return res
                # Create or update learned.rule vat_correction
                Rule = env["learned.rule"]
                existing = Rule.search([
                    ("rule_type", "=", "vat_correction"),
                    ("pattern", "=", partner_name),
                ], limit=1)
                if existing:
                    existing.write({"notes": vat, "confidence": 0.99})
                else:
                    Rule.create({
                        "name": f"VAT correcto {partner_name[:30]}",
                        "pattern": partner_name,
                        "rule_type": "vat_correction",
                        "company_id": env.user.company_id.id,
                        "notes": vat,
                        "confidence": 0.99,
                        "source": "active",
                    })
                # Also try to update existing partner if it exists
                p = env["res.partner"].search([("name", "ilike", partner_name)], limit=1)
                if p:
                    try:
                        p.with_context(no_vat_validation=True).write({"vat": vat})
                    except Exception:
                        try:
                            env.cr.execute("UPDATE res_partner SET vat=%s WHERE id=%s", (vat, p.id))
                        except Exception as e:
                            note_extra = f" (no se actualizó partner existente: {str(e)[:60]})"
                note_extra = f"VAT correcto guardado: {partner_name}={vat}" + note_extra
            res.update({"estado_actual": "REPROCESAR", "note": f"movido a Pendientes/. {note_extra}".strip()})
            return res

        res.update({"estado_actual": "ERROR_RECHAZO", "note": f"acción desconocida {act}"})
        return res
    except Exception as e:
        res.update({"estado_actual": "ERROR_RECHAZO", "note": f"exception: {str(e)[:200]}"})
        return res

'''

# Insert handlers before def execute(
anchor2 = "def execute(env, action: dict) -> dict:"
assert anchor2 in s2, "execute anchor"
s2 = s2.replace(anchor2, new_handlers + "\n" + anchor2)

# Inject dispatch in execute(): early branch for rechazo_*
exec_inject_anchor = '    res = {"row_index": action["row_index"], "id_odoo": action["id_odoo"]}'
exec_inject_replacement = '''    if action.get("action", "").startswith("rechazo_"):
        return _handle_rechazo(env, action)
    res = {"row_index": action["row_index"], "id_odoo": action["id_odoo"]}'''
assert exec_inject_anchor in s2, "exec inject"
s2 = s2.replace(exec_inject_anchor, exec_inject_replacement)

p2.write_text(s2)
print("2) dudas_apply_odoo.py: rechazo handlers + dispatch")


# === 3) Patch process_invoice.py to consult vat_correction ===
p3 = Path("/opt/automation/process_invoice.py")
s3 = p3.read_text()

# Find the supplier creation/update logic. The key is: before creating partner with extracted VAT,
# look up vat_correction rule by supplier_name and override.
# Look for a typical pattern like creating partner...

# Add a helper at module-level (after imports) and a hook in supplier resolution
helper = '''

def _maybe_correct_vat(env, supplier_name: str, supplier_vat: str) -> tuple[str, str | None]:
    """Consult learned.rule vat_correction rules and override VAT if pattern matches.
    Returns (corrected_vat, applied_pattern or None)."""
    if not supplier_name:
        return supplier_vat, None
    name_upper = supplier_name.upper()
    try:
        rule = env["learned.rule"].search([
            ("rule_type", "=", "vat_correction"),
        ], limit=20)
        for r in rule:
            if (r.pattern or "").upper() in name_upper or name_upper in (r.pattern or "").upper():
                corrected = (r.notes or "").strip().upper()
                if corrected:
                    return corrected, r.pattern
    except Exception:
        pass
    return supplier_vat, None

'''

# Insert right after the imports / before first def
import_end_anchor = "log = logging.getLogger("
assert import_end_anchor in s3
s3 = s3.replace(import_end_anchor, helper + "\n" + import_end_anchor, 1)

# Find where supplier is resolved - search for "supplier_vat" references / partner creation
# Typical Odoo pattern: search by VAT, then create. We override VAT BEFORE that.
# Look for: `data["supplier_vat"]` and inject correction nearby
inject_marker = 'supplier_vat = (data.get("supplier_vat") or "").strip()'
if inject_marker in s3:
    new_lines = '''supplier_vat = (data.get("supplier_vat") or "").strip()
    supplier_name = (data.get("supplier_name") or "").strip()
    corrected_vat, applied = _maybe_correct_vat(env, supplier_name, supplier_vat)
    if applied:
        log.info(f"  vat_correction applied: {applied!r} -> VAT {supplier_vat!r} replaced with {corrected_vat!r}")
        supplier_vat = corrected_vat'''
    s3 = s3.replace(inject_marker, new_lines)
    print("3) process_invoice.py: vat_correction hook injected at supplier_vat resolution")
else:
    # Fallback: search for any location parsing supplier_vat
    print("3) WARNING: marker 'supplier_vat = ...' not found; manual inspection needed")

p3.write_text(s3)
print("Phase 2 patches applied.")
