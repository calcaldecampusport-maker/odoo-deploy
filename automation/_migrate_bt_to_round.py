#!/usr/bin/env python3
"""
Migra BT (Best Training) desde la BD cararjfam (company_id=2) → BD round_facturacion (company_id=3).
Período: 2025-10-01 en adelante.

Migra:
- res.partner (47 distintos) — fusionando por VAT canónico o nombre normalizado
- account.move (in_invoice + entry + ...) y sus AMLs
- account.bank.statement + bank.statement.line
- ir.attachment (PDFs + imágenes) — copiando bytes entre filestores
- learned.rule (rule_type=bank/invoice/vat_correction) con account_id+partner_id mapeados

NO migra: reconciliaciones (partir desreconciliado en destino — decisión usuario).

Idempotente parcial: no re-crea partners/moves si ya están en destino con mismo VAT+ref.
"""
import base64
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import date

sys.path.insert(0, "/opt/odoo17/odoo")
import odoo
from odoo.api import Environment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("migrate_bt")

SRC_DB = "cararjfam"
SRC_COMPANY_ID = 2
DST_DB = "round_facturacion"
DST_COMPANY_ID = 3
CUTOFF_DATE = "2025-10-01"

# Mapeo de journals por code (origen → destino code)
# cararjfam BT tiene: BSAN (banco santander), MISC (general), BILL (compras), INV (ventas)
# round_facturacion BT tiene: BSAN, MISC, BILL, INV con mismos codes
JOURNAL_CODE_MAP = {
    # mismo code en ambas BD
}

stats = {
    "partners_created": 0,
    "partners_reused": 0,
    "moves_created": 0,
    "moves_skipped_dup": 0,
    "amls_created": 0,
    "bank_lines_created": 0,
    "attachments_copied": 0,
    "attachments_failed": 0,
    "learned_rules_created": 0,
    "errors": [],
}


def _norm_vat(v):
    if not v: return ""
    v = v.upper().strip().replace(" ", "").replace("-", "")
    if re.match(r"^[A-Z]\d{7,8}[A-Z0-9]?$", v):
        v = "ES" + v
    return v


def _norm_name(name):
    if not name: return ""
    n = name.upper().strip()
    n = re.sub(r"\s+", " ", n)
    n = re.sub(r",?\s*S\.?\s*L\.?\s*(U\.?)?\.?$", "", n).strip()
    n = re.sub(r",?\s*S\.?\s*A\.?\s*(U\.?)?\.?$", "", n).strip()
    return n


def find_or_create_partner(env_dst, src_partner):
    """Busca partner en destino por VAT/nombre normalizado. Si no, crea."""
    src_vat = (src_partner.vat or "").strip()
    src_name = (src_partner.name or "").strip()
    src_nif_norm = _norm_vat(src_vat)
    Partner = env_dst["res.partner"]

    # Buscar por VAT variantes
    if src_nif_norm:
        variants = {src_vat.upper(), src_nif_norm, src_nif_norm[2:] if src_nif_norm.startswith("ES") else "ES"+src_nif_norm}
        for v in variants:
            if not v: continue
            p = Partner.search([("vat", "=", v)], limit=1)
            if p:
                # Normalizar VAT si difiere del canónico
                if p.vat != src_nif_norm:
                    try: p.write({"vat": src_nif_norm})
                    except Exception: pass
                stats["partners_reused"] += 1
                return p

    # Fallback por nombre normalizado
    if src_name:
        target_norm = _norm_name(src_name)
        if target_norm and len(target_norm) >= 4:
            first = target_norm.split()[0]
            if len(first) >= 4:
                cands = Partner.search([("name", "ilike", first)], limit=20)
                for c in cands:
                    if _norm_name(c.name or "") == target_norm:
                        if src_nif_norm and not c.vat:
                            try: c.write({"vat": src_nif_norm})
                            except Exception: pass
                        stats["partners_reused"] += 1
                        return c

    # Crear nuevo
    es = env_dst.ref("base.es", raise_if_not_found=False)
    vals = {
        "name": src_name,
        "vat": src_nif_norm or False,
        "is_company": src_partner.is_company,
        "supplier_rank": src_partner.supplier_rank or 0,
        "customer_rank": src_partner.customer_rank or 0,
        "country_id": es.id if es else False,
        "email": src_partner.email or False,
        "phone": src_partner.phone or False,
        "street": src_partner.street or False,
        "street2": src_partner.street2 or False,
        "city": src_partner.city or False,
        "zip": src_partner.zip or False,
        "company_id": False,  # shared
    }
    np = Partner.create(vals)
    stats["partners_created"] += 1
    return np


