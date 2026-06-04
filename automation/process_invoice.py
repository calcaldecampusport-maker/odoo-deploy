#!/usr/bin/env python3
"""
Process a vendor invoice into Odoo via direct ORM access.

Multi-company aware: pass --company-id <n> and the script will resolve
the right journal, expense account and taxes for that company.

JSON schema expected (produced by Cowork, posted by poller):
{
  "supplier_name": "Iberdrola Clientes SAU",
  "supplier_vat":  "A95758389",
  "invoice_ref":   "F12345678",
  "invoice_date":  "2026-01-15",
  "due_date":      "2026-02-15",
  "subtotal":      100.00,
  "tax_total":     21.00,
  "total":         121.00,
  "lines": [{"description":"...", "amount": 100.00, "tax_rate": 21}],
  "extraction_confidence": 0.95
}

Exit codes:
  0  -> created OK in draft
  10 -> validation failed (move PDF to Revision)
  20 -> duplicate (already exists, no action)
  30 -> ORM error
  40 -> bad input
"""
# === pipeline isolation guard (auto-injected) ===
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
if _HERE not in _sys.path:
    _sys.path.insert(0, _HERE)
try:
    import companies as _comp_guard
    if getattr(_comp_guard, "PIPELINE_NAME", None) != 'cararjfam':
        raise RuntimeError(
            f"PIPELINE_MISMATCH: script {__file__} expected pipeline='cararjfam' "
            f"but loaded companies.PIPELINE_NAME={getattr(_comp_guard, 'PIPELINE_NAME', None)!r}"
        )
except ImportError:
    pass  # script sin dependencia de companies.py (e.g. drive_ops)
# === end isolation guard ===

import argparse
import json
import logging
import sys
from pathlib import Path
from datetime import datetime

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"
DEFAULT_EXPENSE_ACCOUNT_CODE = "600000"
# We accept any confidence: invoices stay in draft, the human reviewer is the
# real safety net. The math/VAT-format checks below still gate hard errors.
MIN_CONFIDENCE = 0.0
TOTAL_TOLERANCE = 0.02
SPECIAL_TAX_PREFIXES = (" EX", " EU", " IG", " RC", " ND")

# Auto-publish (validate) the invoice if extraction_confidence >= this.
AUTO_POST_THRESHOLD = 0.90

# Per-document-type default expense account override.
DOC_TYPE_DEFAULT_ACCOUNT = {
    "invoice": "600000",
    "nomina": "640000",       # Sueldos y salarios
    "irpf_payment": "475100", # HP retenciones practicadas
    "ss_payment": "642000",   # Seg Social a cargo empresa
    "other_official": "629000",
}

sys.path.insert(0, ODOO_PATH)
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402



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


log = logging.getLogger("invoice_processor")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def normalize_vat(vat: str, country: str = "ES") -> str:
    if not vat:
        return ""
    v = vat.replace(" ", "").replace("-", "").replace(".", "").upper()
    if country == "ES" and not v.startswith("ES") and len(v) in (8, 9):
        v = "ES" + v
    return v


