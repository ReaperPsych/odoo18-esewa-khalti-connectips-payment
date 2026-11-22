import re
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests import tagged

from odoo.addons.nepal_wallets import const
from odoo.addons.nepal_wallets.tests.common import NepalWalletsCommon

@tagged('post_install', '-at_install')
class TestNepalWalletsConnectIPS(NepalWalletsCommon):

    def _create_connectips_tx(self, reference='CIPS-ORDER-2026-0001', amount=250.75):
        return self._create_transaction(
            flow='redirect',
            provider_id=self.connectips_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_connectips').id,
            reference=reference,
            amount=amount,
            currency_id=self.currency_npr.id,
        )

    def test_connectips_pfx_and_pem_rsa_sha256_signing(self):
        tx = self._create_connectips_tx(reference='CIPS-RSA-001', amount=250.75)
        rendering = tx._get_specific_rendering_values({})

        self.assertLessEqual(len(rendering['TXNID']), const.CONNECTIPS_TXNID_MAX_LEN)
        self.assertEqual(rendering['TXNAMT'], '25075')
        self.assertEqual(rendering['TXNCRNCY'], 'NPR')
        self.assertTrue(re.match(r'^\d{2}-\d{2}-\d{4}$', rendering['TXNDATE']))

        # ConnectIPS requires literal TOKEN=TOKEN suffix on initiation
        plaintext = self.connectips_provider._nw_connectips_build_initiation_message(rendering)
        self.assertTrue(plaintext.endswith(',TOKEN=TOKEN'))
        self.assertTrue(
            self.connectips_provider._nw_connectips_verify_signature(
                plaintext, rendering['TOKEN'], public_key=self.rsa_public_key
            )
        )
        self.assertFalse(
            self.connectips_provider._nw_connectips_verify_signature(
                plaintext.replace('25075', '100'), rendering['TOKEN'], public_key=self.rsa_public_key
            )
        )

        self.connectips_provider.write({
            'nw_connectips_pfx_file': False,
            'nw_connectips_pfx_password': False,
            'nw_connectips_private_key_pem': self.pem_private_key,
        })
        pem_sig = self.connectips_provider._nw_connectips_sign(plaintext)
        self.assertTrue(
            self.connectips_provider._nw_connectips_verify_signature(
                plaintext, pem_sig, public_key=self.rsa_public_key
            )
        )

    def test_connectips_uat_validatetxn_success_and_basic_auth(self):
        self.connectips_provider.nw_test_mode = 'uat'
        tx = self._create_connectips_tx(reference='CIPS-UAT-001', amount=500.0)
        rendering = tx._get_specific_rendering_values({})
        self.assertEqual(rendering['api_url'], const.CONNECTIPS_UAT_GATEWAY_URL)
        txn_id = rendering['TXNID']

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b'{"status": "SUCCESS"}'
        mock_resp.json.return_value = {
            'merchantId': 550,
            'appId': 'MER-550-APP-1',
            'referenceId': txn_id,
            'txnAmt': '50000',
            'txnCrncy': 'NPR',
            'status': 'SUCCESS',
            'statusDesc': 'TRANSACTION SUCCESSFUL',
        }

        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=mock_resp) as mock_post:
            tx._handle_notification_data(const.CODE_CONNECTIPS, {'TXNID': txn_id, '_nw_callback_type': 'success'})
            mock_post.assert_called_once()
            _, kwargs = mock_post.call_args
            self.assertEqual(kwargs['auth'], ('MER-550-APP-1', 'test_api_password'))
            self.assertEqual(kwargs['json']['txnAmt'], 50000)
            self.assertEqual(kwargs['json']['referenceId'], txn_id)

            # ConnectIPS requires literal TOKEN=TOKEN suffix on initiation
            val_msg = f"MERCHANTID=550,APPID=MER-550-APP-1,REFERENCEID={txn_id},TXNAMT=50000"
            self.assertTrue(
                self.connectips_provider._nw_connectips_verify_signature(
                    val_msg, kwargs['json']['token'], public_key=self.rsa_public_key
                )
            )
            self.assertEqual(tx.state, 'done')

    def test_connectips_key_self_test_button(self):
        action = self.connectips_provider.action_nw_test_connectips_key()
        self.assertEqual(action['params']['type'], 'success')

    def test_connectips_wrong_pfx_password_raises(self):
        self.connectips_provider.nw_connectips_pfx_password = 'wrong_password'
        with self.assertRaises(ValidationError):
            self.connectips_provider._nw_connectips_load_private_key()
