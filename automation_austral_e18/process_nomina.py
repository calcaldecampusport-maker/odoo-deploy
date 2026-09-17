"""Asiento de NÓMINA (portado de Austral a XML-RPC, en BORRADOR — regla wiemspro).

  DEBE 640 Total devengo          | HABER 4751 Retención IRPF
  DEBE 642 SS a cargo empresa     | HABER 476  A pagar TGSS (empresa+empleado+autónomos)
                                  | HABER 465NNN Líquido por trabajador (subcuenta por NIF)

Las subcuentas 465NNN se reutilizan (búsqueda por NIF/nombre) o se crean en el
primer hueco libre. Las validaciones de SS de Austral se vuelcan a la narración.
"""
import base64
import logging
import re

log = logging.getLogger("nomina")
TOL = 0.05

# Tipos cotización empresa 2026 (Orden ISM/118/2026)
T_CC, T_DES_IND, T_DES_TEMP, T_FP, T_FOG = 0.2435, 0.0550, 0.0670, 0.0060, 0.0020


def _acc(rpc, cid, code_exact, pref=None, name_ilike=None):
    """Cuenta por código exacto; si no, primera del prefijo (con filtro de nombre)."""
    r = rpc.search_read("account.account", [("code", "=", code_exact), ("deprecated", "=", False)],
                        ["id", "code", "name"], limit=1, company_id=cid)
    if r:
        return r[0]
    dom = [("code", "=like", f"{pref or code_exact[:3]}%"), ("deprecated", "=", False)]
    if name_ilike:
        rr = rpc.search_read("account.account", dom + [("name", "ilike", name_ilike)],
                             ["id", "code", "name"], limit=1, order="code", company_id=cid)
        if rr:
            return rr[0]
    rr = rpc.search_read("account.account", dom, ["id", "code", "name"],
                         limit=1, order="code", company_id=cid)
    return rr[0] if rr else None


def _empleado(rpc, cid, name, nif):
    if nif:
        r = rpc.search_read("res.partner", [("vat", "in", [nif, "ES" + nif])], ["id"],
                            limit=1, company_id=cid)
        if r:
            return r[0]["id"]
    if name:
        r = rpc.search_read("res.partner", [("name", "ilike", name)], ["id"], limit=1, company_id=cid)
        if r:
            return r[0]["id"]
    # la COMPANIA se rellena como en el resto de contactos (antes quedaba vacia). La
    # posicion fiscal no se toca: un empleado no tiene regimen de IVA que aplicar.
    return rpc.create("res.partner", {"name": name or "Empleado", "vat": nif or False,
                                      "is_company": False, "company_id": cid},
                      company_id=cid)


def _cuenta_465(rpc, cid, partner_name, nif):
    """Subcuenta 465NNN del empleado (por NIF o nombre); si no existe, primera libre."""
    nif_limpio = (nif or "").replace("ES", "").strip()
    for patron in ([nif_limpio] if nif_limpio else []) + \
                  [t for t in re.findall(r"[A-Za-zÁ-ú]+", partner_name or "") if len(t) > 3][:1]:
        r = rpc.search_read("account.account",
                            [("code", "=like", "465%"), ("name", "ilike", patron),
                             ("deprecated", "=", False)],
                            ["id", "code"], limit=1, company_id=cid)
        if r:
            return r[0]["id"]
    usados = {a["code"] for a in rpc.search_read("account.account", [("code", "=like", "465%")],
                                                 ["code"], limit=1200, company_id=cid)}
    libre = next((f"465{n:03d}" for n in range(1, 1000) if f"465{n:03d}" not in usados), None)
    if not libre:
        return None
    nombre = f"Líquido pdte pago {partner_name}" + (f" ({nif_limpio})" if nif_limpio else "")
    return rpc.create("account.account", {"code": libre, "name": nombre[:64],
                                          "account_type": "liability_current",
                                          "reconcile": True}, company_id=cid)


