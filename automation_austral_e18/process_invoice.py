"""Alta de una factura de proveedor en Odoo 18 (wiems_v18_prod) vía XML-RPC.

Reglas heredadas del sistema Austral, portadas a XML-RPC:
  * Crea el proveedor (res.partner) si no existe (por NIF, luego por nombre).
  * Cuenta de gasto por HISTÓRICO del proveedor; sin referencia -> por defecto +
    marca la factura para REVISIÓN (regla del usuario, ver expense_router).
  * IVA de compra por tipo (21/10/4 PGC; 15 US).
  * DEDUP por (move_type, partner, ref) -> idempotente (transacción por llamada RPC).
  * SIEMPRE en BORRADOR: la valida un humano.
  * Adjunta el PDF original al asiento (ir.attachment).

process() devuelve un dict con status y needs_review (para que el poller mueva el
PDF a Contabilizado o a Revisión en Drive).
"""
import os as _os
import base64
import re

import expense_router as er

REQUIRED = ["supplier_name", "invoice_ref", "invoice_date", "subtotal", "total", "lines"]


def normalize_vat(vat):
    if not vat:
        return None
    v = re.sub(r"\s+", "", str(vat)).upper()
    if v.startswith("ES") and len(v) > 9:
        v = v[2:]
    return v or None


def validate(data):
    miss = [k for k in REQUIRED if k not in data or data[k] in (None, "")]
    if miss:
        return f"faltan campos: {miss}"
    if not isinstance(data.get("lines"), list) or not data["lines"]:
        return "sin líneas"
    return None


def _country_id(rpc, cid, code):
    if not code:
        return None
    r = rpc.search_read("res.country", [("code", "=", str(code).strip().upper())],
                        ["id"], limit=1, company_id=cid)
    return r[0]["id"] if r else None


_FP_CACHE = {}       # {cid: [posiciones auto_apply]}
_GRUPO_CACHE = {}    # {(cid, group_id): {country_ids}} (+ 'ue')
_PAIS_CACHE = {}     # {cid: country_id de la compañía}


def _norm_fp(v):
    import unicodedata
    t = unicodedata.normalize('NFKD', str(v or '')).encode('ascii', 'ignore').decode().upper()
    return ''.join(c for c in t if c.isalnum())


# nombres INEQUIVOCOS de las tres posiciones, para las compañías que no marcan ninguna
# con deteccion automatica (Medical). Lista cerrada a proposito: «Regimen Exportacion
# Ceuta, Canarias y Melilla» no entra aqui.
_FP_NOMBRES = {
    'nacional': ('REGIMENNACIONAL', 'NACIONAL', 'DOMESTIC'),
    'intra': ('REGIMENINTRACOMUNITARIO', 'INTRACOMUNITARIO', 'INTRACOMMUNITY',
              'REGIMENINTRACOMUNITARIA'),
    'extra': ('REGIMENEXTRACOMUNITARIO', 'EXTRACOMUNITARIO', 'EXTRACOMMUNITY',
              'REGIMENEXPORTACION', 'EXPORTACION'),
}


def _fp_por_nombre(rpc, cid, clase):
    """Posición fiscal por su nombre exacto, para las BD sin deteccion automatica."""
    nombres = _FP_NOMBRES.get(clase) or ()
    try:
        todas = rpc.search_read('account.fiscal.position', [('company_id', '=', cid)],
                                ['name'], company_id=cid)
    except Exception:  # noqa: BLE001
        return None
    hit = [f for f in todas if _norm_fp(f['name']) in nombres]
    return hit[0]['id'] if len(hit) == 1 else None


def posicion_fiscal(rpc, cid, country_id=None, vat=None):
    """Id de la posición fiscal que le toca a un contacto por su PAÍS, o None.

    Reproduce el algoritmo de Odoo (`account.fiscal.position._get_fiscal_position`): entre
    las posiciones marcadas «deteccion automatica», busca primero las que EXIGEN VAT si el
    contacto lo tiene y, en cada pasada, por país exacto → grupo de países → sin país
    (el resto del mundo). Asi un francés con VAT es intracomunitario y no se lleva una
    posición de ventanilla única, que es de ventas a particulares.

    Si la compañía no marca ninguna posición como automática (Medical), se cae al nombre,
    y solo si es inequívoco. Sin país no se adivina; sin candidatas, None: el campo se
    queda como estaba.
    """
    if not country_id:
        return None
    try:
        fps = _FP_CACHE.get(cid)
        if fps is None:
            fps = rpc.search_read('account.fiscal.position',
                                  [('company_id', '=', cid), ('auto_apply', '=', True)],
                                  ['name', 'country_id', 'country_group_id', 'vat_required',
                                   'sequence'],
                                  company_id=cid)
            _FP_CACHE[cid] = fps
        cty = int(country_id)
        con_vat = bool((vat or '').strip())
        if not fps:
            # sin posiciones automáticas: por nombre, según el país
            if _pais_de_la_empresa(rpc, cid) == cty:
                return _fp_por_nombre(rpc, cid, 'nacional')
            return _fp_por_nombre(rpc, cid, 'intra' if _en_grupo_ue(rpc, cid, cty)
                                  else 'extra')

        def _en_grupo(f):
            g = f.get('country_group_id')
            if not g:
                return False
            paises = _GRUPO_CACHE.get((cid, g[0]))
            if paises is None:
                try:
                    gr = rpc.read('res.country.group', [g[0]], ['country_ids'], company_id=cid)
                    paises = set(gr[0].get('country_ids') or []) if gr else set()
                except Exception:  # noqa: BLE001
                    paises = set()
                _GRUPO_CACHE[(cid, g[0])] = paises
            return cty in paises

        # como Odoo: primero las que exigen VAT (si lo hay), luego las que no
        for req in ((True, False) if con_vat else (False,)):
            grupo = [f for f in fps if bool(f.get('vat_required')) == req]
            if not grupo:
                continue
            grupo.sort(key=lambda f: f.get('sequence') or 0)
            for prueba in (lambda f: f.get('country_id') and f['country_id'][0] == cty,
                           _en_grupo,
                           lambda f: not f.get('country_id') and not f.get('country_group_id')):
                hit = [f for f in grupo if prueba(f)]
                if hit:
                    return hit[0]['id']
        return None
    except Exception as e:  # noqa: BLE001 — nunca bloquea el alta del contacto
        log.warning(f'posicion fiscal no resuelta para el pais {country_id}: {e}')
        return None


