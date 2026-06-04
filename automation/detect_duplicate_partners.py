#!/usr/bin/env python3
"""
Detector diario de partners duplicados (proveedores/clientes).

Tres detecciones:
1. Mismo VAT con varios ids (causa típica: SQL crudo, race condition, VAT extraído inconsistente)
2. Mismo nombre normalizado (uppercase + strip + sin SL/SA suffix) con varios ids
3. VAT idéntico salvo prefijo ES (ej. "B86002318" vs "ESB86002318")

Si encuentra duplicados → envía email de alerta al usuario.
Cron: 0 6 * * * (06:00 diario, después del backup 04:00, antes del pipeline 23:00).
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

import json
import logging
import smtplib
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, "/opt/odoo17/odoo")
sys.path.insert(0, _HERE)
import odoo
from odoo.api import Environment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("dup_check")

ODOO_CONF = "/etc/odoo17.conf"
DB_NAME = "cararjfam"


def find_duplicates():
    duplicates = []
    odoo.tools.config.parse_config(["-c", ODOO_CONF])
    reg = odoo.registry(DB_NAME)
    with reg.cursor() as cr:
        # 1. Mismo VAT
        cr.execute("""
            SELECT vat, ARRAY_AGG(id ORDER BY id) AS ids, ARRAY_AGG(name ORDER BY id) AS names
            FROM res_partner
            WHERE vat IS NOT NULL AND vat != '' AND active = true
              AND (supplier_rank > 0 OR customer_rank > 0)
            GROUP BY vat
            HAVING COUNT(*) > 1
        """)
        for vat, ids, names in cr.fetchall():
            duplicates.append({"reason": f"VAT idéntico {vat}", "ids": list(ids), "names": list(names), "vats": [vat] * len(ids)})

        # 2. Nombre normalizado
        cr.execute("""
            SELECT UPPER(TRIM(REGEXP_REPLACE(name, '\\s+', ' ', 'g'))) AS norm,
                   ARRAY_AGG(id ORDER BY id),
                   ARRAY_AGG(name ORDER BY id),
                   ARRAY_AGG(COALESCE(vat,'-') ORDER BY id)
            FROM res_partner
            WHERE active = true AND name IS NOT NULL
              AND (supplier_rank > 0 OR customer_rank > 0)
            GROUP BY norm
            HAVING COUNT(*) > 1
        """)
        for norm, ids, names, vats in cr.fetchall():
            # Skip si ya está cubierto por #1 (todos mismo VAT no-vacío)
            if len(set(v for v in vats if v != "-")) == 1 and "-" not in vats:
                continue
            duplicates.append({"reason": f"Nombre normalizado idéntico '{norm}'",
                               "ids": list(ids), "names": list(names), "vats": list(vats)})

        # 3. Mismo VAT ignorando prefijo ES (ej. B86002318 vs ESB86002318)
        cr.execute("""
            SELECT
              CASE WHEN vat LIKE 'ES%' THEN SUBSTRING(vat FROM 3) ELSE vat END AS bare,
              ARRAY_AGG(id ORDER BY id),
              ARRAY_AGG(name ORDER BY id),
              ARRAY_AGG(vat ORDER BY id)
            FROM res_partner
            WHERE vat IS NOT NULL AND vat != '' AND active = true
              AND (supplier_rank > 0 OR customer_rank > 0)
            GROUP BY bare
            HAVING COUNT(DISTINCT vat) > 1
        """)
        for bare, ids, names, vats in cr.fetchall():
            duplicates.append({"reason": f"VAT idéntico salvo prefijo ES ({bare})",
                               "ids": list(ids), "names": list(names), "vats": list(vats)})

    return duplicates


def send_alert(duplicates):
    try:
        import email_config
        smtp_host = getattr(email_config, "SMTP_HOST", "smtp.gmail.com")
        smtp_port = getattr(email_config, "SMTP_PORT", 587)
        smtp_user = email_config.SMTP_USER
        smtp_pass = email_config.SMTP_PASS
        email_to = getattr(email_config, "SUMMARY_TO", None) or getattr(email_config, "EMAIL_TO", smtp_user)
    except Exception as e:
        log.error(f"could not load email_config: {e}")
        return

    rows = "".join(
        f"<tr><td>{d['reason']}</td>"
        f"<td>{', '.join(str(i) for i in d['ids'])}</td>"
        f"<td>{'<br>'.join(d['names'])}</td>"
        f"<td>{'<br>'.join(d['vats'])}</td></tr>"
        for d in duplicates
    )
    body = f"""<html><body style="font-family:Arial">
    <h2>⚠ Partners duplicados detectados ({len(duplicates)})</h2>
    <p>El cron diario ha encontrado posibles duplicados en res_partner. Revisar y fusionar via
    Odoo (base.partner.merge.automatic.wizard).</p>
    <table border="1" cellpadding="6" style="border-collapse:collapse">
    <tr style="background:#fff2cc"><th>Motivo</th><th>IDs</th><th>Nombres</th><th>VATs</th></tr>
    {rows}
    </table>
    <p style="color:#666">Script: <code>/opt/automation/detect_duplicate_partners.py</code> · Cron 06:00 diario.</p>
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["From"] = smtp_user
    msg["To"] = email_to
    msg["Subject"] = f"⚠ {len(duplicates)} partner duplicado(s) en Odoo CARARJFAM"
    msg.attach(MIMEText(body, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_pass)
        smtp.send_message(msg)
    log.info(f"alert email sent to {email_to}")


def main():
    dups = find_duplicates()
    log.info(f"detected {len(dups)} duplicate group(s)")
    if dups:
        log.warning(f"DUPLICATES: {json.dumps(dups, ensure_ascii=False, default=str)}")
        try:
            send_alert(dups)
        except Exception as e:
            log.exception(f"alert email failed: {e}")
    print(json.dumps({"duplicates": len(dups), "details": dups}, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