def _validaciones_ss(extra, employees):
    """Las 10 reglas de Austral (las que tengan datos). Devuelve avisos []."""
    avisos = []
    sum_cuotas = 0.0
    for e in employees:
        nom = e.get("name", "?")
        bccc = round(float(e.get("base_contingencias_comunes") or 0), 2)
        dev = round(float(e.get("bruto") or 0), 2)
        c_cc = round(float(e.get("cuota_cc_empresa") or 0), 2)
        tipo = (e.get("tipo_contrato") or "indefinido").lower()
        if bccc and dev and abs(bccc - dev) > max(dev * 0.02, 1.0):
            avisos.append(f"[R2] {nom}: BCCC ({bccc:.2f}) difiere del devengo ({dev:.2f}) >2% — revisar exentos")
        if bccc and c_cc and abs(c_cc - round(bccc * T_CC, 2)) > TOL:
            avisos.append(f"[R4] {nom}: cuota CC empresa {c_cc:.2f} ≠ esperada {round(bccc * T_CC, 2):.2f} (24,35%)")
        for k, t, tag in (("cuota_desempleo_empresa", T_DES_TEMP if "temp" in tipo else T_DES_IND, "R5 desempleo"),
                          ("cuota_fp_empresa", T_FP, "R6 FP"), ("cuota_fogasa_empresa", T_FOG, "R7 FOGASA")):
            c = round(float(e.get(k) or 0), 2)
            if bccc and c and abs(c - round(bccc * t, 2)) > TOL:
                avisos.append(f"[{tag}] {nom}: {c:.2f} ≠ esperada {round(bccc * t, 2):.2f}")
        sum_cuotas += sum(round(float(e.get(k) or 0), 2) for k in
                          ("cuota_cc_empresa", "cuota_at_empresa", "cuota_desempleo_empresa",
                           "cuota_fp_empresa", "cuota_fogasa_empresa"))
    aport = round(float(extra.get("aportaciones_empresa_total") or 0), 2)
    if sum_cuotas and aport and abs(sum_cuotas - aport) > 0.10:
        avisos.append(f"[R10] Σ cuotas empresa ({sum_cuotas:.2f}) ≠ aportación declarada ({aport:.2f})")
    return avisos


