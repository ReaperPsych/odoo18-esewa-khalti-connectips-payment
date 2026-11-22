import base64
import json
import os
import re
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from odoo.exceptions import AccessError, ValidationError
from odoo.tests import HttpCase, tagged

from odoo.addons.nepal_wallets import const
from odoo.addons.nepal_wallets.tests.common import NepalWalletsCommon

@tagged('post_install', '-at_install')
class TestNepalWalletsAuditFixes(NepalWalletsCommon):

    def _create_tx(self, provider, method_xmlid, reference, amount=100.0, currency=None):
        return self._create_transaction(
            flow='redirect',
            provider_id=provider.id,
            payment_method_id=self.env.ref(method_xmlid).id,
            reference=reference,
            amount=amount,
            currency_id=(currency or self.currency_npr).id,
        )

    def test_01_default_install_safety(self):
        field = self.env['payment.provider']._fields['nw_test_mode']
        self.assertEqual(field.default(self.env['payment.provider']), 'uat')

        xml_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            'data',
            'payment_provider_data.xml',
        )
        tree = ET.parse(xml_path)
        records = tree.findall('.//record[@model="payment.provider"]')
        self.assertEqual(len(records), 3)
        for rec in records:
            fields_map = {f.attrib['name']: (f.text or '').strip() for f in rec.findall('field')}
            self.assertEqual(fields_map.get('state'), 'disabled')
            self.assertEqual(fields_map.get('is_published'), 'False')
            self.assertEqual(fields_map.get('nw_test_mode'), 'uat')

    def test_02_khalti_pidx_replay_and_duplicate_txn_id_blocked(self):
        self.khalti_provider.nw_test_mode = 'uat'
        tx_a = self._create_tx(self.khalti_provider, 'nepal_wallets.payment_method_nepal_khalti', 'REPLAY-A', 200.0)
        tx_b = self._create_tx(self.khalti_provider, 'nepal_wallets.payment_method_nepal_khalti', 'REPLAY-B', 200.0)

        init_resp = MagicMock(status_code=200, content=b'{"pidx":"pidx_A"}')
        init_resp.json.return_value = {
            'pidx': 'pidx_A',
            'payment_url': 'https://test-pay.khalti.com/?pidx=pidx_A',
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=init_resp):
            tx_a._get_specific_rendering_values({})

        lookup_a = MagicMock(status_code=200, content=b'{"status":"Completed"}')
        lookup_a.json.return_value = {
            'pidx': 'pidx_A',
            'total_amount': 20000,
            'status': 'Completed',
            'transaction_id': 'KHL_UNIQUE_TXN_1',
            'refunded': False,
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=lookup_a):
            tx_a._handle_notification_data(const.CODE_KHALTI, {'pidx': 'pidx_A'})

        self.assertEqual(tx_a.state, 'done')
        self.assertEqual(tx_a.nw_khalti_pidx, 'pidx_A')
        self.assertEqual(tx_a.provider_reference, 'KHL_UNIQUE_TXN_1')

        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=lookup_a):
            self.env['payment.transaction']._handle_notification_data(
                const.CODE_KHALTI,
                {'pidx': 'pidx_A', 'purchase_order_id': tx_b.reference},
            )
        self.assertEqual(tx_b.state, 'draft')

        tx_b._nw_get_or_create_gateway_txn_id()
        with self.assertRaises(ValidationError):
            self.env['payment.transaction']._handle_notification_data(
                const.CODE_KHALTI,
                {'purchase_order_id': tx_b.nw_gateway_txn_id},
            )
        self.assertEqual(tx_b.state, 'draft')

        tx_b.nw_khalti_pidx = 'pidx_B'
        lookup_dup = MagicMock(status_code=200, content=b'{"status":"Completed"}')
        lookup_dup.json.return_value = {
            'pidx': 'pidx_B',
            'total_amount': 20000,
            'status': 'Completed',
            'transaction_id': 'KHL_UNIQUE_TXN_1',
            'refunded': False,
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=lookup_dup):
            with self.assertRaises(ValidationError):
                tx_b._handle_notification_data(const.CODE_KHALTI, {'pidx': 'pidx_B'})
        self.assertEqual(tx_b.state, 'draft')

    def test_03_reference_fallback_removed_and_esewa_hmac_required_for_all_statuses(self):
        tx_e = self._create_tx(self.esewa_provider, 'nepal_wallets.payment_method_nepal_esewa', 'PREDICT-ESW-001', 100.0)
        r_e = tx_e._get_specific_rendering_values({})

        with self.assertRaises(ValidationError):
            self.env['payment.transaction']._get_tx_from_notification_data(
                const.CODE_ESEWA, {'transaction_uuid': 'PREDICT-ESW-001'}
            )

        for bad_status in ('AMBIGUOUS', 'FAILED', 'CANCELED'):
            with self.assertRaises(ValidationError):
                tx_e._handle_notification_data(const.CODE_ESEWA, {
                    'transaction_uuid': r_e['transaction_uuid'],
                    'status': bad_status,
                })
        self.assertEqual(tx_e.state, 'draft')

        with self.assertRaises(ValidationError):
            tx_e._handle_notification_data(const.CODE_ESEWA, {
                'transaction_uuid': r_e['transaction_uuid'],
                'status': 'COMPLETE',
                '_nw_callback_type': 'cancel',
            })
        self.assertEqual(tx_e.state, 'draft')

    def test_04_uat_cancel_and_failure_verify_via_s2s_before_mutating(self):
        self.esewa_provider.nw_test_mode = 'uat'
        tx_e = self._create_tx(self.esewa_provider, 'nepal_wallets.payment_method_nepal_esewa', 'UAT-CAN-ESW', 100.0)
        r_e = tx_e._get_specific_rendering_values({})
        s2s_complete = MagicMock(status_code=200)
        s2s_complete.raise_for_status.return_value = None
        s2s_complete.json.return_value = {'status': 'COMPLETE', 'transaction_uuid': r_e['transaction_uuid']}
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.get', return_value=s2s_complete) as mock_get:
            tx_e._handle_notification_data(const.CODE_ESEWA, {
                'transaction_uuid': r_e['transaction_uuid'],
                'status': 'CANCELED',
                '_nw_callback_type': 'cancel',
            })
            mock_get.assert_called_once()
            self.assertEqual(mock_get.call_args.kwargs.get('allow_redirects'), False)
        self.assertEqual(tx_e.state, 'draft')

        self.connectips_provider.nw_test_mode = 'uat'
        tx_c = self._create_tx(self.connectips_provider, 'nepal_wallets.payment_method_nepal_connectips', 'UAT-FAIL-CIPS', 100.0)
        r_c = tx_c._get_specific_rendering_values({})
        s2s_cips = MagicMock(status_code=200, content=b'{"status":"SUCCESS"}')
        s2s_cips.json.return_value = {
            'merchantId': 550,
            'appId': 'MER-550-APP-1',
            'referenceId': r_c['TXNID'],
            'txnAmt': '10000',
            'txnCrncy': 'NPR',
            'status': 'SUCCESS',
            'statusDesc': 'SUCCESS',
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=s2s_cips) as mock_post:
            tx_c._handle_notification_data(const.CODE_CONNECTIPS, {
                'TXNID': r_c['TXNID'],
                '_nw_callback_type': 'failure',
                'status': 'FAILED',
            })
            mock_post.assert_called_once()
            self.assertEqual(mock_post.call_args.kwargs.get('allow_redirects'), False)
        self.assertEqual(tx_c.state, 'done')

    def test_05_non_npr_currency_rejected_in_uat_mode_and_connectips_validatetxn(self):
        currency_usd = self.env['res.currency'].with_context(active_test=False).search([('name', '=', 'USD')], limit=1)
        currency_usd.active = True

        self.esewa_provider.nw_test_mode = 'uat'
        self.khalti_provider.nw_test_mode = 'uat'
        self.connectips_provider.nw_test_mode = 'uat'

        for prov, xmlid, ref in (
            (self.esewa_provider, 'nepal_wallets.payment_method_nepal_esewa', 'USD-ESW'),
            (self.khalti_provider, 'nepal_wallets.payment_method_nepal_khalti', 'USD-KHL'),
            (self.connectips_provider, 'nepal_wallets.payment_method_nepal_connectips', 'USD-CIPS'),
        ):
            tx = self._create_tx(prov, xmlid, ref, 100.0, currency=currency_usd)
            with self.assertRaises(ValidationError):
                tx._get_specific_rendering_values({})

        tx_c = self._create_tx(self.connectips_provider, 'nepal_wallets.payment_method_nepal_connectips', 'CRNCY-CIPS', 100.0)
        r_c = tx_c._get_specific_rendering_values({})
        for bad_crncy in ('USD', 'EUR', 'INR'):
            bad_resp = MagicMock(status_code=200, content=b'{"status":"SUCCESS"}')
            bad_resp.json.return_value = {
                'merchantId': 550,
                'appId': 'MER-550-APP-1',
                'referenceId': r_c['TXNID'],
                'txnAmt': '10000',
                'txnCrncy': bad_crncy,
                'status': 'SUCCESS',
            }
            with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=bad_resp):
                with self.assertRaises(ValidationError):
                    tx_c._handle_notification_data(const.CODE_CONNECTIPS, {'TXNID': r_c['TXNID']})
        self.assertEqual(tx_c.state, 'draft')

    def test_06_cannot_resurrect_canceled_or_error_transaction(self):
        tx_k = self._create_tx(self.khalti_provider, 'nepal_wallets.payment_method_nepal_khalti', 'RESURRECT-KHL', 100.0)
        r_k = tx_k._get_specific_rendering_values({})
        tx_k._set_canceled()
        with self.assertRaises(ValidationError):
            tx_k._handle_notification_data(const.CODE_KHALTI, {
                'pidx': r_k['query_params']['pidx'],
                'status': 'Completed',
                'total_amount': 10000,
                'transaction_id': 'KHL_RES_1',
            })
        self.assertEqual(tx_k.state, 'cancel')

        tx_c = self._create_tx(self.connectips_provider, 'nepal_wallets.payment_method_nepal_connectips', 'RESURRECT-CIPS', 100.0)
        r_c = tx_c._get_specific_rendering_values({})
        tx_c._set_error("Previous failure")
        with self.assertRaises(ValidationError):
            tx_c._handle_notification_data(const.CODE_CONNECTIPS, {
                'TXNID': r_c['TXNID'],
                'status': 'SUCCESS',
                'txnAmt': 10000,
            })
        self.assertEqual(tx_c.state, 'error')

    def test_07_fail_closed_s2s_missing_identity_and_non_dict_json(self):
        self.khalti_provider.nw_test_mode = 'uat'
        tx_k = self._create_tx(self.khalti_provider, 'nepal_wallets.payment_method_nepal_khalti', 'FAILCLOSED-KHL', 100.0)
        tx_k.nw_khalti_pidx = 'pidx_fc_1'

        missing_pidx = MagicMock(status_code=200, content=b'{"status":"Completed"}')
        missing_pidx.json.return_value = {'status': 'Completed', 'total_amount': 10000, 'transaction_id': 'T1'}
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=missing_pidx):
            with self.assertRaises(ValidationError):
                tx_k._handle_notification_data(const.CODE_KHALTI, {'pidx': 'pidx_fc_1'})

        list_resp = MagicMock(status_code=200, content=b'[1,2,3]')
        list_resp.json.return_value = [1, 2, 3]
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=list_resp):
            with self.assertRaises(ValidationError):
                tx_k._handle_notification_data(const.CODE_KHALTI, {'pidx': 'pidx_fc_1'})

    def test_08_decimal_rounding_paisa_validation_and_non_ascii_ref(self):
        self.assertEqual(self.esewa_provider._nw_format_esewa_amount(10.005), '10.01')
        self.assertEqual(self.esewa_provider._nw_format_esewa_amount(100.0), '100')

        tx_tiny = self._create_tx(self.esewa_provider, 'nepal_wallets.payment_method_nepal_esewa', 'TINY-ESW', 0.001)
        with self.assertRaises(ValidationError):
            tx_tiny._get_specific_rendering_values({})

        tx_p = self._create_tx(self.khalti_provider, 'nepal_wallets.payment_method_nepal_khalti', 'PAISA-KHL', 10.0)
        for bad_val in (True, False, 1000.0, 1000.9, '1000.5', '-1000'):
            with self.assertRaises(ValidationError):
                tx_p._nw_assert_paisa_matches(bad_val, 'Khalti')
        tx_p._nw_assert_paisa_matches(1000, 'Khalti')
        tx_p._nw_assert_paisa_matches('1000', 'Khalti')

        tx_na = self._create_tx(self.connectips_provider, 'nepal_wallets.payment_method_nepal_connectips', 'अर्डर-१२३', 100.0)
        r_na = tx_na._get_specific_rendering_values({})
        self.assertFalse(r_na['TXNID'].startswith('-'))
        self.assertTrue(len(r_na['REFERENCEID']) > 0)

    def test_09_khalti_live_host_strict_and_connectips_key_hardening(self):
        self.khalti_provider.write({
            'state': 'enabled',
            'nw_khalti_secret_key': 'live_secret_key_real_999',
        })
        tx_k = self._create_tx(self.khalti_provider, 'nepal_wallets.payment_method_nepal_khalti', 'LIVE-KHL-HOST', 100.0)
        for bad_url in (
            'https://test-pay.khalti.com/?pidx=p1',
            'https://user@pay.khalti.com/?pidx=p1',
            'https://pay.khalti.com:8443/?pidx=p1',
        ):
            resp = MagicMock(status_code=200, content=b'{"pidx":"p1"}')
            resp.json.return_value = {'pidx': 'p1', 'payment_url': bad_url}
            with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=resp):
                with self.assertRaises(ValidationError):
                    tx_k._get_specific_rendering_values({})

        self.connectips_provider.write({
            'nw_connectips_pfx_file': False,
            'nw_connectips_pfx_password': 'leftover_pfx_password',
            'nw_connectips_private_key_pem': self.pem_private_key,
        })
        key = self.connectips_provider._nw_connectips_load_private_key()
        self.assertIsNotNone(key)

        ec_key = ec.generate_private_key(ec.SECP256R1())
        ec_pem = ec_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode('utf-8')
        self.connectips_provider.nw_connectips_private_key_pem = ec_pem
        with self.assertRaises(ValidationError):
            self.connectips_provider._nw_connectips_load_private_key()

        portal_user = self.env['res.users'].create({
            'name': 'Non Admin User',
            'login': 'nw_non_admin_test',
            'groups_id': [(6, 0, [self.env.ref('base.group_user').id])],
        })
        with self.assertRaises(AccessError):
            self.connectips_provider.with_user(portal_user).action_nw_test_connectips_key()

    def test_10_refund_statuses_handled_explicitly(self):
        tx_k = self._create_tx(self.khalti_provider, 'nepal_wallets.payment_method_nepal_khalti', 'PARTREF-KHL', 100.0)
        r_k = tx_k._get_specific_rendering_values({})
        tx_k._handle_notification_data(const.CODE_KHALTI, {
            'pidx': r_k['query_params']['pidx'],
            'status': 'Partially refunded',
            'total_amount': 10000,
        })
        self.assertEqual(tx_k.state, 'cancel')
        self.assertIn('partially refunded', (tx_k.state_message or '').lower())

    def test_11_connectips_validatetxn_omitted_txncrncy_allowed_and_usd_rejected(self):
        self.connectips_provider.nw_test_mode = 'uat'

        tx_ok = self._create_tx(self.connectips_provider, 'nepal_wallets.payment_method_nepal_connectips', 'CIPS-NOCRNCY', 100.0)
        r_ok = tx_ok._get_specific_rendering_values({})
        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.content = b'{"status":"SUCCESS"}'
        resp_ok.json.return_value = {
            'merchantId': 550,
            'appId': 'MER-550-APP-1',
            'referenceId': r_ok['TXNID'],
            'txnAmt': '10000',
            'status': 'SUCCESS',
            'statusDesc': 'TRANSACTION SUCCESSFUL',
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=resp_ok):
            tx_ok._handle_notification_data(const.CODE_CONNECTIPS, {'TXNID': r_ok['TXNID']})
        self.assertEqual(tx_ok.state, 'done')

        tx_bad = self._create_tx(self.connectips_provider, 'nepal_wallets.payment_method_nepal_connectips', 'CIPS-USDCRNCY', 100.0)
        r_bad = tx_bad._get_specific_rendering_values({})
        resp_bad = MagicMock()
        resp_bad.status_code = 200
        resp_bad.content = b'{"status":"SUCCESS"}'
        resp_bad.json.return_value = {
            'merchantId': 550,
            'appId': 'MER-550-APP-1',
            'referenceId': r_bad['TXNID'],
            'txnAmt': '10000',
            'txnCrncy': 'USD',
            'status': 'SUCCESS',
        }
        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', return_value=resp_bad):
            with self.assertRaises(ValidationError):
                tx_bad._handle_notification_data(const.CODE_CONNECTIPS, {'TXNID': r_bad['TXNID']})

    def test_12_reconciliation_cron_recovers_abandoned_uat_transactions(self):
        self.esewa_provider.nw_test_mode = 'uat'
        self.khalti_provider.nw_test_mode = 'uat'
        self.connectips_provider.nw_test_mode = 'uat'

        tx_e = self._create_tx(self.esewa_provider, 'nepal_wallets.payment_method_nepal_esewa', 'CRON-ESW-01', 150.0)
        r_e = tx_e._get_specific_rendering_values({})

        tx_k = self._create_tx(self.khalti_provider, 'nepal_wallets.payment_method_nepal_khalti', 'CRON-KHL-01', 200.0)
        tx_k.nw_khalti_pidx = 'pidx_cron_recover_01'

        tx_c = self._create_tx(self.connectips_provider, 'nepal_wallets.payment_method_nepal_connectips', 'CRON-CIPS-01', 250.0)
        r_c = tx_c._get_specific_rendering_values({})

        esewa_resp = MagicMock(status_code=200)
        esewa_resp.raise_for_status.return_value = None
        esewa_resp.json.return_value = {
            'product_code': 'EPAYTEST',
            'transaction_uuid': r_e['transaction_uuid'],
            'total_amount': 150.0,
            'status': 'COMPLETE',
            'ref_id': 'ESWCRON01',
        }

        khalti_resp = MagicMock(status_code=200, content=b'{"status":"Completed"}')
        khalti_resp.json.return_value = {
            'pidx': 'pidx_cron_recover_01',
            'total_amount': 20000,
            'status': 'Completed',
            'transaction_id': 'KHLCRON01',
            'refunded': False,
        }

        cips_resp = MagicMock(status_code=200, content=b'{"status":"SUCCESS"}')
        cips_resp.json.return_value = {
            'merchantId': 550,
            'appId': 'MER-550-APP-1',
            'referenceId': r_c['TXNID'],
            'txnAmt': '25000',
            'status': 'SUCCESS',
            'statusDesc': 'TRANSACTION SUCCESSFUL',
        }

        def fake_post(url, **kwargs):
            if 'khalti.com' in url:
                return khalti_resp
            return cips_resp

        with patch('odoo.addons.nepal_wallets.models.payment_provider.requests.get', return_value=esewa_resp), \
             patch('odoo.addons.nepal_wallets.models.payment_provider.requests.post', side_effect=fake_post):
            count = self.env['payment.transaction']._nw_cron_reconcile_pending_transactions()

        self.assertGreaterEqual(count, 3)
        self.assertEqual(tx_e.state, 'done')
        self.assertEqual(tx_k.state, 'done')
        self.assertEqual(tx_c.state, 'done')