def _pais_de_la_empresa(rpc, cid):
    """Id del país de la compañía (para saber qué es «nacional»)."""
    if cid in _PAIS_CACHE:
        return _PAIS_CACHE[cid]
    pais = None
    try:
        co = rpc.read('res.company', [cid], ['country_id'], company_id=cid)
        if co and co[0].get('country_id'):
            pais = co[0]['country_id'][0]
    except Exception:  # noqa: BLE001
        pass
    _PAIS_CACHE[cid] = pais
    return pais


def _en_grupo_ue(rpc, cid, country_id):
    """¿El país está en el grupo «Europe» de Odoo? (para las BD sin posiciones auto)"""
    if 'ue' not in _GRUPO_CACHE:
        paises = set()
        try:
            g = rpc.search_read('res.country.group', [('name', 'in', ['Europe', 'Europa'])],
                                ['country_ids'], limit=1, company_id=cid)
            if g:
                paises = set(g[0].get('country_ids') or [])
        except Exception:  # noqa: BLE001
            pass
        _GRUPO_CACHE['ue'] = paises
    return int(country_id) in _GRUPO_CACHE['ue']


def _partner_vals_from_data(rpc, cid, data):
    """Datos del proveedor impresos en la factura → campos res.partner."""
    vals = {}
    if data.get("supplier_street"):
        vals["street"] = str(data["supplier_street"])[:128]
    if data.get("supplier_city"):
        vals["city"] = str(data["supplier_city"])[:64]
    if data.get("supplier_zip"):
        vals["zip"] = str(data["supplier_zip"])[:24]
    if data.get("supplier_email") and "@" in str(data["supplier_email"]):
        vals["email"] = str(data["supplier_email"])[:128]
    if data.get("supplier_phone"):
        vals["phone"] = str(data["supplier_phone"])[:32]
    country = _country_id(rpc, cid, data.get("supplier_country_code"))
    if not country:
        vat = normalize_vat(data.get("supplier_vat")) or ""
        if len(vat) >= 2 and vat[:2].isalpha() and vat[:2] != "ES":
            country = _country_id(rpc, cid, vat[:2])
    if country:
        vals["country_id"] = country
    # COMPAÑÍA y POSICIÓN FISCAL: los dos se quedaban vacíos y había que ponerlos a mano
    # en Odoo. La compañía es la de la contabilidad en la que se crea (la convención de
    # esta BD) y la posición fiscal sale del país, con el criterio de Odoo.
    vals["company_id"] = cid
    fp = posicion_fiscal(rpc, cid, country, normalize_vat(data.get("supplier_vat")))
    if fp:
        vals["property_account_position_id"] = fp
    return vals


# NIF/CIF/NIE español (DNI, NIE, CIF) — se ALMACENA con prefijo ES (regla 16/07/2026)
_RE_NIF_ES = re.compile(r'^(\d{8}[A-Z]|[XYZ]\d{7}[A-Z]|[ABCDEFGHJKLMNPQRSUVW]\d{7}[0-9A-J])$', re.I)


def vat_con_prefijo(vat, country_code=None):
    """El NIF de España lleva SIEMPRE 'ES' delante al guardarse en el contacto."""
    v = (vat or '').strip().upper()
    if not v or (len(v) >= 2 and v[:2].isalpha()):
        return v
    if _RE_NIF_ES.match(v) or (country_code or '').strip().upper() == 'ES':
        return 'ES' + v
    return v


def _marca_revision_contacto(rpc, cid, pid):
    """Etiqueta 'Revisar contacto (web)' en el partner recién creado — la pestaña
    Contactos nuevos de la web lo lista para revisión y aprobación humana."""
    try:
        r = rpc.search_read('res.partner.category', [('name', '=', 'Revisar contacto (web)')],
                            ['id'], limit=1, company_id=cid)
        tid = r[0]['id'] if r else rpc.create('res.partner.category',
                                              {'name': 'Revisar contacto (web)'}, company_id=cid)
        rpc.write('res.partner', [pid], {'category_id': [(4, tid)]}, company_id=cid)
    except Exception:  # noqa: BLE001 — la etiqueta nunca bloquea la factura
        pass


_FORMAS_SOC = ('SL', 'SLU', 'SA', 'SAU', 'SLL', 'SCP', 'SLP', 'CB', 'SC',
               'LLC', 'INC', 'LTD', 'GMBH', 'BV', 'NV', 'SPA', 'SRL', 'SAS', 'PTYLTD')


