from odoo import models, fields, api
from datetime import date, timedelta


class WellhubSettlement(models.Model):
    _name = "wellhub.settlement"
    _description = "Liquidación mensual Wellhub conciliada con cargo bancario"
    _order = "period desc"
    _rec_name = "period"

    company_id = fields.Many2one(
        "res.company", required=True, default=lambda self: self.env.company,
    )
    period = fields.Char(
        string="Período", required=True, help='Mes "YYYY-MM"', index=True,
    )
    checkins_count = fields.Integer(
        string="Nº check-ins del mes",
        compute="_compute_checkins", store=True,
    )
    checkin_ids = fields.One2many("wellhub.checkin", "settlement_id",
                                  string="Check-ins")
    precio_unitario_aplicado = fields.Monetary(
        string="Precio aplicado (€)",
        help="Precio que se usó para calcular el importe esperado. Por defecto el de la config.",
        currency_field="currency_id",
    )
    importe_esperado = fields.Monetary(
        string="Importe esperado (€)",
        compute="_compute_esperado", store=True,
        currency_field="currency_id",
    )
    bank_line_id = fields.Many2one(
        "account.bank.statement.line",
        string="Cargo bancario asociado",
        help="Línea del extracto con la transferencia real de Wellhub para este período.",
    )
    importe_recibido = fields.Monetary(
        string="Importe recibido (€)",
        help="Importe real cobrado del extracto bancario.",
        currency_field="currency_id",
    )
    diferencia = fields.Monetary(
        string="Diferencia (€)",
        compute="_compute_diferencia", store=True,
        currency_field="currency_id",
    )
    diferencia_pct = fields.Float(
        string="Desviación %",
        compute="_compute_diferencia", store=True,
        digits=(5, 2),
    )
    estado = fields.Selection(
        [("pendiente", "Pendiente cargo banco"),
         ("ok", "Conciliado OK (±1%)"),
         ("aviso", "Desviación significativa"),
         ("error", "Sin coincidencia clara")],
        compute="_compute_estado", store=True,
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id,
    )
    notes = fields.Text()

    _sql_constraints = [
        ("period_unique",
         "unique(company_id, period)",
         "Ya existe una liquidación Wellhub para ese período/empresa."),
    ]

    @api.depends("checkin_ids")
    def _compute_checkins(self):
        for r in self:
            r.checkins_count = len(r.checkin_ids)

    @api.depends("checkins_count", "precio_unitario_aplicado")
    def _compute_esperado(self):
        for r in self:
            r.importe_esperado = (r.checkins_count or 0) * (r.precio_unitario_aplicado or 0)

    @api.depends("importe_esperado", "importe_recibido")
    def _compute_diferencia(self):
        for r in self:
            r.diferencia = (r.importe_recibido or 0) - (r.importe_esperado or 0)
            r.diferencia_pct = (
                (r.diferencia / r.importe_esperado * 100) if r.importe_esperado else 0
            )

    @api.depends("bank_line_id", "diferencia_pct")
    def _compute_estado(self):
        for r in self:
            if not r.bank_line_id:
                r.estado = "pendiente"
            elif abs(r.diferencia_pct) <= 1.0:
                r.estado = "ok"
            elif abs(r.diferencia_pct) <= 5.0:
                r.estado = "aviso"
            else:
                r.estado = "error"