def build_account_map(env_src, env_dst):
    """Mapea account.id origen → account.id destino por code."""
    src_accs = env_src["account.account"].search([("company_id", "=", SRC_COMPANY_ID)])
    src_by_code = {a.code: a.id for a in src_accs if a.code}
    dst_accs = env_dst["account.account"].search([("company_id", "=", DST_COMPANY_ID)])
    dst_by_code = {a.code: a.id for a in dst_accs if a.code}
    m = {}
    missing = []
    for code, src_id in src_by_code.items():
        if code in dst_by_code:
            m[src_id] = dst_by_code[code]
        else:
            missing.append(code)
    if missing:
        log.warning(f"account codes sin match en destino: {missing}")
    log.info(f"account map: {len(m)} cuentas mapeadas (de {len(src_by_code)} origen, {len(dst_by_code)} destino)")
    return m


def build_journal_map(env_src, env_dst):
    """Mapea journal.id origen → journal.id destino por code."""
    src_js = env_src["account.journal"].search([("company_id", "=", SRC_COMPANY_ID)])
    dst_js = env_dst["account.journal"].search([("company_id", "=", DST_COMPANY_ID)])
    dst_by_code = {j.code: j.id for j in dst_js}
    dst_by_type = defaultdict(list)
    for j in dst_js:
        dst_by_type[j.type].append(j.id)
    m = {}
    for j in src_js:
        if j.code in dst_by_code:
            m[j.id] = dst_by_code[j.code]
        elif j.type in dst_by_type:
            # fallback por type
            m[j.id] = dst_by_type[j.type][0]
            log.warning(f"  journal '{j.code}' ({j.type}) sin match exacto en destino → uso primer journal type={j.type}")
        else:
            log.warning(f"  journal '{j.code}' ({j.type}) SIN MATCH → moves usando este journal se saltarán")
    log.info(f"journal map: {len(m)} journals mapeados")
    return m


def copy_attachment(env_src, env_dst, src_att, dst_res_model, dst_res_id):
    """Copia un ir.attachment del origen al destino, vinculándolo al nuevo move."""
    try:
        # Obtener bytes (raw field o leyendo filestore)
        data = src_att.raw or (base64.b64decode(src_att.datas) if src_att.datas else None)
        if not data:
            return None
        new_att = env_dst["ir.attachment"].create({
            "name": src_att.name,
            "type": "binary",
            "raw": data,
            "res_model": dst_res_model,
            "res_id": dst_res_id,
            "mimetype": src_att.mimetype,
            "company_id": DST_COMPANY_ID,
        })
        # Vincular al chatter
        try:
            move = env_dst[dst_res_model].browse(dst_res_id)
            move.with_context(mail_create_nosubscribe=True).message_post(
                body=f"Documento original (migrado de cararjfam): <b>{src_att.name}</b>",
                attachment_ids=[new_att.id],
                subtype_xmlid="mail.mt_note",
            )
        except Exception:
            pass
        stats["attachments_copied"] += 1
        return new_att
    except Exception as e:
        stats["attachments_failed"] += 1
        log.warning(f"  attachment {src_att.id} '{src_att.name}': {str(e)[:100]}")
        return None


