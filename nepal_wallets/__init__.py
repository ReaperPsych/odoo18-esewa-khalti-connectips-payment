from . import controllers
from . import models

from odoo.addons.payment import reset_payment_provider, setup_provider

from . import const

def post_init_hook(env):
    for code in const.PROVIDER_CODES:
        setup_provider(env, code)

def uninstall_hook(env):
    for code in const.PROVIDER_CODES:
        reset_payment_provider(env, code)