def validate_payload(data: dict) -> list[str]:
    errors = []
    required = ["supplier_name", "supplier_vat", "invoice_ref", "invoice_date",
                "subtotal", "tax_total", "total", "lines"]
    for k in required:
        if k not in data or data[k] in (None, ""):
            errors.append(f"missing field: {k}")
    if errors:
        return errors

    if data.get("extraction_confidence", 0) < MIN_CONFIDENCE:
        errors.append(f"low confidence: {data.get('extraction_confidence')}")

    doc_type = (data.get("document_type") or "invoice").lower()
    try:
        sub = round(float(data["subtotal"]), 2)
        tax = round(float(data["tax_total"]), 2)
        tot = round(float(data["total"]), 2)
        if doc_type == "nomina":
            extra = data.get("extra") or {}
            especie = round(float(extra.get("salario_especie_total") or 0), 2)
            if abs((sub - tax - especie) - tot) > TOTAL_TOLERANCE:
                errors.append(f"math mismatch nomina: bruto({sub})-tax({tax})-especie({especie})!=liquido({tot})")
        else:
            if abs((sub + tax) - tot) > TOTAL_TOLERANCE:
                errors.append(f"math mismatch: subtotal({sub}) + tax({tax}) != total({tot})")
    except (TypeError, ValueError) as e:
        errors.append(f"invalid amounts: {e}")

    try:
        datetime.strptime(data["invoice_date"], "%Y-%m-%d")
    except (TypeError, ValueError):
        errors.append(f"invalid invoice_date format (expected YYYY-MM-DD): {data.get('invoice_date')}")

    if not data.get("lines"):
        errors.append("no lines")
    else:
        line_sum = sum(round(float(l.get("amount", 0)), 2) for l in data["lines"])
        if doc_type != "nomina" and abs(line_sum - sub) > TOTAL_TOLERANCE:
            errors.append(f"line amounts ({line_sum}) do not sum to subtotal ({sub})")

    return errors


def _norm_partner_name(name: str) -> str:
    """Normaliza nombre partner para fallback search anti-duplicados.
    Mayuscula + collapse spaces + strip SL/SA/SLU/SAU suffix."""
    import re as _re
    if not name: return ""
    n = name.upper().strip()
    n = _re.sub(r"\s+", " ", n)
    # Quitar sufijos sociedad sin afectar nombres principales
    n = _re.sub(r",?\s*S\.?\s*L\.?\s*(U\.?)?\.?$", "", n).strip()
    n = _re.sub(r",?\s*S\.?\s*A\.?\s*(U\.?)?\.?$", "", n).strip()
    n = _re.sub(r",?\s*S\.?\s*L\.?\s*L\.?$", "", n).strip()
    return n


def find_or_create_supplier(env, data: dict):
    """Find or create supplier — defensa multi-capa contra duplicados:
    1. Aplica vat_correction si hay regla aprendida para ese partner_name
    2. Genera VARIANTES del VAT: original, con ES prefix, sin ES prefix
    3. Busca por TODAS las variantes (no solo la canónica)
    4. Si no encuentra por VAT, busca por nombre normalizado (catch duplicados por VAT distinto/típo)
    5. Solo crea nuevo si nada matcheó; siempre con VAT canónico (con ES)
    """
    corrected, applied = _maybe_correct_vat(env, data.get("supplier_name",""), data.get("supplier_vat",""))
    if applied:
        log.info(f"  vat_correction applied: pattern={applied!r} VAT {data['supplier_vat']!r} -> {corrected!r}")
        data["supplier_vat"] = corrected

    extracted_vat = (data.get("supplier_vat") or "").strip().upper().replace(" ", "").replace("-", "")
    supplier_name = (data.get("supplier_name") or "").strip()
    canonical = normalize_vat(extracted_vat, "ES") if extracted_vat else ""

    # 1. Buscar por variantes VAT (con ES, sin ES, raw)
    variants = set()
    if extracted_vat:
        variants.add(extracted_vat)
        variants.add(canonical)
        if canonical.startswith("ES"):
            variants.add(canonical[2:])  # sin ES
        else:
            variants.add("ES" + canonical)
    for v in variants:
        if not v: continue
        p = env["res.partner"].search([("vat", "=", v)], limit=1)
        if p:
            if p.supplier_rank < 1:
                p.supplier_rank = 1
            # Normalizar el VAT del partner existente si difiere del canonical
            if canonical and p.vat != canonical:
                try:
                    p.write({"vat": canonical})  # via ORM → respeta base_vat normalize
                    log.info(f"  VAT del partner id={p.id} normalizado: {p.vat!r} -> {canonical!r}")
                except Exception as e:
                    log.warning(f"  could not normalize VAT id={p.id}: {e}")
            return p

    # 2. Fallback: buscar por nombre normalizado (anti-duplicate por VAT typo o ausente)
    if supplier_name:
        norm_target = _norm_partner_name(supplier_name)
        if norm_target and len(norm_target) >= 4:
            # Buscar candidatos por primera palabra significativa
            first_word = norm_target.split()[0] if norm_target.split() else ""
            if len(first_word) >= 4:
                candidates = env["res.partner"].search([
                    ("active", "=", True),
                    ("is_company", "=", True),
                    ("name", "ilike", first_word),
                ], limit=20)
                for c in candidates:
                    if _norm_partner_name(c.name or "") == norm_target:
                        log.info(f"  match by NAME (no VAT match): id={c.id} '{c.name}' — actualizo VAT/supplier_rank")
                        if c.supplier_rank < 1:
                            c.supplier_rank = 1
                        if canonical and not c.vat:
                            try: c.write({"vat": canonical})
                            except Exception: pass
                        return c

    # 3. No match → crear nuevo con VAT canónico
    es = env.ref("base.es", raise_if_not_found=False)
    return env["res.partner"].create({
        "name": supplier_name,
        "vat": canonical if canonical else False,
        "is_company": True,
        "supplier_rank": 1,
        "country_id": es.id if es else False,
        "company_id": False,
    })


