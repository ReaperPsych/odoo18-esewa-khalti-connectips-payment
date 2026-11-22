# Nepal Wallets — Odoo 18 Payment Gateway Suite

A unified, security-hardened **Odoo 18** payment provider addon integrating Nepal's three major digital payment gateways into a single module:

- **eSewa (ePay v2)** — HMAC-SHA256 signed checkout + Server-to-Server Status Check API
- **Khalti by IME (ePayment v2 / KPG-2)** — Server-to-Server Initiate & Lookup APIs with paisa precision
- **ConnectIPS (NCHL)** — `SHA256withRSA` digital signatures via `.pfx` or `.pem` private keys + HTTP Basic Auth `validatetxn` API

---

## Gateway Architecture & Security Overview

| Gateway | Protocol & Auth | Signature / Encryption | Server-to-Server Verification | Currency |
|---|---|---|---|---|
| **eSewa (ePay v2)** | Form POST + Base64 JSON Callback | `HMAC-SHA256` (Base64) + constant-time `hmac.compare_digest` | `GET /api/epay/transaction/status/` | `NPR` |
| **Khalti by IME (KPG-2)** | JSON REST API (`Authorization: Key`) | TLS + Server-side Secret Key (never exposed to browser) | `POST /api/v2/epayment/lookup/` | `NPR` (paisa, min Rs. 10) |
| **ConnectIPS (NCHL)** | Form POST (`x-www-form-urlencoded`) | `SHA256withRSA` (PKCS#12 `.pfx` or `.pem`) | `POST /connectipswebws/api/creditor/validatetxn` (Basic Auth) | `NPR` (paisa) |

### Security & Production Features
- **Safe Default Install:** All three providers install as `Disabled` and `Unpublished` in `Official UAT` mode so installing on a live server never exposes demo routes.
- **Replay & Double-Spend Prevention:** Immutable `nw_khalti_pidx` and `nw_gateway_txn_id` columns with PostgreSQL `UNIQUE` constraints, duplicate `transaction_id` rejection, and terminal-state guards preventing downgrade or resurrection.
- **Concurrency & Atomicity:** PostgreSQL row-level locking (`SELECT ... FOR UPDATE NOWAIT`) and per-callback DB savepoints (`cr.savepoint()`) so rejected callbacks never commit partial writes.
- **Strict Financial Validation:** Exact `Decimal(..., ROUND_HALF_UP)` arithmetic, strict NPR currency enforcement in UAT/Live, mandatory amount presence, and fail-closed gateway identity checks.
- **Web Hardening:** Strict redirect host whitelisting (`pay.khalti.com` in Live), `allow_redirects=False` on outbound API calls, CSRF protection on simulation routes, HTTP Parameter Pollution (HPP) checks, and NUL-byte / oversized identifier filtering before ORM queries.
- **Abandoned-Browser Recovery:** Built-in 15-minute scheduled action (`ir.cron`) that reconciles `draft`/`pending` UAT & Live transactions via gateway status APIs if a customer closes their browser before redirecting back.

---

## Prerequisites

- **Odoo 18.0** (Community, Enterprise Self-Hosted, or Odoo.sh)
- **Odoo Core Dependencies:** `payment` (installed automatically; install `website_sale` for eCommerce checkout)
- **Python Libraries:** `cryptography`, `requests` (standard in Odoo 18 environments)

---

## Step-by-Step Installation

### Step 1 — Copy the Addon into `addons_path`
Place the `nepal_wallets` directory inside your Odoo 18 custom addons folder (e.g., `custom_addons/nepal_wallets/`).

Make sure `odoo.conf` includes your custom addons path:
```ini
[options]
addons_path = /path/to/odoo/addons,/path/to/custom_addons
```

### Step 2 — Restart Odoo & Install the Module
Restart your Odoo server, then either:
- **Via UI:** Enable **Developer Mode** → **Apps → Update Apps List** → search **Nepal Wallets** → click **Install**.
- **Via CLI:**
  ```bash
  ./odoo-bin -c odoo.conf -d <your_database> -i nepal_wallets
  ```

Once installed, **eSewa (Nepal Wallets)**, **Khalti by IME (Nepal Wallets)**, and **ConnectIPS (Nepal Wallets)** appear automatically under **Website / Invoicing → Configuration → Payment Providers**.

---

## Step-by-Step Configuration

### Step 1 — Configure NPR Currency (Required for Official UAT & Live)
Choose one of the two options:

- **Option A (Nepal-only store, fresh database):**
  1. Go to **Invoicing / Accounting → Configuration → Currencies** and activate **NPR**.
  2. Go to **Settings → Users & Companies → Companies → Your Company**, change **Currency** to **NPR**, and save.
- **Option B (Multi-currency store / existing accounting entries):**
  1. Activate **NPR** under **Invoicing → Configuration → Currencies** and add an exchange rate under **Rates** (e.g., `1 USD = 135 NPR`).
  2. Ensure **Multi Currencies** is enabled on your user, then enable **Pricelists** under **Website → Configuration → Settings**.
  3. Go to **Website → eCommerce → Pricelists → New**, create a **Nepal (NPR)** pricelist with **Currency = NPR**, link it to your website under the **Ecommerce** tab, save, and drag it to the top of the pricelist list.

---

### Step 2 — Quick Local Testing (No External Accounts Required)
To test the full Odoo checkout flow offline on `localhost`:
1. Open **Website → Configuration → Payment Providers** and click any wallet (**eSewa**, **Khalti by IME**, or **ConnectIPS**).
2. Set **State** → **`Test Mode`**.
3. Set **Test Environment Mode** → **`Local Interactive Simulation`**.
4. Click **Save**, then click the top-right **Unpublished** button so it turns green (**Published**).
5. Add a product to your cart and go to `/shop/payment` — clicking **Pay Now** opens the built-in styled simulation gateway where you can test **Confirm & Pay**, **Cancel**, and **Fail**.

---

### Step 3 — Official Sandbox / UAT Testing

#### 1. eSewa (ePay v2 UAT)
- **State:** `Test Mode` | **Test Environment Mode:** `Official Sandbox / UAT Gateway`
- **Product Code:** `EPAYTEST` *(pre-filled)*
- **Secret Key:** `8gBm/:&EnhH.1/q` *(pre-filled)*
- **Sandbox Buyer Login:** `9806800001`–`9806800005` | Password: `Nepal@123` | MPIN: `1122` | OTP: `123456`

#### 2. Khalti by IME (KPG-2 Sandbox)
1. Create a free sandbox merchant account at `https://test-admin.khalti.com/#/join/merchant` (OTP: `987654`).
2. Go to **Settings → Keys** and copy the **Live Secret Key**.
3. In Odoo: set **State** → `Test Mode`, **Test Environment Mode** → `Official Sandbox / UAT Gateway`, paste the **Khalti Secret Key**, save, and publish.
4. **Sandbox Buyer Login (Cart ≥ NPR 10.00):** `9800000000`–`9800000005` | MPIN: `1111` | OTP: `987654`

#### 3. ConnectIPS (NCHL UAT)
1. Obtain your UAT merchant pack from **NCHL** (`connectips.com`) or your settlement bank (`Merchant ID`, `App ID`, `App Name`, `API Password`, `CREDITOR.pfx` + `PFX Password`, and static IP whitelisting).
2. Enter the credentials and upload the `.pfx` file (or paste a `.pem` private key) in Odoo.
3. Click **Test ConnectIPS RSA Key** to verify the `SHA256withRSA` key loads cleanly, then set **State** → `Test Mode`, **Test Environment Mode** → `Official Sandbox / UAT Gateway`, save, and publish.

---

### Step 4 — Production Go-Live Checklist
1. Switch each provider's **State** to **`Enabled`** and enter your live merchant credentials (demo/test credentials are automatically rejected in Live mode).
2. Register your live HTTPS callback URLs with ConnectIPS / NCHL:
   - Success: `https://yourdomain.com/payment/nepal_wallets/connectips/return`
   - Failure: `https://yourdomain.com/payment/nepal_wallets/connectips/failure`
3. Deploy behind HTTPS (Nginx/Caddy with `proxy_mode = True` and rate limiting on `/payment/nepal_wallets/*`).
4. Disable `dev_mode` and set a strong `admin_passwd` in `odoo.conf`.
5. Confirm the scheduled action **Nepal Wallets: Reconcile Pending Gateway Transactions** is active under **Settings → Technical → Automation → Scheduled Actions**.

---

## Running Automated Tests

The addon ships with **46 automated tests (64 assertions)** covering unit logic, cryptographic vectors, white-box/grey-box/black-box security cases, reconciliation cron recovery, and end-to-end HTTP flows.

Run on a dedicated test database (never on production):
```bash
./odoo-bin -c odoo.conf -d nepal_wallets_test -i nepal_wallets --test-enable --test-tags /nepal_wallets --stop-after-init --http-port=8082
```

---

## Module Structure

```text
nepal_wallets/
├── __init__.py                         # post_init_hook & uninstall_hook
├── __manifest__.py                     # Module metadata & dependencies
├── const.py                            # Gateway URLs, limits, identifier validator
├── controllers/
│   └── main.py                         # Public callbacks & simulation routes
├── data/
│   ├── ir_cron_data.xml                # 15-min abandoned-browser reconciliation cron
│   ├── payment_method_data.xml         # eSewa, Khalti, ConnectIPS payment methods
│   └── payment_provider_data.xml       # Default disabled/unpublished provider records
├── models/
│   ├── payment_provider.py             # Credentials, HMAC-SHA256, KPG-2, RSA-SHA256
│   └── payment_transaction.py          # Initiation, S2S verification, locking & cron
├── static/
│   ├── description/                    # App icon & description HTML
│   └── img/                            # Wallet icons
├── tests/                              # 46 unit, security & HTTP backtest cases
└── views/
    ├── nepal_wallets_templates.xml     # Redirect forms & local simulation checkout UI
    └── payment_provider_views.xml      # Provider configuration form views
```

---

## Author & License

- **Author:** [Bibeek Kumar Labh](https://bibekklabh.com.np)
- **License:** **LGPL-3** ([LICENSE](LICENSE)) — Free and open-source. Anyone may use, modify, distribute, and deploy this module for personal or commercial/business purposes.