def _clave_nombre(nombre):
    """Nombre normalizado para comparar: sin acentos, mayúsculas, sin puntuación y sin
    la forma social del final ('Austral Textiles, S.L.' == 'AUSTRAL TEXTILES SL')."""
    import unicodedata
    s = unicodedata.normalize('NFKD', str(nombre or '')).encode('ascii', 'ignore').decode()
    s = re.sub(r'[^A-Za-z0-9]', '', s).upper()
    for f in sorted(_FORMAS_SOC, key=len, reverse=True):
        if len(s) > len(f) + 2 and s.endswith(f):
            s = s[:-len(f)]
            break
    return s


def buscar_partner_por_vat(rpc, cid, vat):
    """Busca un res.partner por NIF/VAT SIN distinguir mayúsculas/minúsculas ni
    espacios, guiones o puntos, y con o sin prefijo de país. Devuelve id o None.

    Con ('vat','=',v) —sensible a mayúsculas y al formato exacto— un NIF guardado como
    'esb44821965' o 'ES B44821965' NO se encontraba y se creaba un proveedor DUPLICADO.
    """
    if not vat:
        return None
    v0 = re.sub(r"[^A-Za-z0-9]", "", str(vat)).upper()
    if not v0:
        return None
    nucleo = v0[2:] if (len(v0) > 2 and v0[:2].isalpha()) else v0   # sin prefijo país
    # 1) coincidencia exacta insensible a mayúsculas, con las variantes habituales
    for v in dict.fromkeys([v0, nucleo, "ES" + nucleo]):
        if not v:
            continue
        r = rpc.search_read("res.partner", [("vat", "=ilike", v)], ["id"],
                            limit=1, company_id=cid)
        if r:
            return r[0]["id"]
    # 2) el guardado puede llevar espacios/guiones: comparar ya normalizado
    r = rpc.search_read("res.partner", [("vat", "ilike", nucleo)],
                        ["id", "vat"], limit=20, company_id=cid)
    for p in r:
        g = re.sub(r"[^A-Za-z0-9]", "", str(p.get("vat") or "")).upper()
        if g and (g == v0 or g == nucleo or g == "ES" + nucleo
                  or (len(g) > 2 and g[:2].isalpha() and g[2:] == nucleo)):
            return p["id"]
    return None


def find_or_create_supplier(rpc, company, data):
    """Devuelve (partner_id, created: bool). Al crear rellena TODOS los datos
    impresos en la factura (dirección, país, email, teléfono); si ya existe,
    completa los campos que tenga VACÍOS (nunca pisa datos)."""
    cid = company["odoo_company_id"]
    vat = normalize_vat(data.get("supplier_vat"))
    name = (data.get("supplier_name") or "").strip()
    pid = None
    if vat:
        pid = buscar_partner_por_vat(rpc, cid, vat)
    if not pid and name:
        # '=ilike' ya ignora mayúsculas/minúsculas
        r = rpc.search_read("res.partner",
                            [("name", "=ilike", name), ("supplier_rank", ">", 0)],
                            ["id"], limit=1, company_id=cid)
        if not r:
            r = rpc.search_read("res.partner", [("name", "=ilike", name)], ["id"], limit=1, company_id=cid)
        if r:
            pid = r[0]["id"]
    if not pid and name:
        # el nombre guardado puede diferir en espacios dobles, puntos, comas o el tipo
        # de sociedad ('S.L.' vs 'SL'): se compara ya normalizado antes de crear otro.
        clave = _clave_nombre(name)
        if clave:
            for p in rpc.search_read("res.partner",
                                     [("name", "ilike", clave[:12])],
                                     ["id", "name"], limit=30, company_id=cid):
                if _clave_nombre(p.get("name")) == clave:
                    pid = p["id"]
                    break
    extra = _partner_vals_from_data(rpc, cid, data)
    if pid:
        # completar SOLO campos vacíos del partner existente (incl. vat si faltaba)
        try:
            cur = rpc.read("res.partner", [pid],
                           ["street", "city", "zip", "email", "phone", "country_id", "vat",
                            "company_id", "property_account_position_id"],
                           company_id=cid)[0]
            fill = {k: v for k, v in extra.items() if not cur.get(k)}
            if vat and not cur.get("vat"):
                fill["vat"] = vat_con_prefijo(vat, data.get("supplier_country_code"))
            if fill:
                rpc.write("res.partner", [pid], fill, company_id=cid)
        except Exception:  # noqa: BLE001 — completar datos nunca bloquea la factura
            pass
        return pid, False
    # company_ids: en Odoo 18 el aislamiento de contactos va por este campo y la regla
    # de registro; sin él el tercero queda COMPARTIDO y se ve desde todas las empresas
    vals = {"name": name or (vat or "Proveedor sin nombre"), "supplier_rank": 1,
            "is_company": True, "company_ids": [(6, 0, [cid])], **extra}
    if vat:
        vat_alm = vat_con_prefijo(vat, data.get("supplier_country_code"))
        try:
            pid = rpc.create("res.partner", {**vals, "vat": vat_alm}, company_id=cid)
            _marca_revision_contacto(rpc, cid, pid)
            ensure_cuenta_tercero(rpc, company, pid, vals.get("name"), "payable")
            return pid, True
        except Exception as e:  # noqa: BLE001 — VAT rechazado por Odoo (extranjero/mal escaneado)
            vals["comment"] = f"VAT sin validar (rechazado por Odoo al alta automática): {vat_alm} — {str(e)[:80]}"
    pid = rpc.create("res.partner", vals, company_id=cid)
    _marca_revision_contacto(rpc, cid, pid)
    ensure_cuenta_tercero(rpc, company, pid, vals.get("name"), "payable")
    return pid, True


_cache_plan_tercero = {}


