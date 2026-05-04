#!/usr/bin/env python3
"""
Daily email summary.

Queries Odoo for invoices created today, builds an HTML email with a table per
company, and sends via Gmail SMTP.
"""
import argparse
import logging
import os
import smtplib
import sys
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

ODOO_PATH = "/opt/odoo17/odoo"
ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"
ENV_FILE = "/etc/automation.env"
ODOO_BASE_URL = "https://erp.carajfam.com"

sys.path.insert(0, ODOO_PATH)
sys.path.insert(0, "/opt/automation")
import odoo  # noqa: E402
from odoo.api import Environment  # noqa: E402
import companies as comp  # noqa: E402
import bank_matcher  # noqa: E402

log = logging.getLogger("email_summary")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def _load_env() -> dict:
    out = {}
    for line in Path(ENV_FILE).read_text().splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def _fetch_today(env, target_date: date) -> dict:
    by_company = {}
    bank_by_company = {}
    near_by_company = {}

    for cfg in comp.COMPANIES:
        cid = cfg["odoo_company_id"]
        domain = [
            ("move_type", "=", "in_invoice"),
            ("company_id", "=", cid),
            ("create_date", ">=", f"{target_date} 00:00:00"),
            ("create_date", "<=", f"{target_date} 23:59:59"),
        ]
        moves = env["account.move"].search(domain, order="create_date asc")
        items = []
        for m in moves:
            items.append({
                "id": m.id,
                "supplier": m.partner_id.name or "",
                "vat": (m.partner_id.vat or "").replace("ES", ""),
                "ref": m.ref or "",
                "invoice_date": str(m.invoice_date) if m.invoice_date else "",
                "untaxed": round(m.amount_untaxed, 2),
                "tax": round(m.amount_tax, 2),
                "total": round(m.amount_total, 2),
                "state": m.state,
                "narration": (m.narration or "").strip(),
                "url": f"{ODOO_BASE_URL}/odoo/action-account.action_move_in_invoice_type/{m.id}",
            })
        by_company[cfg["name"]] = {
            "vat": cfg["vat"],
            "items": items,
            "count": len(items),
            "total": round(sum(i["total"] for i in items), 2),
            "tax": round(sum(i["tax"] for i in items), 2),
        }
        bank_by_company[cfg["name"]] = bank_matcher.propose_for_company(env, cid)
        near_by_company[cfg["name"]] = bank_matcher.find_near_matches_for_company(env, cid)

    return {"by_company": by_company, "bank_by_company": bank_by_company,
            "near_by_company": near_by_company}