def process(rpc, company, data, pdf_bytes=None, pdf_name=None):
    """Asiento de devengo de nómina en BORRADOR. Devuelve dict de resultado."""
    cid = company["odoo_company_id"]
    if company.get("chart") != "pgc":
        return {"status": "error", "error": "nóminas solo en plan español"}
    extra = data.get("extra") or {}
    employees = extra.get("employees") or []

    sub = round(float(data.get("subtotal") or 0), 2)                 # total devengo
    irpf = round(float(extra.get("irpf_total") or 0), 2)
    ss_emp = round(float(extra.get("ss_empleado_total") or 0), 2)
    especie = round(float(extra.get("salario_especie_total") or 0), 2)
    aport = round(float(extra.get("aportaciones_empresa_total") or 0), 2)
    liquido = round(float(extra.get("liquido_total") or data.get("total") or 0), 2)

    if employees:   # reconciliar totales con la suma por empleado
        s = lambda k: round(sum(float(e.get(k) or 0) for e in employees), 2)  # noqa: E731
        if abs(s("bruto") - sub) > TOL:
            sub = s("bruto")
        if abs(s("irpf") - irpf) > TOL:
            irpf = s("irpf")
        if abs(s("ss") - ss_emp) > TOL:
            ss_emp = s("ss")
        if abs(s("liquido") - liquido) > TOL:
            liquido = s("liquido")
    # OTRAS RETENCIONES (embargo judicial, anticipos, otros descuentos) que el
    # extractor no desglosa: el residuo devengo - irpf - ss - especie - líquido.
    # Si es > 0 se contabiliza como retención a 4751 (H.P. acreedora) y la nómina
    # cuadra; solo se rechaza si el líquido es MAYOR que el neto (residuo < 0).
    otros_ret = round((sub - irpf - ss_emp - especie) - liquido, 2)
    if otros_ret < -TOL:
        return {"status": "error",
                "error": f"nómina descuadrada: devengo({sub})-irpf({irpf})-ss({ss_emp})"
                         f"-especie({especie})!=líquido({liquido}) (líquido mayor que el neto)"}
    otros_ret = otros_ret if otros_ret > TOL else 0.0

    periodo = extra.get("period") or str(data.get("invoice_date") or "")[:7]
    # nóminas INDIVIDUALES (un PDF por empleado, caso BIO): el ref lleva el NOMBRE
    # para que el anti-duplicado no descarte al resto de empleados del mismo mes.
    # El documento RESUMEN mensual (varios empleados) mantiene el ref genérico.
    # Concepto SIEMPRE "nóminas" y en minúscula (regla 27 jul 2026).
    ref = f"nóminas {periodo}"
    if len(employees) == 1 and str(employees[0].get("name") or "").strip():
        ref = f"nóminas {periodo} {str(employees[0]['name']).strip()[:60]}".lower()
    dup = rpc.search_read("account.move", [("ref", "=", ref), ("company_id", "=", cid),
                                           ("move_type", "=", "entry"), ("state", "!=", "cancel")],
                          ["id"], limit=1, company_id=cid)
    if dup:
        return {"status": "duplicate", "move_id": dup[0]["id"]}

    acc_640 = _acc(rpc, cid, "640000000", "640")
    acc_642 = _acc(rpc, cid, "642000000", "642")
    acc_4751 = _acc(rpc, cid, "475100000", "4751", name_ilike="nomina")
    acc_476 = _acc(rpc, cid, "476000000", "476")
    if not all([acc_640, acc_642, acc_4751, acc_476]):
        return {"status": "error", "error": "faltan cuentas 640/642/4751/476 en el plan"}
    # Diario NOM (nóminas) SIEMPRE; fallback a general si la empresa no lo tiene.
    jr = rpc.search_read("account.journal",
                         [("company_id", "=", cid), ("code", "=", "NOM")], ["id"], limit=1, company_id=cid)
    if not jr:
        jr = rpc.search_read("account.journal",
                             [("company_id", "=", cid), ("name", "ilike", "nómin")], ["id"], limit=1, company_id=cid)
    if not jr:
        jr = rpc.search_read("account.journal", [("type", "=", "general"), ("company_id", "=", cid)],
                             ["id"], limit=1, company_id=cid)
    if not jr:
        return {"status": "error", "error": "sin diario NOM ni general"}

    # descripciones SIEMPRE en minúscula (regla 27 jul 2026)
    lineas = []
    if sub > 0:
        lineas.append((0, 0, {"name": f"nóminas {periodo} — total devengo ({len(employees) or 1} empleados)",
                              "account_id": acc_640["id"], "debit": sub, "credit": 0.0}))
    if aport > 0:
        lineas.append((0, 0, {"name": f"nóminas {periodo} — ss a cargo de la empresa",
                              "account_id": acc_642["id"], "debit": aport, "credit": 0.0}))
    if irpf > 0:
        lineas.append((0, 0, {"name": f"retención irpf nóminas {periodo}",
                              "account_id": acc_4751["id"], "debit": 0.0, "credit": irpf}))
    cr_476 = round(aport + ss_emp + especie, 2)
    if cr_476 > 0:
        lineas.append((0, 0, {"name": f"a pagar tgss nóminas {periodo}",
                              "account_id": acc_476["id"], "debit": 0.0, "credit": cr_476}))
    if otros_ret > 0:   # embargo judicial / anticipos / otras retenciones → 4751
        lineas.append((0, 0, {"name": f"retención embargo/otras nóminas {periodo}",
                              "account_id": acc_4751["id"], "debit": 0.0, "credit": otros_ret}))
    if employees:
        for e in employees:
            liq = round(float(e.get("liquido") or 0), 2)
            if liq <= 0:
                continue
            pid = _empleado(rpc, cid, e.get("name"), e.get("nif"))
            a465 = _cuenta_465(rpc, cid, e.get("name") or "Empleado", e.get("nif"))
            lineas.append((0, 0, {"name": f"líquido {str(e.get('name', '?')).lower()} {periodo}",
                                  "partner_id": pid, "account_id": a465 or acc_476["id"],
                                  "debit": 0.0, "credit": liq}))
    elif liquido > 0:
        a465 = _acc(rpc, cid, "465000000", "465")
        lineas.append((0, 0, {"name": f"líquido pdte pago empleados {periodo}",
                              "account_id": (a465 or acc_476)["id"], "debit": 0.0, "credit": liquido}))

    total_d = round(sum(l[2]["debit"] for l in lineas), 2)
    total_h = round(sum(l[2]["credit"] for l in lineas), 2)
    if abs(total_d - total_h) > TOL:
        return {"status": "error", "error": f"asiento descuadrado D={total_d} H={total_h}"}

    avisos = _validaciones_ss(extra, employees)
    narr = [f"Asiento de devengo de nómina {periodo} (BORRADOR — validar y publicar).",
            f"Devengo {sub} · IRPF {irpf} · SS empleado {ss_emp} · SS empresa {aport} · "
            f"Especie/autónomos {especie} · Líquido {liquido}",
            f"Cuentas: {acc_640['code']} / {acc_642['code']} / {acc_4751['code']} / {acc_476['code']}"]
    if avisos:
        narr.append("AVISOS SS:")
        narr += ["⚠ " + a for a in avisos]
    # moneda = la de la empresa (una nómina en € NUNCA debe quedar en USD — caso
    # Abraham Carle 'Bonificación IDC' en USD dentro de WSL/EUR, 27 jul 2026)
    comp = rpc.read("res.company", [cid], ["currency_id"], company_id=cid)[0]
    comp_cur = comp["currency_id"][0] if comp.get("currency_id") else None
    move_vals = {
        "move_type": "entry", "ref": ref, "date": data.get("invoice_date"),
        "journal_id": jr[0]["id"], "company_id": cid,
        "narration": "\n".join(narr), "line_ids": lineas,
    }
    if comp_cur:
        move_vals["currency_id"] = comp_cur
    move_id = rpc.create("account.move", move_vals, company_id=cid)
    if pdf_bytes:
        try:
            rpc.create("ir.attachment", {"name": pdf_name or "nomina.pdf", "type": "binary",
                                         "res_model": "account.move", "res_id": move_id,
                                         "datas": base64.b64encode(pdf_bytes).decode()},
                       company_id=cid)
        except Exception as e:  # noqa: BLE001
            log.warning(f"adjunto nómina falló: {e}")
    return {"status": "created", "move_id": move_id, "needs_review": bool(avisos),
            "review_reason": ("; ".join(avisos)[:200] if avisos else None),
            "expense_account": acc_640["code"]}
