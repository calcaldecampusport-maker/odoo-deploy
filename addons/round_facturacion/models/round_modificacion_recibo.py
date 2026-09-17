# -*- coding: utf-8 -*-
from odoo import api, fields, models


class RoundModificacionRecibo(models.Model):
    """Modificación temporal de precio sobre una suscripción.

    Se aplica a los recibos cuyo periodo (mes/trim/sem/año) cae dentro del
    rango fecha_desde — fecha_hasta. NoofitPro las crea y revoca.

    Ejemplos:
      - Cliente se va de viaje 2 sem en abril → descuento 50% solo abril.
      - Cargo extra puntual: clase suelta el 12 de mayo → +15 €.
      - Precio alternativo durante un trimestre por baja médica.
    """
    _name = 'round.modificacion.recibo'
    _description = 'Modificación temporal de recibo'
    _order = 'fecha_desde desc'

    subscription_id = fields.Many2one(
        'round.subscription',
        string='Suscripción',
        required=True,
        index=True,
        ondelete='cascade',
    )
    partner_id = fields.Many2one(
        related='subscription_id.partner_id',
        string='Cliente',
        store=True,
    )

    fecha_desde = fields.Date(string='Vigente desde', required=True)
    fecha_hasta = fields.Date(string='Vigente hasta', help="Vacío = indefinido.")

    tipo = fields.Selection(
        [('descuento',          'Descuento puntual'),
         ('cargo_extra',        'Cargo extra'),
         ('precio_alternativo', 'Precio alternativo (sustituye el base)')],
        string='Tipo',
        required=True,
        default='descuento',
    )
    valor = fields.Monetary(
        string='Importe',
        required=True,
        currency_field='currency_id',
        help="Importe positivo. El tipo determina si suma o resta del recibo.",
    )

    razon = fields.Char(string='Razón / Motivo')
    autorizado_por = fields.Many2one('res.users', string='Autorizado por')

    estado = fields.Selection(
        [('activa',    'Activa'),
         ('aplicada',  'Aplicada (recibo emitido)'),
         ('cancelada', 'Cancelada')],
        string='Estado',
        default='activa',
        required=True,
    )

    company_id = fields.Many2one(
        related='subscription_id.company_id',
        store=True,
    )
    currency_id = fields.Many2one(
        related='company_id.currency_id',
        readonly=True,
    )

    # Referencia NoofitPro
    id_noofit_modificacion = fields.Char(string='ID NoofitPro', index=True)

    def action_cancelar(self):
        for rec in self:
            rec.estado = 'cancelada'

    @api.model
    def aplicables_para(self, subscription_id, fecha_periodo):
        """Devuelve las modificaciones activas que aplican a un recibo
        cuyo periodo cae en `fecha_periodo` (datetime.date)."""
        return self.search([
            ('subscription_id', '=', subscription_id),
            ('estado', '=', 'activa'),
            ('fecha_desde', '<=', fecha_periodo),
            '|', ('fecha_hasta', '=', False),
                 ('fecha_hasta', '>=', fecha_periodo),
        ])