def find_purchase_tax(env, rate: float, company_id: int):
    if rate is None:
        return None
    rate = float(rate)
    if rate == 0:
        exempt = env["account.tax"].search(
            [("type_tax_use", "=", "purchase"), ("amount", "=", 0),
             ("company_id", "=", company_id), ("active", "=", True)],
            limit=1,
        )
        return exempt or None

    candidates = env["account.tax"].search([
        ("type_tax_use", "=", "purchase"),
        ("amount", "=", rate),
        ("amount_type", "=", "percent"),
        ("company_id", "=", company_id),
        ("active", "=", True),
    ])
    if not candidates:
        return None
    domestic = candidates.filtered(
        lambda t: t.name and not any(p in t.name for p in SPECIAL_TAX_PREFIXES)
    )
    pool = domestic or candidates
    goods = pool.filtered(lambda t: t.name and t.name.strip().endswith(" G"))
    if goods:
        return goods[0]
    services = pool.filtered(lambda t: t.name and t.name.strip().endswith(" S"))
    if services:
        return services[0]
    return pool[0]


def find_expense_account(env, company_id: int, code: str = DEFAULT_EXPENSE_ACCOUNT_CODE):
    return env["account.account"].search(
        [("code", "=", code), ("company_id", "=", company_id)], limit=1
    )


def find_account_by_doc_type(env, company_id: int, doc_type: str):
    code = DOC_TYPE_DEFAULT_ACCOUNT.get(doc_type, DEFAULT_EXPENSE_ACCOUNT_CODE)
    return find_expense_account(env, company_id, code)


def find_account_by_rule(env, line_description: str, company_id: int):
    """Try to find a learned.rule matching this line. Returns account.account or None."""
    rule_model = env["ir.model"].search([("model", "=", "learned.rule")], limit=1)
    if not rule_model:
        return None
    rule = env["learned.rule"].find_match(line_description, "invoice", company_id)
    if rule and rule.account_id:
        rule.mark_applied()
        return rule.account_id
    return None


def find_purchase_journal(env, company_id: int):
    return env["account.journal"].search(
        [("type", "=", "purchase"), ("company_id", "=", company_id)], limit=1
    )


def already_exists(env, partner_id: int, ref: str, date: str, company_id: int):
    return env["account.move"].search([
        ("move_type", "=", "in_invoice"),
        ("partner_id", "=", partner_id),
        ("ref", "=", ref),
        ("invoice_date", "=", date),
        ("company_id", "=", company_id),
    ], limit=1)