def _render_html(target_date: date, data: dict) -> str:
    parts = [f"""<html><head><meta charset="utf-8"><style>
body {{ font-family: Arial, sans-serif; color:#222; }}
h1 {{ color:#1f4e79; margin-bottom:4px; }}
h2 {{ color:#2e75b6; border-bottom:1px solid #ddd; padding-bottom:4px; margin-top:24px;}}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background:#f0f0f0; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.note {{ color:#a64; font-size: 12px; max-width: 320px; }}
.totals {{ background:#fafafa; font-weight:bold; }}
a {{ color:#1f4e79; text-decoration: none; }}
.empty {{ color:#888; font-style:italic; }}
</style></head><body>
<h1>Resumen diario Odoo</h1>
<p>Fecha: <b>{target_date.isoformat()}</b></p>"""]

    grand_count = 0
    grand_total = 0.0
    for company_name, payload in data["by_company"].items():
        parts.append(f"<h2>{company_name} ({payload['vat']})</h2>")
        items = payload["items"]
        if not items:
            parts.append("<p class='empty'>Sin facturas procesadas hoy.</p>")
            continue
        parts.append("<table><thead><tr>"
                     "<th>#</th><th>Proveedor</th><th>CIF</th><th>Ref</th>"
                     "<th>Fecha</th><th class='num'>Base</th><th class='num'>IVA</th>"
                     "<th class='num'>Total</th><th>Estado</th><th>Notas / link</th>"
                     "</tr></thead><tbody>")
        for it in items:
            note = it["narration"].replace("\n", "<br/>") if it["narration"] else ""
            link = f"<a href='{it['url']}'>Abrir</a>"
            cell_notes = f"<div class='note'>{note}</div>{link}" if note else link
            parts.append(
                f"<tr><td>{it['id']}</td>"
                f"<td>{it['supplier']}</td><td>{it['vat']}</td>"
                f"<td>{it['ref']}</td><td>{it['invoice_date']}</td>"
                f"<td class='num'>{it['untaxed']:.2f}</td>"
                f"<td class='num'>{it['tax']:.2f}</td>"
                f"<td class='num'>{it['total']:.2f}</td>"
                f"<td>{it['state']}</td><td>{cell_notes}</td></tr>"
            )
        parts.append(
            f"<tr class='totals'><td colspan='5'>Total {payload['count']} facturas</td>"
            f"<td class='num'>—</td><td class='num'>{payload['tax']:.2f}</td>"
            f"<td class='num'>{payload['total']:.2f}</td><td colspan='2'></td></tr>"
        )
        parts.append("</tbody></table>")
        grand_count += payload["count"]
        grand_total += payload["total"]

    parts.append(
        f"<p style='margin-top:24px'><b>Gran total:</b> {grand_count} facturas · {grand_total:.2f} €</p>"
    )

    bank_data = data.get("bank_by_company") or {}
    has_any_lines = any(lines for lines in bank_data.values())
    if has_any_lines:
        parts.append("<h2>Conciliación bancaria — propuestas</h2>")
        for company_name, lines in bank_data.items():
            if not lines:
                continue
            parts.append(f"<h3 style='color:#5b9bd5'>{company_name}</h3>")
            parts.append("<table><thead><tr>"
                         "<th>Fecha</th><th class='num'>Importe</th><th>Concepto</th>"
                         "<th>Propuestas (con % confianza)</th></tr></thead><tbody>")
            for ln in lines:
                proposals_html = ""
                if ln["proposals"]:
                    rows = []
                    for p in ln["proposals"]:
                        score = p['score']
                        reasons = ", ".join(p.get('reasons', []))
                        if p.get("kind") == "rule":
                            account = p.get('account_code') or '?'
                            partner = p.get('partner') or ''
                            partner_html = f" → partner: {partner}" if partner else ""
                            rows.append(
                                f"<div style='background:#e8f4ea;padding:4px;border-radius:3px'>"
                                f"<b>{score}%</b> 📘 <b>Regla:</b> cuenta {account}{partner_html}"
                                f"<br/><span class='note'>{reasons}</span></div>"
                            )
                        else:
                            link = f"<a href='{ODOO_BASE_URL}{p['url']}'>{p['move_name']}</a>"
                            partner = p.get('partner', '')
                            ref = f" ref {p['ref']}" if p.get('ref') else ""
                            rows.append(
                                f"<div><b>{score}%</b> {link} — {partner} ({p['amount_total']:.2f}€{ref})"
                                f"<br/><span class='note'>{reasons}</span></div>"
                            )
                    proposals_html = "".join(rows)
                else:
                    proposals_html = "<span class='empty'>Sin candidatos automáticos — revisión manual.</span>"
                parts.append(
                    f"<tr><td>{ln['date']}</td>"
                    f"<td class='num'>{ln['amount']:.2f}</td>"
                    f"<td>{(ln['concept'] or '')[:80]}</td>"
                    f"<td>{proposals_html}</td></tr>"
                )
            parts.append("</tbody></table>")

    near_data = data.get("near_by_company") or {}
    has_near = any(rows for rows in near_data.values())
    if has_near:
        parts.append("<h2 style='color:#c00'>⚠ Conciliaciones descuadradas — esperan instrucciones</h2>")
        parts.append("<p style='font-size:12px; color:#666'>Bank lines que casi coinciden con apuntes abiertos pero con una diferencia. "
                     "Decide qué hacer: ajustar el apunte, crear asiento por la diferencia, o ignorar.</p>")
        for company_name, rows in near_data.items():
            if not rows:
                continue
            parts.append(f"<h3 style='color:#5b9bd5'>{company_name}</h3>")
            parts.append("<table><thead><tr>"
                         "<th>Fecha</th><th class='num'>Importe banco</th><th>Concepto</th>"
                         "<th>Apunte abierto candidato</th><th class='num'>Importe apunte</th>"
                         "<th class='num'>Diferencia</th></tr></thead><tbody>")
            for r in sorted(rows, key=lambda x: -abs(x["bank_amount"])):
                near = r["near"]
                aml_link = f"<a href='{ODOO_BASE_URL}/odoo/action-account.action_move_journal_line/{near['aml_move_id']}'>{near['aml_move_name']}</a>"
                aml_label = (near['aml_label'] or '').replace('<','&lt;').replace('>','&gt;')
                aml_partner = near['aml_partner'] or ''
                parts.append(
                    f"<tr><td>{r['bank_date']}</td>"
                    f"<td class='num'>{r['bank_amount']:.2f}</td>"
                    f"<td>{(r['bank_concept'] or '')[:60]}</td>"
                    f"<td>{near['aml_account']} {aml_link} {aml_partner}<br/><span class='note'>{aml_label}</span></td>"
                    f"<td class='num'>{near['aml_amount']:.2f}</td>"
                    f"<td class='num' style='color:#c00'><b>{near['diff']:+.2f}</b></td>"
                    f"</tr>"
                )
            parts.append("</tbody></table>")

    parts.append(
        f"<hr/><p style='font-size:11px; color:#888'>Enviado automáticamente desde erp.carajfam.com. "
        f"Las facturas están en BORRADOR; entra en Odoo para validarlas.</p></body></html>"
    )
    return "".join(parts)


