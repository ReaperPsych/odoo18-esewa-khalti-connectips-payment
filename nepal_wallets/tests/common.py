import base64
import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from odoo.addons.payment.tests.common import PaymentCommon

from odoo.addons.nepal_wallets import const

class NepalWalletsCommon(PaymentCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        cls.currency_npr = cls.env['res.currency'].with_context(active_test=False).search(
            [('name', '=', 'NPR')], limit=1
        )
        cls.currency_npr.active = True
        cls.currency = cls.currency_npr

        cls.esewa_provider = cls._prepare_provider(const.CODE_ESEWA, update_values={
            'state': 'test',
            'nw_test_mode': 'simulation',
            'nw_esewa_product_code': const.ESEWA_DEFAULT_TEST_PRODUCT_CODE,
            'nw_esewa_secret_key': const.ESEWA_DEFAULT_TEST_SECRET_KEY,
        })
        cls.khalti_provider = cls._prepare_provider(const.CODE_KHALTI, update_values={
            'state': 'test',
            'nw_test_mode': 'simulation',
            'nw_khalti_secret_key': 'test_secret_key_demo_123456',
        })

        cls.rsa_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.rsa_public_key = cls.rsa_private_key.public_key()
        cls.pfx_password = 'test_pfx_password'

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, 'NP'),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, 'Test Merchant'),
            x509.NameAttribute(NameOID.COMMON_NAME, 'connectips.test'),
        ])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(cls.rsa_public_key)
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=365))
            .sign(cls.rsa_private_key, hashes.SHA256())
        )
        pfx_bytes = pkcs12.serialize_key_and_certificates(
            name=b'connectips-test',
            key=cls.rsa_private_key,
            cert=cert,
            cas=None,
            encryption_algorithm=serialization.BestAvailableEncryption(cls.pfx_password.encode('utf-8')),
        )
        cls.pfx_b64 = base64.b64encode(pfx_bytes).decode('utf-8')
        cls.pem_private_key = cls.rsa_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode('utf-8')

        cls.connectips_provider = cls._prepare_provider(const.CODE_CONNECTIPS, update_values={
            'state': 'test',
            'nw_test_mode': 'simulation',
            'nw_connectips_merchant_id': '550',
            'nw_connectips_app_id': 'MER-550-APP-1',
            'nw_connectips_app_name': 'Odoo Test Store',
            'nw_connectips_app_password': 'test_api_password',
            'nw_connectips_pfx_file': cls.pfx_b64,
            'nw_connectips_pfx_password': cls.pfx_password,
        })
