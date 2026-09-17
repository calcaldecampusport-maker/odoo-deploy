"""Extracción de campos de un documento (factura/nómina/IRPF/SS) con `claude -p`
headless. Mismo enfoque que el pipeline Austral (sin coste IAP). Devuelve un dict.

Requiere que el usuario `odoo` tenga la CLI `claude` autenticada (claude /login).
"""
import json
import logging
import os
import subprocess
from pathlib import Path

CLAUDE_BIN = os.getenv("CLAUDE_BIN", "/usr/local/bin/claude")
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "180"))
TOLERANCE = 0.05

log = logging.getLogger("extractor")

PROMPT = """You are a Spanish accounting document extractor for company "{company_name}" (CIF {company_vat}).

Read the file at: {file_path}

Step 1: classify the document. document_type must be one of:
  - "invoice": vendor bill / factura de proveedor (the most common case)
  - "nomina": employee payslip / nomina
  - "irpf_payment": IRPF retention payment receipt to AEAT (modelos 111, 115, 130, 190, 216 etc.)
  - "ss_payment": Social Security cotizacion document from TGSS
  - "other_official": other tax / official document (multas, subvenciones, modelos AEAT distintos a IRPF, certificados)
  - "not_a_document": the file is not a financial document (skip)

Step 2: extract fields. Output ONLY a valid JSON object - no markdown, no prose.

Common fields (always include):
- document_type: one of the values above
- supplier_name (string): vendor / counterparty / employee name. For nomina, use the employee name. For irpf/ss, use "Hacienda Publica" or "TGSS Tesoreria General de la Seguridad Social".
- supplier_vat (string or null): NIF/CIF if printed. US/foreign invoices often have NO VAT — in that case use null and make sure supplier_name is EXACT (the system matches the supplier by company name). For nomina employee, use their NIF. For irpf, use "Q2826000H" (HP). For TGSS, use "Q2827003A". Strip "ES" prefix.
- supplier_street / supplier_city / supplier_zip / supplier_country_code / supplier_email /
  supplier_phone (strings or null): the SUPPLIER's address and contact data AS PRINTED on the
  document (country as ISO-2 code, e.g. "ES", "FR", "US"). Used to fill the vendor record —
  extract whatever is printed, null for what is not.
- invoice_ref (string): document reference (factura number, modelo+trimestre+ejercicio for IRPF, etc.)
- currency (string): ISO-4217 code of the document's currency AS PRINTED ("EUR", "USD", "GBP"...).
  Look at the symbols/labels ($, US$, USD → "USD"). Default "EUR" only if clearly euros.
- invoice_date (string YYYY-MM-DD): issue date.
- due_date (string YYYY-MM-DD or null): fecha de VENCIMIENTO del pago. LOCALÍZALA activamente —
  alimenta la previsión de tesorería. Busca en este orden:
    (a) fecha explícita: "Vencimiento", "Fecha de vencimiento", "Fecha límite de pago",
        "Due date", "Payment due", "Pagadero antes de", "Please pay by";
    (b) domiciliación/cargo SEPA: "Fecha de cargo", "Se cargará en su cuenta el <fecha>",
        "cargo en cuenta el día N" → esa fecha de cargo ES el vencimiento;
    (c) plazos SIN fecha explícita: "Pago a 30 días", "30 días f.f.", "60 días fecha factura",
        "Net 15/30/60" → CALCULA invoice_date + N días y devuelve esa fecha;
    (d) "al contado", "pago inmediato", "recibí", pagado con tarjeta → usa invoice_date.
  null SOLO si el documento no dice nada sobre cuándo se paga.
- is_refund (boolean): true si el documento es una factura RECTIFICATIVA o de ABONO
  (credit note). Señales: "Factura rectificativa", "Abono", "Credit note", "Nota de
  crédito", importes/totales NEGATIVOS, o texto "rectifica/anula/abona la factura X".
  Si is_refund=true devuelve subtotal/tax_total/total y lines[].amount en POSITIVO
  (valor absoluto) — el sistema la contabiliza como in_refund con su serie propia.
- rectifies_ref (string or null): nº de la factura ORIGINAL que rectifica, si el
  documento lo menciona ("abono de la factura ...", "rectificativa de ...",
  "ref. factura original ..."). null si no aparece.
- subtotal (number): base imponible / sueldo bruto / cuota base.
- tax_total (number): IVA / 0 for nominas / 0 for irpf payments.
- total (number): total a pagar.
- lines (array): one entry per relevant section. Each: {{"description": "...", "amount": <number>, "tax_rate": 21|10|4|0, "qty": <units if printed, else null>, "unit_price": <price per unit WITHOUT VAT if printed, else null>, "code": "<product/article code or null>", "account": "<expense account code if a reviewer rule dictates it for THIS line, else null>", "tax_name": "<EXACT Odoo tax name if a reviewer rule dictates the tax for THIS line, else null>"}}. amount = line base total (qty x unit_price).
  Use lines[].tax_name (not the document-level tax_name) whenever DIFFERENT lines of the same
  invoice need DIFFERENT taxes. Copy the tax name VERBATIM and COMPLETE from the rule — never a
  prefix of it ("21% EX B" is a DIFFERENT tax from "21% EX B DUA").
  · REJILLAS DE TALLA/VARIANTE (textil, calzado…): si un mismo artículo reparte sus unidades en
    COLUMNAS de talla en una sola fila (p.ej. cabeceras 3XS 2XS XS S M L XL 2XL 3XL con una cantidad
    debajo de cada talla), NO lo unifiques en una sola línea. Emite UNA línea por CADA talla con
    unidades > 0: description = descripción + la talla entre paréntesis (p.ej. "SP2303 SLIM PLUS
    NEGRO (S)"), el MISMO code, qty = unidades de esa talla, unit_price = precio por unidad, amount =
    qty x unit_price. Así cada talla casa con su línea del pedido de compra (que también viene
    desglosado por talla). La suma de qty de esas líneas debe igualar el total de unidades del artículo
    y la suma de amount debe cuadrar con la base.
- order_refs (array of strings): OUR purchase order / delivery note numbers referenced on the
  invoice (e.g. "P01234", "Pedido: ...", "Su pedido", "Order no.", "Albaran ..."). [] if none.
- company_recipient (string or null): which GROUP COMPANY the document is ADDRESSED to — look at the
  CUSTOMER block ("Bill to" / "Receptor de factura" / "Cliente"), NEVER at the supplier:
    "AUS" = INTERNATIONAL AUSTRAL SPORT SA, a.k.a. "AUSTRAL" (CIF/VAT ESA39100573, Spain)
    There is only ONE company in this pipeline: if the recipient is Austral (or you cannot tell
    but the document is clearly ours), answer "AUS".
  Use null ONLY if the recipient is genuinely unreadable. For bank/card statements, the recipient is
  the account HOLDER company. This field decides in WHICH accounting the entry is posted — be strict.
- reverse_charge (boolean): true if this is a reverse-charge invoice (0% VAT printed with mentions
  like "Reverse Charge", "inversion del sujeto pasivo" / "ISP", "Customer to self-assess VAT",
  EC Dir. 2006/112, or foreign supplier of services). Keep tax_total as printed (usually 0); the
  system applies the Spanish auto-repercusion (472/477) itself.
- reverse_charge_kind (string or null, only if reverse_charge): "intracomunitario" (EU supplier),
  "extracomunitario" (non-EU supplier, e.g. US/UK), or "isp_nacional" (Spanish supplier with
  domestic reverse charge). Decide by the supplier's country.
- irpf_rate / irpf_amount (numbers or null): Spanish IRPF WITHHOLDING if the invoice shows it
  (professionals 15%/7%, rents 19%). total = subtotal + tax_total - irpf_amount in that case.
- expense_account (string or null): ONLY if a reviewer rule or the reviewer note explicitly fixes the
  expense account code for this document (e.g. "627000002"). Otherwise null. If a rule dictates the
  account of ONE line only, use lines[].account instead.
- accounting_date (string YYYY-MM-DD or null): ONLY if a reviewer rule dictates an ACCOUNTING date
  (fecha contable) different from the printed invoice date — e.g. "usa siempre la fecha actual"
  means TODAY ({hoy}). Otherwise null (invoice_date is used).
- tax_name (string or null): ONLY if a reviewer rule dictates the EXACT Odoo tax by its name
  (e.g. "IVA 21% Compra con Inversión del Sujeto Pasivo Nacional"). Copy the name VERBATIM from
  the rule. Otherwise null (the system picks the tax by rate/kind).
- extraction_confidence (number 0..1): your honest confidence.
- extraction_notes (string): any doubt, assumption, OCR ambiguity, or relevant remark for the human reviewer.

- asiento_directo (object or null): ONLY when a REVIEWER RULE says this document must NOT be
  booked as a supplier invoice but as a DIRECT accounting entry (typical wording: "es un recibo,
  contabilízalo como asiento contable directamente contra la cuenta 520004", or a rule that gives
  the entry itself: "16,99 627100 (Debe) a 520004 16,99"). Then output
  {{"debe": "<expense account code>", "haber": "<counterpart account code>", "importe": <number>,
  "motivo": "<the rule, verbatim>"}}. Rules:
  · importe is the TOTAL PAID (VAT included) - a direct entry has no VAT breakdown and no supplier
    account: it is DEBIT expense / CREDIT the account the rule names (usually the 5200xx card).
  · if the rule names only the counterpart ("contra la 520004"), put the expense account you would
    have used in "debe" (the one from the reviewer rules or the supplier history).
  · WITHOUT such a rule this field MUST be null: by default everything is a supplier invoice.

Special fields by document_type (optional but useful):
- For "nomina": REQUIRED include "extra": {{"irpf_total": <number>, "ss_empleado_total": <number>, "aportaciones_empresa_total": <number>, "liquido_total": <number>, "period": "YYYY-MM", "employees": [{{"name": "...", "nif": "...", "bruto": <number>, "irpf": <number>, "ss": <number>, "liquido": <number>}}]}}. aportaciones_empresa_total is the SUM of the FULL company SS contributions (~30% of bruto total). The "lines" array must contain ONE entry per employee with description="Nomina <nombre> <NIF> bruto <bruto> liquido <liquido>", amount=bruto, tax_rate=0.
- For "irpf_payment": include "extra": {{"modelo": "111"|"115"|"130"|"190"|"216", "ejercicio": "YYYY", "periodo": "1T"|"2T"|"3T"|"4T"|"01"|...}}
- For "ss_payment": include "extra": {{"periodo": "YYYY-MM", "ccc": "..."}}

Rules:
- Read tax rates AS PRINTED. Spain has 21/10/4/0. For nominas/irpf/ss, tax_rate is normally 0.
- Confidence: 1.0 = clear PDF nativo; 0.7-0.9 = minor doubts; <0.7 = ambiguous; 0.6 = had to assume IVA rate.
- subtotal + tax_total must equal total within 0.05 EUR. If not, write values as printed and note discrepancy.
- VAT-INCLUDED documents (tickets / facturas simplificadas): lines[].amount MUST be the BASE (price WITHOUT IVA) so the sum of line amounts equals subtotal (base imponible). If only the IVA-inclusive total is legible, derive base = round(total/(1+rate), 2) and tax_total = round(total - base, 2).
- BLURRY or incomplete scans: if the per-line detail is illegible but the TOTALS block (Base/IVA/Total) IS legible, TRUST the totals block - output a SINGLE summary line with amount=subtotal (base) and the correct tax_rate.
- For nominas: "total" is the net paid (liquido). subtotal = bruto. tax_total = retenciones (irpf+ss empleado).
- For irpf payments: "total" is the amount paid to AEAT. subtotal = total, tax_total = 0.
- If the document is not extractable, output: {{"document_type": "not_a_document", "error": "<reason>"}}.

Output: a SINGLE JSON object. Nothing else.
"""


