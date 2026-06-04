"""Refactor: rechazo Drive ops happen in dudas_apply.py (automation venv).
Only vat_correction LEARNED RULE + PARTNER UPDATE go to ORM helper.
"""
from pathlib import Path

# === Refactor dudas_apply_odoo.py ===
p2 = Path("/opt/automation/dudas_apply_odoo.py")
s = p2.read_text()

# Remove old _drive_svc / _handle_rechazo blocks — they tried to import drive_ops in odoo venv
# Find from `import re as _re\ndef _drive_svc():` until `\n\n` after `_handle_rechazo` body
import re
s = re.sub(
    r"\nimport re as _re\ndef _drive_svc\(\):.*?(?=\ndef execute\(env, action: dict\) -> dict:)",
    "\n\nimport re as _re\n\n",
    s,
    flags=re.DOTALL,
    count=1,
)

# Replace dispatch in execute() — handle rechazo_cif (which is what comes from automation side)
old_dispatch = '''    if action.get("action", "").startswith("rechazo_"):
        return _handle_rechazo(env, action)
    res = {"row_index": action["row_index"], "id_odoo": action["id_odoo"]}'''
new_dispatch = '''    # New: VAT correction comes from rechazo_cif processed by dudas_apply
    if action.get("action") == "create_vat_correction":
        return _create_vat_correction(env, action)
    # Skip rechazo_* actions that have no id_odoo (Drive-only ops handled upstream)
    if action.get("action", "").startswith("rechazo_") or "id_odoo" not in action:
        return {"row_index": action.get("row_index"), "estado_actual": action.get("label","SKIP"), "note": "drive op"}
    res = {"row_index": action["row_index"], "id_odoo": action["id_odoo"]}'''
assert old_dispatch in s, "dispatch"
s = s.replace(old_dispatch, new_dispatch)

# Add _create_vat_correction handler before execute
new_helper = '''
def _create_vat_correction(env, action: dict) -> dict:
    """Create/update learned.rule(vat_correction) and try to fix existing partner."""
    res = {"row_index": action.get("row_index"), "archivo": action.get("archivo")}
    partner_name = (action.get("partner_name") or "").upper().strip()
    vat = (action.get("vat") or "").upper().strip()
    company_id = action.get("company_id")
    if not (partner_name and vat):
        res.update({"estado_actual": "ERROR_RECHAZO", "note": "partner/vat ausentes"})
        return res
    Rule = env["learned.rule"]
    domain = [("rule_type", "=", "vat_correction"), ("pattern", "=", partner_name)]
    if company_id:
        domain.append(("company_id", "=", company_id))
    existing = Rule.search(domain, limit=1)
    if existing:
        existing.write({"notes": vat, "confidence": 0.99})
    else:
        vals = {
            "name": f"VAT correcto {partner_name[:30]}",
            "pattern": partner_name,
            "rule_type": "vat_correction",
            "notes": vat,
            "confidence": 0.99,
            "source": "active",
        }
        if company_id:
            vals["company_id"] = company_id
        Rule.create(vals)

    # Try to update existing partner if any
    p = env["res.partner"].search([("name", "ilike", partner_name)], limit=1)
    extra = ""
    if p:
        try:
            env.cr.execute("UPDATE res_partner SET vat=%s WHERE id=%s", (vat, p.id))
            extra = f" + partner {p.name} actualizado"
        except Exception as e:
            extra = f" (no se actualizó partner: {str(e)[:60]})"
    res.update({"estado_actual": "CIF_CORREGIDO", "note": f"learned.rule guardada {partner_name}={vat}{extra}"})
    return res

'''

# Insert before def execute
anchor = "def execute(env, action: dict) -> dict:"
assert anchor in s
s = s.replace(anchor, new_helper + "\n" + anchor)

p2.write_text(s)
print("dudas_apply_odoo.py: refactored — only ORM actions; Drive ops moved to dudas_apply")


# === Refactor dudas_apply.py: Drive ops happen here ===
p1 = Path("/opt/automation/dudas_apply.py")
s1 = p1.read_text()

