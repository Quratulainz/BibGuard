# BibGuard — Free Beta

Run `pip install -r requirements.txt` and `python app.py`, then open
`http://localhost:5000`. A local guest check needs no account or payment
credentials. Try DOI `10.1038/nature14539` in Find a paper.

## Beta settings

- `CREDITS_ENABLED=0` (default) disables checkout, settlement endpoints and credit grants.
- `BETA_DAILY_FILES=5` allows five file checks per network per day, shared by guests and signed-in users. The app still applies its network-wide cap.
- In beta, checks do not use account balances. Keep the same persistent `DB_PATH` to retain existing accounts, balances and payment history.
- To enable credits later, configure and test a payment provider in its sandbox, then set `CREDITS_ENABLED=1` and restart. Use the payment setup below only when testing or enabling credits.
- For a public deployment, set `HTTPS=1`, `BASE_URL` to the public HTTPS origin, and the same strong random `SECRET_KEY` on every worker. Set `TRUSTED_PROXY_HOPS` only when you know how many trusted proxies append the client address and the app can only be reached through those proxies. Use a production WSGI server and persistent storage.
- Admin uses HTTP Basic authentication with username `admin` and password `ADMIN_KEY`. Leave the admin disabled unless needed. Use HTTPS on a public host.
- `OPENALEX_API_KEY` is optional for basic testing. Use an API key for sustained OpenAlex use.

BibTeX can receive a **Verified** verdict when the normalized title and supplied
metadata match. DOI-only, plain-text and PDF matches should be reviewed
manually. The **Download corrected BibTeX** button updates supported bibliographic fields
when a BibTeX title agrees with the registry and either its DOI or full surname
list agrees. Citation keys and unrelated fields are preserved. Uncertain entries
are retained and flagged in the download or report. Pasted or PDF references
with a confident DOI match generate BibTeX entries. Other references are
included as comments for manual review. The CSV lists the fields updated.
PDF checking requires an extractable References or Bibliography heading.

The website submits reference checks as background jobs and polls for progress.
The SQLite database holds completed jobs for retrieval for up to one hour.
Expired rows are removed on subsequent job activity. Keep the SQLite file private
and on disk shared by the WSGI workers on the same host. Run one application
instance with that disk for the beta. A server restart interrupts in-flight jobs.
Publisher links use the DOI resolver wherever a DOI exists. When no DOI exists,
BibGuard displays the source-record link when available. Access to full text
depends on the publisher. Use **Citation styles and copy** on any matched record.

The development setup and paid-mode settings follow.

## Quick start (easiest way, Windows PowerShell)
    python -m venv venv
    .\venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    python setup_wizard.py      # optional for local testing, saves a private .env file
    python app.py               # then open http://localhost:5000
The site reads .env automatically, so you never type $env:... again. Never share or upload the .env file.
Email needs a Gmail "App password" (Google Account -> Security -> 2-Step Verification ON -> App passwords).
If the 4-digit code appears in the terminal, email delivery is in development mode.
Check email any time: http://localhost:5000/admin/test-email?to=you@mail.com


## If email or "Buy credits" does not work
Open  http://localhost:5000/admin/diagnostics  : it tests email, Google sign-in and payments live and tells you what to fix.
Three ways to verify real emails (pick one): Gmail app password (wizard), Brevo API (wizard, option 2), or Google sign-in (set GOOGLE_CLIENT_ID).
In paid mode, Buy credits needs at least one payment provider. After editing .env, stop the site (Ctrl+C), start it again and press Ctrl+F5 in the browser.

Check references against Crossref + OpenAlex, copy real citations, accounts, credits (pay per pack), password reset, admin page.

## Run (Windows PowerShell)
    python -m venv venv
    .\venv\Scripts\Activate.ps1
    pip install -r requirements.txt
    $env:CONTACT_EMAIL="you@mail.com"; $env:ADMIN_KEY="my-secret"
    $env:SECRET_KEY=(python -c "import secrets;print(secrets.token_hex(32))")
    python app.py
Open http://localhost:5000  |  Admin: http://localhost:5000/admin

## Accounts, credits and prices
- Guests (not logged in): 1 file per day (per IP). After sign-up and email verification: 5 free files (one-time). After that: credit packs.
- Default pack: 30 files for $3. Change with PACK_FILES and PACK_PRICE_CENTS (e.g. 200 = $2).
- Admin page shows users, last login, credits, payments, revenue, feedback, and can add credits by hand.

## Email setup (needed for the 4-digit sign-up code and password reset)
Without SMTP settings NOTHING is emailed: codes and reset links are only printed in the terminal.
Gmail example (PowerShell):
    $env:SMTP_HOST="smtp.gmail.com"; $env:SMTP_PORT="587"
    $env:SMTP_USER="you@gmail.com"; $env:SMTP_PASS="your-16-letter-app-password"
    $env:SMTP_FROM="BibGuard <you@gmail.com>"
