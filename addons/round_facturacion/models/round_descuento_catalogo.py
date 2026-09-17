# -*- coding: utf-8 -*-
from odoo import api, fields, models


class RoundDescuentoCatalogo(models.Model):
    """Espejo del catálogo de descuentos predefinidos en NoofitPro.

    Cada descuento tiene un código (DESC_FAMILIA, DESC_EMPLEADO…) y aplica
    un porcentaje o importe fijo. NoofitPro es la fuente de verdad.
    """
    _name = 'round.descuento.catalogo'
    _description = 'Catálogo de descuentos (espejo NoofitPro)'
    _rec_name = 'codigo'
    _order = 'codigo'

    codigo = fields.Char(
        string='Código',
        required=True,
        index=True,
        help="Código único del descuento en NoofitPro (ej. 'DESC_FAMILIA').",
    )
    descripcion = fields.Char(string='Descripción', required=True)

    tipo = fields.Selection(
        [('porcentaje', 'Porcentaje (%)'),
         ('importe',    'Importe fijo (€)')],
        string='Tipo',
        required=True,
        default='porcentaje',
    )
    valor = fields.Float(
        string='Valor',
        required=True,
        help="Si tipo es porcentaje: 10 = 10%. Si es importe: euros.",
    )

    company_id = fields.Many2one(
        'res.company',
        string='Empresa',
        default=lambda self: self.env.company,
        required=True,
    )
    activo = fields.Boolean(default=True)

    _sql_constraints = [
        ('codigo_company_unique',
         'UNIQUE(codigo, company_id)',
         'El código de descuento debe ser único por empresa.'),
    ]

    def name_get(self):
        return [(r.id, f"[{r.codigo}] {r.descripcion}") for r in self]

    def aplicar(self, base):
        """Aplica este descuento a un importe base. Devuelve el importe descontado."""
        self.ensure_one()
        if self.tipo == 'porcentaje':
            return base * (self.valor / 100.0)
        return self.valor
