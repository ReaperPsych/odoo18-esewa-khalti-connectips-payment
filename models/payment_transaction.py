from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import timedelta
import logging
import re
import uuid

from werkzeug import urls

from odoo import _, fields, models
from odoo.exceptions import ValidationError

from odoo.addons.nepal_wallets import const

_logger = logging.getLogger(__name__)

class PaymentTransaction(models.Model):
    _inherit = 'payment.transaction'

    nw_gateway_txn_id = fields.Char(
        string='Nepal Gateway Transaction ID',
        help='Unique per-attempt transaction ID sent to eSewa (transaction_uuid), Khalti (purchase_order_id), or ConnectIPS (TXNID).',
        readonly=True,
        copy=False,
        index=True,
    )
    nw_khalti_pidx = fields.Char(
        string='Khalti PIDX',
        help='Immutable Khalti payment index (pidx) bound at initiation.',
        readonly=True,
        copy=False,
        index=True,
    )

    _sql_constraints = [
        (
            'nw_gateway_txn_id_uniq',
            'UNIQUE(nw_gateway_txn_id)',
            'Nepal Gateway Transaction ID must be unique.',
        ),
        (
            'nw_khalti_pidx_uniq',
            'UNIQUE(nw_khalti_pidx)',
            'Khalti PIDX must be unique.',
        ),
    ]

    def _nw_assert_npr_currency(self, provider_label):
        self.ensure_one()
        if not self.provider_id._nw_is_simulation() and self.currency_id.name != 'NPR':
            raise ValidationError(
                _("%s: Only NPR currency is supported in UAT and Live modes (got %s).",
                  provider_label, self.currency_id.name or 'UNKNOWN')
            )

    def _nw_lock_for_update(self):
        self.ensure_one()
        try:
            self.env.cr.execute(
                "SELECT id FROM payment_transaction WHERE id = %s FOR UPDATE NOWAIT",
                [self.id],
            )
        except Exception as exc:
            raise ValidationError(
                _("Nepal Wallets: Transaction is currently being processed by another request.")
            ) from exc
        self.invalidate_recordset(['state', 'provider_reference', 'nw_khalti_pidx'])

    def _nw_assert_not_terminal_for_resurrection(self, provider_label):
        self.ensure_one()
        if self.state in ('cancel', 'error'):
            _logger.warning(
                "Nepal Wallets (%s): Refusing to resurrect %s transaction %s.",
                provider_label,
                self.state,
                self.reference,
            )
            raise ValidationError(
                _("%s: Cannot reactivate a transaction already in '%s' state.", provider_label, self.state)
            )

    def _nw_get_or_create_gateway_txn_id(self, max_len=None):
        self.ensure_one()
        if self.nw_gateway_txn_id and (not max_len or len(self.nw_gateway_txn_id) <= max_len):
            return self.nw_gateway_txn_id

        clean_ref = re.sub(r'[^A-Za-z0-9-]', '', self.reference or '').strip('-') or 'TXN'
        suffix = uuid.uuid4().hex[:8].upper()
        if max_len:
            prefix_len = max(1, max_len - len(suffix) - 1)
            txn_id = f"{clean_ref[:prefix_len]}-{suffix}"[:max_len]
        else:
            txn_id = f"{clean_ref}-{suffix}"

        self.nw_gateway_txn_id = txn_id
        return txn_id

    def _get_specific_rendering_values(self, processing_values):
        res = super()._get_specific_rendering_values(processing_values)
        if self.provider_code == const.CODE_ESEWA:
            return self._nw_esewa_rendering_values()
        if self.provider_code == const.CODE_KHALTI:
            return self._nw_khalti_rendering_values()
        if self.provider_code == const.CODE_CONNECTIPS:
            return self._nw_connectips_rendering_values()
        return res

    # eSewa initiation
    def _nw_esewa_rendering_values(self):
        self.ensure_one()
        self._nw_assert_npr_currency('eSewa')
        dec_amount = self.provider_id._nw_quantize_amount(self.amount)
        if dec_amount <= Decimal('0'):
            raise ValidationError("eSewa: " + _("Transaction amount must be greater than zero."))

        base_url = self.provider_id.get_base_url()
        success_url = urls.url_join(base_url, '/payment/nepal_wallets/esewa/return')
        failure_url = urls.url_join(base_url, '/payment/nepal_wallets/esewa/cancel')

        amount_str = self.provider_id._nw_format_esewa_amount(self.amount)

        transaction_uuid = self._nw_get_or_create_gateway_txn_id()
        product_code = self.provider_id._nw_esewa_get_product_code()
        signed_field_names = const.ESEWA_REQUEST_SIGNED_FIELDS
        message = (
            f"total_amount={amount_str},"
            f"transaction_uuid={transaction_uuid},"
            f"product_code={product_code}"
        )
        signature = self.provider_id._nw_esewa_generate_signature(message)

        return {
            'api_url': self.provider_id._nw_esewa_get_form_url(),
            'amount': amount_str,
            'tax_amount': '0',
            'total_amount': amount_str,
            'transaction_uuid': transaction_uuid,
            'product_code': product_code,
            'product_service_charge': '0',
            'product_delivery_charge': '0',
            'success_url': success_url,
            'failure_url': failure_url,
            'signed_field_names': signed_field_names,
            'signature': signature,
        }

    # Khalti initiation
    def _nw_khalti_rendering_values(self):
        self.ensure_one()
        self._nw_assert_npr_currency('Khalti')
        amount_paisa = self.provider_id._nw_amount_to_paisa(self.amount)
        if amount_paisa < const.KHALTI_MIN_AMOUNT_PAISA:
            raise ValidationError(
                "Khalti: " + _(
                    "Minimum transaction amount for Khalti is NPR 10.00 (1000 paisa). Current amount: %s",
                    self.amount,
                )
            )

        base_url = self.provider_id.get_base_url()
        return_url = urls.url_join(base_url, '/payment/nepal_wallets/khalti/return')
        purchase_order_id = self._nw_get_or_create_gateway_txn_id()

        if self.provider_id._nw_is_simulation():
            sim_pidx = f"SIMPIDX-{uuid.uuid4().hex[:14].upper()}"
            self.nw_khalti_pidx = sim_pidx
            self.provider_reference = sim_pidx
            return {
                'api_url': urls.url_join(base_url, '/payment/nepal_wallets/khalti/simulation'),
                'query_params': {'pidx': sim_pidx},
            }

        payload = {
            'return_url': return_url,
            'website_url': base_url,
            'amount': amount_paisa,
            'purchase_order_id': purchase_order_id,
            'purchase_order_name': (self.reference or purchase_order_id)[:100],
            'customer_info': {
                'name': self.partner_name or self.partner_id.name or 'Customer',
                'email': self.partner_email or self.partner_id.email or 'customer@example.com',
                'phone': re.sub(r'[^0-9]', '', self.partner_phone or self.partner_id.phone or '9800000000')[-10:] or '9800000000',
            },
        }
        response_data = self.provider_id._nw_khalti_initiate_payment(payload)
        pidx = response_data['pidx']
        payment_url = response_data['payment_url']

        if not const.is_valid_identifier(pidx):
            raise ValidationError("Khalti: " + _("Invalid pidx received from Khalti Initiate API."))

        parsed = urls.url_parse(payment_url)
        if self.provider_id.state == 'enabled':
            allowed_netlocs = {'pay.khalti.com'}
        else:
            allowed_netlocs = {'pay.khalti.com', 'test-pay.khalti.com'}
        netloc = (parsed.netloc or '').lower()
        if parsed.scheme != 'https' or parsed.auth or parsed.port is not None or netloc not in allowed_netlocs:
            _logger.warning("Nepal Wallets (Khalti): Refused untrusted payment_url=%s", payment_url)
            raise ValidationError(
                "Khalti: " + _("Refusing redirect to untrusted payment URL.")
            )

        self.nw_khalti_pidx = pidx
        self.provider_reference = pidx
        clean_action = parsed.replace(query='').to_url()
        query_params = parsed.decode_query().to_dict()

        return {
            'api_url': clean_action,
            'query_params': query_params,
        }

    # ConnectIPS initiation
    def _nw_connectips_rendering_values(self):
        self.ensure_one()
        self._nw_assert_npr_currency('ConnectIPS')
        dec_amount = self.provider_id._nw_quantize_amount(self.amount)
        if dec_amount <= Decimal('0'):
            raise ValidationError("ConnectIPS: " + _("Transaction amount must be greater than zero."))

        txn_id = self._nw_get_or_create_gateway_txn_id(max_len=const.CONNECTIPS_TXNID_MAX_LEN)
        amount_paisa = self.provider_id._nw_amount_to_paisa(self.amount)
        txn_date = fields.Date.context_today(self).strftime('%d-%m-%Y')

        merchant_id = (self.provider_id.nw_connectips_merchant_id or '').strip() or (
            '550' if self.provider_id._nw_is_simulation() else ''
        )
        app_id = (self.provider_id.nw_connectips_app_id or '').strip() or (
            'MER-550-APP-1' if self.provider_id._nw_is_simulation() else ''
        )
        if self.provider_id.state == 'enabled' and (
            merchant_id == '550' or app_id == 'MER-550-APP-1'
        ):
            raise ValidationError(
                "ConnectIPS: " + _("Live mode cannot use demo Merchant ID (550) or demo App ID (MER-550-APP-1).")
            )

        raw_app_name = self.provider_id.nw_connectips_app_name or self.company_id.name or 'Odoo Store'
        app_name = re.sub(r'[^A-Za-z0-9 _.-]', '', raw_app_name).strip()[:const.CONNECTIPS_APPNAME_MAX_LEN] or 'Odoo Store'

        if not merchant_id or not app_id:
            raise ValidationError(
                "ConnectIPS: " + _("Merchant ID and App ID must be configured before initiating payment.")
            )

        clean_ref = (
            re.sub(r'[^A-Za-z0-9-_]', '', self.reference or '').strip('-_')[:const.CONNECTIPS_REFERENCEID_MAX_LEN]
            or txn_id[:const.CONNECTIPS_REFERENCEID_MAX_LEN]
        )
        remarks = re.sub(r'[^A-Za-z0-9 _.-]', '', f"Order {clean_ref}")[:const.CONNECTIPS_REMARKS_MAX_LEN]
        particulars = re.sub(
            r'[^A-Za-z0-9 _.-]', '', f"Payment for {clean_ref}"
        )[:const.CONNECTIPS_PARTICULARS_MAX_LEN]

        params = {
            'MERCHANTID': str(merchant_id),
            'APPID': str(app_id),
            'APPNAME': app_name,
            'TXNID': txn_id,
            'TXNDATE': txn_date,
            'TXNCRNCY': self.currency_id.name or 'NPR',
            'TXNAMT': str(amount_paisa),
            'REFERENCEID': clean_ref,
            'REMARKS': remarks,
            'PARTICULARS': particulars,
        }

        message = self.provider_id._nw_connectips_build_initiation_message(params)
        if (
            self.provider_id._nw_is_simulation()
            and not self.provider_id.nw_connectips_pfx_file
            and not self.provider_id.nw_connectips_private_key_pem
        ):
            token = 'SIMULATION_UNSIGNED_DEMO_TOKEN'
        else:
            token = self.provider_id._nw_connectips_sign(message)

        return {
            'api_url': self.provider_id._nw_connectips_get_gateway_url(),
            'MERCHANTID': params['MERCHANTID'],
            'APPID': params['APPID'],
            'APPNAME': params['APPNAME'],
            'TXNID': params['TXNID'],
            'TXNDATE': params['TXNDATE'],
            'TXNCRNCY': params['TXNCRNCY'],
            'TXNAMT': params['TXNAMT'],
            'REFERENCEID': params['REFERENCEID'],
            'REMARKS': params['REMARKS'],
            'PARTICULARS': params['PARTICULARS'],
            'TOKEN': token,
        }

    def _get_tx_from_notification_data(self, provider_code, notification_data):
        tx = super()._get_tx_from_notification_data(provider_code, notification_data)
        if provider_code not in const.PROVIDER_CODES or len(tx) == 1:
            return tx

        if provider_code == const.CODE_ESEWA:
            txn_uuid = notification_data.get('transaction_uuid') or notification_data.get('ref')
            if not const.is_valid_identifier(txn_uuid):
                raise ValidationError("eSewa: " + _("Missing or invalid transaction_uuid in notification data."))
            tx = self.search([
                ('provider_code', '=', const.CODE_ESEWA),
                ('nw_gateway_txn_id', '=', txn_uuid),
            ])

        elif provider_code == const.CODE_KHALTI:
            pidx = notification_data.get('pidx')
            order_id = notification_data.get('purchase_order_id')
            if pidx is not None and pidx != '':
                if not const.is_valid_identifier(pidx):
                    raise ValidationError("Khalti: " + _("Invalid pidx in notification data."))
                tx = self.search([
                    ('provider_code', '=', const.CODE_KHALTI),
                    ('nw_khalti_pidx', '=', pidx),
                ])
            elif order_id is not None and order_id != '':
                if not const.is_valid_identifier(order_id):
                    raise ValidationError("Khalti: " + _("Invalid purchase_order_id in notification data."))
                tx = self.search([
                    ('provider_code', '=', const.CODE_KHALTI),
                    ('nw_gateway_txn_id', '=', order_id),
                ])
            else:
                raise ValidationError("Khalti: " + _("Missing pidx in notification data."))

        elif provider_code == const.CODE_CONNECTIPS:
            txn_id = notification_data.get('TXNID') or notification_data.get('referenceId')
            if not const.is_valid_identifier(txn_id):
                raise ValidationError("ConnectIPS: " + _("Missing or invalid TXNID in notification data."))
            tx = self.search([
                ('provider_code', '=', const.CODE_CONNECTIPS),
                ('nw_gateway_txn_id', '=', txn_id),
            ])

        if len(tx) > 1:
            raise ValidationError(
                _("Nepal Wallets (%s): Multiple transactions matched the notification identifier.", provider_code)
            )
        if not tx:
            raise ValidationError(
                _("Nepal Wallets (%s): No matching transaction found.", provider_code)
            )
        return tx

    def _process_notification_data(self, notification_data):
        super()._process_notification_data(notification_data)
        if self.provider_code == const.CODE_ESEWA:
            self._nw_process_esewa_notification(notification_data)
        elif self.provider_code == const.CODE_KHALTI:
            self._nw_process_khalti_notification(notification_data)
        elif self.provider_code == const.CODE_CONNECTIPS:
            self._nw_process_connectips_notification(notification_data)

    def _nw_assert_amount_matches(self, received_npr, provider_label):
        if received_npr in (None, '') or isinstance(received_npr, bool):
            raise ValidationError(
                _("%s: Missing or invalid transaction amount in gateway confirmation.", provider_label)
            )
        try:
            expected = self.provider_id._nw_quantize_amount(self.amount)
            actual = Decimal(str(received_npr).replace(',', '').strip()).quantize(
                Decimal('0.01'), rounding=ROUND_HALF_UP
            )
        except (InvalidOperation, ValueError) as exc:
            raise ValidationError(
                _("%s: Invalid amount format received from gateway.", provider_label)
            ) from exc
        if expected != actual:
            raise ValidationError(
                _("%s: Amount mismatch! Expected %s, received %s.", provider_label, expected, actual)
            )

    def _nw_assert_paisa_matches(self, received_paisa, provider_label):
        if received_paisa in (None, '') or isinstance(received_paisa, bool):
            raise ValidationError(
                _("%s: Missing or invalid paisa amount in gateway confirmation.", provider_label)
            )
        if isinstance(received_paisa, int):
            actual_paisa = received_paisa
        elif isinstance(received_paisa, str) and received_paisa.strip().isdigit():
            actual_paisa = int(received_paisa.strip())
        else:
            raise ValidationError(
                _("%s: Invalid paisa amount received from gateway.", provider_label)
            )
        expected_paisa = self.provider_id._nw_amount_to_paisa(self.amount)
        if actual_paisa != expected_paisa:
            raise ValidationError(
                _(
                    "%s: Amount mismatch! Expected %s paisa, received %s paisa.",
                    provider_label,
                    expected_paisa,
                    actual_paisa,
                )
            )

    # eSewa callback processing
    def _nw_process_esewa_notification(self, notification_data):
        self.ensure_one()
        self._nw_lock_for_update()
        self._nw_assert_npr_currency('eSewa')

        status = (notification_data.get('status') or '').upper()
        callback_type = notification_data.get('_nw_callback_type') or 'return'

        if self.state == 'done':
            if status in ('FULL_REFUND', 'PARTIAL_REFUND'):
                _logger.warning(
                    "Nepal Wallets (eSewa): Refund status '%s' received for already-completed transaction %s; manual reconciliation required.",
                    status,
                    self.reference,
                )
            else:
                _logger.info(
                    "Nepal Wallets (eSewa): Ignoring notification for already-completed transaction %s.",
                    self.reference,
                )
            return

        txn_uuid = self.nw_gateway_txn_id
        amount_str = self.provider_id._nw_format_esewa_amount(self.amount)
        is_simulation = self.provider_id._nw_is_simulation()

        if callback_type == 'cancel':
            if status == 'COMPLETE':
                raise ValidationError("eSewa: " + _("Cannot complete a transaction via the cancel endpoint."))
            if not is_simulation:
                status_resp = self.provider_id._nw_esewa_check_status(txn_uuid, amount_str)
                verified_status = (status_resp.get('status') or '').upper()
                if verified_status == 'COMPLETE':
                    _logger.warning(
                        "Nepal Wallets (eSewa): Cancel callback received for %s, but Status API reports COMPLETE; refusing to cancel.",
                        self.reference,
                    )
                    return
                if verified_status in ('PENDING', 'AMBIGUOUS'):
                    _logger.info(
                        "Nepal Wallets (eSewa): Cancel callback received for %s, but Status API reports %s; keeping current state.",
                        self.reference,
                        verified_status,
                    )
                    return
                if verified_status in ('CANCELED', 'CANCELLED', 'USER_CANCELED', 'NOT_FOUND', 'FAILED'):
                    if self.state == 'draft':
                        self._set_canceled(state_message=_("eSewa payment was canceled or did not complete (%s).", verified_status))
                    return
                if verified_status in ('FULL_REFUND', 'PARTIAL_REFUND'):
                    _logger.warning(
                        "Nepal Wallets (eSewa): Status API returned %s on cancel for %s.",
                        verified_status,
                        self.reference,
                    )
                    if self.state == 'draft':
                        self._set_canceled(state_message=_("eSewa transaction status: %s.", verified_status))
                    return
                return
            else:
                if self.state == 'draft':
                    self._set_canceled(state_message=_("eSewa payment was canceled by the customer."))
                return

        self.provider_id._nw_esewa_verify_signature(notification_data)

        raw_amount = notification_data.get('total_amount')
        if status == 'COMPLETE' or raw_amount not in (None, ''):
            self._nw_assert_amount_matches(raw_amount, 'eSewa')

        verified_ref_id = notification_data.get('transaction_code') or notification_data.get('ref_id')

        if status == 'COMPLETE' and not is_simulation:
            status_resp = self.provider_id._nw_esewa_check_status(txn_uuid, amount_str)
            verified_status = (status_resp.get('status') or '').upper()
            if verified_status != 'COMPLETE':
                status = verified_status or 'FAILED'
            else:
                if str(status_resp.get('transaction_uuid') or '').strip() != str(txn_uuid):
                    raise ValidationError("eSewa: " + _("Status API transaction_uuid mismatch."))
                if str(status_resp.get('product_code') or '').strip() != self.provider_id._nw_esewa_get_product_code():
                    raise ValidationError("eSewa: " + _("Status API product_code mismatch."))
                self._nw_assert_amount_matches(status_resp.get('total_amount'), 'eSewa')
                if status_resp.get('ref_id'):
                    verified_ref_id = status_resp['ref_id']

        if status == 'COMPLETE':
            self._nw_assert_not_terminal_for_resurrection('eSewa')
            if verified_ref_id:
                self.provider_reference = verified_ref_id
            self._set_done()
        elif status in ('PENDING', 'AMBIGUOUS'):
            self._nw_assert_not_terminal_for_resurrection('eSewa')
            if verified_ref_id:
                self.provider_reference = verified_ref_id
            self._set_pending()
        elif status in ('FULL_REFUND', 'PARTIAL_REFUND'):
            _logger.warning(
                "Nepal Wallets (eSewa): Transaction %s received refund status '%s'; manual reconciliation may be required.",
                self.reference,
                status,
            )
            if verified_ref_id:
                self.provider_reference = verified_ref_id
            self._set_canceled(state_message=_("eSewa transaction status: %s. Manual reconciliation may be required.", status))
        elif status in ('CANCELED', 'CANCELLED', 'USER_CANCELED'):
            self._set_canceled(state_message=_("eSewa payment was canceled by the customer."))
        else:
            self._set_error("eSewa: " + _("Payment failed with status: %s", status or 'UNKNOWN'))

    # Khalti callback processing
    def _nw_process_khalti_notification(self, notification_data):
        self.ensure_one()
        self._nw_lock_for_update()
        self._nw_assert_npr_currency('Khalti')

        if not self.nw_khalti_pidx:
            raise ValidationError(
                "Khalti: " + _("Transaction has no bound Khalti pidx; refusing unbound callback.")
            )
        incoming_pidx = notification_data.get('pidx')
        if incoming_pidx is not None and incoming_pidx != '' and str(incoming_pidx) != str(self.nw_khalti_pidx):
            raise ValidationError(
                "Khalti: " + _("Callback pidx does not match the initiated transaction pidx.")
            )
        pidx = self.nw_khalti_pidx

        if not self.provider_id._nw_is_simulation():
            lookup = self.provider_id._nw_khalti_lookup_payment(pidx)
            if str(lookup.get('pidx') or '').strip() != str(pidx):
                raise ValidationError("Khalti: " + _("Lookup API returned mismatched or missing pidx."))
            status = lookup.get('status') or ''
            total_amount_paisa = lookup.get('total_amount')
            refunded = bool(lookup.get('refunded'))
            txn_id = lookup.get('transaction_id')
        else:
            status = notification_data.get('status') or ''
            total_amount_paisa = notification_data.get('total_amount') or notification_data.get('amount')
            refunded = bool(notification_data.get('refunded', False))
            txn_id = notification_data.get('transaction_id')

        if self.state == 'done':
            if status in ('Refunded', 'Partially refunded') or refunded:
                _logger.warning(
                    "Nepal Wallets (Khalti): Refund status '%s' (refunded=%s) received for already-completed transaction %s; manual reconciliation required.",
                    status,
                    refunded,
                    self.reference,
                )
            else:
                _logger.info(
                    "Nepal Wallets (Khalti): Ignoring notification for already-completed transaction %s.",
                    self.reference,
                )
            return

        if txn_id:
            if not const.is_valid_identifier(str(txn_id)):
                raise ValidationError("Khalti: " + _("Invalid transaction_id received from Khalti."))
            dup_count = self.env['payment.transaction'].sudo().search_count([
                ('provider_code', '=', const.CODE_KHALTI),
                ('provider_reference', '=', str(txn_id)),
                ('id', '!=', self.id),
            ])
            if dup_count:
                raise ValidationError(
                    "Khalti: " + _("Duplicate Khalti transaction_id already recorded on another transaction.")
                )

        if status == 'Completed':
            if refunded:
                _logger.warning(
                    "Nepal Wallets (Khalti): Transaction %s marked Completed with refunded=True.",
                    self.reference,
                )
                self._set_canceled(state_message=_("Khalti transaction was refunded."))
                return
            self._nw_assert_not_terminal_for_resurrection('Khalti')
            self._nw_assert_paisa_matches(total_amount_paisa, 'Khalti')
            if txn_id:
                self.provider_reference = str(txn_id)
            self._set_done()
        elif status in ('Pending', 'Initiated'):
            self._nw_assert_not_terminal_for_resurrection('Khalti')
            if txn_id:
                self.provider_reference = str(txn_id)
            self._set_pending()
        elif status == 'Partially refunded':
            _logger.warning(
                "Nepal Wallets (Khalti): Transaction %s is Partially refunded; manual reconciliation required.",
                self.reference,
            )
            if txn_id:
                self.provider_reference = str(txn_id)
            self._set_canceled(state_message=_("Khalti transaction was partially refunded; manual reconciliation required."))
        elif status in ('User canceled', 'Canceled', 'Expired', 'Refunded'):
            if txn_id:
                self.provider_reference = str(txn_id)
            self._set_canceled(state_message=_("Khalti payment ended with status: %s", status))
        else:
            self._set_error("Khalti: " + _("Payment failed with status: %s", status or 'UNKNOWN'))

    # ConnectIPS callback processing
    def _nw_process_connectips_notification(self, notification_data):
        self.ensure_one()
        self._nw_lock_for_update()
        self._nw_assert_npr_currency('ConnectIPS')

        if self.state == 'done':
            _logger.info(
                "Nepal Wallets (ConnectIPS): Ignoring notification for already-completed transaction %s.",
                self.reference,
            )
            return

        txn_id = self.nw_gateway_txn_id
        if not txn_id:
            raise ValidationError("ConnectIPS: " + _("Transaction has no gateway TXNID."))
        expected_paisa = self.provider_id._nw_amount_to_paisa(self.amount)

        if not self.provider_id._nw_is_simulation():
            validation = self.provider_id._nw_connectips_validate_txn(txn_id, expected_paisa)
            expected_merchant_id = (self.provider_id.nw_connectips_merchant_id or '').strip()
            expected_app_id = (self.provider_id.nw_connectips_app_id or '').strip()

            if str(validation.get('referenceId') or '').strip() != str(txn_id):
                raise ValidationError("ConnectIPS: " + _("Validation API referenceId mismatch."))
            if validation.get('merchantId') is None or str(validation.get('merchantId')).strip() != expected_merchant_id:
                raise ValidationError("ConnectIPS: " + _("Validation API merchantId mismatch."))
            if str(validation.get('appId') or '').strip() != expected_app_id:
                raise ValidationError("ConnectIPS: " + _("Validation API appId mismatch."))
            # ConnectIPS validatetxn may omit txnCrncy; NPR is already enforced above
            returned_crncy = str(validation.get('txnCrncy') or '').strip().upper()
            if returned_crncy and returned_crncy != 'NPR':
                raise ValidationError("ConnectIPS: " + _("Validation API currency mismatch (expected NPR)."))

            status = (validation.get('status') or '').upper()
            status_desc = validation.get('statusDesc') or ''
            validated_amt = validation.get('txnAmt')
        else:
            status = (notification_data.get('status') or '').upper()
            status_desc = notification_data.get('statusDesc') or ''
            validated_amt = notification_data.get('txnAmt')

        if status == 'SUCCESS':
            self._nw_assert_not_terminal_for_resurrection('ConnectIPS')
            self._nw_assert_paisa_matches(validated_amt, 'ConnectIPS')
            self.provider_reference = txn_id
            self._set_done()
        elif status in ('PENDING', 'IN_PROGRESS'):
            self._nw_assert_not_terminal_for_resurrection('ConnectIPS')
            self.provider_reference = txn_id
            self._set_pending()
        elif status in ('CANCELED', 'CANCELLED', 'USER_CANCELED'):
            self._set_canceled(state_message=_("ConnectIPS payment was canceled by the customer."))
        else:
            self._set_error(
                "ConnectIPS: " + _("Payment failed (%s): %s", status or 'FAILED', status_desc)
            )

    # Scheduled reconciliation cron (abandoned browser recovery)
    def _nw_cron_reconcile_pending_transactions(self, lookback_hours=24, batch_limit=50):
        cutoff = fields.Datetime.now() - timedelta(hours=lookback_hours)
        candidates = self.sudo().search([
            ('provider_code', 'in', list(const.PROVIDER_CODES)),
            ('state', 'in', ('draft', 'pending')),
            ('create_date', '>=', cutoff),
            '|',
            ('nw_gateway_txn_id', '!=', False),
            ('nw_khalti_pidx', '!=', False),
        ], order='create_date asc', limit=batch_limit)

        reconciled = 0
        for tx in candidates:
            if tx.provider_id._nw_is_simulation():
                continue
            try:
                with self.env.cr.savepoint():
                    tx._nw_reconcile_single_transaction()
                    if tx.state == 'done':
                        reconciled += 1
            except Exception as exc:
                _logger.warning(
                    "Nepal Wallets reconciliation skipped for %s (%s): %s",
                    tx.reference,
                    tx.provider_code,
                    exc,
                )
        return reconciled

    def _nw_reconcile_single_transaction(self):
        self.ensure_one()
        self._nw_lock_for_update()
        if self.state not in ('draft', 'pending'):
            return

        if self.provider_code == const.CODE_ESEWA:
            if not self.nw_gateway_txn_id:
                return
            self._nw_assert_npr_currency('eSewa')
            amount_str = self.provider_id._nw_format_esewa_amount(self.amount)
            status_resp = self.provider_id._nw_esewa_check_status(self.nw_gateway_txn_id, amount_str)
            verified_status = (status_resp.get('status') or '').upper()
            if verified_status == 'COMPLETE':
                if str(status_resp.get('transaction_uuid') or '').strip() != str(self.nw_gateway_txn_id):
                    raise ValidationError("eSewa: " + _("Status API transaction_uuid mismatch."))
                if str(status_resp.get('product_code') or '').strip() != self.provider_id._nw_esewa_get_product_code():
                    raise ValidationError("eSewa: " + _("Status API product_code mismatch."))
                self._nw_assert_amount_matches(status_resp.get('total_amount'), 'eSewa')
                if status_resp.get('ref_id'):
                    self.provider_reference = str(status_resp['ref_id'])
                self._set_done()
            elif verified_status in ('FULL_REFUND', 'PARTIAL_REFUND', 'CANCELED'):
                self._set_canceled(state_message=_("eSewa reconciliation status: %s", verified_status))

        elif self.provider_code == const.CODE_KHALTI:
            if not self.nw_khalti_pidx:
                return
            self._nw_process_khalti_notification({'pidx': self.nw_khalti_pidx})

        elif self.provider_code == const.CODE_CONNECTIPS:
            if not self.nw_gateway_txn_id:
                return
            self._nw_process_connectips_notification({'TXNID': self.nw_gateway_txn_id})
