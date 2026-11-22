from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.nepal_wallets import const
from odoo.addons.nepal_wallets.tests.common import NepalWalletsCommon

@tagged('post_install', '-at_install')
class TestNepalWalletsKhalti(NepalWalletsCommon):

    def _create_khalti_tx(self, reference='KHL-TEST-001', amount=125.50):
        return self._create_transaction(
            flow='redirect',
            provider_id=self.khalti_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_khalti').id,
            reference=reference,
            amount=amount,
            currency_id=self.currency_npr.id,
        )

    def test_khalti_minimum_amount_validation(self):
        tx = self._create_khalti_tx(reference='KHL-MIN-001', amount=5.0)
        with self.assertRaises(ValidationError):
            tx._get_specific_rendering_values({})

    def test_khalti_uat_initiate_and_lookup_flow(self):
        self.khalti_provider.nw_test_mode = 'uat'
        tx = self._create_khalti_tx(reference='KHL-UAT-001', amount=125.50)

        init_resp = MagicMock()
        init_resp.status_code = 200
        init_resp.content = b'{"pidx": "pidx_abc123"}'
        init_resp.json.return_value = {
            'pidx': 'pidx_abc123',
            'payment_url': 'https://test-pay.khalti.com/?pidx=pidx_abc123',
            'expires_in': 1800,
        }

        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=init_resp) as mock_post:
            rendering = tx._get_specific_rendering_values({})
            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            self.assertEqual(kwargs['headers']['Authorization'], 'Key test_secret_key_demo_123456')
            self.assertEqual(kwargs['json']['amount'], 12550)
            self.assertEqual(rendering['api_url'], 'https://test-pay.khalti.com/')
            self.assertEqual(rendering['query_params'], {'pidx': 'pidx_abc123'})
            self.assertEqual(tx.nw_khalti_pidx, 'pidx_abc123')
            self.assertEqual(tx.provider_reference, 'pidx_abc123')

        lookup_resp = MagicMock()
        lookup_resp.status_code = 200
        lookup_resp.content = b'{"status": "Completed"}'
        lookup_resp.json.return_value = {
            'pidx': 'pidx_abc123',
            'total_amount': 12550,
            'status': 'Completed',
            'transaction_id': 'KHLTXN9988',
            'fee': 0,
            'refunded': False,
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=lookup_resp) as mock_lookup:
            tx._handle_notification_data(const.CODE_KHALTI, {'pidx': 'pidx_abc123', 'status': 'Pending'})
            mock_lookup.assert_called_once()
            self.assertEqual(tx.state, 'done')
            self.assertEqual(tx.nw_khalti_pidx, 'pidx_abc123')
            self.assertEqual(tx.provider_reference, 'KHLTXN9988')

    def test_khalti_rejects_amount_mismatch_on_lookup(self):
        self.khalti_provider.nw_test_mode = 'uat'
        tx = self._create_khalti_tx(reference='KHL-MISMATCH-001', amount=100.0)
        tx.nw_khalti_pidx = 'pidx_mismatch'
        tx.provider_reference = 'pidx_mismatch'

        lookup_resp = MagicMock()
        lookup_resp.status_code = 200
        lookup_resp.content = b'{"status": "Completed"}'
        lookup_resp.json.return_value = {
            'pidx': 'pidx_mismatch',
            'total_amount': 5000,  # Rs. 50 instead of Rs. 100!
            'status': 'Completed',
            'transaction_id': 'KHLTXN_BAD',
            'refunded': False,
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=lookup_resp):
            with self.assertRaises(ValidationError):
                tx._handle_notification_data(const.CODE_KHALTI, {'pidx': 'pidx_mismatch'})

    def test_khalti_simulation_cancel(self):
        self.khalti_provider.nw_test_mode = 'simulation'
        tx = self._create_khalti_tx(reference='KHL-SIM-CANCEL', amount=50.0)
        rendering = tx._get_specific_rendering_values({})
        pidx = rendering['query_params']['pidx']

        tx._handle_notification_data(const.CODE_KHALTI, {
            'pidx': pidx,
            'status': 'User canceled',
            'total_amount': '5000',
        })
        self.assertEqual(tx.state, 'cancel')
