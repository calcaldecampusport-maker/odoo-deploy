from odoo import models, fields, api


class WellhubConfig(models.Model):
    _name = "wellhub.config"
    _description = "Configuración Wellhub por empresa"
    _rec_name = "company_id"

    company_id = fields.Many2one(
        "res.company", string="Empresa", required=True,
        default=lambda self: self.env.company,
        help="Empresa gym-partner de Wellhub",
    )
    active = fields.Boolean(default=True)

    # === Credenciales / webhook ===
    webhook_secret = fields.Char(
        string="Secret HMAC (X-Gympass-Signature)",
        help="Clave secreta que Wellhub usa para firmar el webhook. Se obtiene del Partner Portal.",
    )
    client_id = fields.Char(string="OAuth client_id (opcional)")
    client_secret = fields.Char(string="OAuth client_secret (opcional)")
    location_id = fields.Char(
        string="Location ID en Wellhub",
        help="ID del local del gimnasio en el ecosistema Wellhub (puede haber varios por empresa).",
    )
    api_base_url = fields.Char(
        string="API base URL",
        default="https://api.wellhub.com",
    )

    # === Económico ===
    precio_unitario_default = fields.Monetary(
        string="Precio por check-in (€)",
        help="Importe que Wellhub paga por cada visita registrada. Se usa para calcular la liquidación esperada.",
        currency_field="currency_id",
    )
    currency_id = fields.Many2one(
        "res.currency", default=lambda self: self.env.company.currency_id
    )
    partner_wellhub_id = fields.Many2one(
        "res.partner", string="Partner Wellhub en BD",
        help="Partner asociado a las facturas/cobros de Wellhub (Gympass US LLC). "
             "Se usa para identificar el cargo bancario.",
    )
    bank_concept_pattern = fields.Char(
        string="Patrón concepto bancario",
        default="GYMPASS",
        help="Texto que aparece en payment_ref del extracto cuando llega la transferencia mensual de Wellhub.",
    )

    # === Notificaciones ===
    email_alerta = fields.Char(
        string="Email para alertas de discrepancia",
        help="Si la conciliación mensual detecta una desviación >5%, envía aviso aquí.",
    )

    # === Tracking ===
    last_checkin_at = fields.Datetime(string="Último check-in recibido", readonly=True)
    last_settlement_at = fields.Date(string="Última liquidación conciliada", readonly=True)
    total_checkins = fields.Integer(string="Total check-ins acumulados", readonly=True)

    _sql_constraints = [
        ("company_unique", "unique(company_id)",
         "Solo puede haber una configuración Wellhub por empresa."),
    ]

    def action_test_webhook(self):
        """Botón en form: muestra info del endpoint para configurar en Wellhub Partner Portal."""
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Configurar webhook en Wellhub",
                "message": (
                    f"URL: https://erp.carajfam.com/api/wellhub/checkin\n"
                    f"Método: POST\n"
                    f"Firma: HMAC-SHA1 con header X-Gympass-Signature\n"
                    f"Secret actual configurado: {'SI' if self.webhook_secret else 'NO — falta configurar arriba'}"
                ),
                "type": "info",
                "sticky": True,
            },
        }
