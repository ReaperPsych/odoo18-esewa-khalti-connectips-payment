import base64
from decimal import Decimal, ROUND_HALF_UP
import hashlib
import hmac
import json
import logging

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import pkcs12
from werkzeug import urls

from odoo import _, api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from odoo.addons.nepal_wallets import const

_logger = logging.getLogger(__name__)

class PaymentProvider(models.Model):
    _inherit = 'payment.provider'

    code = fields.Selection(
        selection_add=[
            (const.CODE_ESEWA, 'eSewa (Nepal Wallets)'),
            (const.CODE_KHALTI, 'Khalti by IME (Nepal Wallets)'),
            (const.CODE_CONNECTIPS, 'ConnectIPS (Nepal Wallets)'),
        ],
        ondelete={
            const.CODE_ESEWA: 'set default',
            const.CODE_KHALTI: 'set default',
            const.CODE_CONNECTIPS: 'set default',
        },
    )

    nw_test_mode = fields.Selection(
        selection=[
            ('simulation', 'Local Interactive Simulation (Works on localhost without external credentials)'),
            ('uat', 'Official Sandbox / UAT Gateway'),
        ],
        string='Test Environment Mode',
        default='uat',
        help='Choose between the built-in local simulator or the official UAT/Sandbox gateway when Provider State is Test Mode.',
    )

    # eSewa fields
    nw_esewa_product_code = fields.Char(
        string='eSewa Product Code / Merchant ID',
        default=const.ESEWA_DEFAULT_TEST_PRODUCT_CODE,
        help='Merchant product code assigned by eSewa (use EPAYTEST for official sandbox).',
    )
    nw_esewa_secret_key = fields.Char(
        string='eSewa Secret Key',
        default=const.ESEWA_DEFAULT_TEST_SECRET_KEY,
        help='Secret key provided by eSewa for HMAC-SHA256 signature generation and verification.',
        groups='base.group_system',
        copy=False,
    )

    # Khalti fields
    nw_khalti_secret_key = fields.Char(
        string='Khalti Secret Key',
        help='Live Secret Key from admin.khalti.com (Production) or test-admin.khalti.com (Sandbox). Sent server-side only via Authorization: Key <secret>.',
        groups='base.group_system',
        copy=False,
    )
    nw_khalti_public_key = fields.Char(
        string='Khalti Public Key (Optional)',
        help='Optional public key reference from your Khalti merchant dashboard.',
    )

    # ConnectIPS fields
    nw_connectips_merchant_id = fields.Char(
        string='ConnectIPS Merchant ID',
        help='Numeric Merchant ID issued by NCHL (e.g. 550).',
    )
    nw_connectips_app_id = fields.Char(
        string='ConnectIPS App ID',
        help='Application ID issued by NCHL (e.g. MER-550-APP-1). Also used as Basic Auth username for validatetxn.',
    )
    nw_connectips_app_name = fields.Char(
        string='ConnectIPS App Name',
        default='Odoo Store',
        help='Originating application name (max 30 characters).',
    )
    nw_connectips_app_password = fields.Char(
        string='ConnectIPS API Password',
        help='HTTP Basic Auth password issued by NCHL for the validatetxn API.',
        groups='base.group_system',
        copy=False,
    )
    nw_connectips_pfx_file = fields.Binary(
        string='ConnectIPS Certificate (.pfx)',
        help='PKCS#12 (.pfx) digital certificate issued by NCHL for SHA256withRSA signing.',
        groups='base.group_system',
        copy=False,
        attachment=False,
    )
    nw_connectips_pfx_filename = fields.Char(string='PFX Filename')
    nw_connectips_pfx_password = fields.Char(
        string='PFX Password',
        help='Passphrase used to unlock the .pfx keystore file.',
        groups='base.group_system',
        copy=False,
    )
    nw_connectips_private_key_pem = fields.Text(
        string='Private Key (PEM Alternative)',
        help='Optional PEM-encoded RSA private key (used if no .pfx file is uploaded).',
        groups='base.group_system',
        copy=False,
    )

    @api.constrains(
        'state',
        'code',
        'nw_esewa_product_code',
        'nw_esewa_secret_key',
        'nw_khalti_secret_key',
        'nw_connectips_merchant_id',
        'nw_connectips_app_id',
        'nw_connectips_app_password',
        'nw_connectips_pfx_file',
        'nw_connectips_private_key_pem',
    )
    def _check_nw_live_credentials(self):
        for provider in self.filtered(lambda p: p.state == 'enabled' and p.code in const.PROVIDER_CODES):
            if provider.code == const.CODE_ESEWA:
                code = (provider.nw_esewa_product_code or '').strip()
                key = (provider.nw_esewa_secret_key or '').strip()
                if not code or code == const.ESEWA_DEFAULT_TEST_PRODUCT_CODE:
                    raise ValidationError(
                        "eSewa: " + _("Live mode requires your production eSewa Product Code (cannot use EPAYTEST).")
                    )
                if not key or key == const.ESEWA_DEFAULT_TEST_SECRET_KEY:
                    raise ValidationError(
                        "eSewa: " + _("Live mode requires your production eSewa Secret Key (cannot use sandbox test key).")
                    )
            elif provider.code == const.CODE_KHALTI:
                key = (provider.nw_khalti_secret_key or '').strip()
                if not key:
                    raise ValidationError(
                        "Khalti: " + _("Live mode requires a production Khalti Secret Key.")
                    )
                if key.lower().startswith('test_'):
                    raise ValidationError(
                        "Khalti: " + _("Live mode cannot use a sandbox/test Khalti Secret Key.")
                    )
            elif provider.code == const.CODE_CONNECTIPS:
                merchant_id = (provider.nw_connectips_merchant_id or '').strip()
                app_id = (provider.nw_connectips_app_id or '').strip()
                if not merchant_id or not app_id:
                    raise ValidationError(
                        "ConnectIPS: " + _("Live mode requires Merchant ID and App ID.")
                    )
                if merchant_id == '550' or app_id == 'MER-550-APP-1':
                    raise ValidationError(
                        "ConnectIPS: " + _("Live mode cannot use demo Merchant ID (550) or demo App ID (MER-550-APP-1).")
                    )
                if not (provider.nw_connectips_app_password or '').strip():
                    raise ValidationError(
                        "ConnectIPS: " + _("Live mode requires ConnectIPS API Password.")
                    )
                if not provider.nw_connectips_pfx_file and not (provider.nw_connectips_private_key_pem or '').strip():
                    raise ValidationError(
                        "ConnectIPS: " + _("Live mode requires a .pfx certificate or PEM private key.")
                    )

    def write(self, vals):
        res = super().write(vals)
        if 'state' in vals or 'nw_test_mode' in vals:
            for provider in self.filtered(lambda p: p.code in const.PROVIDER_CODES):
                if provider._nw_is_simulation():
                    # Clear NPR-only currency lock so local simulation works on any store currency
                    super(PaymentProvider, provider).write({'available_currency_ids': [(5, 0, 0)]})
                else:
                    npr = self.env['res.currency'].with_context(active_test=False).search(
                        [('name', 'in', list(const.SUPPORTED_CURRENCIES))]
                    )
                    super(PaymentProvider, provider).write({'available_currency_ids': [(6, 0, npr.ids)]})
        return res

    def _get_supported_currencies(self):
        supported_currencies = super()._get_supported_currencies()
        if self.code in const.PROVIDER_CODES and not self._nw_is_simulation():
            supported_currencies = supported_currencies.filtered(
                lambda c: c.name in const.SUPPORTED_CURRENCIES
            )
        return supported_currencies

    def _get_default_payment_method_codes(self):
        default_codes = super()._get_default_payment_method_codes()
        if self.code not in const.PROVIDER_CODES:
            return default_codes
        return const.DEFAULT_PAYMENT_METHOD_CODES[self.code]

    def _nw_is_simulation(self):
        self.ensure_one()
        return self.state == 'test' and self.nw_test_mode == 'simulation'

    @staticmethod
    def _nw_quantize_amount(amount):
        return Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

    @staticmethod
    def _nw_format_esewa_amount(amount):
        dec = PaymentProvider._nw_quantize_amount(amount)
        if dec == dec.to_integral_value():
            return str(int(dec))
        return f"{dec:.2f}"

    @staticmethod
    def _nw_amount_to_paisa(amount):
        dec = PaymentProvider._nw_quantize_amount(amount)
        return int(dec * 100)

    # eSewa helpers
    def _nw_esewa_get_product_code(self):
        self.ensure_one()
        code = (self.nw_esewa_product_code or '').strip()
        if self.state == 'enabled':
            if not code or code == const.ESEWA_DEFAULT_TEST_PRODUCT_CODE:
                raise ValidationError(
                    "eSewa: " + _("Live mode requires your production eSewa Product Code (cannot use EPAYTEST).")
                )
            return code
        return code or const.ESEWA_DEFAULT_TEST_PRODUCT_CODE

    def _nw_esewa_get_secret_key(self):
        self.ensure_one()
        key = (self.nw_esewa_secret_key or '').strip()
        if self.state == 'enabled':
            if not key or key == const.ESEWA_DEFAULT_TEST_SECRET_KEY:
                raise ValidationError(
                    "eSewa: " + _("Live mode requires your production eSewa Secret Key (cannot use sandbox test key).")
                )
            return key
        return key or const.ESEWA_DEFAULT_TEST_SECRET_KEY

    def _nw_esewa_get_form_url(self):
        self.ensure_one()
        if self.state == 'enabled':
            return const.ESEWA_LIVE_FORM_URL
        if self._nw_is_simulation():
            return urls.url_join(self.get_base_url(), '/payment/nepal_wallets/esewa/simulation')
        return const.ESEWA_UAT_FORM_URL

    def _nw_esewa_get_status_url(self):
        self.ensure_one()
        if self.state == 'enabled':
            return const.ESEWA_LIVE_STATUS_URL
        return const.ESEWA_UAT_STATUS_URL

    def _nw_esewa_generate_signature(self, message, secret_key=None):
        self.ensure_one()
        key = secret_key if secret_key is not None else self._nw_esewa_get_secret_key()
        digest = hmac.new(
            key.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256,
        ).digest()
        return base64.b64encode(digest).decode('utf-8')

    @staticmethod
    def _nw_esewa_decode_callback_data(encoded_data):
        if not encoded_data or not isinstance(encoded_data, str) or len(encoded_data) > 16384 or '\x00' in encoded_data:
            raise ValidationError("eSewa: " + _("Missing or oversized callback data payload."))
        try:
            padded = encoded_data + '=' * (-len(encoded_data) % 4)
            decoded_bytes = base64.b64decode(padded)
            payload = json.loads(decoded_bytes.decode('utf-8'))
            if not isinstance(payload, dict):
                raise ValueError("Decoded payload is not a JSON object.")
            return payload
        except Exception as exc:
            _logger.warning("Nepal Wallets (eSewa): Failed to decode Base64 callback payload: %s", exc)
            raise ValidationError(
                "eSewa: " + _("Failed to decode callback payload.")
            ) from exc

    def _nw_esewa_verify_signature(self, payload):
        self.ensure_one()
        received_sig = payload.get('signature')
        signed_field_names = payload.get('signed_field_names')

        if not received_sig:
            raise ValidationError("eSewa: " + _("Missing signature in payment response."))
        if not signed_field_names:
            raise ValidationError("eSewa: " + _("Missing signed_field_names in payment response."))

        fields_list = [f.strip() for f in str(signed_field_names).split(',') if f.strip()]

        # Prevent signed_field_names downgrade/stripping attacks
        required_signed = {'status', 'total_amount', 'transaction_uuid', 'product_code'}
        missing_signed = required_signed - set(fields_list)
        if missing_signed:
            raise ValidationError(
                "eSewa: " + _("Callback signature does not cover mandatory fields: %s", ', '.join(sorted(missing_signed)))
            )

        expected_code = self._nw_esewa_get_product_code()
        if str(payload.get('product_code', '')).strip() != expected_code:
            raise ValidationError("eSewa: " + _("Product code mismatch in payment response."))

        message = ','.join(f"{field}={payload.get(field, '')}" for field in fields_list)
        expected_sig = self._nw_esewa_generate_signature(message)

        if not hmac.compare_digest(str(received_sig), str(expected_sig)):
            _logger.warning(
                "Nepal Wallets (eSewa): HMAC-SHA256 signature verification failed for transaction_uuid=%s",
                payload.get('transaction_uuid'),
            )
            raise ValidationError("eSewa: " + _("Invalid HMAC-SHA256 response signature."))
        return True

    def _nw_esewa_check_status(self, transaction_uuid, total_amount):
        self.ensure_one()
        status_url = self._nw_esewa_get_status_url()
        clean_amount = str(total_amount).replace(',', '').strip()
        params = {
            'product_code': self._nw_esewa_get_product_code(),
            'total_amount': clean_amount,
            'transaction_uuid': transaction_uuid,
        }
        try:
            resp = requests.get(
                status_url,
                params=params,
                timeout=const.DEFAULT_API_TIMEOUT,
                allow_redirects=False,
            )
            if isinstance(resp.status_code, int) and 300 <= resp.status_code < 400:
                _logger.error("Nepal Wallets (eSewa): Status API returned unexpected redirect %s.", resp.status_code)
                raise ValidationError("eSewa: " + _("Could not verify transaction with eSewa Status API."))
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                _logger.error("Nepal Wallets (eSewa): Status API returned non-object JSON: %s", type(data).__name__)
                raise ValidationError("eSewa: " + _("Unexpected response format from eSewa Status API."))
            return data
        except ValidationError:
            raise
        except (requests.RequestException, ValueError) as exc:
            _logger.error("Nepal Wallets (eSewa): Status check request failed: %s", exc)
            raise ValidationError(
                "eSewa: " + _("Could not verify transaction with eSewa Status API.")
            ) from exc

    # Khalti helpers
    def _nw_khalti_get_initiate_url(self):
        self.ensure_one()
        if self.state == 'enabled':
            return const.KHALTI_LIVE_INITIATE_URL
        return const.KHALTI_UAT_INITIATE_URL

    def _nw_khalti_get_lookup_url(self):
        self.ensure_one()
        if self.state == 'enabled':
            return const.KHALTI_LIVE_LOOKUP_URL
        return const.KHALTI_UAT_LOOKUP_URL

    def _nw_khalti_headers(self):
        self.ensure_one()
        key = (self.nw_khalti_secret_key or '').strip()
        if not key:
            raise ValidationError(
                "Khalti: " + _("Khalti Secret Key is required in Provider Configuration.")
            )
        if self.state == 'enabled' and key.lower().startswith('test_'):
            raise ValidationError(
                "Khalti: " + _("Live mode cannot use a sandbox/test Khalti Secret Key.")
            )
        return {
            'Authorization': f'Key {key}',
            'Content-Type': 'application/json',
        }

    def _nw_khalti_initiate_payment(self, payload):
        self.ensure_one()
        url = self._nw_khalti_get_initiate_url()
        try:
            resp = requests.post(
                url,
                json=payload,
                headers=self._nw_khalti_headers(),
                timeout=const.DEFAULT_API_TIMEOUT,
                allow_redirects=False,
            )
            if isinstance(resp.status_code, int) and 300 <= resp.status_code < 400:
                _logger.error("Nepal Wallets (Khalti): Initiate API returned unexpected redirect %s.", resp.status_code)
                raise ValidationError("Khalti: " + _("Payment initiation failed. Please try again."))
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                _logger.error("Nepal Wallets (Khalti): Initiate API returned non-object JSON: %s", type(data).__name__)
                raise ValidationError("Khalti: " + _("Unexpected response format from Khalti Initiate API."))
            if resp.status_code >= 400:
                detail = data.get('detail') or data.get('error_key') or str(data) or resp.reason
                _logger.warning("Nepal Wallets (Khalti): Initiate failed (%s): %s", resp.status_code, detail)
                raise ValidationError(
                    "Khalti: " + _("Payment initiation failed. Please try again or contact support.")
                )
            if not data.get('pidx') or not data.get('payment_url'):
                raise ValidationError(
                    "Khalti: " + _("Unexpected response from Khalti Initiate API: missing pidx or payment_url.")
                )
            return data
        except ValidationError:
            raise
        except (requests.RequestException, ValueError) as exc:
            _logger.error("Nepal Wallets (Khalti): Initiate request failed: %s", exc)
            raise ValidationError(
                "Khalti: " + _("Could not connect to Khalti Initiate API.")
            ) from exc

    def _nw_khalti_lookup_payment(self, pidx):
        self.ensure_one()
        url = self._nw_khalti_get_lookup_url()
        try:
            resp = requests.post(
                url,
                json={'pidx': pidx},
                headers=self._nw_khalti_headers(),
                timeout=const.DEFAULT_API_TIMEOUT,
                allow_redirects=False,
            )
            if isinstance(resp.status_code, int) and 300 <= resp.status_code < 400:
                _logger.error("Nepal Wallets (Khalti): Lookup API returned unexpected redirect %s.", resp.status_code)
                raise ValidationError("Khalti: " + _("Payment verification failed."))
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                _logger.error("Nepal Wallets (Khalti): Lookup API returned non-object JSON: %s", type(data).__name__)
                raise ValidationError("Khalti: " + _("Unexpected response format from Khalti Lookup API."))
            if resp.status_code >= 400:
                detail = data.get('detail') or str(data) or resp.reason
                _logger.warning("Nepal Wallets (Khalti): Lookup failed (%s): %s", resp.status_code, detail)
                raise ValidationError(
                    "Khalti: " + _("Payment verification failed.")
                )
            return data
        except ValidationError:
            raise
        except (requests.RequestException, ValueError) as exc:
            _logger.error("Nepal Wallets (Khalti): Lookup request failed: %s", exc)
            raise ValidationError(
                "Khalti: " + _("Could not connect to Khalti Lookup API.")
            ) from exc

    # ConnectIPS helpers
    def _nw_connectips_get_gateway_url(self):
        self.ensure_one()
        if self.state == 'enabled':
            return const.CONNECTIPS_LIVE_GATEWAY_URL
        if self._nw_is_simulation():
            return urls.url_join(self.get_base_url(), '/payment/nepal_wallets/connectips/simulation')
        return const.CONNECTIPS_UAT_GATEWAY_URL

    def _nw_connectips_get_validate_url(self):
        self.ensure_one()
        if self.state == 'enabled':
            return const.CONNECTIPS_LIVE_VALIDATE_URL
        return const.CONNECTIPS_UAT_VALIDATE_URL

    def _nw_connectips_load_private_key(self):
        self.ensure_one()
        if self.nw_connectips_pfx_file:
            try:
                pfx_bytes = base64.b64decode(self.nw_connectips_pfx_file)
                password = (
                    self.nw_connectips_pfx_password.encode('utf-8')
                    if self.nw_connectips_pfx_password
                    else None
                )
                private_key, _cert, _additional = pkcs12.load_key_and_certificates(
                    pfx_bytes, password
                )
                if private_key is None:
                    raise ValueError("No private key found inside .pfx certificate.")
                if not isinstance(private_key, RSAPrivateKey):
                    raise ValueError("Certificate private key is not an RSA key.")
                return private_key
            except Exception as exc:
                _logger.error("Nepal Wallets (ConnectIPS): Failed to load .pfx private key: %s", exc)
                raise ValidationError(
                    "ConnectIPS: " + _("Failed to load .pfx private key. Please verify the certificate and password.")
                ) from exc

        if self.nw_connectips_private_key_pem:
            try:
                pem_bytes = self.nw_connectips_private_key_pem.strip().encode('utf-8')
                is_encrypted = b'ENCRYPTED' in pem_bytes
                password = (
                    self.nw_connectips_pfx_password.encode('utf-8')
                    if is_encrypted and self.nw_connectips_pfx_password
                    else None
                )
                private_key = serialization.load_pem_private_key(pem_bytes, password=password)
                if not isinstance(private_key, RSAPrivateKey):
                    raise ValueError("PEM private key is not an RSA key.")
                return private_key
            except Exception as exc:
                _logger.error("Nepal Wallets (ConnectIPS): Failed to load PEM private key: %s", exc)
                raise ValidationError(
                    "ConnectIPS: " + _("Failed to load PEM private key. Please verify the key configuration.")
                ) from exc

        raise ValidationError(
            "ConnectIPS: " + _("Please upload a .pfx certificate or paste a PEM private key.")
        )

    @staticmethod
    def _nw_connectips_build_initiation_message(params):
        return (
            f"MERCHANTID={params['MERCHANTID']},"
            f"APPID={params['APPID']},"
            f"APPNAME={params['APPNAME']},"
            f"TXNID={params['TXNID']},"
            f"TXNDATE={params['TXNDATE']},"
            f"TXNCRNCY={params['TXNCRNCY']},"
            f"TXNAMT={params['TXNAMT']},"
            f"REFERENCEID={params['REFERENCEID']},"
            f"REMARKS={params['REMARKS']},"
            f"PARTICULARS={params['PARTICULARS']},"
            f"TOKEN=TOKEN"
        )

    @staticmethod
    def _nw_connectips_build_validation_message(merchant_id, app_id, reference_id, txn_amt):
        return (
            f"MERCHANTID={merchant_id},"
            f"APPID={app_id},"
            f"REFERENCEID={reference_id},"
            f"TXNAMT={txn_amt}"
        )

    def _nw_connectips_sign(self, message):
        self.ensure_one()
        private_key = self._nw_connectips_load_private_key()
        signature = private_key.sign(
            message.encode('utf-8'),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode('utf-8')

    def _nw_connectips_verify_signature(self, message, signature_b64, public_key=None):
        self.ensure_one()
        pub_key = public_key or self._nw_connectips_load_private_key().public_key()
        try:
            sig_bytes = base64.b64decode(signature_b64)
            pub_key.verify(
                sig_bytes,
                message.encode('utf-8'),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
            return True
        except (InvalidSignature, ValueError):
            return False

    def _nw_connectips_validate_txn(self, reference_id, txn_amt_paisa):
        self.ensure_one()
        merchant_id = (self.nw_connectips_merchant_id or '').strip()
        app_id = (self.nw_connectips_app_id or '').strip()
        if not merchant_id or not app_id:
            raise ValidationError(
                "ConnectIPS: " + _("Merchant ID and App ID must be configured.")
            )
        if self.state == 'enabled' and (
            merchant_id == '550' or app_id == 'MER-550-APP-1'
        ):
            raise ValidationError(
                "ConnectIPS: " + _("Live mode cannot use demo Merchant ID (550) or demo App ID (MER-550-APP-1).")
            )
        if not self.nw_connectips_app_password:
            raise ValidationError(
                "ConnectIPS: " + _("ConnectIPS API Password is required for validatetxn.")
            )

        msg = self._nw_connectips_build_validation_message(
            merchant_id=merchant_id,
            app_id=app_id,
            reference_id=reference_id,
            txn_amt=txn_amt_paisa,
        )
        token = self._nw_connectips_sign(msg)

        payload = {
            'merchantId': int(merchant_id) if merchant_id.isdigit() else merchant_id,
            'appId': app_id,
            'referenceId': reference_id,
            'txnAmt': int(txn_amt_paisa),
            'token': token,
        }
        url = self._nw_connectips_get_validate_url()
        try:
            resp = requests.post(
                url,
                json=payload,
                auth=(app_id, self.nw_connectips_app_password),
                headers={'Content-Type': 'application/json'},
                timeout=const.DEFAULT_API_TIMEOUT,
                allow_redirects=False,
            )
            if isinstance(resp.status_code, int) and 300 <= resp.status_code < 400:
                _logger.error("Nepal Wallets (ConnectIPS): validatetxn returned unexpected redirect %s.", resp.status_code)
                raise ValidationError("ConnectIPS: " + _("Payment validation failed."))
            data = resp.json() if resp.content else {}
            if not isinstance(data, dict):
                _logger.error("Nepal Wallets (ConnectIPS): validatetxn returned non-object JSON: %s", type(data).__name__)
                raise ValidationError("ConnectIPS: " + _("Unexpected response format from ConnectIPS Validation API."))
            if resp.status_code >= 400:
                detail = data.get('statusDesc') or data.get('message') or resp.reason
                _logger.warning("Nepal Wallets (ConnectIPS): Validation API error (%s): %s", resp.status_code, detail)
                raise ValidationError(
                    "ConnectIPS: " + _("Payment validation failed.")
                )
            return data
        except ValidationError:
            raise
        except (requests.RequestException, ValueError) as exc:
            _logger.error("Nepal Wallets (ConnectIPS): validatetxn request failed: %s", exc)
            raise ValidationError(
                "ConnectIPS: " + _("Could not connect to ConnectIPS Validation API.")
            ) from exc

    def action_nw_test_connectips_key(self):
        self.ensure_one()
        if not self.env.is_admin() and not self.env.user.has_group('base.group_system'):
            raise AccessError(_("Only System Administrators can test ConnectIPS private keys."))
        try:
            sig = self._nw_connectips_sign('CONNECTIPS_KEY_SELF_TEST')
            valid = self._nw_connectips_verify_signature('CONNECTIPS_KEY_SELF_TEST', sig)
            if not valid:
                raise UserError(_("Signature verification failed after signing."))
        except ValidationError as exc:
            raise UserError(str(exc)) from exc

        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('ConnectIPS Key Verified'),
                'message': _('SHA256withRSA private key loaded and self-test signature verified successfully.'),
                'type': 'success',
                'sticky': False,
            },
        }
