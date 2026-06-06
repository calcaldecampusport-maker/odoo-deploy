{
    "name": "Wellhub Integration",
    "version": "17.0.1.0.0",
    "summary": "Integración con Wellhub (Gympass) — check-ins, conciliación pago mensual, entradas puntuales",
    "description": """
Integración con Wellhub para gimnasios partner:
- Webhook /api/wellhub/checkin recibe check-ins en tiempo real
- Validación HMAC-SHA1 del header X-Gympass-Signature
- Genera entrada.puntual asociada (con fuente=wellhub)
- Conciliación mensual con cargo bancario de Gympass
- Menú Configuración → Wellhub y Económico → Entradas puntuales
""",
    "author": "CARARJFAM",
    "category": "Accounting",
    "depends": ["base", "account", "mail", "web"],
    "data": [
        "security/ir.model.access.csv",
        "data/ir_cron.xml",
        "views/menu.xml",
        "views/wellhub_config_view.xml",
        "views/wellhub_checkin_view.xml",
        "views/wellhub_settlement_view.xml",
        "views/entrada_puntual_view.xml",
    ],
    "installable": True,
    "application": False,
    "license": "LGPL-3",
}
