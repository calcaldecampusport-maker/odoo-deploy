# -*- coding: utf-8 -*-
from odoo import api, fields, models


class RoundLogWebhook(models.Model):
    """Log de cada evento webhook intercambiado con el MCP.

    Sirve para:
      - Trazabilidad legal/auditoría
      - Reintentos automáticos en caso de fallo
      - Debug en caso de divergencia entre NoofitPro y Odoo
    """
    _name = 'round.log.webhook'
    _description = 'Log de webhooks (entrada/salida)'
    _order = 'create_date desc'
    _rec_name = 'evento'

    direccion = fields.Selection(
        [('entrada', 'NoofitPro → Odoo (entrada)'),
         ('salida',  'Odoo → NoofitPro (salida)')],
        string='Dirección',
        required=True,
        index=True,
    )

    evento = fields.Char(
        string='Evento',
        required=True,
        index=True,
        help="Tipo de evento: cliente.alta, cuota.cambio, recibo.cobrado_sepa…",
    )

    payload = fields.Text(string='Payload (JSON)')
    response = fields.Text(string='Respuesta (JSON)')

    estado = fields.Selection(
        [('pendiente',  'Pendiente'),
         ('procesado',  'Procesado OK'),
         ('error',      'Error')],
        string='Estado',
        default='pendiente',
        required=True,
        index=True,
    )

    error_msg = fields.Text(string='Mensaje de error')
    intentos = fields.Integer(string='Intentos', default=0)

    # Identificación del evento (idempotencia)
    webhook_id = fields.Char(
        string='Webhook ID',
        index=True,
        help="UUID enviado en X-Webhook-Id, para evitar procesar el mismo evento 2 veces.",
    )

    procesado_at = fields.Datetime(string='Procesado el')

    # Referencias relacionadas (cuando aplique)
    partner_id = fields.Many2one('res.partner', string='Cliente afectado', index=True)
    subscription_id = fields.Many2one('round.subscription', string='Suscripción afectada')
    invoice_id = fields.Many2one('account.move', string='Factura afectada')

    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
    )

    _sql_constraints = [
        ('webhook_id_unique',
         'UNIQUE(webhook_id)',
         'Cada webhook_id debe ser único (idempotencia).'),
    ]

    def action_reintentar(self):
        """Reintentar procesar el webhook (a conectar con la lógica real
        del MCP en una iteración futura)."""
        for rec in self:
            rec.intentos += 1
            rec.estado = 'pendiente'
            rec.error_msg = False
