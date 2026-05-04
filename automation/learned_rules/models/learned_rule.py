from odoo import fields, models, api


class LearnedRule(models.Model):
    _name = "learned.rule"
    _description = "Regla aprendida para clasificacion automatica"
    _order = "rule_type, sequence, name"

    name = fields.Char("Nombre", required=True, help="Nombre legible de la regla")
    pattern = fields.Char("Patron", required=True, help="Texto que debe aparecer en el concepto / descripcion (case-insensitive substring)")
    rule_type = fields.Selection(
        [("bank", "Banco — concepto en extracto"),
         ("invoice", "Factura — descripcion de linea")],
        string="Tipo", required=True, default="bank",
    )
    company_id = fields.Many2one(
        "res.company", string="Empresa",
        required=True, default=lambda self: self.env.company,
    )
    account_id = fields.Many2one("account.account", string="Cuenta destino")
    partner_id = fields.Many2one("res.partner", string="Partner sugerido")
    tax_id = fields.Many2one("account.tax", string="Impuesto")
    notes = fields.Text("Notas")
    confidence = fields.Float("Confianza", default=0.95, help="Score que se aplica si esta regla coincide (0..1)")
    source = fields.Selection(
        [("active", "Manual (CSV)"),
         ("passive", "Aprendida automaticamente"),
         ("system", "Sistema")],
        string="Origen", default="active", required=True,
    )
    times_applied = fields.Integer("Veces aplicada", default=0, readonly=True)
    last_applied = fields.Datetime("Ultima aplicacion", readonly=True)
    sequence = fields.Integer("Orden", default=10)
    active = fields.Boolean("Activa", default=True)

    @api.model
    def find_match(self, text, rule_type, company_id):
        """Return the best matching rule for a given text, or empty recordset."""
        if not text:
            return self.browse()
        rules = self.search([
            ("rule_type", "=", rule_type),
            ("company_id", "=", company_id),
            ("active", "=", True),
        ])
        text_lower = text.lower()
        candidates = []
        for r in rules:
            pat = (r.pattern or "").lower().strip()
            if not pat:
                continue
            if pat in text_lower:
                candidates.append((r, len(pat)))
        if not candidates:
            return self.browse()
        candidates.sort(key=lambda c: -c[1])
        return candidates[0][0]

    def mark_applied(self):
        for r in self:
            r.times_applied = (r.times_applied or 0) + 1
            r.last_applied = fields.Datetime.now()
