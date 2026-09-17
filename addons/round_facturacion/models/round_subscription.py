# -*- coding: utf-8 -*-
from odoo import api, fields, models


class RoundSubscription(models.Model):
    """Suscripción de un cliente a una cuota.

    Un cliente puede tener varias suscripciones activas (cuota base + extras).
    Cada suscripción genera N recibos según su periodicidad.

    NoofitPro crea, modifica y cancela suscripciones vía webhook al MCP.
    Odoo es responsable de generar los recibos y cobrarlos.
    """
    _name = 'round.subscription'
    _description = 'Suscripción de cliente'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'
    _rec_name = 'display_name'

    display_name = fields.Char(compute='_compute_display_name', store=True)

    partner_id = fields.Many2one(
        'res.partner',
        string='Cliente',
        required=True,
        index=True,
        domain=[('customer_rank', '>', 0)],
    )
    cuota_id = fields.Many2one(
        'round.cuota.catalogo',
        string='Cuota',
        required=True,
    )

    fecha_inicio = fields.Date(string='Fecha alta', required=True, default=fields.Date.today)
    fecha_fin    = fields.Date(string='Fecha baja', help="Fecha de cancelación (vacío = activa).")

    periodicidad = fields.Selection(
        [('mensual',    'Mensual'),
         ('trimestral', 'Trimestral'),
         ('semestral',  'Semestral'),
         ('anual',      'Anual')],
        string='Periodicidad',
        required=True,
        default='mensual',
    )

    forma_pago = fields.Selection(
        [('sepa',          'SEPA Direct Debit'),
         ('tarjeta_token', 'Tarjeta tokenizada'),
         ('enlace_pago',   'Enlace de pago'),
         ('efectivo',      'Efectivo / caja')],
        string='Forma de pago',
        required=True,
        default='sepa',
    )

    mandate_id = fields.Many2one(
        'account.banking.mandate',
        string='Mandato SEPA',
        help="Sólo si forma_pago = sepa.",
    )
    token_tarjeta = fields.Char(
        string='Token tarjeta',
        help="Token de la pasarela (Redsys/Paycomet) para cobros recurrentes.",
    )
    pasarela_id = fields.Many2one(
        'round.pasarela.config',
        string='Pasarela',
        help="Configuración de pasarela del trainer/centro asignado.",
    )

    descuentos_activos_ids = fields.Many2many(
        'round.descuento.catalogo',
        string='Descuentos activos',
    )

    estado = fields.Selection(
        [('borrador',   'Borrador'),
         ('activa',     'Activa'),
         ('suspendida', 'Suspendida (impago)'),
         ('cancelada',  'Cancelada')],
        string='Estado',
        default='borrador',
        required=True,
        tracking=True,
    )

    # Etiqueta analítica del trainer/centro al que pertenece el cliente
    trainer_analytic_id = fields.Many2one(
        'account.analytic.account',
        string='Trainer / Centro (analítica)',
    )

    # Importe efectivo (precio cuota - descuentos)
    importe_base = fields.Monetary(
        compute='_compute_importes',
        string='Importe base',
        currency_field='currency_id',
    )
    importe_descuentos = fields.Monetary(
        compute='_compute_importes',
        string='Total descuentos',
        currency_field='currency_id',
    )
    importe_efectivo = fields.Monetary(
        compute='_compute_importes',
        string='Importe efectivo',
        currency_field='currency_id',
    )

    # Recibos generados
    invoice_ids = fields.One2many(
        'account.move',
        'round_subscription_id',
        string='Facturas / Recibos',
    )
    invoice_count = fields.Integer(compute='_compute_invoice_count', string='Nº recibos')

    # Modificaciones temporales
    modificacion_ids = fields.One2many(
        'round.modificacion.recibo',
        'subscription_id',
        string='Modificaciones',
    )

    # Referencia externa NoofitPro
    id_noofit_subscription = fields.Char(string='ID NoofitPro', index=True)

    company_id = fields.Many2one(
        'res.company',
        string='Empresa',
        default=lambda self: self.env.company,
        required=True,
    )
    currency_id = fields.Many2one(
        'res.currency',
        related='company_id.currency_id',
        readonly=True,
    )

    def init(self):
        # B7 — una suscripción ACTIVA por (partner, cuota). Índice parcial.
        self.env.cr.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS uq_subscription_partner_cuota_activa
            ON round_subscription (partner_id, cuota_id)
            WHERE estado = 'activa'
        """)

    @api.depends('partner_id', 'cuota_id', 'estado')
    def _compute_display_name(self):
        for rec in self:
            partner = rec.partner_id.name or '?'
            cuota = rec.cuota_id.codigo or '?'
            rec.display_name = f"{partner} · {cuota} ({rec.estado})"

    @api.depends('cuota_id', 'periodicidad', 'descuentos_activos_ids')
    def _compute_importes(self):
        for rec in self:
            base = 0.0
            if rec.cuota_id and rec.periodicidad == 'mensual':
                base = rec.cuota_id.precio_mensual
            elif rec.cuota_id and rec.periodicidad == 'trimestral':
                base = rec.cuota_id.precio_trimestral
            elif rec.cuota_id and rec.periodicidad == 'semestral':
                base = rec.cuota_id.precio_semestral
            elif rec.cuota_id and rec.periodicidad == 'anual':
                base = rec.cuota_id.precio_anual
            descuento_total = 0.0
            for d in rec.descuentos_activos_ids:
                descuento_total += d.aplicar(base)
            rec.importe_base = base
            rec.importe_descuentos = descuento_total
            rec.importe_efectivo = max(0.0, base - descuento_total)

    @api.depends('invoice_ids')
    def _compute_invoice_count(self):
        for rec in self:
            rec.invoice_count = len(rec.invoice_ids)

    def action_activar(self):
        for rec in self:
            rec.estado = 'activa'

    def action_suspender(self):
        for rec in self:
            rec.estado = 'suspendida'

    def action_reactivar(self):
        for rec in self:
            if rec.estado == 'suspendida':
                rec.estado = 'activa'

    def action_cancelar(self):
        for rec in self:
            rec.estado = 'cancelada'
            rec.fecha_fin = fields.Date.today()

    def action_view_invoices(self):
        self.ensure_one()
        return {
            'name': 'Recibos',
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'view_mode': 'tree,form',
            'domain': [('round_subscription_id', '=', self.id)],
            'context': {'default_round_subscription_id': self.id},
        }


class AccountMoveSubscriptionRef(models.Model):
    """Añade referencia inversa a la suscripción Round desde la factura."""
    _inherit = 'account.move'

    round_subscription_id = fields.Many2one(
        'round.subscription',
        string='Suscripción Round',
        index=True,
    )
