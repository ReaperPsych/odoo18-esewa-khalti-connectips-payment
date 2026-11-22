{
    'name': 'Nepal Wallets',
    'version': '18.0.1.0.0',
    'category': 'Accounting/Payment Providers',
    'sequence': 1,
    'summary': 'Unified Nepal payment gateway integration for eSewa (ePay v2), Khalti (by IME / KPG-2), and ConnectIPS (NCHL).',
    'description': """
Nepal Wallets — Unified Payment Gateway for Odoo 18
====================================================

Accept digital payments in Nepal directly from Odoo Website, eCommerce, Invoices, and Portal:

* **eSewa (ePay v2):** HMAC-SHA256 Base64 request & response signature verification + server-to-server Status Check API.
* **Khalti by IME (ePayment v2 / KPG-2):** Server-to-server Initiate & Lookup API with ``Authorization: Key`` authentication and paisa precision.
* **ConnectIPS (NCHL):** RSA-SHA256 (``SHA256withRSA``) signing via ``.pfx`` or ``.pem`` private key + HTTP Basic Auth ``validatetxn`` verification.

All three providers support Production, Official Sandbox/UAT, and Local Interactive Demo Simulation modes.
    """,
    'author': 'Bibeek Kumar Labh',
    'website': 'https://bibekklabh.com.np',
    'depends': ['payment'],
    'external_dependencies': {
        'python': ['cryptography', 'requests'],
    },
    'data': [
        'views/nepal_wallets_templates.xml',
        'views/payment_provider_views.xml',
        'data/payment_method_data.xml',
        'data/payment_provider_data.xml',
        'data/ir_cron_data.xml',
    ],
    'images': [
        'static/description/banner.png',
    ],
    'post_init_hook': 'post_init_hook',
    'uninstall_hook': 'uninstall_hook',
    'application': True,
    'installable': True,
    'license': 'LGPL-3',
}