App password: Google Account -> Security -> turn on 2-Step Verification -> App passwords.
Test it: open http://localhost:5000/admin/test-email?to=you@mail.com
(result "sent" = works, "dev" = SMTP_HOST empty, "fail" = the exact error is shown).
A dedicated email service (Brevo, Mailgun, Resend, Amazon SES...) is better for a public site.

## Forgot password (email)
Without SMTP settings the reset link is printed in the terminal (good for testing).
For real emails set SMTP_HOST, SMTP_PORT (587), SMTP_USER, SMTP_PASS, SMTP_FROM.
Gmail: SMTP_HOST=smtp.gmail.com, use an "App password" as SMTP_PASS.
Set BASE_URL (e.g. https://yoursite.com) so reset links point to your real site.

## Payments (easiest: PayPal. If PayPal is not available in your country: Lemon Squeezy. The wizard asks you.)
A) Stripe (optional, automatic, verified twice: Stripe webhook + a direct check with Stripe when the customer returns):
   PAY_CURRENCY=usd (cards, wallets) or myr (adds FPX; FPX needs MYR and at least RM2). PACK_PRICE_CENTS is in that currency.
   Enable payment methods: Stripe Dashboard -> Settings -> Payment methods.
   Test mode: use sk_test_ key and card 4242 4242 4242 4242, any future date, any CVC.
   Webhook events: checkout.session.completed and checkout.session.async_payment_succeeded. set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET, BASE_URL.
   Webhook URL: BASE_URL/api/stripe-webhook, event: checkout.session.completed.
   Local test: `stripe listen --forward-to localhost:5000/api/stripe-webhook`
B) Lemon Squeezy (easiest, no payment code of your own): create a store + one product "BibGuard credit pack", copy its buy link.
   LEMON_CHECKOUT_URL=<buy link>, LEMON_WEBHOOK_SECRET=<any long secret you type when creating the webhook>,
   LEMON_VARIANT_ID=<optional product variant id>. Webhook: URL BASE_URL/api/lemon-webhook, event order_created.
   Set PAY_CURRENCY and PACK_PRICE_CENTS to match the product price (used only for display). PACK_FILES = files per purchase.
   Lemon Squeezy must reach your server, so local testing needs a tunnel (ngrok / cloudflared) or use the admin page to grant credits.
C) Any other provider (Lemon Squeezy, Gumroad, Paddle...): set PAYMENT_LINK to its hosted payment page.
   Credits are then added by you on the admin page after you see the payment.

## Settings
CONTACT_EMAIL, ADMIN_KEY (optional, defaults to disabled), SECRET_KEY, BASE_URL,
FREE_FILES_PER_DAY=1, USER_FREE_TOTAL=5, PACK_FILES=30, PACK_PRICE_CENTS=300, IP_CAP_PER_DAY=30,
MAX_ENTRIES=150, SEARCH_CAP_PER_DAY=100, SCOPUS_API_KEY (optional), HTTPS=1 (when on https), DB_PATH.

## Deploy
Render/Railway: build `pip install -r requirements.txt`, start `gunicorn app:app --workers 2 --threads 4 --timeout 180`.
Add all settings as environment variables and a persistent disk for DB_PATH.

## PayPal (automatic, no webhook needed)
1. developer.paypal.com -> Apps & Credentials -> create an app. Copy Client ID and Secret (start in Sandbox).
2. Set PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET, PAYPAL_MODE=sandbox (use live when ready), BASE_URL, PAY_CURRENCY (usd).
3. Customers pay with PayPal or card. On return the server captures the order and checks status, amount, currency and user
   before adding credits (each PayPal order can only give credits once).
When several providers are configured, the customer chooses on a "Choose how you want to pay" popup.

## Verify real emails WITHOUT sending email: Google sign-in
Google proves the address is real. Setup: console.cloud.google.com -> new project -> APIs & Services -> OAuth consent screen
(External, add your email as a test user while testing) -> Credentials -> Create credentials -> OAuth client ID -> Web application.
Authorized JavaScript origins: http://localhost , http://localhost:5000 , and later your https site. Copy the Client ID into
GOOGLE_CLIENT_ID (python setup_wizard.py asks for it). A "Continue with Google" button then appears in the login popup.
While the consent screen is in Testing mode only the listed test users can sign in; publish it so everyone can.

## Email by API instead of Gmail SMTP: Brevo
Brevo -> Senders -> add your email, click the link they email you -> SMTP & API -> API Keys -> Generate.
Set BREVO_API_KEY and BREVO_SENDER (python setup_wizard.py asks for both). It sends over HTTPS, so blocked SMTP ports do not matter.
