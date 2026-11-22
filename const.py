CODE_ESEWA = 'nepal_esewa'
CODE_KHALTI = 'nepal_khalti'
CODE_CONNECTIPS = 'nepal_connectips'

PROVIDER_CODES = (CODE_ESEWA, CODE_KHALTI, CODE_CONNECTIPS)

SUPPORTED_CURRENCIES = ('NPR',)

DEFAULT_PAYMENT_METHOD_CODES = {
    CODE_ESEWA: {'nepal_esewa'},
    CODE_KHALTI: {'nepal_khalti'},
    CODE_CONNECTIPS: {'nepal_connectips'},
}

# eSewa (ePay v2)
ESEWA_UAT_FORM_URL = 'https://rc-epay.esewa.com.np/api/epay/main/v2/form'
ESEWA_LIVE_FORM_URL = 'https://epay.esewa.com.np/api/epay/main/v2/form'
ESEWA_UAT_STATUS_URL = 'https://rc-epay.esewa.com.np/api/epay/transaction/status/'
ESEWA_LIVE_STATUS_URL = 'https://epay.esewa.com.np/api/epay/transaction/status/'

ESEWA_DEFAULT_TEST_PRODUCT_CODE = 'EPAYTEST'
ESEWA_DEFAULT_TEST_SECRET_KEY = '8gBm/:&EnhH.1/q'
ESEWA_REQUEST_SIGNED_FIELDS = 'total_amount,transaction_uuid,product_code'

# Khalti (KPG-2)
KHALTI_UAT_INITIATE_URL = 'https://dev.khalti.com/api/v2/epayment/initiate/'
KHALTI_LIVE_INITIATE_URL = 'https://khalti.com/api/v2/epayment/initiate/'
KHALTI_UAT_LOOKUP_URL = 'https://dev.khalti.com/api/v2/epayment/lookup/'
KHALTI_LIVE_LOOKUP_URL = 'https://khalti.com/api/v2/epayment/lookup/'

KHALTI_MIN_AMOUNT_PAISA = 1000  # Rs. 10.00 minimum per KPG-2 specification

# ConnectIPS (NCHL)
CONNECTIPS_UAT_GATEWAY_URL = 'https://uat.connectips.com:7443/connectipswebgw/loginpage'
CONNECTIPS_LIVE_GATEWAY_URL = 'https://login.connectips.com/connectipswebgw/loginpage'
CONNECTIPS_UAT_VALIDATE_URL = 'https://uat.connectips.com:7443/connectipswebws/api/creditor/validatetxn'
CONNECTIPS_LIVE_VALIDATE_URL = 'https://gateway.connectips.com/connectipswebws/api/creditor/validatetxn'

CONNECTIPS_TXNID_MAX_LEN = 20
CONNECTIPS_REFERENCEID_MAX_LEN = 20
CONNECTIPS_REMARKS_MAX_LEN = 50
CONNECTIPS_PARTICULARS_MAX_LEN = 100
CONNECTIPS_APPNAME_MAX_LEN = 30

DEFAULT_API_TIMEOUT = 15

MAX_IDENTIFIER_LEN = 256

def is_valid_identifier(value, max_len=MAX_IDENTIFIER_LEN):
    if not isinstance(value, str):
        return False
    stripped = value.strip()
    if not stripped or len(value) > max_len:
        return False
    return not any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