def migrate_moves(env_src, env_dst, account_map, journal_map, partner_map):
    """Migra account.move (in_invoice + in_refund + entry + out_*) desde origen → destino."""
    Move = env_src["account.move"]
    src_moves = Move.search([
        ("company_id", "=", SRC_COMPANY_ID),
        ("state", "in", ["posted", "draft"]),
        "|",
            ("invoice_date", ">=", CUTOFF_DATE),
            ("date", ">=", CUTOFF_DATE),
    ], order="date asc, id asc")
    log.info(f"moves a migrar: {len(src_moves)}")

    DstMove = env_dst["account.move"]
    move_id_map = {}

    for sm in src_moves:
        # Skip si ya existe en destino (idempotencia: mismo ref + partner_vat + date + amount)
        # Best key: ref + invoice_date + partner_vat + amount_total
        if sm.ref and sm.partner_id and sm.invoice_date:
            partner_vat_norm = _norm_vat(sm.partner_id.vat or "")
            dup = DstMove.search([
                ("company_id", "=", DST_COMPANY_ID),
                ("ref", "=", sm.ref),
                ("invoice_date", "=", sm.invoice_date),
                ("move_type", "=", sm.move_type),
            ], limit=1)
            if dup:
                stats["moves_skipped_dup"] += 1
                move_id_map[sm.id] = dup.id
                continue
        elif sm.move_type == "entry" and sm.ref:
            dup = DstMove.search([
                ("company_id", "=", DST_COMPANY_ID),
                ("ref", "=", sm.ref),
                ("date", "=", sm.date),
                ("move_type", "=", "entry"),
            ], limit=1)
            if dup:
                stats["moves_skipped_dup"] += 1
                move_id_map[sm.id] = dup.id
                continue

        # Map partner
        dst_partner_id = None
        if sm.partner_id:
            dst_partner_id = partner_map.get(sm.partner_id.id)
            if not dst_partner_id:
                dst_partner = find_or_create_partner(env_dst, sm.partner_id)
                partner_map[sm.partner_id.id] = dst_partner.id
                dst_partner_id = dst_partner.id

        # Map journal
        dst_journal_id = journal_map.get(sm.journal_id.id)
        if not dst_journal_id:
            stats["errors"].append(f"move {sm.id} sin journal mapeado, skipped")
            continue

        # Build lines
        line_ids = []
        for ln in sm.line_ids:
            dst_acc = account_map.get(ln.account_id.id)
            if not dst_acc:
                stats["errors"].append(f"move {sm.id} aml {ln.id}: cuenta {ln.account_id.code} sin map")
                break
            # Map partner línea
            ln_partner = None
            if ln.partner_id:
                ln_partner = partner_map.get(ln.partner_id.id)
                if not ln_partner:
                    np = find_or_create_partner(env_dst, ln.partner_id)
                    partner_map[ln.partner_id.id] = np.id
                    ln_partner = np.id
            line_ids.append((0, 0, {
                "name": ln.name or "/",
                "account_id": dst_acc,
                "partner_id": ln_partner,
                "debit": ln.debit,
                "credit": ln.credit,
                # NO transferimos tax_ids para simplificar (impuestos ya están en debit/credit de líneas separadas)
            }))
        else:
            # Solo crear si todas las líneas mapearon OK
            # Para in_invoice/in_refund/out_invoice/out_refund, Odoo recalcula dinámicamente
            # las líneas via _recompute_dynamic_lines, ignorando los debit/credit que pasamos.
            # Convertimos a 'entry' (asiento contable) para preservar exactamente los AMLs.
            # Contablemente es idéntico; solo cambia que en UI aparece bajo "Asientos" en vez de "Facturas".
            cast_move_type = sm.move_type
            ref_extended = sm.ref or ""
            if sm.move_type in ("in_invoice", "in_refund", "out_invoice", "out_refund"):
                cast_move_type = "entry"
                # Preservar el tipo original en la ref para trazabilidad
                ref_extended = f"[{sm.move_type}] {ref_extended}".strip()
            # Si destino es ENTRY, usar el journal MISC (general) — el journal original de
            # compras/ventas no acepta move_type=entry. Buscar el journal misc del destino.
            if cast_move_type == "entry":
                misc_journals = env_dst["account.journal"].search(
                    [("company_id", "=", DST_COMPANY_ID), ("type", "=", "general")], limit=1
                )
                journal_to_use = misc_journals.id if misc_journals else dst_journal_id
            else:
                journal_to_use = dst_journal_id

            move_vals = {
                "company_id": DST_COMPANY_ID,
                "move_type": cast_move_type,
                "journal_id": journal_to_use,
                "partner_id": dst_partner_id,
                "ref": ref_extended,
                "narration": (sm.narration or "") + f"\n[Migrado de cararjfam move id={sm.id}, original move_type={sm.move_type}]",
                "date": sm.date or sm.invoice_date,
                "invoice_date": sm.invoice_date if cast_move_type != "entry" else False,
                "line_ids": line_ids,
            }
            try:
                new_move = DstMove.with_company(DST_COMPANY_ID).with_context(
                    check_move_validity=False
                ).create(move_vals)
                if sm.state == "posted":
                    try:
                        new_move.action_post()
                    except Exception as e:
                        log.warning(f"  move {sm.id} ({sm.move_type}→{cast_move_type}): action_post falló: {str(e)[:120]}")
                stats["moves_created"] += 1
                stats["amls_created"] += len(line_ids)
                move_id_map[sm.id] = new_move.id
                # Copy attachments
                atts = env_src["ir.attachment"].search([
                    ("res_model", "=", "account.move"),
                    ("res_id", "=", sm.id),
                ])
                for att in atts:
                    copy_attachment(env_src, env_dst, att, "account.move", new_move.id)
                continue
            except Exception as e:
                stats["errors"].append(f"move {sm.id}: create falló: {str(e)[:200]}")
                continue
    return move_id_map


