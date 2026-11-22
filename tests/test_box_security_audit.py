import base64
import json
import re
from unittest.mock import MagicMock, patch

from odoo.exceptions import ValidationError
from odoo.tests import HttpCase, tagged

from odoo.addons.nepal_wallets import const
from odoo.addons.nepal_wallets.tests.common import NepalWalletsCommon

@tagged('post_install', '-at_install')
class TestNepalWalletsWhiteBox(NepalWalletsCommon):

    def test_wb_01_connectips_delimiter_injection_and_production_guards(self):
        self.connectips_provider.nw_connectips_app_name = 'Evil,TXNAMT=1,Store'
        tx = self._create_transaction(
            flow='redirect',
            provider_id=self.connectips_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_connectips').id,
            reference='REF,INJ=1-001',
            amount=500.0,
            currency_id=self.currency_npr.id,
        )
        rendering = tx._get_specific_rendering_values({})
        self.assertNotIn(',', rendering['APPNAME'])
        self.assertNotIn('=', rendering['APPNAME'])
        self.assertNotIn(',', rendering['PARTICULARS'])
        self.assertNotIn('=', rendering['PARTICULARS'])

        plaintext = self.connectips_provider._nw_connectips_build_initiation_message(rendering)
        self.assertEqual(len(plaintext.split(',')), 11)
        self.assertTrue(
            self.connectips_provider._nw_connectips_verify_signature(
                plaintext, rendering['TOKEN'], public_key=self.rsa_public_key
            )
        )

        with self.assertRaises(ValidationError):
            self.connectips_provider.state = 'enabled'

        with self.assertRaises(ValidationError):
            self.khalti_provider.write({
                'state': 'enabled',
                'nw_khalti_secret_key': 'test_secret_key_should_fail',
            })

    def test_wb_02_khalti_evilkhalti_domain_bypass_and_strict_amounts(self):
        self.khalti_provider.nw_test_mode = 'uat'
        tx_k = self._create_transaction(
            flow='redirect',
            provider_id=self.khalti_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_khalti').id,
            reference='WB-DOMAIN-001',
            amount=100.0,
            currency_id=self.currency_npr.id,
        )
        spoofed_resp = MagicMock()
        spoofed_resp.status_code = 200
        spoofed_resp.content = b'{"pidx": "x"}'
        spoofed_resp.json.return_value = {
            'pidx': 'pidx_bypass',
            'payment_url': 'https://evilkhalti.com/?pidx=pidx_bypass',
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=spoofed_resp):
            with self.assertRaises(ValidationError):
                tx_k._get_specific_rendering_values({})

        tx_zero_e = self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='WB-ZERO-ESW',
            amount=0.0,
            currency_id=self.currency_npr.id,
        )
        with self.assertRaises(ValidationError):
            tx_zero_e._get_specific_rendering_values({})

        tx_drift = self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='WB-DRIFT-ESW',
            amount=100.00,
            currency_id=self.currency_npr.id,
        )
        with self.assertRaises(ValidationError):
            tx_drift._nw_assert_amount_matches('100.01', 'eSewa')

    def test_wb_03_cross_transaction_pidx_and_gateway_identity_binding(self):
        self.khalti_provider.nw_test_mode = 'uat'
        tx_k = self._create_transaction(
            flow='redirect',
            provider_id=self.khalti_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_khalti').id,
            reference='WB-SWAP-KHL',
            amount=500.0,
            currency_id=self.currency_npr.id,
        )
        tx_k.nw_khalti_pidx = 'pidx_legitimate_order_B'
        tx_k.provider_reference = 'pidx_legitimate_order_B'
        with self.assertRaises(ValidationError):
            tx_k._handle_notification_data(const.CODE_KHALTI, {
                'pidx': 'pidx_foreign_cheap_order_A',
                'purchase_order_id': 'WB-SWAP-KHL',
            })

        self.esewa_provider.nw_test_mode = 'uat'
        tx_e = self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='WB-IDBIND-ESW',
            amount=200.0,
            currency_id=self.currency_npr.id,
        )
        r_e = tx_e._get_specific_rendering_values({})
        sf = 'transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names'
        cb = {
            'transaction_code': 'ESW111',
            'status': 'COMPLETE',
            'total_amount': '200',
            'transaction_uuid': r_e['transaction_uuid'],
            'product_code': 'EPAYTEST',
            'signed_field_names': sf,
        }
        cb['signature'] = self.esewa_provider._nw_esewa_generate_signature(
            ','.join(f"{k}={cb[k]}" for k in sf.split(','))
        )
        bad_status = MagicMock()
        bad_status.raise_for_status.return_value = None
        bad_status.json.return_value = {
            'status': 'COMPLETE',
            'total_amount': 200.0,
            'transaction_uuid': 'DIFFERENT-TXN-UUID',
            'product_code': 'EPAYTEST',
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.get', return_value=bad_status):
            with self.assertRaises(ValidationError):
                tx_e._handle_notification_data(const.CODE_ESEWA, cb)

        self.connectips_provider.nw_test_mode = 'uat'
        tx_c = self._create_transaction(
            flow='redirect',
            provider_id=self.connectips_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_connectips').id,
            reference='WB-IDBIND-CIPS',
            amount=200.0,
            currency_id=self.currency_npr.id,
        )
        r_c = tx_c._get_specific_rendering_values({})
        bad_val = MagicMock()
        bad_val.status_code = 200
        bad_val.content = b'{"status":"SUCCESS"}'
        bad_val.json.return_value = {
            'merchantId': 9999,  # Wrong merchant!
            'appId': 'MER-550-APP-1',
            'referenceId': r_c['TXNID'],
            'txnAmt': '20000',
            'txnCrncy': 'NPR',
            'status': 'SUCCESS',
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=bad_val):
            with self.assertRaises(ValidationError):
                tx_c._handle_notification_data(const.CODE_CONNECTIPS, {'TXNID': r_c['TXNID']})

@tagged('post_install', '-at_install')
class TestNepalWalletsGreyAndBlackBox(HttpCase, NepalWalletsCommon):

    def _extract_csrf_token(self, html_text):
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_text)
        self.assertTrue(match, "Expected csrf_token hidden input in HTML form")
        return match.group(1)

    def _get_session_csrf_token(self):
        return self._extract_csrf_token(self.url_open('/web/login').text)

    def test_gb_01_esewa_unsigned_raw_query_return_ignored(self):
        tx = self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='GB-RAW-ESW',
            amount=400.0,
            currency_id=self.currency_npr.id,
        )
        r = tx._get_specific_rendering_values({})
        self.env.cr.flush()

        resp = self.url_open(
            f"/payment/nepal_wallets/esewa/return?transaction_uuid={r['transaction_uuid']}&status=COMPLETE&total_amount=400",
            allow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        tx.invalidate_recordset()
        self.assertEqual(tx.state, 'draft')

    def test_gb_02_client_tampered_simulation_amount_ignored(self):
        tx_k = self._create_transaction(
            flow='redirect',
            provider_id=self.khalti_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_khalti').id,
            reference='GB-AMT-KHL',
            amount=150.0,
            currency_id=self.currency_npr.id,
        )
        r_k = tx_k._get_specific_rendering_values({})
        pidx = r_k['query_params']['pidx']
        self.env.cr.flush()

        page_k = self.url_open(f'/payment/nepal_wallets/khalti/simulation?pidx={pidx}')
        csrf_k = self._extract_csrf_token(page_k.text)
        self.url_open(
            '/payment/nepal_wallets/khalti/simulation/process',
            data={'csrf_token': csrf_k, 'action': 'complete', 'pidx': pidx, 'total_amount': '1'},
            allow_redirects=False,
        )
        tx_k.invalidate_recordset()
        self.assertEqual(tx_k.state, 'done')
        self.assertEqual(tx_k.amount, 150.0)

    def test_gb_03_uat_lockout_across_all_six_simulation_endpoints(self):
        self.esewa_provider.nw_test_mode = 'uat'
        self.khalti_provider.nw_test_mode = 'uat'
        self.connectips_provider.nw_test_mode = 'uat'

        tx_e = self._create_transaction(
            flow='redirect', provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='GB-LOCK-E', amount=100.0, currency_id=self.currency_npr.id,
        )
        r_e = tx_e._get_specific_rendering_values({})

        tx_k = self._create_transaction(
            flow='redirect', provider_id=self.khalti_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_khalti').id,
            reference='GB-LOCK-K', amount=100.0, currency_id=self.currency_npr.id,
        )
        tx_k.nw_khalti_pidx = 'pidx_uat_lock_001'
        tx_k.provider_reference = 'pidx_uat_lock_001'

        tx_c = self._create_transaction(
            flow='redirect', provider_id=self.connectips_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_connectips').id,
            reference='GB-LOCK-C', amount=100.0, currency_id=self.currency_npr.id,
        )
        r_c = tx_c._get_specific_rendering_values({})
        self.env.cr.flush()

        csrf_token = self._get_session_csrf_token()
        checks = [
            self.url_open('/payment/nepal_wallets/esewa/simulation', data={'transaction_uuid': r_e['transaction_uuid']}),
            self.url_open('/payment/nepal_wallets/esewa/simulation/process', data={'csrf_token': csrf_token, 'action': 'complete', 'transaction_uuid': r_e['transaction_uuid']}),
            self.url_open('/payment/nepal_wallets/khalti/simulation?pidx=pidx_uat_lock_001'),
            self.url_open('/payment/nepal_wallets/khalti/simulation/process', data={'csrf_token': csrf_token, 'action': 'complete', 'pidx': 'pidx_uat_lock_001'}),
            self.url_open('/payment/nepal_wallets/connectips/simulation', data={'TXNID': r_c['TXNID']}),
            self.url_open('/payment/nepal_wallets/connectips/simulation/process', data={'csrf_token': csrf_token, 'action': 'complete', 'TXNID': r_c['TXNID']}),
        ]
        for resp in checks:
            self.assertEqual(resp.status_code, 403)

    def test_bb_01_fuzzing_oversized_sqli_xss_callbacks_never_500(self):
        oversized = 'A' * 20000
        json_list_b64 = base64.b64encode(b'[1, 2, 3]').decode('utf-8')
        fuzz_urls = [
            f'/payment/nepal_wallets/esewa/return?data={oversized}',
            f'/payment/nepal_wallets/esewa/return?data={json_list_b64}',
            "/payment/nepal_wallets/esewa/cancel?transaction_uuid=' OR 1=1--",
            '/payment/nepal_wallets/khalti/return?pidx=<script>alert(1)</script>',
            "/payment/nepal_wallets/connectips/return?TXNID='; DROP TABLE payment_transaction;--",
            '/payment/nepal_wallets/connectips/failure?TXNID=UNKNOWN_999',
        ]
        for url in fuzz_urls:
            resp = self.url_open(url, allow_redirects=False)
            self.assertIn(resp.status_code, (302, 303), f"Unexpected status on {url}: {resp.status_code}")
            self.assertNotIn('<script>alert(1)</script>', resp.text)

    def test_bb_02_unknown_transaction_simulation_enumeration_blocked(self):
        csrf_token = self._get_session_csrf_token()
        probes = [
            self.url_open('/payment/nepal_wallets/esewa/simulation', data={'transaction_uuid': 'GUESS-0001'}),
            self.url_open('/payment/nepal_wallets/esewa/simulation/process', data={'csrf_token': csrf_token, 'action': 'complete', 'transaction_uuid': ''}),
            self.url_open('/payment/nepal_wallets/khalti/simulation?pidx=SIMPIDX-FAKE'),
            self.url_open('/payment/nepal_wallets/khalti/simulation/process', data={'csrf_token': csrf_token, 'action': 'complete', 'pidx': 'SIMPIDX-FAKE'}),
            self.url_open('/payment/nepal_wallets/connectips/simulation', data={'TXNID': 'FAKE-TXN'}),
            self.url_open('/payment/nepal_wallets/connectips/simulation/process', data={'csrf_token': csrf_token, 'action': 'complete', 'TXNID': 'FAKE-TXN'}),
        ]
        for resp in probes:
            self.assertEqual(resp.status_code, 403)

    def test_bb_03_unauthenticated_cancel_flood_cannot_downgrade_paid_transactions(self):
        tx_e = self._create_transaction(
            flow='redirect', provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='BB-FLOOD-E', amount=100.0, currency_id=self.currency_npr.id,
        )
        r_e = tx_e._get_specific_rendering_values({})
        tx_e._set_done()

        tx_c = self._create_transaction(
            flow='redirect', provider_id=self.connectips_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_connectips').id,
            reference='BB-FLOOD-C', amount=100.0, currency_id=self.currency_npr.id,
        )
        r_c = tx_c._get_specific_rendering_values({})
        tx_c._set_done()
        self.env.cr.flush()

        self.url_open(f"/payment/nepal_wallets/esewa/cancel?transaction_uuid={r_e['transaction_uuid']}", allow_redirects=False)
        self.url_open(f"/payment/nepal_wallets/connectips/failure?TXNID={r_c['TXNID']}", allow_redirects=False)

        tx_e.invalidate_recordset()
        tx_c.invalidate_recordset()
        self.assertEqual(tx_e.state, 'done')
        self.assertEqual(tx_c.state, 'done')
