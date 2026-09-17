# -*- coding: utf-8 -*-
{
    'name': 'Round Facturación',
    'version': '17.0.1.0.0',
    'category': 'Accounting',
    'summary': 'Módulo de facturación Round/Best Training integrado con NoofitPro',
    'description': """
Round Facturación
=================

Modelos custom para gestionar la facturación recurrente de Round/Best Training:

* Catálogo de cuotas (espejo de NoofitPro)
* Catálogo de descuentos
* Modificaciones de recibo con vigencia desde/hasta
* Suscripciones de cliente
* Configuración de pasarelas de pago (Redsys/Paycomet) por trainer
* Log de webhooks (entrada/salida) con MCP

Documento de arquitectura: docs/facturacion-odoo-arquitectura.md
""",
    'author': 'Round/Best Training',
    'website': 'https://round.carajfam.com',
    'license': 'LGPL-3',
    'depends': [
        'base',
        'mail',
        'account',
        'analytic',
        'account_payment_order',
        'account_banking_mandate',
        'account_banking_sepa_direct_debit',
    ],
    'data': [
        'security/round_facturacion_security.xml',
        'security/ir.model.access.csv',
        'views/round_cuota_catalogo_views.xml',
        'views/round_descuento_catalogo_views.xml',
        'views/round_modificacion_recibo_views.xml',
        'views/round_subscription_views.xml',
        'views/round_pasarela_config_views.xml',
        'views/round_log_webhook_views.xml',
        'views/res_partner_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}