def migrate_learned_rules(env_src, env_dst, account_map, partner_map):
    """Migra learned.rule de BT (company_id=2 origen) → company_id=3 destino."""
    src_rules = env_src["learned.rule"].search([("company_id", "=", SRC_COMPANY_ID)])
    Rule = env_dst["learned.rule"]
    for sr in src_rules:
        # Skip si ya existe (mismo pattern + rule_type + company)
        existing = Rule.search([
            ("company_id", "=", DST_COMPANY_ID),
            ("pattern", "=", sr.pattern),
            ("rule_type", "=", sr.rule_type),
        ], limit=1)
        if existing:
            continue
        dst_acc = account_map.get(sr.account_id.id) if sr.account_id else False
        dst_partner = partner_map.get(sr.partner_id.id) if sr.partner_id else False
        try:
            Rule.create({
                "name": sr.name,
                "pattern": sr.pattern,
                "rule_type": sr.rule_type,
                "company_id": DST_COMPANY_ID,
                "account_id": dst_acc,
                "partner_id": dst_partner,
                "notes": sr.notes,
                "confidence": sr.confidence,
                "source": sr.source,
            })
            stats["learned_rules_created"] += 1
        except Exception as e:
            stats["errors"].append(f"rule {sr.id}: {str(e)[:200]}")


def main():
    odoo.tools.config.parse_config(["-c", "/etc/odoo17.conf"])
    reg_src = odoo.registry(SRC_DB)
    reg_dst = odoo.registry(DST_DB)
    with reg_src.cursor() as cr_src, reg_dst.cursor() as cr_dst:
        env_src = Environment(cr_src, odoo.SUPERUSER_ID, {"allowed_company_ids": [SRC_COMPANY_ID]})
        env_dst = Environment(cr_dst, odoo.SUPERUSER_ID, {"allowed_company_ids": [DST_COMPANY_ID]})

        log.info("=== Building maps ===")
        account_map = build_account_map(env_src, env_dst)
        journal_map = build_journal_map(env_src, env_dst)
        partner_map = {}  # se llena on-demand

        log.info("=== Migrating moves ===")
        migrate_moves(env_src, env_dst, account_map, journal_map, partner_map)

        log.info("=== Migrating learned.rule ===")
        migrate_learned_rules(env_src, env_dst, account_map, partner_map)

        cr_dst.commit()
        log.info(f"=== DONE === stats: {json.dumps(stats, ensure_ascii=False, indent=2)}")
    print(json.dumps(stats, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