drive_handler = '''
def _do_drive_action(svc, action: dict, log_obj=None) -> dict:
    """Execute Drive ops for rechazo_borrar / _ignorar / _reprocesar. Returns result dict."""
    import re as _re
    file_id = action.get("drive_file_id")
    res = {"row_index": action.get("row_index"), "archivo": action.get("archivo"),
           "decision": action.get("decision"), "label": action.get("label")}
    if not file_id:
        res.update({"estado_actual": "ERROR_RECHAZO", "note": "drive_file_id ausente"})
        return res
    try:
        meta = svc.files().get(fileId=file_id, fields="parents,name", supportsAllDrives=True).execute()
        if not meta.get("parents"):
            res.update({"estado_actual": "ERROR_RECHAZO", "note": "archivo sin parents"})
            return res
        parent = meta["parents"][0]
        rev_meta = svc.files().get(fileId=parent, fields="parents", supportsAllDrives=True).execute()
        root = rev_meta["parents"][0] if rev_meta.get("parents") else parent

        act = action["action"]
        if act == "rechazo_borrar":
            svc.files().update(fileId=file_id, body={"trashed": True}, supportsAllDrives=True).execute()
            res.update({"estado_actual": "BORRADO", "note": "movido a papelera Drive"})
            return res

        if act == "rechazo_ignorar":
            q = "'%s' in parents and name='ignorados' and mimeType='application/vnd.google-apps.folder' and trashed=false" % root
            sub = svc.files().list(q=q, fields="files(id)", supportsAllDrives=True, includeItemsFromAllDrives=True).execute().get("files", [])
            if sub:
                ign_id = sub[0]["id"]
            else:
                ign_id = svc.files().create(
                    body={"name": "ignorados", "mimeType": "application/vnd.google-apps.folder", "parents": [root]},
                    fields="id", supportsAllDrives=True,
                ).execute()["id"]
            svc.files().update(fileId=file_id, addParents=ign_id, removeParents=parent, supportsAllDrives=True).execute()
            res.update({"estado_actual": "IGNORADO", "note": "movido a ignorados/"})
            return res

        if act in ("rechazo_reprocesar", "rechazo_cif"):
            svc.files().update(fileId=file_id, addParents=root, removeParents=parent, supportsAllDrives=True).execute()
            res.update({"estado_actual": "REPROCESAR", "note": "movido a Pendientes/ — extractor lo procesará en la próxima pasada"})
            return res

        res.update({"estado_actual": "ERROR_RECHAZO", "note": f"acción desconocida {act}"})
        return res
    except Exception as e:
        res.update({"estado_actual": "ERROR_RECHAZO", "note": f"exception: {str(e)[:200]}"})
        return res


def _extract_partner_from_motivo(motivo: str) -> str:
    import re as _re
    m = _re.search(r"contacto \\[([^\\]]+)\\]", motivo or "")
    return (m.group(1).strip() if m else "").upper()

'''

# Insert before def main()
anchor1 = "def main():\n    p = argparse.ArgumentParser()"
assert anchor1 in s1
s1 = s1.replace(anchor1, drive_handler + "\n" + anchor1)

# In main(), replace the rechazos extension block to do Drive ops in-place
# and only enqueue ORM action for CIF
old_rechazo_block = '''        # ===== RECHAZOS sheet =====
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

new_rechazo_block = '''        # ===== RECHAZOS sheet =====
        rechazos = collect_rechazos(svc, cfg)
        rechazo_results = []
        for r in rechazos:
            cls = classify_rechazo_decision(r["tu_decision"])
            action = {
                "row_index": r["row_index"],
                "archivo": r["archivo"],
                "drive_file_id": r["drive_file_id"],
                "motivo": r["motivo"],
                "decision": r["tu_decision"],
                "action": cls["action"],
                "label": cls["label"],
                "vat": cls.get("vat"),
            }
            # Drive op (works in this venv since we have google libs)
            if cls["action"].startswith("rechazo_"):
                drive_res = _do_drive_action(svc, action)
                rechazo_results.append(drive_res)
                # For rechazo_cif, also queue an ORM action to create learned.rule + update partner
                if cls["action"] == "rechazo_cif":
                    partner_name = _extract_partner_from_motivo(r["motivo"])
                    actions_payload.append({
                        "row_index": r["row_index"],
                        "action": "create_vat_correction",
                        "label": "VAT_CORRECTION",
                        "partner_name": partner_name,
                        "vat": cls.get("vat"),
                        "company_id": cfg["odoo_company_id"],
                        "archivo": r["archivo"],
                    })
        if rechazo_results:
            log.info(f"  {len(rechazo_results)} rechazo decisions processed (Drive ops)")
            for r in rechazo_results:
                log.info(f"    {r['archivo']} -> {r['estado_actual']}: {r['note']}")'''

assert old_rechazo_block in s1, "rechazo block in main"
s1 = s1.replace(old_rechazo_block, new_rechazo_block)

p1.write_text(s1)
print("dudas_apply.py: Drive ops in-place; CIF queues ORM action only")