def attach_pdf(env, move, pdf_path: Path):
    if not pdf_path or not pdf_path.exists():
        return None
    with open(pdf_path, "rb") as f:
        data_bytes = f.read()
    mimetype = "application/pdf"
    name = pdf_path.name
    if pdf_path.suffix.lower() in (".jpg", ".jpeg"):
        mimetype = "image/jpeg"
    elif pdf_path.suffix.lower() == ".png":
        mimetype = "image/png"
    elif pdf_path.suffix.lower() in (".heic", ".heif"):
        mimetype = "image/heif"
    att = env["ir.attachment"].create({
        "name": name,
        "type": "binary",
        "raw": data_bytes,
        "res_model": "account.move",
        "res_id": move.id,
        "mimetype": mimetype,
    })
    # Vincular al chatter del move para que aparezca en la sección de notas/mensajes
    try:
        move.with_context(mail_create_nosubscribe=True).message_post(
            body=f"Documento original adjunto: <b>{name}</b>",
            attachment_ids=[att.id],
            subtype_xmlid="mail.mt_note",
        )
    except Exception as e:
        log.warning(f"could not post attachment to chatter: {e}")
    return att


def build_invoice_lines(env, data: dict, default_account, company_id: int):
    lines = []
    for raw in data["lines"]:
        tax = find_purchase_tax(env, raw.get("tax_rate"), company_id)
        description = raw.get("description") or raw.get("desc") or "Linea sin descripcion"
        # Apply per-line learned rule if any matches the description
        rule_account = find_account_by_rule(env, description, company_id)
        account_to_use = rule_account or default_account
        line = {
            "name": description,
            "quantity": 1.0,
            "price_unit": float(raw["amount"]),
            "account_id": account_to_use.id,
        }
        if tax:
            line["tax_ids"] = [(6, 0, [tax.id])]
        lines.append((0, 0, line))
    return lines