def _plan_tercero(rpc, cid, tipo):
    """(prefijo, nº de dígitos) de las cuentas POR TERCERO de esta contabilidad.

    Se DEDUCE del propio plan en vez de fijarlo: los proveedores llevan 410NNN en
    wiemspro y BSS (6 dígitos, genérica 410000) y 41000NNN en Medical y CARAJFAM
    (8 dígitos, genérica 41000000). Solo se devuelve algo si la empresa YA tiene la
    costumbre —al menos 5 cuentas de tercero y numeración densa—, para no estrenar una
    convención inventada donde no se usa: los clientes de Best Training van todos a la
    430000, el plan de CORP es americano (211000) y las cuentas de Austral llevan
    estructura propia (400NNNNNN) en vez de un contador.
    """
    from collections import Counter
    k = (cid, tipo)
    if k in _cache_plan_tercero:
        return _cache_plan_tercero[k]
    out = None
    try:
        rows = rpc.search_read("account.account",
                               [("account_type", "=", tipo), ("deprecated", "=", False)],
                               ["code"], limit=4000, company_id=cid)
        codes = [c for c in (str(r.get("code") or "").strip() for r in rows) if c.isdigit()]
        # GENÉRICAS y de uso especial: todo ceros tras la raíz (410000, 41000000) — no
        # son de un tercero, así que no cuentan para deducir la numeración
        dedic = [c for c in codes if len(c) > 3 and c[3:].strip("0")]
        if dedic:
            largo = Counter(len(c) for c in dedic).most_common(1)[0][0]
            dedic = [c for c in dedic if len(c) == largo]
            for ndig in (3, 4):
                if largo - ndig < 3:
                    continue
                cnt = Counter(c[:largo - ndig] for c in dedic)
                # El contador de terceros vive en el bloque «raíz + ceros» (410NNN,
                # 41000NNN). Si el prefijo lleva dígitos propios el código tiene
                # estructura y no es un contador —los 430039NNN de Austral—, así que no
                # se toca. Y tiene que estar ahí la MAYORÍA de las cuentas de tercero de
                # la empresa: si solo es un tramo suelto, tampoco es la numeración.
                cands = [(k, v) for k, v in cnt.items() if not k[3:].strip("0")]
                if not cands:
                    continue
                pref, n = max(cands, key=lambda x: x[1])
                nums = {int(c[largo - ndig:]) for c in dedic
                        if c.startswith(pref) and c[largo - ndig:].isdigit()}
                nums.discard(0)
                if n >= 5 and n >= 0.5 * len(dedic) and nums:
                    out = (pref, ndig)
                    break
    except Exception:  # noqa: BLE001 — sin plan deducido no se toca el tercero
        out = None
    _cache_plan_tercero[k] = out
    return out


def ensure_cuenta_tercero(rpc, company, partner_id, partner_name, lado="payable"):
    """Cuenta contable PROPIA del tercero, creada al vuelo (plan español).

    Cada proveedor con la suya según la convención de SU contabilidad (410595 Adobe en
    wiemspro, 41000126 Anthropic en Medical). Si el tercero todavía usa la genérica:
    busca una cuenta del rango que lleve su nombre —creada a mano antes— y, si no
    existe, crea la SIGUIENTE del rango y se la asigna. Idempotente: si ya tiene la
    suya, no hace nada.
    """
    if company.get("chart") != "pgc":
        return None
    cid = company["odoo_company_id"]
    campo = ("property_account_payable_id" if lado == "payable"
             else "property_account_receivable_id")
    tipo = "liability_payable" if lado == "payable" else "asset_receivable"
    plan = _plan_tercero(rpc, cid, tipo)
    if not plan:
        return None
    pref, ndig = plan
    try:
        cur = rpc.read("res.partner", [partner_id], [campo], company_id=cid)[0].get(campo)
        if cur:
            code = str((cur[1] or "")).split(" ")[0]
            # la GENÉRICA (410000, 41000000) es la de todos: no cuenta como dedicada
            if code.startswith(pref) and code[len(pref):].strip("0"):
                return None
        # 1) ¿ya existe una cuenta del rango con su nombre? (la creó una persona)
        toks = [t for t in re.findall(r"[A-Za-zÁ-ú0-9]+", partner_name or "") if len(t) > 2][:2]
        acc = None
        if toks:
            dom = [("code", "=like", pref + "%"), ("account_type", "=", tipo),
                   ("deprecated", "=", False)] + [("name", "ilike", t) for t in toks]
            r = rpc.search_read("account.account", dom, ["id", "code"], limit=1,
                                company_id=cid)
            if r:
                acc = r[0]["id"]
        if not acc:
            # 2) la SIGUIENTE del rango. No se rellenan los huecos: son bloques que la
            #    contabilidad reserva (Medical tiene libres 140-155 y 165-200). Solo si
            #    el rango se agota por arriba se aprovecha un hueco.
            existentes = rpc.search_read("account.account", [("code", "=like", pref + "%")],
                                        ["code"], limit=2000, company_id=cid)
            usados = {str(a["code"]).strip() for a in existentes}
            nums = [int(c[len(pref):]) for c in usados
                    if len(c) == len(pref) + ndig and c[len(pref):].isdigit()]
            tope = 10 ** ndig - 1
            sig = (max(nums) + 1) if nums else 1
            libre = f"{pref}{sig:0{ndig}d}" if sig <= tope else None
            if libre is None or libre in usados:
                libre = next((c for c in (f"{pref}{n:0{ndig}d}"
                                          for n in range(1, tope + 1))
                              if c not in usados), None)
            if not libre:
                return None
            acc = rpc.create("account.account", {
                "code": libre, "name": (partner_name or "Tercero")[:64],
                "account_type": tipo, "reconcile": True,
            }, company_id=cid)
        rpc.write("res.partner", [partner_id], {campo: acc}, company_id=cid)
        return acc
    except Exception:  # noqa: BLE001 — la cuenta dedicada no bloquea la factura
        return None


