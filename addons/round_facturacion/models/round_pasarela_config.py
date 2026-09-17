# -*- coding: utf-8 -*-
from odoo import api, fields, models


class RoundPasarelaConfig(models.Model):
    """Configuración de pasarela de pago por trainer/centro.

    Cada cuenta analítica (= trainer/centro) puede tener sus propias
    credenciales Redsys/Paycomet. Cuando se cobra a un cliente, el sistema
    busca la pasarela del trainer al que está asignado y usa esas credenciales.

    Misma SL = mismo CIF, distintos comercios (TPV virtual) por trainer.
    """
    _name = 'round.pasarela.config'
    _description = 'Configuración pasarela de pago'
    _rec_name = 'nombre'

    nombre = fields.Char(string='Nombre', required=True)
    analytic_id = fields.Many2one(
        'account.analytic.account',
        string='Trainer / Centro',
        required=True,
        index=True,
        help="Cuenta analítica del trainer/centro al que pertenece esta pasarela.",
    )

    proveedor = fields.Selection(
        [('redsys',   'Redsys'),
         ('paycomet', 'Paycomet'),
         ('stub',     'Stub (simulado, sin cobro real)')],
        string='Proveedor',
        required=True,
        default='stub',
    )

    # Credenciales (en POC en claro; en producción cifrar con vault)
    fuc = fields.Char(
        string='FUC / Código comercio',
        help="FUC en Redsys, código de comercio en Paycomet.",
    )
    terminal = fields.Char(string='Terminal')
    secret_key = fields.Char(
        string='Clave secreta',
        help="Clave de firma (HMAC) para Redsys. En Paycomet, API key.",
    )
    merchant_url = fields.Char(
        string='URL del comercio',
        help="URL pública para webhooks de la pasarela.",
    )

    sandbox = fields.Boolean(
        string='Modo sandbox',
        default=True,
        help="True = entorno de pruebas, False = producción.",
    )

    activo = fields.Boolean(default=True)
    company_id = fields.Many2one(
        'res.company',
        default=lambda self: self.env.company,
        required=True,
    )

    _sql_constraints = [
        ('analytic_proveedor_unique',
         'UNIQUE(analytic_id, proveedor, company_id)',
         'Solo puede haber una configuración por trainer + proveedor + empresa.'),
    ]