def process(env, data: dict, pdf_path: Path | None, company_id: int):
    journal = find_purchase_journal(env, company_id)
    if not journal:
        log.error(f"no purchase journal for company_id={company_id}")
        return 30
    doc_type = (data.get("document_type") or "invoice").lower()
    expense_account = find_account_by_doc_type(env, company_id, doc_type)
    if not expense_account:
        log.error(f"no default account for doc_type={doc_type} company_id={company_id}")
        return 30

    supplier = find_or_create_supplier(env, data)
    log.info(f"supplier: id={supplier.id} name={supplier.name!r} vat={supplier.vat}")

    existing = already_exists(env, supplier.id, data["invoice_ref"], data["invoice_date"], company_id)
    if existing:
        log.warning(f"duplicate: account.move id={existing.id} already exists for company {company_id}")
        print(f"INVOICE_ID={existing.id}")
        print(f"DUPLICATE=1")
        return 20

    try:
        total_for_type_check = float(data.get("total") or 0)
    except (TypeError, ValueError):
        total_for_type_check = 0.0
    move_type = "in_refund" if total_for_type_check < 0 else "in_invoice"
    if move_type == "in_refund":
        if "lines" in data and isinstance(data["lines"], list):
            for raw in data["lines"]:
                try:
                    raw["amount"] = abs(float(raw.get("amount", 0)))
                except (TypeError, ValueError):
                    pass

    move_vals = {
        "move_type": move_type,
        "partner_id": supplier.id,
        "ref": data["invoice_ref"],
        "invoice_date": data["invoice_date"],
        "journal_id": journal.id,
        "company_id": company_id,
        "invoice_line_ids": build_invoice_lines(env, data, expense_account, company_id),
    }
    if data.get("due_date"):
        move_vals["invoice_date_due"] = data["due_date"]

    move = env["account.move"].with_company(company_id).create(move_vals)
    log.info(f"created account.move id={move.id} state={move.state} amount_total={move.amount_total} (company {company_id})")

    expected_total = round(float(data["total"]), 2)
    actual_total = round(move.amount_total, 2)
    if abs(actual_total - expected_total) > TOTAL_TOLERANCE:
        log.warning(
            f"total mismatch after computation: expected={expected_total} actual={actual_total} "
            f"(invoice id={move.id} kept in draft for review)"
        )

    if pdf_path:
        att = attach_pdf(env, move, pdf_path)
        if att:
            log.info(f"attached file id={att.id} name={att.name}")

    confidence = float(data.get("extraction_confidence", 0) or 0)
    notes = (data.get("extraction_notes") or "").strip()

    narration_parts = []
    if doc_type != "invoice":
        narration_parts.append(f"📄 Tipo documento: {doc_type}")
    if notes:
        narration_parts.append(f"⚠ Observaciones extraccion automatica:\n{notes}")
    narration_parts.append(f"Confianza: {confidence:.2f}")
    if pdf_path:
        narration_parts.append(f"Origen: {pdf_path.name}")
    move.narration = "\n\n".join(narration_parts)

    chatter_lines = [
        f"Factura procesada automaticamente. Tipo: {doc_type}. Confianza: {confidence:.2f}.",
    ]
    if notes:
        chatter_lines.append(f"<b>Observaciones:</b><br/>{notes}")
    if pdf_path:
        chatter_lines.append(f"Origen: {pdf_path.name}")
    move.message_post(body="<br/><br/>".join(chatter_lines), message_type="comment")

    auto_posted = False
    always_post = doc_type in ("nomina", "irpf_payment", "ss_payment", "other_official")
    if always_post or (confidence >= AUTO_POST_THRESHOLD and doc_type == "invoice"):
        try:
            move.action_post()
            auto_posted = True
            log.info(f"auto-posted move id={move.id} (doc_type={doc_type}, confidence {confidence:.2f})")
        except Exception as e:
            log.warning(f"auto-post failed for move id={move.id}: {e} — kept in draft")

    print(f"INVOICE_ID={move.id}")
    print(f"AMOUNT_TOTAL={move.amount_total}")
    print(f"STATE={move.state}")
    print(f"COMPANY_ID={company_id}")
    print(f"AUTO_POSTED={1 if auto_posted else 0}")
    print(f"DOC_TYPE={doc_type}")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--json", required=True, help="Path to invoice JSON")
    p.add_argument("--pdf", required=False, help="Path to invoice PDF/image")
    p.add_argument("--company-id", type=int, required=True, help="Odoo res.company id")
    args = p.parse_args()

    json_path = Path(args.json)
    pdf_path = Path(args.pdf) if args.pdf else None

    if not json_path.exists():
        log.error(f"JSON not found: {json_path}")
        return 40

    with open(json_path) as f:
        try:
            data = json.load(f)
        except json.JSONDecodeError as e:
            log.error(f"invalid JSON: {e}")
            return 40

    errors = validate_payload(data)
    if errors:
        log.error("validation failed:")
        for e in errors:
            log.error(f"  - {e}")
        print("VALIDATION_ERRORS=" + "; ".join(errors))
        return 10

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid", "allowed_company_ids": [args.company_id]})
        try:
            rc = process(env, data, pdf_path, args.company_id)
            if rc in (0, 20):
                cr.commit()
            else:
                cr.rollback()
            return rc
        except odoo.exceptions.ValidationError as e:
            cr.rollback()
            log.warning(f"Odoo validation rejected the invoice: {e}")
            print(f"VALIDATION_ERRORS={e}")
            return 10
        except odoo.exceptions.UserError as e:
            cr.rollback()
            log.warning(f"Odoo user error: {e}")
            print(f"VALIDATION_ERRORS={e}")
            return 10
        except Exception as e:
            cr.rollback()
            log.exception("ORM error during processing")
            print(f"ERROR={type(e).__name__}: {e}")
            return 30


if __name__ == "__main__":
    sys.exit(main())