def _send(env_cfg: dict, html: str, target_date: date, total_count: int, attachments: list[Path] | None = None):
    msg = MIMEMultipart("mixed")
    msg["Subject"] = f"[Odoo] Resumen {target_date.isoformat()} — {total_count} factura(s)"
    msg["From"] = env_cfg["SUMMARY_FROM"]
    msg["To"] = env_cfg["SUMMARY_TO"]
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)

    if attachments:
        from email.mime.base import MIMEBase
        from email import encoders
        for path in attachments:
            try:
                with open(path, "rb") as fh:
                    part = MIMEBase("application", "octet-stream")
                    part.set_payload(fh.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{path.name}"')
                msg.attach(part)
            except Exception:
                continue

    with smtplib.SMTP(env_cfg["SMTP_HOST"], int(env_cfg["SMTP_PORT"])) as s:
        s.starttls()
        s.login(env_cfg["SMTP_USER"], env_cfg["SMTP_PASSWORD"])
        s.send_message(msg)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--date", default=date.today().isoformat(), help="YYYY-MM-DD")
    p.add_argument("--dry-run", action="store_true", help="print HTML to stdout, don't send")
    args = p.parse_args()

    target = datetime.strptime(args.date, "%Y-%m-%d").date()
    env_cfg = _load_env()

    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    registry = odoo.registry(DB_NAME)
    with registry.cursor() as cr:
        env = Environment(cr, odoo.SUPERUSER_ID, {"tz": "Europe/Madrid"})
        data = _fetch_today(env, target)

    html = _render_html(target, data)
    total_count = sum(c["count"] for c in data["by_company"].values())

    if args.dry_run:
        print(html)
        log.info(f"dry-run: {total_count} invoices")
        return 0

    dudas_dir = Path("/tmp/dudas")
    attachments = sorted(dudas_dir.glob("dudas_*.csv")) if dudas_dir.exists() else []

    _send(env_cfg, html, target, total_count, attachments=attachments)
    log.info(f"email sent ({total_count} invoices, {len(attachments)} dudas attachments) to {env_cfg['SUMMARY_TO']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
