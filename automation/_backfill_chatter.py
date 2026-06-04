"""Backfill: cada ir_attachment PDF/imagen sobre account.move que NO tenga
mail.message asociado en el chatter del move, lo crea ahora vía message_post.
Idempotente."""
import sys
sys.path.insert(0, "/opt/odoo17/odoo")
import odoo
from odoo.api import Environment

odoo.tools.config.parse_config(["-c", "/etc/odoo17.conf"])
reg = odoo.registry("cararjfam")
with reg.cursor() as cr:
    env = Environment(cr, odoo.SUPERUSER_ID, {})
    cr.execute("""
        SELECT a.id, a.name, a.res_id
        FROM ir_attachment a
        WHERE a.res_model = 'account.move'
          AND a.mimetype IN ('application/pdf','image/jpeg','image/png','image/heif')
          AND NOT EXISTS (
            SELECT 1 FROM message_attachment_rel r
            JOIN mail_message m ON m.id = r.message_id
            WHERE r.attachment_id = a.id
              AND m.model = 'account.move'
              AND m.res_id = a.res_id
          )
        ORDER BY a.res_id
    """)
    rows = cr.fetchall()
    print(f"backfill: {len(rows)} adjuntos sin chatter link")
    ok = fail = 0
    for att_id, name, move_id in rows:
        try:
            m = env["account.move"].browse(move_id)
            if not m.exists():
                fail += 1
                continue
            m.with_context(mail_create_nosubscribe=True).message_post(
                body=f"Documento original adjunto (backfill): <b>{name}</b>",
                attachment_ids=[att_id],
                subtype_xmlid="mail.mt_note",
            )
            ok += 1
        except Exception as e:
            fail += 1
            if fail <= 3:
                print(f"fail att={att_id} move={move_id}: {str(e)[:120]}")
    cr.commit()
    print(f"OK {ok}  FAIL {fail}")
