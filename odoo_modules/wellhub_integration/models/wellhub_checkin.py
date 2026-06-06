from odoo import models, fields, api


class WellhubCheckin(models.Model):
    _name = "wellhub.checkin"
    _description = "Check-in Wellhub recibido por webhook"
    _order = "checkin_at desc"
    _rec_name = "display_name"

    company_id = fields.Many2one(
        "res.company", string="Empresa", required=True,
        default=lambda self: self.env.company,
    )
    checkin_at = fields.Datetime(
        string="Fecha y hora", required=True, index=True,
        help="Momento en que Wellhub registró el check-in del usuario.",
    )
    user_unique_token = fields.Char(
        string="UUID usuario Wellhub", required=True, index=True,
        help="Identificador único del subscriber Wellhub (no es PII directa).",
    )
    location_id = fields.Char(string="Location ID", index=True)
    event_id = fields.Char(
        string="ID evento Wellhub", index=True,
        help="ID único del evento de check-in (para detectar duplicados si Wellhub reintenta el webhook).",
    )
    raw_payload = fields.Text(
        string="Payload JSON crudo", help="Payload completo recibido del webhook para auditoría/debug.",
    )
    signature_valid = fields.Boolean(
        string="Firma HMAC validada", default=True, readonly=True,
        help="False = webhook recibido con firma inválida (descartado pero registrado).",
    )

    # Relación con entrada puntual
    entrada_puntual_id = fields.Many2one(
        "entrada.puntual", string="Entrada puntual asociada",
        ondelete="set null",
    )

    # Conciliación
    settlement_id = fields.Many2one(
        "wellhub.settlement", string="Liquidación mensual",
        help="Liquidación mensual a la que pertenece este check-in.",
    )

    display_name = fields.Char(compute="_compute_display_name", store=True)

    _sql_constraints = [
        ("event_id_unique",
         "unique(company_id, event_id)",
         "Ya existe un check-in con ese event_id (idempotencia del webhook)."),
    ]

    @api.depends("checkin_at", "user_unique_token", "location_id")
    def _compute_display_name(self):
        for r in self:
            dt = r.checkin_at.strftime("%Y-%m-%d %H:%M") if r.checkin_at else "?"
            user_short = (r.user_unique_token or "")[:8]
            r.display_name = f"{dt} · {user_short}… · {r.location_id or '-'}"
