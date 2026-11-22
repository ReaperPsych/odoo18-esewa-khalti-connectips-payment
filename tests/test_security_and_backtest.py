import re
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, tagged

from odoo.addons.nepal_wallets import const
from odoo.addons.nepal_wallets.tests.common import NepalWalletsCommon

@tagged('post_install', '-at_install')
class TestNepalWalletsSecurity(NepalWalletsCommon):

    def test_esewa_signed_field_names_stripping_rejected(self):
        tx = self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='SEC-STRIP-001',
            amount=500.0,
            currency_id=self.currency_npr.id,
        )
        rendering = tx._get_specific_rendering_values({})
        txn_uuid = rendering['transaction_uuid']

        stripped_msg = f"transaction_uuid={txn_uuid}"
        payload = {
            'transaction_code': 'FAKE01',
            'status': 'COMPLETE',
            'total_amount': '500',
            'transaction_uuid': txn_uuid,
            'product_code': 'EPAYTEST',
            'signed_field_names': 'transaction_uuid',
            'signature': self.esewa_provider._nw_esewa_generate_signature(stripped_msg),
        }
        with self.assertRaises(ValidationError):
            tx._handle_notification_data(const.CODE_ESEWA, payload)
        self.assertNotEqual(tx.state, 'done')

    def test_esewa_production_blocks_public_test_credentials(self):
        with self.assertRaises(ValidationError):
            self.esewa_provider.write({
                'state': 'enabled',
                'nw_esewa_product_code': const.ESEWA_DEFAULT_TEST_PRODUCT_CODE,
                'nw_esewa_secret_key': const.ESEWA_DEFAULT_TEST_SECRET_KEY,
            })

    def test_done_transaction_cannot_be_downgraded_by_cancel_replay(self):
        tx = self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='SEC-REPLAY-001',
            amount=300.0,
            currency_id=self.currency_npr.id,
        )
        rendering = tx._get_specific_rendering_values({})
        txn_uuid = rendering['transaction_uuid']

        signed_fields = 'transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names'
        ok_payload = {
            'transaction_code': 'REAL999',
            'status': 'COMPLETE',
            'total_amount': '300',
            'transaction_uuid': txn_uuid,
            'product_code': 'EPAYTEST',
            'signed_field_names': signed_fields,
        }
        msg = ','.join(f"{k}={ok_payload[k]}" for k in signed_fields.split(','))
        ok_payload['signature'] = self.esewa_provider._nw_esewa_generate_signature(msg)
        tx._handle_notification_data(const.CODE_ESEWA, ok_payload)
        self.assertEqual(tx.state, 'done')

        tx._handle_notification_data(const.CODE_ESEWA, {
            'transaction_uuid': txn_uuid,
            'status': 'CANCELED',
        })
        self.assertEqual(tx.state, 'done')

    def test_missing_amount_on_completion_rejected(self):
        tx_k = self._create_transaction(
            flow='redirect',
            provider_id=self.khalti_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_khalti').id,
            reference='SEC-NOAMT-KHL',
            amount=100.0,
            currency_id=self.currency_npr.id,
        )
        rendering_k = tx_k._get_specific_rendering_values({})
        with self.assertRaises(ValidationError):
            tx_k._handle_notification_data(const.CODE_KHALTI, {
                'pidx': rendering_k['query_params']['pidx'],
                'status': 'Completed',
            })

        tx_c = self._create_transaction(
            flow='redirect',
            provider_id=self.connectips_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_connectips').id,
            reference='SEC-NOAMT-CIPS',
            amount=100.0,
            currency_id=self.currency_npr.id,
        )
        rendering_c = tx_c._get_specific_rendering_values({})
        with self.assertRaises(ValidationError):
            tx_c._handle_notification_data(const.CODE_CONNECTIPS, {
                'TXNID': rendering_c['TXNID'],
                'status': 'SUCCESS',
            })

    def test_decimal_paisa_precision(self):
        self.assertEqual(self.khalti_provider._nw_amount_to_paisa(19.99), 1999)
        self.assertEqual(self.khalti_provider._nw_amount_to_paisa(100.55), 10055)
        self.assertEqual(self.khalti_provider._nw_amount_to_paisa(0.10), 10)
        self.assertEqual(self.khalti_provider._nw_amount_to_paisa(99999.99), 9999999)

    def test_khalti_rejects_untrusted_redirect_host(self):
        self.khalti_provider.nw_test_mode = 'uat'
        tx = self._create_transaction(
            flow='redirect',
            provider_id=self.khalti_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_khalti').id,
            reference='SEC-REDIR-KHL',
            amount=100.0,
            currency_id=self.currency_npr.id,
        )
        evil_resp = MagicMock()
        evil_resp.status_code = 200
        evil_resp.content = b'{"pidx": "x"}'
        evil_resp.json.return_value = {
            'pidx': 'pidx_evil',
            'payment_url': 'http://evil.example.com/?pidx=pidx_evil',
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=evil_resp):
            with self.assertRaises(ValidationError):
                tx._get_specific_rendering_values({})