def ensure_payable_410(rpc, company, partner_id, partner_name):
    """Cuenta propia del PROVEEDOR (la convención la deduce ensure_cuenta_tercero)."""
    return ensure_cuenta_tercero(rpc, company, partner_id, partner_name, "payable")


def find_purchase_journal(rpc, cid):
    r = rpc.search_read("account.journal", [("type", "=", "purchase"), ("company_id", "=", cid)],
                        ["id"], limit=1, company_id=cid)
    return r[0]["id"] if r else None


_BAD_TAX = ("intracomunitar", "importaci", "extracomunitar", "inversión", "no deducible", "special", "especial")


# 0%: preferencia entre los tipos exentos/no sujetos del plan (los de operaciones
# corrientes; nunca intracomunitario/importaciones/DUA/recargo/retenciones)
_TAX0_PREF = ("no sujeto (servicios", "no sujeto (bienes", "exento (operaciones",
              "0% iva soportado (servicios", "0% iva soportado (bienes")
_MALOS_TAX0 = ("intracomunitar", "importaci", "extracomunitar", "inversión", "inversion",
               "recargo", "retenci", "dua", "withholding", "no deducible")
_cache_tax0 = {}


def find_zero_purchase_tax(rpc, cid):
    """Tipo de compra al 0% para las líneas SIN IVA nacional (aranceles, suplidos,
    operaciones no sujetas…). Es el que Odoo muestra como «0% S Exent NS»: así la
    línea queda declarada como no sujeta/exenta en vez de sin impuesto."""
    if cid in _cache_tax0:
        return _cache_tax0[cid]
    tax_id = None
    try:
        taxes = rpc.search_read("account.tax",
                                [("type_tax_use", "=", "purchase"), ("amount", "=", 0),
                                 ("amount_type", "=", "percent"), ("company_id", "=", cid)],
                                ["id", "name"], company_id=cid)
        buenos = [t for t in taxes
                  if not any(m in (t.get("name") or "").lower() for m in _MALOS_TAX0)]
        for pat in _TAX0_PREF:
            for t in buenos:
                if pat in (t.get("name") or "").lower():
                    tax_id = t["id"]
                    break
            if tax_id:
                break
        if not tax_id and buenos:
            tax_id = buenos[0]["id"]
    except Exception as e:  # noqa: BLE001 — sin tipo al 0% la línea va sin impuesto
        print(f"  ⚠ no se pudo resolver el IVA 0% de compras: {e}")
    _cache_tax0[cid] = tax_id
    return tax_id


def find_purchase_tax(rpc, cid, rate):
    """Impuesto de compra 'soportado' del tipo dado. Prefiere el genérico
    (bienes/servicios corrientes) frente a intracomunitario/importaciones.
    Con tipo 0 devuelve el 0% no sujeto/exento (no None): las líneas sin IVA
    nacional deben quedar declaradas, no sin impuesto."""
    try:
        rate = float(rate)
    except (TypeError, ValueError):
        return find_zero_purchase_tax(rpc, cid)
    if rate == 0:
        return find_zero_purchase_tax(rpc, cid)
    taxes = rpc.search_read("account.tax",
                            [("type_tax_use", "=", "purchase"), ("amount", "=", rate),
                             ("amount_type", "=", "percent"), ("company_id", "=", cid)],
                            ["id", "name", "price_include"], company_id=cid)
    if not taxes:
        return None

    def score(t):
        n = (t.get("name") or "").lower()
        s = 0
        if any(b in n for b in _BAD_TAX):
            s -= 10
        if "soportado" in n:
            s += 5
        if "corrientes" in n:
            s += 2
        if t.get("price_include"):
            s -= 1
        return s

    return sorted(taxes, key=score, reverse=True)[0]["id"]


def find_reverse_tax(rpc, cid, rate, kind=None):
    """Impuesto de compra con INVERSIÓN DEL SUJETO PASIVO (autorepercusión 472/477)
    según el origen: intracomunitario (UE), extracomunitario (fuera UE) o ISP
    nacional. Prefiere el de servicios (el caso habitual: SaaS/licencias)."""
    try:
        rate = float(rate) or 21.0
    except (TypeError, ValueError):
        rate = 21.0
    kind = (kind or "intracomunitario").lower()
    # los nombres de los impuestos de AUSTRAL no son los del plan PYMES: su intracom
    # se llama «IVA 0% Intracom Compras» (0%, no 21%), así que se buscan varios
    # patrones y NO se exige el tipo — vale el que tengan definido para ese origen.
    patrones = {"intracomunitario": ["intracomunitari", "intracom"],
                "extracomunitario": ["extracomunitari", "extracom", "importacion", "importación"],
                "isp_nacional": ["inversión del sujeto pasivo", "inversion del sujeto pasivo",
                                 "ISP", "inv. sujeto pasivo"]}
    for pat in patrones.get(kind, ["intracomunitari", "intracom"]):
        taxes = rpc.search_read("account.tax",
                                [("type_tax_use", "=", "purchase"),
                                 ("amount_type", "=", "percent"), ("company_id", "=", cid),
                                 ("name", "ilike", pat)],
                                ["id", "name", "amount"], company_id=cid)
        if taxes:   # si hay varios, el del tipo pedido; si no, el que haya
            mismo = [t for t in taxes if abs((t.get("amount") or 0) - rate) < 0.01]
            taxes = mismo or taxes
        if taxes:
            serv = [t for t in taxes if "servici" in (t.get("name") or "").lower()]
            return (serv or taxes)[0]["id"]
    # fallback: el intracomunitario clásico si el específico no existe en el plan
    if kind != "intracomunitario":
        return find_reverse_tax(rpc, cid, rate, "intracomunitario")
    return None