@tagged('post_install', '-at_install')
class TestNepalWalletsBlackBoxAuditFixes(HttpCase, NepalWalletsCommon):

    def _extract_csrf_token(self, html_text):
        match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_text)
        self.assertTrue(match, "Expected csrf_token hidden input in HTML form")
        return match.group(1)

    def test_bb_fix_01_nul_bytes_and_oversized_ids_never_500(self):
        nul_b64 = base64.b64encode(json.dumps({'status': 'COMPLETE', 'transaction_uuid': 'abc\x00def'}).encode()).decode()
        big = 'X' * 500
        callback_urls = [
            f'/payment/nepal_wallets/esewa/return?data={nul_b64}',
            '/payment/nepal_wallets/esewa/cancel?transaction_uuid=TEST%00REF',
            '/payment/nepal_wallets/khalti/return?pidx=test%00pidx&purchase_order_id=ORD%001',
            '/payment/nepal_wallets/connectips/return?TXNID=TXN%001',
            '/payment/nepal_wallets/connectips/failure?TXNID=TXN%001',
            f'/payment/nepal_wallets/khalti/return?pidx={big}',
            f'/payment/nepal_wallets/connectips/return?TXNID={big}',
        ]
        for url in callback_urls:
            resp = self.url_open(url, allow_redirects=False)
            self.assertIn(resp.status_code, (302, 303), f"Unexpected status {resp.status_code} for {url}")

        sim_checks = [
            self.url_open('/payment/nepal_wallets/esewa/simulation', data={'transaction_uuid': 'A\x00B'}),
            self.url_open('/payment/nepal_wallets/khalti/simulation?pidx=A%00B'),
            self.url_open('/payment/nepal_wallets/connectips/simulation', data={'TXNID': 'A\x00B'}),
            self.url_open('/payment/nepal_wallets/esewa/simulation', data={'transaction_uuid': big}),
        ]
        for resp in sim_checks:
            self.assertEqual(resp.status_code, 403)

    def test_bb_fix_02_hpp_duplicate_and_conflicting_params_rejected(self):
        tx_e = self._create_transaction(
            flow='redirect',
            provider_id=self.esewa_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_esewa').id,
            reference='HPP-ESW-001',
            amount=100.0,
            currency_id=self.currency_npr.id,
        )
        r_e = tx_e._get_specific_rendering_values({})
        sf = 'transaction_code,status,total_amount,transaction_uuid,product_code,signed_field_names'
        payload = {
            'transaction_code': 'ESW_HPP',
            'status': 'COMPLETE',
            'total_amount': '100',
            'transaction_uuid': r_e['transaction_uuid'],
            'product_code': 'EPAYTEST',
            'signed_field_names': sf,
        }
        payload['signature'] = self.esewa_provider._nw_esewa_generate_signature(
            ','.join(f"{k}={payload[k]}" for k in sf.split(','))
        )
        valid_b64 = base64.b64encode(json.dumps(payload).encode()).decode()
        self.env.cr.flush()

        resp = self.url_open(
            '/payment/nepal_wallets/esewa/return?data=invalid1',
            data={'data': valid_b64},
            allow_redirects=False,
        )
        self.assertIn(resp.status_code, (302, 303))
        tx_e.invalidate_recordset()
        self.assertEqual(tx_e.state, 'draft')

    def test_bb_fix_03_simulation_csrf_state_guard_and_no_credential_leak(self):
        tx_c = self._create_transaction(
            flow='redirect',
            provider_id=self.connectips_provider.id,
            payment_method_id=self.env.ref('nepal_wallets.payment_method_nepal_connectips').id,
            reference='SIM-HARD-CIPS',
            amount=100.0,
            currency_id=self.currency_npr.id,
        )
        r_c = tx_c._get_specific_rendering_values({})
        self.env.cr.flush()

        page = self.url_open('/payment/nepal_wallets/connectips/simulation', data={'TXNID': r_c['TXNID']})
        self.assertEqual(page.status_code, 200)
        self.assertNotIn('MER-550-APP-1', page.text)

        no_csrf = self.url_open(
            '/payment/nepal_wallets/connectips/simulation/process',
            data={'action': 'complete', 'TXNID': r_c['TXNID']},
            allow_redirects=False,
        )
        self.assertEqual(no_csrf.status_code, 400)
        tx_c.invalidate_recordset()
        self.assertEqual(tx_c.state, 'draft')

        csrf = self._extract_csrf_token(page.text)
        ok = self.url_open(
            '/payment/nepal_wallets/connectips/simulation/process',
            data={'csrf_token': csrf, 'action': 'complete', 'TXNID': r_c['TXNID']},
            allow_redirects=False,
        )
        self.assertIn(ok.status_code, (302, 303))
        tx_c.invalidate_recordset()
        self.assertEqual(tx_c.state, 'done')

        replay_sim = self.url_open('/payment/nepal_wallets/connectips/simulation', data={'TXNID': r_c['TXNID']})
        self.assertEqual(replay_sim.status_code, 403)