def _strip_code_fences(text: str) -> str:
    if text.startswith("```"):
        lines = text.split("\n")
        cleaned = [l for l in lines if not l.strip().startswith("```")]
        return "\n".join(cleaned).strip()
    return text


def _parse_first_json(text: str) -> dict:
    text = text.replace("�", "?")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder(strict=False)
    for i, c in enumerate(text):
        if c == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                return obj
            except json.JSONDecodeError:
                continue
    return {"error": "could not parse claude output as JSON"}


def extract(file_path: Path, company: dict, hint: str = None, rules: list = None) -> dict:
    """Invoca `claude -p` sobre el fichero y devuelve el dict extraído.

    rules: reglas del revisor [{texto, imagen}] — se inyectan en el prompt (y las
    imágenes se leen como referencia). Vienen de la tabla regla_asiento de la web."""
    from datetime import date as _date
    file_path = Path(file_path)
    prompt = PROMPT.format(
        company_name=company.get("name", ""),
        company_vat=company.get("vat") or "-",
        file_path=file_path.name,
        hoy=_date.today().isoformat(),
    )
    extra_dirs = []
    if rules:
        prompt += ("\n\nREVIEWER ACCOUNTING RULES (human instructions — they take PRECEDENCE over the"
                   " defaults above. Apply each rule ONLY if this document matches what it describes:"
                   " rules naming a specific supplier apply only to that supplier's documents):\n")
        for r in rules:
            linea = "- " + str(r.get("texto") or "").strip()
            img = r.get("imagen")
            if img and Path(img).exists():
                linea += f" (reference image — READ it: {img})"
                extra_dirs.append(str(Path(img).parent))
            prompt += linea + "\n"
    if hint:
        prompt += ("\n\nNOTA DEL REVISOR (instrucción humana, tenla MUY en cuenta): " + str(hint) + "\n")
    log.info(f"  claude -p sobre {file_path.name}" + (f" ({len(rules)} reglas)" if rules else ""))
    cmd = [CLAUDE_BIN, "-p", prompt, "--output-format", "text",
           "--permission-mode", "bypassPermissions", "--add-dir", str(file_path.parent)]
    for d in sorted(set(extra_dirs)):
        cmd += ["--add-dir", d]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=CLAUDE_TIMEOUT,
            cwd=str(file_path.parent),
            env={**os.environ, "HOME": os.environ.get("HOME", "/opt/odoo17")},
        )
    except subprocess.TimeoutExpired:
        return {"error": "claude timed out"}
    if result.returncode != 0:
        return {"error": f"claude exit {result.returncode}: {result.stderr[:300]}"}
    return _parse_first_json(_strip_code_fences(result.stdout.strip()))