def find_or_create_irpf_tax(rpc, cid, rate):
    """Impuesto negativo de RETENCIÓN IRPF (Austral): 19% → cuenta 4751 de
    alquileres/arrendamientos; resto (15/7/2/1) → profesionales/general.
    Lo crea si no existe, con las líneas de reparto hacia la cuenta 4751."""
    try:
        rate = abs(float(rate))
    except (TypeError, ValueError):
        return None
    if rate <= 0:
        return None
    name = f"Retención IRPF {rate:g}% " + ("arrendamientos" if abs(rate - 19) < 0.5 else "profesionales")
    tax = rpc.search_read("account.tax",
                          [("company_id", "=", cid), ("type_tax_use", "=", "purchase"),
                           ("name", "=", name)], ["id"], limit=1, company_id=cid)
    if tax:
        return tax[0]["id"]
    # cuenta 4751 destino: por concepto en el nombre; fallback a la primera 4751
    dom_base = [("code", "=like", "4751%"), ("deprecated", "=", False)]
    pats = ["alquiler", "arrendam"] if abs(rate - 19) < 0.5 else ["profesional"]
    acc = None
    for p in pats:
        r = rpc.search_read("account.account", dom_base + [("name", "ilike", p)],
                            ["id", "code"], limit=1, company_id=cid)
        if r:
            acc = r[0]["id"]
            break
    if not acc:
        r = rpc.search_read("account.account", dom_base, ["id", "code"], limit=1,
                            order="code", company_id=cid)
        acc = r[0]["id"] if r else None
    if not acc:
        return None
    grp = rpc.search_read("account.tax.group", [("company_id", "=", cid)], ["id"],
                          limit=1, company_id=cid)
    vals = {"name": name, "amount_type": "percent", "amount": -rate,
            "type_tax_use": "purchase", "price_include": False,
            "invoice_repartition_line_ids": [
                (0, 0, {"repartition_type": "base", "factor_percent": 100, "document_type": "invoice"}),
                (0, 0, {"repartition_type": "tax", "factor_percent": 100, "account_id": acc,
                        "document_type": "invoice"})],
            "refund_repartition_line_ids": [
                (0, 0, {"repartition_type": "base", "factor_percent": 100, "document_type": "refund"}),
                (0, 0, {"repartition_type": "tax", "factor_percent": 100, "account_id": acc,
                        "document_type": "refund"})]}
    if grp:
        vals["tax_group_id"] = grp[0]["id"]
    try:
        return rpc.create("account.tax", vals, company_id=cid)
    except Exception:  # noqa: BLE001 — sin retención la factura irá descuadrada a revisión
        return None


def already_exists(rpc, cid, partner_id, ref):
    if not ref:
        return None
    r = rpc.search_read("account.move",
                        [("move_type", "in", ["in_invoice", "in_refund"]),
                         ("partner_id", "=", partner_id), ("ref", "=", ref),
                         ("company_id", "=", cid),
                         ("state", "!=", "cancel")],
                        ["id", "name", "amount_total"], limit=1, company_id=cid)
    return r[0] if r else None


def find_tax_by_name(rpc, cid, name):
    """Impuesto de compra por NOMBRE exacto de Odoo (dictado por regla del revisor).
    Exacto primero; si no, contiene — y de los que contienen, el de nombre más corto."""
    name = str(name or "").strip()
    if not name:
        return None
    r = rpc.search_read("account.tax", [("type_tax_use", "=", "purchase"),
                                        ("company_id", "=", cid), ("name", "=ilike", name)],
                        ["id"], limit=1, company_id=cid)
    if r:
        return r[0]["id"]
    r = rpc.search_read("account.tax", [("type_tax_use", "=", "purchase"),
                                        ("company_id", "=", cid), ("name", "ilike", name)],
                        ["id", "name"], company_id=cid)
    if r:
        return sorted(r, key=lambda t: len(t.get("name") or ""))[0]["id"]
    return None

def _aviso(avisos, texto):
    if avisos is not None:
        avisos.append(texto)

_TAX_HIJOS = {}


def _tax_es_hijo(rpc, cid, tax_id):
    """True si ese impuesto es HIJO de un grupo (usarlo suelto en una línea suele
    ser un error: el grupo es el que lleva la mecánica completa, p.ej. DUA)."""
    key = int(cid)
    if key not in _TAX_HIJOS:
        try:
            grupos = rpc.search_read("account.tax", [("children_tax_ids", "!=", False),
                                                     ("company_id", "=", cid)],
                                     ["children_tax_ids"], company_id=cid)
            _TAX_HIJOS[key] = {h for g in grupos for h in (g.get("children_tax_ids") or [])}
        except Exception:  # noqa: BLE001 — el aviso nunca bloquea la factura
            _TAX_HIJOS[key] = set()
    return int(tax_id) in _TAX_HIJOS[key]


