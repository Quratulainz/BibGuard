"""Run once:  python setup_wizard.py
Asks a few questions, TESTS your email settings by sending a real test email, and saves everything in a file called .env
(the website reads .env automatically, so you never type $env:... again). Never share or upload the .env file."""
import os, secrets, smtplib, getpass
from email.message import EmailMessage

ENV = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

def load():
    d = {}
    if os.path.exists(ENV):
        for line in open(ENV, encoding="utf-8-sig"):
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.strip().split("=", 1); d[k.strip()] = v.strip().strip("'\"")
    return d

def save(d):
    with open(ENV, "w", encoding="utf-8") as f:
        f.write("# Created by setup_wizard.py. Keep this file private.\n")
        for k, v in d.items(): f.write(f"{k}={v}\n")

def ask(label, default="", secret=False):
    hint = f" [{default}]" if default and not secret else ""
    v = (getpass.getpass if secret else input)(f"{label}{hint}: ").strip()
    return v or default

def yes(label): return input(f"{label} (y/n): ").strip().lower().startswith("y")

def test_smtp(host, port, user, pw, to):
    try:
        m = EmailMessage(); m["From"], m["To"], m["Subject"] = user, to, "BibGuard setup test"
        m.set_content("Good news: your BibGuard email settings work. Verification codes will now arrive by email.")
        with smtplib.SMTP(host, port, timeout=25) as s:
            s.starttls(); s.login(user, pw); s.send_message(m)
        return True, ""
    except smtplib.SMTPAuthenticationError:
        return False, ("Gmail refused the login. Use a 16-letter APP PASSWORD (not your normal password):\n"
                       "  Google Account -> Security -> turn ON 2-Step Verification -> search 'App passwords' -> create one.")
    except Exception as e:
        return False, f"Could not send: {e!r}\n  Check your internet, and that the address is correct."

def main():
    env = load()
    print("\n=== BibGuard setup ===\n(Typing of passwords is hidden. Press Enter to keep the value in brackets.)\n")
    env.setdefault("SECRET_KEY", secrets.token_hex(32))
    env["ADMIN_KEY"] = ask("Admin password for /admin (username: admin)", env.get("ADMIN_KEY") or secrets.token_urlsafe(24), secret=True)
    env["CONTACT_EMAIL"] = ask("Your contact email", env.get("CONTACT_EMAIL", ""))
    env["BASE_URL"] = ask("Website address (use http://localhost:5000 while testing)", env.get("BASE_URL", "http://localhost:5000"))

    print("\n--- 1) Email (sends the 4-digit code and password-reset links) ---")
    for attempt in range(3):
        user = ask("Gmail address that will SEND the emails", env.get("SMTP_USER", ""))
        pw = ask("Gmail App Password (16 letters)", secret=True).replace(" ", "")
        if not user or not pw: print("Skipped."); break
        print("Sending a test email to yourself...")
        ok, msg = test_smtp("smtp.gmail.com", 587, user, pw, user)
        if ok:
            env.update(SMTP_HOST="smtp.gmail.com", SMTP_PORT="587", SMTP_USER=user, SMTP_PASS=pw, SMTP_FROM=f"BibGuard <{user}>")
            print(f"SUCCESS. Check the inbox (or Spam) of {user} for 'BibGuard setup test'."); break
        print("\nFAILED:", msg, "\n")
    else:
        print("Email was not set up. You can run this wizard again later.")

    if not env.get("SMTP_HOST") and yes("Gmail did not work? Use Brevo instead (free plan, sends by internet API, no SMTP port problems)"):
        import requests
        print("In Brevo: create account -> Senders -> add your email as a sender and click the link Brevo emails you -> SMTP & API -> API Keys -> Generate.")
        key = ask("Brevo API key", env.get("BREVO_API_KEY", ""), secret=True); sender = ask("Verified sender email", env.get("BREVO_SENDER", ""))
        try:
            ok = requests.get("https://api.brevo.com/v3/account", headers={"api-key": key}, timeout=20).status_code == 200
        except Exception as e:
            ok = False; print("Could not reach Brevo:", e)
        if ok and sender: env.update(BREVO_API_KEY=key, BREVO_SENDER=sender); print("SUCCESS: Brevo key accepted. Start the site and sign up to get a real email.")
        else: print("Brevo key was not accepted, nothing saved.")

    print("\n--- Google sign-in (optional, proves a user's email is real WITHOUT sending any email) ---")
    print("Google Cloud Console -> create project -> APIs & Services -> OAuth consent screen -> Credentials -> Create OAuth client ID (Web).\nAuthorized JavaScript origins: http://localhost and http://localhost:5000 (and your real site later).")
    gid = ask("Google Client ID (press Enter to skip)", env.get("GOOGLE_CLIENT_ID", ""))
    if gid: env["GOOGLE_CLIENT_ID"] = gid

    print("\n--- 2) Payments (PayPal is the easiest) ---")
    if env.get("CREDITS_ENABLED", "0") == "1" and yes("Set up PayPal now? You need Client ID and Secret from developer.paypal.com"):
        env["PAYPAL_CLIENT_ID"] = ask("PayPal Client ID", env.get("PAYPAL_CLIENT_ID", ""))
        env["PAYPAL_CLIENT_SECRET"] = ask("PayPal Secret", env.get("PAYPAL_CLIENT_SECRET", ""), secret=True)
        env["PAYPAL_MODE"] = ask("Mode: sandbox (testing) or live (real money)", env.get("PAYPAL_MODE", "sandbox"))
        env["PAY_CURRENCY"] = ask("Currency (usd, myr, eur...)", env.get("PAY_CURRENCY", "usd")).lower()
        env["PACK_PRICE_CENTS"] = str(int(float(ask("Price of one pack in that currency, e.g. 3 or 2.5", str(int(env.get("PACK_PRICE_CENTS", "300")) / 100))) * 100))
        env["PACK_FILES"] = ask("File checks inside one pack", env.get("PACK_FILES", "30"))
    if env.get("CREDITS_ENABLED", "0") == "1" and not env.get("PAYPAL_CLIENT_ID") and yes("PayPal is not available in your country? Set up Lemon Squeezy instead (needs a store and a product buy link)"):
        env["LEMON_CHECKOUT_URL"] = ask("Lemon Squeezy product buy link", env.get("LEMON_CHECKOUT_URL", ""))
        env["LEMON_WEBHOOK_SECRET"] = env.get("LEMON_WEBHOOK_SECRET") or secrets.token_urlsafe(24)
        print(f"\nIn Lemon Squeezy: Settings -> Webhooks -> add webhook\n  URL    : {env['BASE_URL']}/api/lemon-webhook   (must be a public https address)\n  Secret : {env['LEMON_WEBHOOK_SECRET']}\n  Event  : order_created\n  Product 'confirmation redirect': {env['BASE_URL']}/?paid=1")
        env["PAY_CURRENCY"] = ask("Currency of your product price", env.get("PAY_CURRENCY", "usd")).lower()
        env["PACK_PRICE_CENTS"] = str(int(float(ask("Price of one pack, e.g. 3", str(int(env.get("PACK_PRICE_CENTS", "300")) / 100))) * 100))
        env["PACK_FILES"] = ask("File checks inside one pack", env.get("PACK_FILES", "30"))
    save(env)
    print(f"\nSaved to {ENV}\nNow run:  python app.py   and open http://localhost:5000\nAdmin page: /admin (username: admin)\n")

if __name__ == "__main__":
    main()
