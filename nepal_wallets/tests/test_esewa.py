import base64
import json
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.nepal_wallets import const
from odoo.addons.nepal_wallets.tests.common import NepalWalletsCommon

@tagged('post_install', '-at_install')
class TestNepalWalletsEsewa(NepalWalletsCommon):

    def _create_esewa_tx(self, reference='ESW-TEST-001', amount=1250.0):
        return self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference=reference,
            amount=amount,
            currency_id=self.currency_npr.id,
        )

    def test_esewa_hmac_sha256_known_vector(self):
        import hashlib
        import hmac

        message = 'total_amount=100,transaction_uuid=11-201-13,product_code=EPAYTEST'
        sig = self.esewa_provider._nw_esewa_generate_signature(message)
        expected = base64.b64encode(
            hmac.new(
                const.ESEWA_DEFAULT_TEST_SECRET_KEY.encode('utf-8'),
                message.encode('utf-8'),
                hashlib.sha256,
            ).digest()
        ).decode('utf-8')
        self.assertEqual(sig, expected)
        self.assertEqual(sig, '5DZywcrTKD0gia/rsSMcrRHmJl+4Tbol6S+lWgdJ94E=')

    def test_esewa_rendering_values_and_signature(self):
        tx = self._create_esewa_tx(reference='ESW-REND-001', amount=500.0)
        rendering = tx._get_specific_rendering_values({})

        self.assertEqual(rendering['product_code'], 'EPAYTEST')
        self.assertEqual(rendering['total_amount'], '500')
        self.assertEqual(rendering['signed_field_names'], 'total_amount,transaction_uuid,product_code')
        self.assertTrue(tx.nw_gateway_txn_id)
        self.assertEqual(rendering['transaction_uuid'], tx.nw_gateway_txn_id)

        expected_msg = f"total_amount=500,transaction_uuid={tx.nw_gateway_txn_id},product_code=EPAYTEST"
        self.assertEqual(
            rendering['signature'],
            self.esewa_provider._nw_esewa_generate_signature(expected_msg),
        )

    def test_esewa_callback_with_comma_formatted_amount_succeeds(self):
        tx = self._create_esewa_tx(reference='ESW-COMMA-001', amount=1250.0)
        rendering = tx._get_specific_rendering_values({})
        txn_uuid = rendering['transaction_uuid']

        signed_fields = 'transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names'
        payload = {
            'transaction_code': '0LD5CEH',
            'status': 'COMPLETE',
            'total_amount': '1,250.0',
            'transaction_uuid': txn_uuid,
            'product_code': 'EPAYTEST',
            'signed_field_names': signed_fields,
        }
        msg = ','.join(f"{k}={payload[k]}" for k in signed_fields.split(','))
        payload['signature'] = self.esewa_provider._nw_esewa_generate_signature(msg)

        encoded = base64.b64encode(json.dumps(payload).encode('utf-8')).decode('utf-8')
        decoded = self.esewa_provider._nw_esewa_decode_callback_data(encoded)
        tx._handle_notification_data(const.CODE_ESEWA, decoded)

        self.assertEqual(tx.state, 'done')
        self.assertEqual(tx.provider_reference, '0LD5CEH')

    def test_esewa_rejects_tampered_signature(self):
        tx = self._create_esewa_tx(reference='ESW-TAMPER-001', amount=200.0)
        rendering = tx._get_specific_rendering_values({})
        payload = {
            'transaction_code': 'FAKE123',
            'status': 'COMPLETE',
            'total_amount': '200',
            'transaction_uuid': rendering['transaction_uuid'],
            'product_code': 'EPAYTEST',
            'signed_field_names': 'transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names',
            'signature': 'INVALID_SIGNATURE_STRING==',
        }
        with self.assertRaises(ValidationError):
            tx._handle_notification_data(const.CODE_ESEWA, payload)
        self.assertNotEqual(tx.state, 'done')

    def test_esewa_uat_status_check_verification(self):
        self.esewa_provider.nw_test_mode = 'uat'
        tx = self._create_esewa_tx(reference='ESW-UAT-001', amount=350.0)
        rendering = tx._get_specific_rendering_values({})
        self.assertEqual(rendering['api_url'], const.ESEWA_UAT_FORM_URL)

        signed_fields = 'transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names'
        payload = {
            'transaction_code': '0UAT999',
            'status': 'COMPLETE',
            'total_amount': '350',
            'transaction_uuid': rendering['transaction_uuid'],
            'product_code': 'EPAYTEST',
            'signed_field_names': signed_fields,
        }
        msg = ','.join(f"{k}={payload[k]}" for k in signed_fields.split(','))
        payload['signature'] = self.esewa_provider._nw_esewa_generate_signature(msg)

        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {
            'product_code': 'EPAYTEST',
            'transaction_uuid': rendering['transaction_uuid'],
            'total_amount': 350.0,
            'status': 'COMPLETE',
            'ref_id': '0UAT999',
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.get', return_value=mock_resp) as mock_get:
            tx._handle_notification_data(const.CODE_ESEWA, payload)
            mock_get.assert_called_once()
            self.assertEqual(tx.state, 'done')