def build_lines(rpc, cid, data, expense_account_id, chart="pgc", avisos=None):
    reverse = bool(data.get("reverse_charge")) and chart == "pgc"
    irpf_tax = (find_or_create_irpf_tax(rpc, cid, data.get("irpf_rate"))
                if chart == "pgc" and float(data.get("irpf_amount") or 0) > 0 else None)
    # impuesto EXACTO dictado por regla del revisor: manda sobre todo lo demás
    tax_regla = find_tax_by_name(rpc, cid, data.get("tax_name")) if data.get("tax_name") else None
    lines = []
    for ln in data["lines"]:
        amount = abs(float(ln.get("amount") or 0))
        # impuesto EXACTO dictado por regla para ESTA línea (prioridad máxima) — permite
        # mezclar tipos en una misma factura, p.ej. facturas de agente de aduanas: la
        # línea del DUA lleva "21% EX B DUA" y el resto "21% S".
        tax_ln, nombre_ln = None, str(ln.get("tax_name") or "").strip()
        if nombre_ln:
            tax_ln = find_tax_by_name(rpc, cid, nombre_ln)
            desc_ln = (ln.get("description") or "")[:40]
            if not tax_ln:
                _aviso(avisos, f"el impuesto '{nombre_ln}' que pide la regla NO existe en "
                               f"Odoo — la línea '{desc_ln}' lleva el tipo por porcentaje")
            elif _tax_es_hijo(rpc, cid, tax_ln):
                _aviso(avisos, f"'{nombre_ln}' es un impuesto HIJO de un grupo "
                               f"(línea '{desc_ln}') — ¿debería ser el grupo completo?")
        if tax_ln:
            tax_id = tax_ln
        elif tax_regla:
            tax_id = tax_regla
        elif reverse:
            # ISP: el IVA impreso es 0 pero se autorepercute (472/477) según el origen
            tax_id = find_reverse_tax(rpc, cid, ln.get("tax_rate") or 21,
                                      data.get("reverse_charge_kind"))
        else:
            tax_id = find_purchase_tax(rpc, cid, ln.get("tax_rate"))
        # cuenta dictada por regla del revisor para ESTA línea (prioridad máxima)
        acc_id = expense_account_id
        code_ln = str(ln.get("account") or "").strip()
        if code_ln:
            acc = er._account_by_code(rpc, cid, code_ln)
            if acc:
                acc_id = acc["id"]
        tids = [t for t in (tax_id, irpf_tax) if t]
        lines.append((0, 0, {
            "name": (ln.get("description") or "Gasto")[:200],
            "quantity": 1,
            "price_unit": amount,
            "account_id": acc_id,
            "tax_ids": [(6, 0, tids)],
        }))
    return lines


# el documento no siempre es un PDF: muchos recibos llegan como foto (PNG/JPG/HEIC).
# Si se adjunta con mimetype de PDF, ni Odoo ni el visor de la web pueden mostrarlo.
_MIMES_ADJUNTO = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".webp": "image/webp", ".heic": "image/heic",
    ".heif": "image/heif", ".gif": "image/gif", ".tif": "image/tiff", ".tiff": "image/tiff",
}


def attach_pdf(rpc, cid, move_id, pdf_bytes, pdf_name):
    """Adjunta el documento al asiento con su mimetype REAL (por extension, y si no
    la trae, por la cabecera del fichero)."""
    if not pdf_bytes:
        return None
    nombre = pdf_name or "factura.pdf"
    mime = _MIMES_ADJUNTO.get(_os.path.splitext(nombre)[1].lower())
    if not mime:
        if pdf_bytes[:5] == b"%PDF-":
            mime = "application/pdf"
        elif pdf_bytes[1:4] == b"PNG":
            mime = "image/png"
        elif pdf_bytes[:2] == bytes((255, 216)):        # JPEG: FF D8
            mime = "image/jpeg"
        else:
            mime = "application/octet-stream"
    return rpc.create("ir.attachment", {
        "name": nombre,
        "res_model": "account.move",
        "res_id": move_id,
        "type": "binary",
        "mimetype": mime,
        "datas": base64.b64encode(pdf_bytes).decode("ascii"),
    }, company_id=cid)


