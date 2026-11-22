import base64
import json
import logging
import uuid

from werkzeug import urls as werkzeug_urls
from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import ValidationError
from odoo.http import request

from odoo.addons.nepal_wallets import const

_logger = logging.getLogger(__name__)

class NepalWalletsController(http.Controller):

    # Shared helpers
    @staticmethod
    def _has_duplicate_or_conflicting_params(param_names):
        args = request.httprequest.args
        form = request.httprequest.form
        for name in param_names:
            if len(args.getlist(name)) > 1 or len(form.getlist(name)) > 1:
                return True
            if name in args and name in form:
                return True
        return False

    @staticmethod
    def _get_simulation_tx(provider_code, field_name, identifier):
        if not const.is_valid_identifier(identifier):
            _logger.warning(
                "Nepal Wallets: Blocked simulation access for provider=%s (invalid/empty identifier).",
                provider_code,
            )
            raise Forbidden("Simulation endpoint is disabled for this provider.")

        domain = [
            ('provider_code', '=', provider_code),
            (field_name, '=', identifier),
        ]
        tx = request.env['payment.transaction'].sudo().search(domain)
        if (
            len(tx) != 1
            or not tx.provider_id._nw_is_simulation()
            or tx.state not in ('draft', 'pending')
        ):
            _logger.warning(
                "Nepal Wallets: Blocked simulation access for provider=%s (not in simulation mode, unknown tx, or non-open state).",
                provider_code,
            )
            raise Forbidden("Simulation endpoint is disabled for this provider.")
        return tx

    @staticmethod
    def _safe_handle_notification(provider_code, payload):
        try:
            with request.env.cr.savepoint():
                request.env['payment.transaction'].sudo()._handle_notification_data(
                    provider_code, payload
                )
        except (ValidationError, ValueError) as exc:
            _logger.warning(
                "Nepal Wallets (%s): Rejected invalid notification: %s",
                provider_code,
                exc,
            )

    # eSewa routes
    @http.route(
        '/payment/nepal_wallets/esewa/return',
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        save_session=False,
    )
    def esewa_return(self, **data):
        _logger.info("Nepal Wallets (eSewa): Return callback received with keys: %s", list(data.keys()))
        if self._has_duplicate_or_conflicting_params(('data', 'transaction_uuid', 'ref')):
            _logger.warning("Nepal Wallets (eSewa): Rejected duplicate/conflicting callback parameters.")
            return request.redirect('/payment/status')

        # eSewa v2 sends signed base64 JSON in ?data=
        raw_data = data.get('data')
        if raw_data:
            try:
                payload = request.env['payment.provider'].sudo()._nw_esewa_decode_callback_data(
                    raw_data
                )
            except ValidationError as exc:
                _logger.warning("Nepal Wallets (eSewa): Invalid callback data: %s", exc)
                return request.redirect('/payment/status')
            payload['_nw_callback_type'] = 'return'
            self._safe_handle_notification(const.CODE_ESEWA, payload)
        return request.redirect('/payment/status')

    @http.route(
        '/payment/nepal_wallets/esewa/cancel',
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        save_session=False,
    )
    def esewa_cancel(self, **data):
        _logger.info("Nepal Wallets (eSewa): Cancel/failure callback received with keys: %s", list(data.keys()))
        if self._has_duplicate_or_conflicting_params(('data', 'transaction_uuid', 'ref', 'status')):
            _logger.warning("Nepal Wallets (eSewa): Rejected duplicate/conflicting cancel parameters.")
            return request.redirect('/payment/status')

        if data.get('data'):
            try:
                payload = request.env['payment.provider'].sudo()._nw_esewa_decode_callback_data(
                    data['data']
                )
            except ValidationError as exc:
                _logger.warning("Nepal Wallets (eSewa): Invalid cancel data: %s", exc)
                return request.redirect('/payment/status')
            if (payload.get('status') or '').upper() == 'COMPLETE':
                _logger.warning("Nepal Wallets (eSewa): Rejected COMPLETE status on cancel endpoint.")
                return request.redirect('/payment/status')
            payload['status'] = payload.get('status') or 'CANCELED'
            payload['_nw_callback_type'] = 'cancel'
            self._safe_handle_notification(const.CODE_ESEWA, payload)
        else:
            txn_uuid = data.get('transaction_uuid') or data.get('ref')
            if txn_uuid is not None:
                if not const.is_valid_identifier(txn_uuid):
                    _logger.warning("Nepal Wallets (eSewa): Rejected invalid cancel identifier.")
                    return request.redirect('/payment/status')
                payload = {
                    'transaction_uuid': txn_uuid,
                    'status': 'CANCELED',
                    '_nw_callback_type': 'cancel',
                }
                self._safe_handle_notification(const.CODE_ESEWA, payload)
        return request.redirect('/payment/status')

    @http.route(
        '/payment/nepal_wallets/esewa/simulation',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def esewa_simulation(self, **post):
        txn_uuid = post.get('transaction_uuid') or ''
        tx = self._get_simulation_tx(const.CODE_ESEWA, 'nw_gateway_txn_id', txn_uuid)
        amount_str = tx.provider_id._nw_format_esewa_amount(tx.amount)

        values = {
            'wallet_name': 'eSewa',
            'wallet_subtitle': 'ePay v2 • HMAC-SHA256 Demo Sandbox',
            'brand_bg': 'linear-gradient(135deg, #4da435 0%, #60bb46 100%)',
            'brand_color': '#60bb46',
            'brand_letter': 'e',
            'company_name': tx.company_id.name,
            'partner_name': tx.partner_name or tx.partner_id.name or 'Customer',
            'txn_ref': txn_uuid,
            'amount_display': f"{tx.currency_id.name} {amount_str}",
            'process_url': '/payment/nepal_wallets/esewa/simulation/process',
            'hidden_fields': {
                'transaction_uuid': txn_uuid,
            },
            'demo_account_hint': 'Test eSewa ID: 9806800001 | MPIN: 1122 | OTP: 123456',
        }
        return request.render('nepal_wallets.simulation_gateway_page', values)

    @http.route(
        '/payment/nepal_wallets/esewa/simulation/process',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=True,
    )
    def esewa_simulation_process(self, **post):
        action = post.get('action', 'cancel')
        txn_uuid = post.get('transaction_uuid', '')
        tx = self._get_simulation_tx(const.CODE_ESEWA, 'nw_gateway_txn_id', txn_uuid)

        total_amount = tx.provider_id._nw_format_esewa_amount(tx.amount)
        product_code = tx.provider_id._nw_esewa_get_product_code()

        status_map = {'complete': 'COMPLETE', 'cancel': 'CANCELED', 'fail': 'FAILED'}
        status = status_map.get(action, 'CANCELED')
        txn_code = f"ESW{uuid.uuid4().hex[:7].upper()}" if status == 'COMPLETE' else ''
        signed_field_names = (
            'transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names'
        )
        payload = {
            'transaction_code': txn_code,
            'status': status,
            'total_amount': total_amount,
            'transaction_uuid': txn_uuid,
            'product_code': product_code,
            'signed_field_names': signed_field_names,
        }
        msg = ','.join(f"{f}={payload[f]}" for f in signed_field_names.split(','))
        payload['signature'] = tx.provider_id._nw_esewa_generate_signature(msg)
        encoded = base64.b64encode(json.dumps(payload).encode('utf-8')).decode('utf-8')
        query = werkzeug_urls.url_encode({'data': encoded})
        return request.redirect(f"/payment/nepal_wallets/esewa/return?{query}")

    # Khalti routes
    @http.route(
        '/payment/nepal_wallets/khalti/return',
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        save_session=False,
    )
    def khalti_return(self, **data):
        _logger.info("Nepal Wallets (Khalti): Return callback received with keys: %s", list(data.keys()))
        if self._has_duplicate_or_conflicting_params(('pidx', 'purchase_order_id', 'status', 'transaction_id')):
            _logger.warning("Nepal Wallets (Khalti): Rejected duplicate/conflicting callback parameters.")
            return request.redirect('/payment/status')

        pidx = data.get('pidx')
        order_id = data.get('purchase_order_id')
        if pidx is not None and pidx != '':
            if not const.is_valid_identifier(pidx):
                _logger.warning("Nepal Wallets (Khalti): Rejected invalid pidx.")
                return request.redirect('/payment/status')
            self._safe_handle_notification(const.CODE_KHALTI, data)
        elif order_id is not None and order_id != '':
            if not const.is_valid_identifier(order_id):
                _logger.warning("Nepal Wallets (Khalti): Rejected invalid purchase_order_id.")
                return request.redirect('/payment/status')
            self._safe_handle_notification(const.CODE_KHALTI, data)
        return request.redirect('/payment/status')

    @http.route(
        '/payment/nepal_wallets/khalti/simulation',
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
    )
    def khalti_simulation(self, **params):
        pidx = params.get('pidx', '')
        tx = self._get_simulation_tx(const.CODE_KHALTI, 'nw_khalti_pidx', pidx)
        amount_npr = f"{tx.provider_id._nw_quantize_amount(tx.amount):.2f}"
        amount_paisa = tx.provider_id._nw_amount_to_paisa(tx.amount)

        values = {
            'wallet_name': 'Khalti by IME',
            'wallet_subtitle': 'KPG-2 ePayment v2 • Demo Sandbox',
            'brand_bg': 'linear-gradient(135deg, #4b1d7d 0%, #5c2d91 100%)',
            'brand_color': '#5c2d91',
            'brand_letter': 'K',
            'company_name': tx.company_id.name,
            'partner_name': tx.partner_name or tx.partner_id.name or 'Customer',
            'txn_ref': pidx,
            'amount_display': f"{tx.currency_id.name} {amount_npr} ({amount_paisa} paisa)",
            'process_url': '/payment/nepal_wallets/khalti/simulation/process',
            'hidden_fields': {
                'pidx': pidx,
            },
            'demo_account_hint': 'Test Khalti ID: 9800000001 | MPIN: 1111 | OTP: 987654',
        }
        return request.render('nepal_wallets.simulation_gateway_page', values)

    @http.route(
        '/payment/nepal_wallets/khalti/simulation/process',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=True,
    )
    def khalti_simulation_process(self, **post):
        action = post.get('action', 'cancel')
        pidx = post.get('pidx', '')
        tx = self._get_simulation_tx(const.CODE_KHALTI, 'nw_khalti_pidx', pidx)
        amount_paisa = tx.provider_id._nw_amount_to_paisa(tx.amount)

        status_map = {
            'complete': 'Completed',
            'cancel': 'User canceled',
            'fail': 'Expired',
        }
        status = status_map.get(action, 'User canceled')
        txn_id = f"KHL{uuid.uuid4().hex[:10].upper()}" if status == 'Completed' else ''

        notification_data = {
            'pidx': pidx,
            'status': status,
            'total_amount': amount_paisa,
            'transaction_id': txn_id,
            'refunded': False,
        }
        self._safe_handle_notification(const.CODE_KHALTI, notification_data)
        return request.redirect('/payment/status')

    # ConnectIPS routes
    @http.route(
        '/payment/nepal_wallets/connectips/return',
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        save_session=False,
    )
    def connectips_return(self, **data):
        _logger.info("Nepal Wallets (ConnectIPS): Success return received with keys: %s", list(data.keys()))
        if self._has_duplicate_or_conflicting_params(('TXNID', 'referenceId', 'status')):
            _logger.warning("Nepal Wallets (ConnectIPS): Rejected duplicate/conflicting callback parameters.")
            return request.redirect('/payment/status')

        txn_id = data.get('TXNID') or data.get('referenceId')
        if txn_id is not None and txn_id != '':
            if not const.is_valid_identifier(txn_id):
                _logger.warning("Nepal Wallets (ConnectIPS): Rejected invalid TXNID.")
                return request.redirect('/payment/status')
            payload = dict(data, _nw_callback_type='success')
            self._safe_handle_notification(const.CODE_CONNECTIPS, payload)
        return request.redirect('/payment/status')

    @http.route(
        '/payment/nepal_wallets/connectips/failure',
        type='http',
        auth='public',
        methods=['GET', 'POST'],
        csrf=False,
        save_session=False,
    )
    def connectips_failure(self, **data):
        _logger.info("Nepal Wallets (ConnectIPS): Failure return received with keys: %s", list(data.keys()))
        if self._has_duplicate_or_conflicting_params(('TXNID', 'referenceId', 'status')):
            _logger.warning("Nepal Wallets (ConnectIPS): Rejected duplicate/conflicting failure parameters.")
            return request.redirect('/payment/status')

        txn_id = data.get('TXNID') or data.get('referenceId')
        if txn_id is not None and txn_id != '':
            if not const.is_valid_identifier(txn_id):
                _logger.warning("Nepal Wallets (ConnectIPS): Rejected invalid failure TXNID.")
                return request.redirect('/payment/status')
            payload = dict(data, _nw_callback_type='failure', status='FAILED')
            self._safe_handle_notification(const.CODE_CONNECTIPS, payload)
        return request.redirect('/payment/status')

    @http.route(
        '/payment/nepal_wallets/connectips/simulation',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=False,
    )
    def connectips_simulation(self, **post):
        txn_id = post.get('TXNID', '')
        tx = self._get_simulation_tx(const.CODE_CONNECTIPS, 'nw_gateway_txn_id', txn_id)
        txn_amt = tx.provider_id._nw_amount_to_paisa(tx.amount)
        amount_npr = f"{tx.provider_id._nw_quantize_amount(tx.amount):.2f}"

        values = {
            'wallet_name': 'ConnectIPS (NCHL)',
            'wallet_subtitle': 'Direct Bank Transfer • SHA256withRSA Demo Sandbox',
            'brand_bg': 'linear-gradient(135deg, #0f3e76 0%, #165ca8 100%)',
            'brand_color': '#165ca8',
            'brand_letter': 'C',
            'company_name': tx.company_id.name,
            'partner_name': tx.partner_name or tx.partner_id.name or 'Customer',
            'txn_ref': txn_id,
            'amount_display': f"{tx.currency_id.name} {amount_npr} ({txn_amt} paisa)",
            'process_url': '/payment/nepal_wallets/connectips/simulation/process',
            'hidden_fields': {
                'TXNID': txn_id,
            },
            'demo_account_hint': 'Local Demo Sandbox — Click Confirm & Pay to simulate a verified ConnectIPS bank transfer.',
        }
        return request.render('nepal_wallets.simulation_gateway_page', values)

    @http.route(
        '/payment/nepal_wallets/connectips/simulation/process',
        type='http',
        auth='public',
        methods=['POST'],
        csrf=True,
    )
    def connectips_simulation_process(self, **post):
        action = post.get('action', 'cancel')
        txn_id = post.get('TXNID', '')
        tx = self._get_simulation_tx(const.CODE_CONNECTIPS, 'nw_gateway_txn_id', txn_id)
        txn_amt = tx.provider_id._nw_amount_to_paisa(tx.amount)

        status_map = {
            'complete': ('SUCCESS', 'TRANSACTION SUCCESSFUL'),
            'cancel': ('CANCELED', 'TRANSACTION CANCELED BY USER'),
            'fail': ('FAILED', 'TRANSACTION FAILED'),
        }
        status, status_desc = status_map.get(action, ('CANCELED', 'CANCELED'))
        notification_data = {
            'TXNID': txn_id,
            'referenceId': txn_id,
            'txnAmt': txn_amt,
            'status': status,
            'statusDesc': status_desc,
        }
        self._safe_handle_notification(const.CODE_CONNECTIPS, notification_data)
        return request.redirect('/payment/status')
