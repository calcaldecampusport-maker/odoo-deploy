"""Patch dudas_xlsx_publish.py to add 2 sheets (Duplicados + Rechazados) per company,
mirroring the daily email content."""
from pathlib import Path

p = Path("/opt/automation/dudas_xlsx_publish.py")
s = p.read_text()

# 1) Extend _build_workbook signature: add extractor_data param
old_sig = "def _build_workbook(rows: list[list], periodic_data: dict | None = None) -> bytes:"
new_sig = "def _build_workbook(rows: list[list], periodic_data: dict | None = None, extractor_data: dict | None = None) -> bytes:"
assert old_sig in s, "sig"
s = s.replace(old_sig, new_sig)

# 2) Insert duplicates+errors sheets right BEFORE the periodic block
periodic_anchor = "    if periodic_data and periodic_data.get('patterns'):"
extractor_block = '''    if extractor_data:
        dups = extractor_data.get("duplicates") or []
        errs = extractor_data.get("errors") or []
        if dups:
            ws_d = wb.create_sheet("Duplicados")
            ws_d.append(["Archivo PDF", "Proveedor", "Ref factura", "Fecha", "Total", "Factura existente (id)"])
            for c in ws_d[1]:
                c.fill = hdr_fill; c.font = hdr_font; c.alignment = Alignment(horizontal="center")
            dup_fill = PatternFill(start_color="d9e1f2", end_color="d9e1f2", fill_type="solid")
            for it in dups:
                ws_d.append([it.get("file",""), it.get("supplier",""), it.get("ref",""),
                             str(it.get("invoice_date","")), it.get("total",""), it.get("invoice_id","")])
                for c in ws_d[ws_d.max_row]: c.fill = dup_fill
            for col, w in {"A":40,"B":30,"C":25,"D":12,"E":12,"F":15}.items():
                ws_d.column_dimensions[col].width = w
            ws_d.freeze_panes = "A2"
        if errs:
            ws_e = wb.create_sheet("Rechazados")
            ws_e.append(["Archivo PDF", "Drive file_id", "Motivo del rechazo", "tu_decision", "notas"])
            for c in ws_e[1]:
                c.fill = hdr_fill; c.font = hdr_font; c.alignment = Alignment(horizontal="center")
            err_fill = PatternFill(start_color="ffc7ce", end_color="ffc7ce", fill_type="solid")
            for it in errs:
                reason = (it.get("reason","") or "")[:1000]
                ws_e.append([it.get("file",""), it.get("id",""), reason, "", ""])
                for c in ws_e[ws_e.max_row]: c.fill = err_fill
            for col, w in {"A":40,"B":50,"C":80,"D":35,"E":30}.items():
                ws_e.column_dimensions[col].width = w
            ws_e.freeze_panes = "A2"

    if periodic_data and periodic_data.get('patterns'):'''
assert periodic_anchor in s, "periodic anchor"
s = s.replace(periodic_anchor, extractor_block)

# 3) In publish(), load extractor JSON for this company and pass to builder
old_call = "    xlsx_bytes = _build_workbook(final_rows, periodic_data=periodic_data)"
new_call = '''    extractor_data = None
    try:
        from datetime import date as _date
        run_file = _Path(f"/tmp/extractor_runs/{_date.today().isoformat()}.json")
        if run_file.exists():
            run = _json.loads(run_file.read_text())
            for st in (run.get("summary") or []):
                if st.get("company") == payload.get("company"):
                    extractor_data = {
                        "duplicates": st.get("duplicates") or [],
                        "errors": st.get("errors") or [],
                    }
                    break
    except Exception:
        log.exception("could not load extractor JSON")
    xlsx_bytes = _build_workbook(final_rows, periodic_data=periodic_data, extractor_data=extractor_data)'''
assert old_call in s, "call"
s = s.replace(old_call, new_call)

p.write_text(s)
print("publish patched: duplicates+errors sheets added")