def process(rpc, company, data, pdf_bytes=None, pdf_name=None):
    """Crea la factura de proveedor en BORRADOR. Devuelve dict de resultado."""
    err = validate(data)
    if err:
        return {"status": "error", "error": err}

    cid = company["odoo_company_id"]
    partner_id, created = find_or_create_supplier(rpc, company, data)
    # cuenta 410NNN dedicada del proveedor (plan español) — antes de crear la factura
    ensure_payable_410(rpc, company, partner_id, data.get("supplier_name"))

    if _os.environ.get("FORCE_NO_DEDUP") == "1":
        # orden expresa del revisor web ("no es duplicado"): se omite el chequeo
        dup = None
    else:
        dup = already_exists(rpc, cid, partner_id, data.get("invoice_ref"))
    if dup:
        # GUARD: misma ref pero IMPORTE distinto -> NO es duplicado silencioso
        # (factura corregida u OCR): a revision con motivo claro.
        try:
            _tot = round(abs(float(data.get("total"))), 2)
        except (TypeError, ValueError):
            _tot = None
        if _tot is not None and abs(abs(dup.get('amount_total') or 0) - _tot) > 0.01:
            return {"status": "failed",
                    "reason": f"misma ref que {dup.get('name')} pero importe distinto ({dup.get('amount_total')} vs {_tot}): factura corregida u OCR — revisar a mano"}
        return {"status": "duplicate", "move_id": dup["id"], "partner_id": partner_id}

    # Cuenta de gasto: la fijada por regla del revisor manda; si no, por histórico.
    acc_code = str(data.get("expense_account") or "").strip()
    route = None
    if acc_code:
        acc = er._account_by_code(rpc, cid, acc_code)
        if acc:
            route = {"account_id": acc["id"], "account_code": acc["code"],
                     "account_name": acc["name"], "from_history": True,
                     "info": "cuenta fijada por regla del revisor"}
    if not route:
        route = er.route_expense_account(rpc, cid, partner_id, chart=company.get("chart", "pgc"))
    if not route["account_id"]:
        return {"status": "error", "error": "sin cuenta de gasto", "partner_id": partner_id}
    needs_review = not route["from_history"]

    total = float(data.get("total") or 0)
    # RECTIFICATIVAS/ABONOS: flag del extractor (o total negativo) → in_refund.
    # Al publicarla, Odoo usa la serie de rectificativas (RFACTURA/...).
    es_abono = bool(data.get("is_refund")) or total < 0
    move_type = "in_refund" if es_abono else "in_invoice"
    journal_id = find_purchase_journal(rpc, cid)

    notas = []
    if data.get("extraction_notes"):
        notas.append("OCR: " + str(data["extraction_notes"]))
    notas.append(f"Cuenta gasto {route['account_code']}: {route['info']}")
    if needs_review:
        notas.append("⚠ REVISAR cuenta de gasto (sin histórico del proveedor)")
    if data.get("reverse_charge"):
        notas.append(f"ISP {data.get('reverse_charge_kind') or 'intracomunitario'}: "
                     "autorepercusión IVA (472/477)")
    if float(data.get("irpf_amount") or 0) > 0:
        notas.append(f"Retención IRPF {data.get('irpf_rate')}%: {data.get('irpf_amount')}")

    avisos_tax = []          # impuestos por línea que no cuadran → revisión humana
    vals = {
        "move_type": move_type,
        "partner_id": partner_id,
        "invoice_date": data["invoice_date"],
        "ref": data.get("invoice_ref"),
        "company_id": cid,
        "invoice_line_ids": build_lines(rpc, cid, data, route["account_id"],
                                        chart=company.get("chart", "pgc"), avisos=avisos_tax),
        "narration": " | ".join(notas),
    }
    if avisos_tax:
        needs_review = True
        notas.extend("⚠ " + a for a in avisos_tax)
        vals["narration"] = " | ".join(notas)
    if journal_id:
        vals["journal_id"] = journal_id
    if data.get("due_date"):
        vals["invoice_date_due"] = data["due_date"]
    # FECHA CONTABLE (regla global del usuario, 14 jul 2026): SIEMPRE la fecha de
    # REGISTRO (hoy) — la fecha de factura queda en invoice_date. Evita contabilizar
    # en meses con el IVA ya presentado. Una regla puede dictar otra (accounting_date).
    from datetime import date as _date
    vals["date"] = data.get("accounting_date") or _date.today().isoformat()
    # MONEDA del documento (p.ej. facturas de Asana en USD): el asiento se crea en
    # esa divisa y Odoo convierte a la de la company con el cambio del día.
    cur = str(data.get("currency") or "").strip().upper()
    if cur and cur != "EUR":
        rc = rpc.search_read("res.currency", [("name", "=", cur), ("active", "=", True)],
                             ["id"], limit=1, company_id=cid)
        if rc:
            vals["currency_id"] = rc[0]["id"]
        else:
            notas.append(f"⚠ moneda {cur} no activa en Odoo — asiento en moneda de la empresa")
            vals["narration"] = " | ".join(notas)

    # RECTIFICATIVA: enlazar la factura ORIGINAL que rectifica (reversed_entry_id),
    # buscada por su nº en el mismo proveedor. Si no se encuentra, se deja nota.
    if es_abono and (data.get("rectifies_ref") or "").strip():
        ref_o = str(data["rectifies_ref"]).strip()
        try:
            orig = rpc.search_read("account.move",
                                   [("move_type", "=", "in_invoice"),
                                    ("partner_id", "=", partner_id),
                                    ("ref", "ilike", ref_o[:40]),
                                    ("state", "!=", "cancel"), ("company_id", "=", cid)],
                                   ["id", "name"], limit=1, company_id=cid)
            if orig:
                vals["reversed_entry_id"] = orig[0]["id"]
                notas.append(f"RECTIFICATIVA de {orig[0]['name']} ({ref_o})")
            else:
                notas.append(f"⚠ RECTIFICATIVA de '{ref_o}' — original no encontrada (enlazar a mano)")
            vals["narration"] = " | ".join(notas)
        except Exception:  # noqa: BLE001 — el enlace nunca bloquea la factura
            pass

    move_id = rpc.create("account.move", vals, company_id=cid)
    # VENCIMIENTO (regla global, 15 jul 2026): si el documento trae due_date debe quedar
    # en el asiento — fija date_maturity del apunte 4xx y alimenta la previsión de
    # tesorería. Los plazos de pago del proveedor pueden recalcularlo en el create,
    # así que se verifica y se fuerza (anulando el plazo) si Odoo lo pisó.
    if data.get("due_date"):
        try:
            cur_due = rpc.read("account.move", [move_id], ["invoice_date_due"],
                               company_id=cid)[0].get("invoice_date_due")
            if str(cur_due or "") != str(data["due_date"]):
                rpc.write("account.move", [move_id],
                          {"invoice_payment_term_id": False,
                           "invoice_date_due": data["due_date"]}, company_id=cid)
        except Exception:  # noqa: BLE001 — el vencimiento nunca bloquea la factura
            pass
    att_id = attach_pdf(rpc, cid, move_id, pdf_bytes, pdf_name)

    return {
        "status": "created", "move_id": move_id, "partner_id": partner_id,
        "partner_created": created, "move_type": move_type,
        "expense_account": route["account_code"], "from_history": route["from_history"],
        "needs_review": needs_review, "review_reason": ((" · ".join(avisos_tax) or route["info"])
                          if needs_review else None),
        "attachment_id": att_id,
    }