@tagged('post_install', '-at_install')
class TestNepalWalletsHttpBacktest(HttpCase, NepalWalletsCommon):

    def _extract_csrf_token(self, html_text):
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_text)
        self.assertTrue(match, "Expected csrf_token hidden input in HTML form")
        return match.group(1)

    def _get_session_csrf_token(self):
        login_page = self.url_open('/web/login')
        return self._extract_csrf_token(login_page.text)

    def test_http_simulation_routes_blocked_in_uat_mode(self):
        self.esewa_provider.nw_test_mode = 'uat'
        tx = self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='HTTP-LOCK-001',
            amount=250.0,
            currency_id=self.currency_npr.id,
        )
        rendering = tx._get_specific_rendering_values({})
        self.env.cr.flush()

        csrf_token = self._get_session_csrf_token()
        resp = self.url_open(
            '/payment/nepal_wallets/esewa/simulation/process',
            data={
                'csrf_token': csrf_token,
                'action': 'complete',
                'transaction_uuid': rendering['transaction_uuid'],
            },
        )
        self.assertEqual(resp.status_code, 403)
        self.assertNotEqual(tx.state, 'done')

    def test_http_malformed_callback_does_not_500(self):
        resp = self.url_open(
            '/payment/nepal_wallets/esewa/return?data=!!!GARBAGE!!!',
            allow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))

    def test_http_end_to_end_backtest_all_three_wallets(self):
        tx_e = self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='BACKTEST-ESW-001',
            amount=150.0,
            currency_id=self.currency_npr.id,
        )
        r_e = tx_e._get_specific_rendering_values({})
        self.env.cr.flush()
        page_e = self.url_open('/payment/nepal_wallets/esewa/simulation', data={'transaction_uuid': r_e['transaction_uuid']})
        self.assertEqual(page_e.status_code, 200)
        self.assertIn('nw-card', page_e.text)
        csrf_e = self._extract_csrf_token(page_e.text)

        res_e = self.url_open(
            '/payment/nepal_wallets/esewa/simulation/process',
            data={'csrf_token': csrf_e, 'action': 'complete', 'transaction_uuid': r_e['transaction_uuid']},
            allow_redirects=False,
        )
        self.assertIn(res_e.status_code, (302, 303))
        self.url_open(res_e.headers['Location'], allow_redirects=False)
        tx_e.invalidate_recordset()
        self.assertEqual(tx_e.state, 'done')

        tx_k = self._create_transaction(
            flow='redirect',
            provider_id=self.khalti_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_khalti').id,
            reference='BACKTEST-KHL-001',
            amount=225.50,
            currency_id=self.currency_npr.id,
        )
        r_k = tx_k._get_specific_rendering_values({})
        pidx = r_k['query_params']['pidx']
        self.env.cr.flush()
        page_k = self.url_open(f'/payment/nepal_wallets/khalti/simulation?pidx={pidx}')
        self.assertEqual(page_k.status_code, 200)
        self.assertIn('nw-card', page_k.text)
        csrf_k = self._extract_csrf_token(page_k.text)

        self.url_open(
            '/payment/nepal_wallets/khalti/simulation/process',
            data={'csrf_token': csrf_k, 'action': 'complete', 'pidx': pidx},
            allow_redirects=False,
        )
        tx_k.invalidate_recordset()
        self.assertEqual(tx_k.state, 'done')

        tx_c = self._create_transaction(
            flow='redirect',
            provider_id=self.connectips_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_connectips').id,
            reference='BACKTEST-CIPS-001',
            amount=375.25,
            currency_id=self.currency_npr.id,
        )
        r_c = tx_c._get_specific_rendering_values({})
        self.env.cr.flush()
        page_c = self.url_open('/payment/nepal_wallets/connectips/simulation', data={'TXNID': r_c['TXNID']})
        self.assertEqual(page_c.status_code, 200)
        self.assertIn('nw-card', page_c.text)
        csrf_c = self._extract_csrf_token(page_c.text)

        self.url_open(
            '/payment/nepal_wallets/connectips/simulation/process',
            data={'csrf_token': csrf_c, 'action': 'complete', 'TXNID': r_c['TXNID']},
            allow_redirects=False,
        )
        tx_c.invalidate_recordset()
        self.assertEqual(tx_c.state, 'done')