def validate(data: dict) -> str | None:
    """None si el payload es válido; si no, string con el motivo."""
    if "error" in data:
        return data["error"]
    dt = (data.get("document_type") or "").lower()
    if dt == "not_a_document":
        return f"not a document: {data.get('error', '')}"
    # supplier_vat NO es obligatorio: algunas facturas extranjeras no llevan NIF y el
    # proveedor se cruza por NOMBRE (find_or_create_supplier ya lo hace).
    required = ["supplier_name", "invoice_ref", "invoice_date",
                "subtotal", "tax_total", "total", "lines"]
    missing = [k for k in required if k not in data or data[k] in (None, "")]
    if missing:
        return f"missing fields: {missing}"
    try:
        sub = round(float(data["subtotal"]), 2)
        tax = round(float(data["tax_total"]), 2)
        tot = round(float(data["total"]), 2)
        if dt == "nomina":
            # Nómina: bruto − (IRPF + SS empleado + especie + OTRAS retenciones) = líquido.
            # 'tax_total' del extractor no siempre trae las retenciones; se calculan de
            # 'extra'. Las OTRAS deducciones no desglosadas (embargo judicial, anticipos)
            # las absorbe process_nomina como residuo → aquí solo se rechaza si el
            # líquido SUPERA el devengo neto de especie (descuadre imposible).
            ex = data.get("extra") or {}
            especie = round(float(ex.get("salario_especie_total") or 0), 2)
            irpf = round(float(ex.get("irpf_total") or 0), 2)
            ss = round(float(ex.get("ss_empleado_total") or 0), 2)
            liq = round(float(ex.get("liquido_total") or tot or 0), 2)
            deduc = tax if tax > 0 else (irpf + ss)   # retenciones estándar declaradas
            residuo = round((sub - deduc - especie) - liq, 2)   # otras retenciones (embargo…)
            if residuo < -TOLERANCE:
                return (f"math mismatch nomina: líquido({liq}) mayor que devengo neto "
                        f"bruto({sub})-deduc({deduc})-especie({especie})")
        else:
            # con retención IRPF: total = base + IVA − retención. Si no viene declarada
            # pero el descuadre coincide con un tipo estándar, se infiere (Austral FAC13).
            irpf = round(float(data.get("irpf_amount") or 0), 2)
            if irpf <= 0 and sub > 0:
                diff = round(sub + tax - tot, 2)
                for rate in (19.0, 15.0, 7.0, 2.0, 1.0):
                    if diff > 0 and abs(diff - round(sub * rate / 100.0, 2)) <= 0.05:
                        data["irpf_rate"] = rate
                        data["irpf_amount"] = diff
                        irpf = diff
                        break
            if abs((sub + tax - irpf) - tot) > TOLERANCE:
                return (f"math mismatch: {sub}+{tax}-irpf({irpf})!={tot}" if irpf
                        else f"math mismatch: {sub}+{tax}!={tot}")
    except (TypeError, ValueError) as e:
        return f"invalid numbers: {e}"
    return None
