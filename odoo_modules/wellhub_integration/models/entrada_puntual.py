from odoo import models, fields, api


class EntradaPuntual(models.Model):
    _name = "entrada.puntual"
    _description = "Entrada puntual al gym (visita unitaria, sin cuota mensual)"
    _order = "fecha desc, id desc"
    _rec_name = "display_name"

    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company, index=True,
    )
    fecha = fields.Datetime(required=True, index=True, default=fields.Datetime.now)
    fecha_solo = fields.Date(string="Fecha", compute="_compute_fecha_solo", store=True, index=True)

    fuente = fields.Selection(
        [("wellhub", "Wellhub / Gympass"),
         ("walkin", "Walk-in (pago en recepción)"),
         ("tpv", "TPV / pase libre"),
         ("invitado", "Invitado / cortesía"),
         ("manual", "Alta manual")],
        required=True, default="manual", index=True,
    )

    partner_id = fields.Many2one(
        "res.partner", string="Cliente",
        help="Persona física que entra (opcional para fuentes anónimas como Wellhub UUID).",
    )
    usuario_uuid = fields.Char(
        string="UUID externo",
        help="ID externo del usuario (e.g. Wellhub unique_token, gympass id).",
        index=True,
    )

    precio = fields.Monetary(
        string="Precio (€)", currency_field="currency_id",
        help="Importe que ingresará el gym por esta entrada. 0 si es cortesía.",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )

    wellhub_checkin_id = fields.Many2one(
        "wellhub.checkin", string="Check-in Wellhub",
        ondelete="cascade",
    )
    settlement_id = fields.Many2one(
        related="wellhub_checkin_id.settlement_id", string="Liquidación", store=True,
    )

    notes = fields.Char(string="Notas")
    display_name = fields.Char(compute="_compute_display_name", store=True)

    @api.depends("fecha")
    def _compute_fecha_solo(self):
        for r in self:
            r.fecha_solo = r.fecha.date() if r.fecha else False

    @api.depends("fecha", "fuente", "partner_id", "usuario_uuid")
    def _compute_display_name(self):
        for r in self:
            dt = r.fecha.strftime("%Y-%m-%d %H:%M") if r.fecha else "?"
            who = r.partner_id.name if r.partner_id else (r.usuario_uuid or "")[:8]
            r.display_name = f"{dt} · {r.fuente} · {who}"
