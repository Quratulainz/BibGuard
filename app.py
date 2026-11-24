"""BibGuard - verify BibTeX / reference lists against Crossref + OpenAlex."""
from contextvars import ContextVar
import os, re, io, json, time, threading, html, hmac, hashlib, secrets, smtplib, difflib, sqlite3, unicodedata
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import OrderedDict
from urllib.parse import quote, urlencode, urlsplit
import requests
from flask import Flask, request, jsonify, send_from_directory, session, redirect
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import generate_password_hash, check_password_hash

def _load_env_file():
    """Reads KEY=VALUE lines from a .env file next to app.py (no extra package needed). Real environment variables win."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path): return
    for line in open(path, encoding="utf-8-sig"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line: continue
        k, v = line.split("=", 1); k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'": v = v[1:-1]
        os.environ.setdefault(k, v)
_load_env_file()

app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024  # 12 MB per request (PDFs)
app.secret_key = os.getenv("SECRET_KEY") or secrets.token_hex(32)
if os.getenv("HTTPS") == "1" and (len(os.getenv("SECRET_KEY", "")) < 32
        or any(word in app.secret_key.lower() for word in ("change-me", "put-a-long-random"))):
    raise RuntimeError("Set a strong, stable SECRET_KEY for HTTPS deployment.")
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=os.getenv("HTTPS") == "1", PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30)

MAILTO = os.getenv("CONTACT_EMAIL", "you@example.com")   # polite-pool email for Crossref/OpenAlex
ADMIN_KEY = os.getenv("ADMIN_KEY", "change-me")
FREE_FILES = int(os.getenv("FREE_FILES_PER_DAY", "1"))      # guests (per IP)
CREDITS_ENABLED = os.getenv("CREDITS_ENABLED") == "1"
BETA_DAILY_FILES = max(1, int(os.getenv("BETA_DAILY_FILES", "5")))
USER_FREE_TOTAL = int(os.getenv("USER_FREE_TOTAL", "5"))    # free files per account (one-time), then credits are needed
PACK_FILES = int(os.getenv("PACK_FILES", "30"))             # files in one paid pack
PACK_PRICE_CENTS = int(os.getenv("PACK_PRICE_CENTS", "300"))  # $3.00
STRIPE_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WH = os.getenv("STRIPE_WEBHOOK_SECRET", "")
PAY_CURRENCY = os.getenv("PAY_CURRENCY", "usd").lower()    # usd, myr (FPX needs myr), ...
LEMON_URL = os.getenv("LEMON_CHECKOUT_URL", "")            # Lemon Squeezy product "buy link"
LEMON_SECRET = os.getenv("LEMON_WEBHOOK_SECRET", "")       # signing secret you type when creating the webhook
LEMON_VARIANT = os.getenv("LEMON_VARIANT_ID", "")          # optional: only this product gives credits
PAYPAL_ID, PAYPAL_SECRET = os.getenv("PAYPAL_CLIENT_ID", ""), os.getenv("PAYPAL_CLIENT_SECRET", "")
PAYPAL_BASE = "https://api-m.paypal.com" if os.getenv("PAYPAL_MODE", "sandbox") == "live" else "https://api-m.sandbox.paypal.com"
PAYMENT_LINK = os.getenv("PAYMENT_LINK", "")                # any hosted payment page (Lemon Squeezy, Gumroad, Stripe Payment Link...)
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")            # public address of your site, used in emails and Stripe redirects
if os.getenv("HTTPS") == "1":
    public_url = urlsplit(BASE_URL)
    if public_url.scheme != "https" or not public_url.hostname or public_url.path or public_url.query or public_url.fragment:
        raise RuntimeError("Set BASE_URL to the public HTTPS origin.")
    app.config["TRUSTED_HOSTS"] = [public_url.hostname]

proxy_hops = int(os.getenv("TRUSTED_PROXY_HOPS", "0"))
if proxy_hops > 0:
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops)

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")        # enables "Continue with Google" (Google proves the email is real)
BREVO_KEY = os.getenv("BREVO_API_KEY", "")                  # email by HTTPS API (works when SMTP ports are blocked)
BREVO_SENDER = os.getenv("BREVO_SENDER", "")                # sender address you verified inside Brevo
SMTP_HOST, SMTP_PORT = os.getenv("SMTP_HOST", ""), int(os.getenv("SMTP_PORT", "587"))
SMTP_USER, SMTP_PASS = os.getenv("SMTP_USER", ""), os.getenv("SMTP_PASS", "").replace(" ", "")  # Google shows app passwords with spaces
SMTP_FROM = os.getenv("SMTP_FROM", "") or SMTP_USER or "noreply@bibguard.local"
IP_CAP = int(os.getenv("IP_CAP_PER_DAY", "30"))             # hard cap per IP, stops multi-account abuse
MAX_ENTRIES = int(os.getenv("MAX_ENTRIES", "150"))
DB = os.getenv("DB_PATH", "bibguard.db")
UA = {"User-Agent": f"BibGuard/1.0 (mailto:{MAILTO})"}


# ---------- storage ----------
def db():
    c = sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS usage(ip TEXT, day TEXT, files INT, PRIMARY KEY(ip,day))")
    c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT UNIQUE, pw TEXT, created TEXT)")
    for col in ("free_used INT DEFAULT 0", "credits INT DEFAULT 0", "last_login TEXT", "verified INT DEFAULT 1"):
        try: c.execute(f"ALTER TABLE users ADD COLUMN {col}")
        except sqlite3.OperationalError: pass
    c.execute("CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INT, ref TEXT UNIQUE, files INT, cents INT, ts TEXT)")
    try: c.execute("ALTER TABLE payments ADD COLUMN cur TEXT")
    except sqlite3.OperationalError: pass
    c.execute("CREATE TABLE IF NOT EXISTS email_codes(uid INTEGER PRIMARY KEY, hash TEXT, exp REAL, attempts INT, sent REAL)")
    c.execute("CREATE TABLE IF NOT EXISTS resets(hash TEXT PRIMARY KEY, uid INT, exp REAL)")
    c.execute("""CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
                 ip TEXT, rating INT, kind TEXT, message TEXT, email TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS verify_jobs(
                 id TEXT PRIMARY KEY, expires REAL, total INT, done INT, result TEXT, error TEXT)""")
    return c

def client_ip():
    return request.remote_addr or "?"

def today(): return time.strftime("%Y-%m-%d")


# ---------- BibTeX parsing ----------
def clean(s):
    s = re.sub(r"\\['\"`^~=.]\{?(\w)\}?", r"\1", s)
    s = re.sub(r"\\[a-zA-Z]+\s*", "", s)
    return re.sub(r"\s+", " ", s.replace("{", "").replace("}", "").replace("\\", "")).strip()

def parse_fields(body, spans=None):
    f, i, n = {}, 0, len(body)
    head = re.compile(r"\s*,?\s*([A-Za-z_\-]+)\s*=\s*")
    while i < n:
        m = head.match(body, i)
        if not m:
            i += 1; continue
        name, i = m.group(1).lower(), m.end()
        if i >= n: break
        value_start = i
        if body[i] == "{":
            d, s = 0, i
            while i < n:
                if body[i] == "{": d += 1
                elif body[i] == "}":
                    d -= 1
                    if d == 0: break
                i += 1
            val = body[s + 1:i]; i += 1
        elif body[i] == '"':
            s = i + 1; i += 1
            while i < n and body[i] != '"':
                i += 2 if body[i] == "\\" else 1
            val = body[s:i]; i += 1
        else:
            m2 = re.compile(r"[^,\s]+").match(body, i)
            val = m2.group(0) if m2 else ""
            i = m2.end() if m2 else i + 1
        f[name] = clean(val)
        if spans is not None:
            spans.append((name, m.start(1), value_start, i))
    return f

def parse_bib(text):
    out, pos = [], 0
    head = re.compile(r"@(\w+)\s*([{(])\s*([^,\s]+)\s*,")
    while True:
        m = head.search(text, pos)
        if not m: break
        i = j = m.end()
        closing = ")" if m.group(2) == "(" else "}"
        braces, quoted = 0, False
        while j < len(text):
            ch = text[j]
            if ch == "\\":
                j += 2
                continue
            if ch == '"' and braces == 0:
                quoted = not quoted
            if not quoted:
                if ch == closing and braces == 0:
                    break
                if ch == "{":
                    braces += 1
                elif ch == "}" and braces:
                    braces -= 1
            j += 1
        if j >= len(text):
            raise ValueError(f"Unclosed BibTeX entry: {m.group(3)}")
        pos = j + 1
        if m.group(1).lower() in ("comment", "string", "preamble"): continue
        out.append({"type": m.group(1).lower(), "key": m.group(3), "fields": parse_fields(text[i:j]),
                    "original": text[m.start():pos], "start": m.start(), "end": pos,
                    "body_start": i - m.start()})
    return out

DOI_RE = re.compile(r"10\.\d{4,9}/[^\s<>]+", re.I)

def parse_plain(text):
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if len(parts) < 2:
        parts = [p.strip() for p in text.splitlines() if len(p.strip()) > 25 or DOI_RE.search(p)]
    return [{"type": "misc", "key": f"ref{i+1}", "fields": {"raw": re.sub(r"^(?:\[\d+\]\s*|\d+[.)]\s+)", "", re.sub(r"\s+", " ", p))}}
            for i, p in enumerate(parts)]

def parse_any(text):
    return parse_bib(text) if re.search(r"@\w+\s*[{(]", text) else parse_plain(text)


# ---------- helpers ----------
def norm(s):
    s = "".join(ch for ch in unicodedata.normalize("NFKD", s or "").casefold() if not unicodedata.combining(ch))
    return "".join(ch for ch in s if ch.isalnum())

def sim(a, b):
    a, b = norm(a), norm(b)
    return difflib.SequenceMatcher(None, a, b).ratio() if a and b else 0.0

def clean_doi(d):
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", (d or "").strip(), flags=re.I)
    return d.strip().rstrip(".,;")

def surnames(author):
    out = []
    for a in re.split(r"\s+and\s+", author or ""):
        a = a.strip()
        if not a or a.lower() == "others": continue
        out.append(norm(a.split(",")[0] if "," in a else a.split()[-1]))
    return [x for x in out if x]

LOOKUP_FAILURES = ContextVar("lookup_failures", default=None)
LOOKUP_CACHE = OrderedDict()
LOOKUP_CACHE_LOCK = threading.Lock()
HTTP_CLIENT = threading.local()
PROVIDER_SLOTS = {provider: threading.BoundedSemaphore(2) for provider in ("Crossref", "OpenAlex")}
RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

def http_json(url, params=None):
    openalex = url.startswith("https://api.openalex.org/")
    key = (url, tuple(sorted((params or {}).items())))
    now = time.monotonic()
    with LOOKUP_CACHE_LOCK:
        cached = LOOKUP_CACHE.get(key)
        if cached and cached[0] > now:
            LOOKUP_CACHE.move_to_end(key)
            return cached[1]
    if openalex and os.getenv("OPENALEX_API_KEY"):
        params = {**(params or {}), "api_key": os.environ["OPENALEX_API_KEY"]}
    if not hasattr(HTTP_CLIENT, "session"):
        HTTP_CLIENT.session = requests.Session()
    provider = "OpenAlex" if openalex else "Crossref"
    reason = "request failed"
    for attempt in range(2):
        try:
            with PROVIDER_SLOTS[provider]:
                response = HTTP_CLIENT.session.get(url, params=params, headers=UA, timeout=(5, 12))
            status = response.status_code
            if status in (200, 404):
                payload = response.json() if status == 200 else None
                if status == 404 or isinstance(payload, dict):
                    with LOOKUP_CACHE_LOCK:
                        LOOKUP_CACHE[key] = (time.monotonic() + (900 if payload else 120), payload)
                        LOOKUP_CACHE.move_to_end(key)
                        if len(LOOKUP_CACHE) > 4096:
                            LOOKUP_CACHE.popitem(last=False)
                    return payload
            reason = f"HTTP {status}" if status != 200 else "invalid metadata response"
            if status not in RETRYABLE_STATUS:
                break
            retry_after = response.headers.get("Retry-After", "")
            if retry_after and (not retry_after.isdigit() or int(retry_after) > 4):
                break
            delay = max(1, int(retry_after)) if retry_after else 1
        except requests.Timeout:
            reason, delay = "timeout", 1
        except requests.ConnectionError:
            reason, delay = "connection failed", 1
        except (requests.RequestException, ValueError):
            reason = "invalid response or request failed"
            break
        if attempt == 0:
            time.sleep(delay)
    failures = LOOKUP_FAILURES.get()
    if failures is not None:
        failures.add(f"{provider}: {reason}")
    return None


# ---------- scholarly sources ----------
def from_crossref(m):
    date = m.get("published-print") if m.get("volume") else None
    dp = ((date or m.get("issued") or {}).get("date-parts") or [[None]])[0]
    title = (m.get("title") or [""])[0]
    doi = m.get("DOI", "")
    return {"title": clean(title), "doi": doi, "src": "Crossref",
            "authors": [(a.get("family", ""), a.get("given", "")) for a in m.get("author", []) if a.get("family")],
            "year": str(dp[0]) if dp and dp[0] else "",
            "venue": clean((m.get("container-title") or [""])[0]),
            "volume": m.get("volume", ""), "pages": m.get("page", ""), "issue": m.get("issue", ""), "type": m.get("type", ""),
            "link": f"https://doi.org/{quote(doi, safe='/')}" if doi else m.get("URL", ""),
            "retracted": any((u.get("type") or "").lower() == "retraction" for u in m.get("updated-by", []))
                         or title.lower().startswith("retracted")}

def from_openalex(w):
    au = []
    for a in w.get("authorships", []):
        n = ((a.get("author") or {}).get("display_name") or "").split()
        if n: au.append((n[-1], " ".join(n[:-1])))
    doi = clean_doi(w.get("doi") or "")
    loc = w.get("primary_location") or {}
    return {"title": clean(w.get("title") or w.get("display_name") or ""), "doi": doi, "src": "OpenAlex",
            "authors": au, "year": str(w.get("publication_year") or ""),
            "venue": clean((loc.get("source") or {}).get("display_name") or ""),
            "volume": (w.get("biblio") or {}).get("volume") or "", "issue": (w.get("biblio") or {}).get("issue") or "",
            "pages": "-".join(x for x in [(w.get("biblio") or {}).get("first_page"), (w.get("biblio") or {}).get("last_page")] if x),
            "type": w.get("type") or "",
            "link": f"https://doi.org/{quote(doi, safe='/')}" if doi else (loc.get("landing_page_url") or w.get("id") or ""),
            "retracted": bool(w.get("is_retracted"))}

def crossref_doi(doi):
    d = http_json(f"https://api.crossref.org/works/{quote(doi, safe='/')}", {"mailto": MAILTO})
    return from_crossref(d["message"]) if d else None

def openalex_doi(doi):
    w = http_json(f"https://api.openalex.org/works/doi:{quote(doi, safe='/')}", {"mailto": MAILTO})
    return from_openalex(w) if w else None

def crossref_search(q, rows=3):
    d = http_json("https://api.crossref.org/works", {"query.bibliographic": q, "rows": rows, "mailto": MAILTO})
    return [from_crossref(x) for x in (d or {}).get("message", {}).get("items", [])]

def openalex_search(q, n=3):
    d = http_json("https://api.openalex.org/works", {"search": q, "per-page": n, "mailto": MAILTO})
    return [from_openalex(x) for x in (d or {}).get("results", [])]


# ---------- verification ----------
def prep(e):
    f = e["fields"]
    yr = re.findall(r"\d{4}", f.get("year", "") or f.get("date", ""))
    doi = clean_doi(f.get("doi", ""))
    if not doi:
        m = DOI_RE.search(f.get("url", "") or f.get("raw", ""))
        doi = clean_doi(m.group(0)) if m else ""
    return {"key": e["key"], "type": e["type"], "title": f.get("title", ""), "author": f.get("author", ""),
            "year": yr[0] if yr else "", "doi": doi, "raw": f.get("raw", ""), "fields": f}

def title_sim(p, c):
    if p["raw"]:
        nt = norm(c["title"])
        return 1.0 if len(nt) > 15 and nt in norm(p["raw"]) else sim(p["raw"], c["title"]) * 0.9
    return sim(p["title"], c["title"])

def judge(p, c):
    ts, issues = title_sim(p, c), []
    mine, theirs = surnames(p["author"]), [norm(f) for f, _ in c["authors"]]
    if mine and (not theirs or mine != theirs):
        if theirs and re.search(r"\band others\s*$", p["author"], re.I) and mine == theirs[:len(mine)]:
            issues.append("Only the supplied authors could be compared")
        else:
            issues.append("The supplied author list differs from the registry record")
    elif not p["raw"] and not mine:
        issues.append("No author list supplied for comparison")
    if p["year"] and not c["year"]:
        issues.append("The registry did not provide a publication year")
    elif p["year"] and p["year"] != c["year"]:
        issues.append(f"Year differs: your file says {p['year']}, record says {c['year'] or 'unknown'}")
    elif not p["raw"] and not p["year"]:
        issues.append("No publication year supplied for comparison")
    for fields, record_key in ((("journal", "booktitle"), "venue"), (("volume",), "volume"),
                               (("number", "issue"), "issue"), (("pages",), "pages")):
        supplied = next((p["fields"][key] for key in fields if p["fields"].get(key)), "")
        if supplied and not c.get(record_key):
            issues.append(f"The registry did not provide {record_key}")
        elif supplied and norm(str(c[record_key])) != norm(supplied):
            issues.append(f"{record_key.title()} differs from the registry record")
    return ts, issues

def fmt_authors(c): return " and ".join(f"{f}, {g}".strip(", ") for f, g in c["authors"])

def to_bib(p, c):
    esc = lambda s: s.replace("&", r"\&")
    f = {"title": "{" + esc(c["title"]) + "}", "author": "{" + esc(fmt_authors(c)) + "}", "year": "{" + c["year"] + "}"}
    if p["type"] in ("inproceedings", "incollection", "conference", "proceedings") and c["venue"]:
        f["booktitle"] = "{" + esc(c["venue"]) + "}"
    elif c["venue"] and p["type"] in ("article", "misc"):
        f["journal"] = "{" + esc(c["venue"]) + "}"
    if c["volume"]: f["volume"] = "{" + str(c["volume"]) + "}"
    if c["pages"]: f["pages"] = "{" + str(c["pages"]).replace("-", "--").replace("----", "--") + "}"
    if c["doi"]: f["doi"] = "{" + c["doi"] + "}"
    typ = "article" if p["type"] == "misc" and c["venue"] else p["type"]
    return f"@{typ}{{{p['key']},\n" + ",\n".join(f"  {k} = {v}" for k, v in f.items() if v != "{}") + "\n}\n"

def bib_value(value):
    escapes = {"&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_",
               "$": r"\$", "{": r"\{", "}": r"\}", "\\": r"\textbackslash{}"}
    return "{" + "".join(escapes.get(ch, ch) for ch in str(value)) + "}"

def correct_bib(entry, match):
    original = entry["original"]
    body_start = entry["body_start"]
    body = original[body_start:-1]
    spans = []
    fields = parse_fields(body, spans)
    previous = 0
    for index, (field, start, first, end) in enumerate(spans):
        gap = body[previous:start].strip()
        value = body[first:end]
        if gap != ("," if index else "") or not value or "#" in value and not value.startswith(("{", '"')):
            return original, []
        previous = end
    if len(fields) != len(spans) or body[previous:].strip() not in ("", ","):
        return original, []

    updates = {"year": match["year"], "volume": match["volume"], "pages": match["pages"],
               "doi": match["doi"]}
    if fields.get("date") and match["year"] and re.match(r"^\d{4}(?:-|$)", fields["date"]):
        if fields["date"][:4] != match["year"]:
            updates["date"] = match["year"]
    updates["issue" if "issue" in fields and "number" not in fields else "number"] = match["issue"]
    if entry["type"] == "article":
        updates["journal"] = match["venue"]
    elif entry["type"] in ("inproceedings", "incollection", "conference"):
        updates["booktitle"] = match["venue"]
    if match["pages"]:
        updates["pages"] = re.sub(r"(?<=\d)\s*[-–—]+\s*(?=\d)", "--", str(match["pages"]))
    if not fields.get("author") and match["authors_list"]:
        updates["author"] = fmt_authors({"authors": match["authors_list"]})
    updates = {key: str(value) for key, value in updates.items() if value and (
        fields.get(key, "").casefold() != str(value).casefold())}
    for field, _, first, end in reversed(spans):
        if field in updates:
            body = body[:first] + bib_value(updates[field]) + body[end:]
    missing = [f"  {key} = {bib_value(value)}" for key, value in updates.items() if key not in fields]
    if missing:
        body = body.rstrip()
        if body and not body.endswith(","):
            body += ","
        body += "\n" + ",\n".join(missing) + "\n"
    return original[:body_start] + body + original[-1], list(updates)

def metadata_issues(source, match):
    _, issues = judge(source, {**match, "authors": match["authors_list"]})
    if source["doi"] and source["doi"].casefold() != (match["doi"] or "").casefold():
        issues.append("DOI differs from the matched record")
    if norm(source["title"]) != norm(match["title"]):
        issues.append("Title differs from the registry record")
    return issues

def correction_fields(entry, result):
    match = result.get("match")
    if not match or result["retracted"] or not match["doi"] or result.get("ambiguous"):
        return False
    source = prep(entry)
    doi_agrees = bool(source["doi"] and source["doi"].casefold() == match["doi"].casefold())
    if source["raw"]:
        return doi_agrees and result["status"] == "found"
    if result.get("score") != 1 or norm(source["title"]) != norm(match["title"]):
        return False
    if source["doi"]:
        return doi_agrees
    if result.get("lookup_details"):
        return False
    supplied = surnames(source["author"])
    registered = [norm(family) for family, _ in match["authors_list"]]
    return bool(supplied and supplied == registered)

def corrected_download(text, entries, results):
    pieces, offset, corrected, review = [], 0, 0, 0
    bibtex = "original" in entries[0]
    for entry, result in zip(entries, results):
        result["corrected_fields"] = []
        confident = correction_fields(entry, result)
        if bibtex:
            pieces.append(text[offset:entry["start"]])
            offset = entry["end"]
            updated, fields = correct_bib(entry, result["match"]) if confident else (entry["original"], [])
            if fields:
                corrected += 1
                result["corrected_fields"] = fields
                corrected_entry = parse_bib(updated)[0]
                result["corrected_status"] = ("partial" if metadata_issues(prep(corrected_entry), result["match"]) else "verified")
            if (result.get("corrected_status", result["status"]) != "verified" or result["retracted"]):
                review += 1
                updated = "% Review entry before citing. See the CSV report.\n" + updated
            pieces.append(updated)
        elif confident:
            match = result["match"]
            kind = "article" if match["type"] in ("article", "journal-article") else "misc"
            record = {**match, "authors": match["authors_list"]}
            pieces.append(to_bib({"key": entry["key"], "type": kind}, record))
            result["corrected_fields"] = ["generated"]
            corrected += 1
        else:
            review += 1
            pieces.append("% Review original reference: " + entry["fields"]["raw"] + "\n")
    unchecked = len(entries) - len(results)
    if bibtex:
        pieces.append(text[offset:])
    else:
        pieces.extend("% Unchecked reference: " + entry["fields"]["raw"] + "\n"
                      for entry in entries[len(results):])
    return "".join(pieces), corrected, review + unchecked

SCOPUS_KEY = os.getenv("SCOPUS_API_KEY", "")   # optional: enables the "Indexed in Scopus" badge
_scopus = {}

def scopus_indexed(doi):
    """True/False if Scopus knows this DOI, None when no key or the lookup failed."""
    doi = re.sub(r"[^\w./;()\-:]", "", doi or "")
    if not SCOPUS_KEY or not doi: return None
    if doi in _scopus: return _scopus[doi]
    try:
        r = requests.get("https://api.elsevier.com/content/search/scopus", params={"query": f'DOI("{doi}")', "count": 1},
                         headers={"X-ELS-APIKey": SCOPUS_KEY, "Accept": "application/json"}, timeout=15)
        if r.status_code != 200: return None
        n = int(r.json()["search-results"]["opensearch:totalResults"])
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None
    _scopus[doi] = n > 0
    return _scopus[doi]

def public(c):
    return {"title": c["title"], "authors_list": [[f, g] for f, g in c["authors"]], "year": c["year"], "venue": c["venue"],
            "volume": c.get("volume", ""), "issue": c.get("issue", ""), "pages": c.get("pages", ""), "doi": c["doi"],
            "link": c["link"], "src": c["src"], "type": c.get("type", ""), "retracted": c["retracted"], "scopus": None}

def verify_entry(e):
    failures = set()
    token = LOOKUP_FAILURES.set(failures)
    try:
        return verify_with_sources(e, failures)
    finally:
        LOOKUP_FAILURES.reset(token)

def verify_with_sources(e, failures):
    p = prep(e)
    raw_is_doi = bool(p["raw"] and p["doi"] and clean_doi(p["raw"]).casefold() == p["doi"].casefold())
    q = p["title"] or ("" if raw_is_doi else p["raw"])
    res = {"key": p["key"], "title": q[:200], "status": "not_found", "issues": [], "match": None,
           "retracted": False, "fixed_bib": e.get("original") or
           ("% Original reference: " + p["raw"] if p["raw"] else "")}
    if not q and not p["doi"]:
        res["issues"] = ["No title or DOI found in this entry"]; return res
    cands, doi_cand = [], None
    if p["doi"]:
        doi_cand = crossref_doi(p["doi"]) or openalex_doi(p["doi"])
        if doi_cand: cands.append(doi_cand)
    best, best_ts = None, 0
    def pick():
        nonlocal best, best_ts
        supplied = surnames(p["author"])
        def rank(record):
            registered = [norm(family) for family, _ in record["authors"]]
            return (title_sim(p, record), record is doi_cand, bool(supplied and supplied == registered),
                    record["src"] == "Crossref", (record["doi"] or "").casefold(), norm(record["title"]))
        if cands:
            best = max(cands, key=rank)
            best_ts = title_sim(p, best)
    pick()
    if best_ts < 1 and q:
        first = (surnames(p["author"]) or [""])[0]
        cands += crossref_search(f"{q} {first}".strip()); pick()
    if best_ts < 1 and q:
        cands += openalex_search(q); pick()
    if doi_cand and not q:
        best, best_ts = doi_cand, 1.0
    if failures:
        res["lookup_details"] = sorted(failures)
    if not best or best_ts < 0.75:
        if failures:
            res["status"] = "unavailable"
            res["issues"].append("Lookup incomplete. " + "; ".join(sorted(failures)) + ".")
        else:
            res["issues"].append("No sufficiently close record found. Check the reference manually.")
        return res
    if p["doi"] and not doi_cand:
        res["issues"].append("The supplied DOI could not be confirmed")
    ts, issues = judge(p, best)
    identities = {record["doi"].casefold() for record in cands
                  if record["doi"] and title_sim(p, record) == 1}
    res["ambiguous"] = not p["doi"] and len(identities) > 1
    if res["ambiguous"]:
        issues.append("Multiple records share this title. Confirm the DOI before correcting.")
    res["issues"] += issues
    if doi_cand and doi_cand is not best and title_sim(p, doi_cand) < 0.6:
        res["issues"].append(f"DOI {p['doi']} belongs to a different paper: \"{doi_cand['title'][:90]}\"")
    elif p["doi"] and best["doi"] and p["doi"].casefold() != best["doi"].casefold():
        res["issues"].append(f"DOI differs from matched record ({best['doi']})")
    if not best["retracted"] and best["doi"] and best["src"] == "Crossref":
        oa = openalex_doi(best["doi"])
        best = {**best, "retracted": bool(oa and oa["retracted"])}
        if oa is None:
            res["retraction_check"] = "Crossref checked; OpenAlex confirmation unavailable"
    if not q:
        res["issues"] = ["DOI resolved. No title, authors, or year could be verified."]
        res["status"] = "found"
    elif p["raw"]:
        res["issues"].append("Plain-text match. Verify the supplied authors and year manually.")
        res["status"] = "found" if ts >= 0.92 and len(res["issues"]) == 1 else "partial"
    else:
        if ts != 1.0:
            res["issues"].append("Title differs from the registry record")
        res["status"] = "verified" if not res["issues"] else "partial"
    res["score"] = round(ts, 2)
    res["retracted"] = best["retracted"]
    res["match"] = public(best)
    res["match"]["scopus"] = scopus_indexed(best["doi"])
    res["match"]["authors"] = ", ".join(f"{g} {f}".strip() for f, g in best["authors"][:6]) + (" et al." if len(best["authors"]) > 6 else "")
    if res["status"] == "verified" and not res["retracted"]:
        res["fixed_bib"] = e.get("original") or to_bib(p, best)
    return res

HEADING = r"(?:references|bibliography|literature cited|works cited)"

def split_refs(text):
    """Turn raw PDF text into a blank-line separated list of references."""
    text = re.sub(r"-\n(?=[a-z])", "", text)
    idx = [m.start() for m in re.finditer(rf"(?im)^\s*(?:\d+\.?\s*)?{HEADING}\s*$", text)]
    if not idx: return ""
    body = text[idx[-1]:]
    body = re.sub(rf"(?im)^\s*(?:\d+\.?\s*)?{HEADING}\s*$", "", body, count=1)
    parts = re.split(r"(?m)^\s*(?:\[\d+\]|\d{1,3}\.)\s+", body)
    if len(parts) > 1:
        refs = parts[1:]
    else:  # author-year style: a new reference starts with "Surname, I."
        refs = re.split(r"\n(?=[A-Z][A-Za-z'\-]+,\s+(?:[A-Z]\.|[A-Z][a-z]+))", body)
    refs = [re.sub(r"\s+", " ", x).strip() for x in refs]
    return "\n\n".join(x for x in refs if len(x) > 30)

def pdf_refs(raw):
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ValueError("PDF support is not installed on this server (pip install pypdf).")
    try:
        text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(raw)).pages)
    except Exception:
        raise ValueError("Could not read this PDF. Scanned PDFs (images) are not supported.")
    refs = split_refs(text)
    if not refs:
        raise ValueError("No readable reference section found in this PDF. Paste the bibliography or upload a BibTeX file.")
    return refs

def read_upload(f):
    raw = f.read()
    if (f.filename or "").lower().endswith(".pdf"): return pdf_refs(raw)
    return raw.decode("utf-8", errors="replace")


# ---------- routes ----------
@app.after_request
def no_stale_cache(r):
    if request.path == "/" or request.path.startswith("/static/"): r.headers["Cache-Control"] = "no-cache"
    if request.path.startswith("/admin"): r.headers["Cache-Control"] = "no-store"
    return r

@app.get("/")
def index(): return send_from_directory("static", "index.html")

def used_today(c, key):
    row = c.execute("SELECT files FROM usage WHERE ip=? AND day=?", (key, today())).fetchone()
    return row[0] if row else 0

def balance(c, uid):
    r = c.execute("SELECT free_used, credits FROM users WHERE id=?", (uid,)).fetchone()
    return (max(USER_FREE_TOTAL - (r[0] or 0), 0), r[1] or 0) if r else (0, 0)

def base_url(): return BASE_URL or "http://localhost:5000"

VERIFY_JOB_SLOTS = threading.BoundedSemaphore(2)
VERIFY_POOL = ThreadPoolExecutor(max_workers=12, thread_name_prefix="bibguard-lookup")
VERIFY_JOB_TTL = 3600

@app.post("/api/verify")
def api_verify():
    try:
        sources = [(f.filename or "file", read_upload(f)) for f in request.files.getlist("files")[:3]]
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if request.form.get("text", "").strip(): sources.append(("pasted text", request.form["text"]))
    if not sources: return jsonify(error="Upload a .bib, .txt or .pdf file, or paste references."), 400
    try:
        parsed = [(name, text, parse_any(text)) for name, text in sources]
    except ValueError as e:
        return jsonify(error=str(e)), 400
    empty = [name for name, _, entries in parsed if not entries]
    if empty:
        return jsonify(error="No references could be read from: " + ", ".join(empty)), 400
    background = request.form.get("background") == "1"
    if background and not VERIFY_JOB_SLOTS.acquire(blocking=False):
        return jsonify(error="The checker is busy. Please try again in a moment."), 503
    handed_off = False
    try:
        job_id = secrets.token_urlsafe(24) if background else None
        budget = reserve_checks(parsed, job_id)
        if not isinstance(budget, int):
            return budget
        if not background:
            return jsonify(check_files(parsed, budget))
        worker = threading.Thread(target=run_verify_job, args=(job_id, parsed, budget), daemon=True)
        worker.start()
        handed_off = True
        return jsonify(job=job_id), 202
    finally:
        if background and not handed_off:
            VERIFY_JOB_SLOTS.release()

def reserve_checks(parsed, job_id):
    uid, n, ipkey = session.get("uid"), len(parsed), f"ip:{client_ip()}"
    email = user_email() if CREDITS_ENABLED and uid else None
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        ip_used = used_today(c, ipkey)
        if ip_used + n > IP_CAP: return jsonify(error="This network reached today's usage limit. Please try again tomorrow."), 429
        if not CREDITS_ENABLED:
            if ip_used + n > BETA_DAILY_FILES:
                return jsonify(error=f"Free beta limit reached ({BETA_DAILY_FILES} file checks per network per day)."), 429
            left = BETA_DAILY_FILES - ip_used - n
        elif uid and email:
            free_left, cred = balance(c, uid)
            if n > free_left + cred:
                return jsonify(error=f"You have used your {USER_FREE_TOTAL} free files. Buy a pack to continue.", pay=True), 402
            uf = min(n, free_left); uc = n - uf
            cur = c.execute("UPDATE users SET free_used=free_used+?, credits=credits-? WHERE id=? AND credits>=?", (uf, uc, uid, uc))
            if cur.rowcount == 0: return jsonify(error="Not enough credits.", pay=True), 402
            left = free_left + cred - n
        else:
            if ip_used + n > FREE_FILES:
                return jsonify(error=f"Guest limit reached ({FREE_FILES} file{'s' if FREE_FILES != 1 else ''} per day). Create a free account to get {USER_FREE_TOTAL} more files."), 429
            left = FREE_FILES - ip_used - n
        c.execute("INSERT INTO usage VALUES(?,?,?) ON CONFLICT(ip,day) DO UPDATE SET files=files+?", (ipkey, today(), n, n))
        if job_id:
            c.execute("DELETE FROM verify_jobs WHERE expires < ?", (time.time(),))
            total = sum(min(len(entries), MAX_ENTRIES) for _, _, entries in parsed)
            c.execute("INSERT INTO verify_jobs VALUES (?, ?, ?, 0, NULL, NULL)",
                      (job_id, time.time() + VERIFY_JOB_TTL, total))
    return left

def check_files(parsed, left, progress=None):
    files = []
    done = 0
    for name, text, entries in parsed:
        note = f"Only the first {MAX_ENTRIES} of {len(entries)} entries were checked." if len(entries) > MAX_ENTRIES else ""
        limited = entries[:MAX_ENTRIES]
        results = [None] * len(limited)
        futures = {VERIFY_POOL.submit(verify_entry, entry): index for index, entry in enumerate(limited)}
        for future in as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                app.logger.warning("Unexpected metadata for one reference", exc_info=True)
                entry = limited[index]
                fields = entry["fields"]
                raw = fields.get("raw", "")
                results[index] = {
                    "key": entry["key"], "title": fields.get("title", raw)[:200],
                    "status": "unavailable", "issues": ["The lookup returned unreadable metadata."],
                    "match": None, "retracted": False,
                    "fixed_bib": entry.get("original") or ("% Original reference: " + raw),
                }
            done += 1
            if progress and done % 5 == 0:
                progress(done)
        count = lambda s: sum(r["status"] == s for r in results)
        export = text if "original" in entries[0] else "\n".join(
            [r["fixed_bib"] for r in results] +
            ["% Unchecked reference: " + entry["fields"]["raw"] for entry in entries[MAX_ENTRIES:]]
        )
        corrected_bib, corrected_count, review_count = corrected_download(text, entries, results)
        files.append({"name": name, "note": note, "bibliography": export,
                      "corrected_bib": corrected_bib, "corrected_count": corrected_count, "review_count": review_count, "total": len(results), "verified": count("verified"),
                      "partial": count("partial"), "not_found": count("not_found"),
                      "found": count("found"), "unavailable": count("unavailable"),
                      "retracted": sum(r["retracted"] for r in results), "entries": results})
    return {"files": files, "left": left, "beta": not CREDITS_ENABLED}

def run_verify_job(job_id, parsed, left):
    def progress(done):
        with db() as c:
            c.execute("UPDATE verify_jobs SET done=? WHERE id=?", (done, job_id))

    try:
        result = check_files(parsed, left, progress)
        with db() as c:
            c.execute("UPDATE verify_jobs SET done=total, result=?, expires=? WHERE id=?",
                      (json.dumps(result), time.time() + VERIFY_JOB_TTL, job_id))
    except Exception:
        app.logger.exception("Reference check failed")
        with db() as c:
            c.execute("UPDATE verify_jobs SET error=? WHERE id=?",
                      ("The server could not complete this check. Try a smaller batch.", job_id))
    finally:
        VERIFY_JOB_SLOTS.release()

@app.get("/api/verify-jobs/<job_id>")
def verify_job(job_id):
    with db() as c:
        c.execute("DELETE FROM verify_jobs WHERE expires < ?", (time.time(),))
        row = c.execute("SELECT total,done,result,error FROM verify_jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return jsonify(error="This check expired. Submit the references again."), 404
    total, done, result, error = row
    if error:
        return jsonify(error=error), 502
    if result is not None:
        response = jsonify(status="complete", **json.loads(result))
    else:
        response = jsonify(status="running", total=total, done=done)
    response.headers["Cache-Control"] = "no-store"
    return response


# ---------- accounts ----------
_fails, _forgot = {}, {}
def throttled():
    now, ip = time.time(), client_ip()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < 900]
    return len(_fails[ip]) >= 10

def user_email():
    uid = session.get("uid")
    if not uid: return None
    with db() as c:
        r = c.execute("SELECT email FROM users WHERE id=? AND verified=1", (uid,)).fetchone()
    return r[0] if r else None

def now_s(): return time.strftime("%Y-%m-%d %H:%M")

@app.get("/api/me")
def api_me():
    out = {"pack_files": PACK_FILES, "pack_price": PACK_PRICE_CENTS / 100, "free_total": USER_FREE_TOTAL,
           "guest_limit": FREE_FILES, "pay_enabled": bool(providers()), "providers": providers(), "currency": PAY_CURRENCY.upper(), "google_client_id": GOOGLE_CLIENT_ID}
    email = user_email()
    out.update(credits_enabled=CREDITS_ENABLED, user=email)
    with db() as c:
        if not CREDITS_ENABLED:
            out.update(guest_limit=BETA_DAILY_FILES, left=max(BETA_DAILY_FILES - used_today(c, f"ip:{client_ip()}"), 0))
        elif email:
            fl, cr = balance(c, session["uid"]); out.update(user=email, free_left=fl, credits=cr, left=fl + cr)
        else:
            out.update(user=None, left=max(FREE_FILES - used_today(c, f"ip:{client_ip()}"), 0))
    return jsonify(out)

@app.get("/api/account")
def api_account():
    uid = session.get("uid")
    if not uid: return jsonify(error="Please log in."), 401
    with db() as c:
        r = c.execute("SELECT email,free_used,credits,created,last_login FROM users WHERE id=?", (uid,)).fetchone()
        pays = c.execute("SELECT ts,files,cents,cur FROM payments WHERE uid=? ORDER BY id DESC LIMIT 20", (uid,)).fetchall()
    if not r: return jsonify(error="Please log in."), 401
    return jsonify(email=r[0], free_left=max(USER_FREE_TOTAL - (r[1] or 0), 0), credits=r[2] or 0, created=r[3], last_login=r[4],
                   payments=[{"ts": x[0], "files": x[1], "amount": x[2] / 100, "cur": (x[3] or "usd").upper()} for x in pays])

def start_session(uid):
    session.clear(); session.permanent = True; session["uid"] = uid
    with db() as c: c.execute("UPDATE users SET last_login=? WHERE id=?", (now_s(), uid))

_rates = {}
def rate_ok(name, limit, window):
    k, now = (name, client_ip()), time.time()
    _rates[k] = [t for t in _rates.get(k, []) if now - t < window]
    if len(_rates[k]) >= limit: return False
    _rates[k].append(now); return True

def email_on(): return bool(SMTP_HOST or (BREVO_KEY and BREVO_SENDER))

def is_local():
    return not BASE_URL.startswith("https://") and request.remote_addr in ("::1", "127.0.0.1")

def send_mail(to, subject, body):
    """Returns ("sent"|"dev"|"fail", error_text). "dev" = SMTP not configured, message printed in the terminal."""
    if BREVO_KEY and BREVO_SENDER:
        try:
            r = requests.post("https://api.brevo.com/v3/smtp/email", timeout=20, headers={"api-key": BREVO_KEY, "accept": "application/json"},
                              json={"sender": {"name": "BibGuard", "email": BREVO_SENDER}, "to": [{"email": to}], "subject": subject, "textContent": body})
            if r.status_code in (200, 201): return "sent", ""
            print("BREVO ERROR:", r.status_code, r.text[:300], flush=True); return "fail", f"Brevo {r.status_code}: {r.text[:200]}"
        except requests.RequestException as e:
            print("BREVO ERROR:", repr(e), flush=True); return "fail", repr(e)
    if not SMTP_HOST:
        print(f"\n--- DEV EMAIL (SMTP not configured, nothing was really sent) ---\nTo: {to}\nSubject: {subject}\n{body}\n------------------------------------\n", flush=True)
        return "dev", ""
    try:
        m = EmailMessage(); m["From"], m["To"], m["Subject"] = SMTP_FROM, to, subject; m.set_content(body)
        srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) if SMTP_PORT == 465 else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        with srv:
            if SMTP_PORT != 465: srv.starttls()
            if SMTP_USER: srv.login(SMTP_USER, SMTP_PASS)
            srv.send_message(m)
        return "sent", ""
    except Exception as e:
        print("MAIL ERROR:", repr(e), flush=True); return "fail", repr(e)

def deliver(to, subject, body):
    """Send now. Returns (ok, note_or_error)."""
    if not email_on() and not is_local(): return False, "The email service is not set up on this server yet."
    st, err = send_mail(to, subject, body)
    if st == "fail": return False, "We could not send the email. Please try again in a minute." + (f" [Owner details: {err}]" if is_local() else "")
    return True, ("EMAIL IS NOT SET UP, so nothing was emailed. Your 4-digit code is printed in the black terminal window where you ran python app.py. To send real emails: run python setup_wizard.py, or use Google sign-in." if st == "dev" else "")

def code_hash(uid, code): return hashlib.sha256(f"{app.secret_key}:{uid}:{code}".encode()).hexdigest()

def new_code(c, uid):
    """Creates a 4-digit code. Returns (code, 0) or (None, seconds_to_wait)."""
    row = c.execute("SELECT sent FROM email_codes WHERE uid=?", (uid,)).fetchone()
    if row and time.time() - row[0] < 60: return None, int(60 - (time.time() - row[0])) + 1
    code = f"{secrets.randbelow(10000):04d}"
    c.execute("INSERT OR REPLACE INTO email_codes VALUES(?,?,?,?,?)", (uid, code_hash(uid, code), time.time() + 600, 0, time.time()))
    return code, 0

def mail_code(email, code):
    return deliver(email, "Your BibGuard verification code", f"Your BibGuard verification code is: {code}\n\nIt expires in 10 minutes. If you did not sign up, you can ignore this email.")

@app.post("/api/register")
def api_register():
    if not rate_ok("register", 10, 3600): return jsonify(error="Too many sign-up attempts. Try again later."), 429
    d = request.get_json(silent=True) or {}
    email, pw = (d.get("email") or "").strip().lower(), d.get("password") or ""
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) or len(email) > 200: return jsonify(error="Enter a valid email address."), 400
    if len(pw) < 8: return jsonify(error="Password must be at least 8 characters."), 400
    if not email_on() and not is_local(): return jsonify(error="The email service is not set up on this server yet."), 503
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT id,verified FROM users WHERE email=?", (email,)).fetchone()
        if row and row[1]: return jsonify(error="This email is already registered. Please log in."), 409
        if row:  # unverified earlier attempt: the person who proves the email owns the account
            uid = row[0]
        else:
            if c.execute("SELECT COUNT(*) FROM users WHERE created>=?", (today(),)).fetchone()[0] >= int(os.getenv("MAX_SIGNUPS_PER_DAY", "500")):
                return jsonify(error="Sign-ups are paused today."), 429
            uid = c.execute("INSERT INTO users(email,pw,created,verified) VALUES(?,?,?,0)", (email, generate_password_hash(pw), now_s())).lastrowid
        code, wait = new_code(c, uid)
        if row and code is not None:
            c.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(pw), uid))
    if code is None: return jsonify(ok=True, verify=True, email=email, note="A code was sent a moment ago. Check your inbox and spam folder.")
    ok, note = mail_code(email, code)
    if not ok: return jsonify(error=note), 502
    return jsonify(ok=True, verify=True, email=email, note=note)

@app.post("/api/verify-email")
def api_verify_email():
    if not rate_ok("verify", 30, 900): return jsonify(error="Too many attempts. Try again in 15 minutes."), 429
    d = request.get_json(silent=True) or {}
    email, code = (d.get("email") or "").strip().lower(), re.sub(r"\D", "", d.get("code") or "")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        u = c.execute("SELECT id,verified,pw FROM users WHERE email=?", (email,)).fetchone()
        if not u: return jsonify(error="Wrong code or password."), 400
        if u[1]: return jsonify(error="This email is already verified. Please log in."), 400
        row = c.execute("SELECT hash,exp,attempts FROM email_codes WHERE uid=?", (u[0],)).fetchone()
        if not row or row[1] < time.time(): return jsonify(error="The code expired. Press Resend code."), 400
        if row[2] >= 5:
            c.execute("DELETE FROM email_codes WHERE uid=?", (u[0],)); return jsonify(error="Too many wrong tries. Press Resend code."), 400
        if (len(code) != 4 or not hmac.compare_digest(row[0], code_hash(u[0], code))
                or not isinstance(d.get("password"), str) or not check_password_hash(u[2], d["password"])):
            c.execute("UPDATE email_codes SET attempts=attempts+1 WHERE uid=?", (u[0],))
            return jsonify(error=f"Wrong code or password. {4 - row[2]} tries left."), 400
        c.execute("UPDATE users SET verified=1 WHERE id=?", (u[0],)); c.execute("DELETE FROM email_codes WHERE uid=?", (u[0],))
    start_session(u[0])
    return jsonify(ok=True)

@app.post("/api/resend-code")
def api_resend_code():
    if not rate_ok("resend", 8, 3600): return jsonify(error="Too many requests. Try again later."), 429
    email = ((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        u = c.execute("SELECT id,verified FROM users WHERE email=?", (email,)).fetchone()
        if not u or u[1]: return jsonify(ok=True, note="If this email is waiting for verification, a new code was sent.")
        code, wait = new_code(c, u[0])
    if code is None: return jsonify(error=f"Please wait {wait} seconds before asking for a new code."), 429
    ok, note = mail_code(email, code)
    return (jsonify(ok=True, note=note or "A new code was sent.") if ok else (jsonify(error=note), 502))

@app.post("/api/login")
def api_login():
    if throttled(): return jsonify(error="Too many attempts. Try again in 15 minutes."), 429
    d = request.get_json(silent=True) or {}
    with db() as c:
        r = c.execute("SELECT id,pw,verified FROM users WHERE email=?", ((d.get("email") or "").strip().lower(),)).fetchone()
    if not r or not check_password_hash(r[1], d.get("password") or ""):
        _fails.setdefault(client_ip(), []).append(time.time())
        return jsonify(error="Wrong email or password."), 401
    if not r[2]:
        email = d["email"].strip().lower()
        with db() as c: code, _ = new_code(c, r[0])
        note = ""
        if code: ok, note = mail_code(email, code)
        return jsonify(error="Please verify your email first. We sent you a 4-digit code.", needs_verify=True, email=email, note=note), 403
    start_session(r[0])
    return jsonify(ok=True)

@app.post("/api/google-login")
def api_google_login():
    """Google signs the user in and proves the email address is real. We only trust a token Google itself validates for OUR client id."""
    if not GOOGLE_CLIENT_ID: return jsonify(error="Google sign-in is not set up on this server."), 503
    if not rate_ok("google", 30, 900): return jsonify(error="Too many attempts. Try again later."), 429
    cred = ((request.get_json(silent=True) or {}).get("credential") or "")[:4000]
    try:
        r = requests.get("https://oauth2.googleapis.com/tokeninfo", params={"id_token": cred}, timeout=15)
        t = r.json() if r.status_code == 200 else {}
    except (requests.RequestException, ValueError):
        return jsonify(error="Could not reach Google. Try again."), 502
    email = (t.get("email") or "").lower()
    if (t.get("aud") != GOOGLE_CLIENT_ID or str(t.get("email_verified")).lower() != "true" or not email
            or t.get("iss") not in ("accounts.google.com", "https://accounts.google.com") or float(t.get("exp") or 0) < time.time()):
        return jsonify(error="Google sign-in could not be verified."), 401
    with db() as c:
        u = c.execute("SELECT id,verified FROM users WHERE email=?", (email,)).fetchone()
        if u:
            uid = u[0]
            if not u[1]:   # someone may have pre-registered this address with their own password: wipe it, Google proves the real owner
                c.execute("UPDATE users SET verified=1, pw=? WHERE id=?", (generate_password_hash(secrets.token_urlsafe(32)), uid))
        else:
            if c.execute("SELECT COUNT(*) FROM users WHERE created>=?", (today(),)).fetchone()[0] >= int(os.getenv("MAX_SIGNUPS_PER_DAY", "500")):
                return jsonify(error="Sign-ups are paused today."), 429
            uid = c.execute("INSERT INTO users(email,pw,created,verified) VALUES(?,?,?,1)", (email, generate_password_hash(secrets.token_urlsafe(32)), now_s())).lastrowid
    start_session(uid)
    return jsonify(ok=True)

@app.post("/api/logout")
def api_logout():
    session.clear(); return jsonify(ok=True)

@app.post("/api/forgot")
def api_forgot():
    if not rate_ok("forgot", 5, 3600): return jsonify(error="Too many requests. Try again in an hour."), 429
    if not email_on() and not is_local(): return jsonify(error="The email service is not set up on this server yet."), 503
    email = ((request.get_json(silent=True) or {}).get("email") or "").strip().lower()
    tok, now = None, time.time()
    with db() as c:
        r = c.execute("SELECT id FROM users WHERE email=? AND verified=1", (email,)).fetchone()
        if r:
            tok = secrets.token_urlsafe(32)
            c.execute("DELETE FROM resets WHERE uid=?", (r[0],))
            c.execute("INSERT INTO resets VALUES(?,?,?)", (hashlib.sha256(tok.encode()).hexdigest(), r[0], now + 3600))
    if tok:
        body = f"Someone asked to reset the password for this email.\n\nOpen this link within 1 hour:\n{base_url()}/?reset={tok}\n\nIf it was not you, ignore this email."
        if email_on(): threading.Thread(target=send_mail, args=(email, "Reset your BibGuard password", body), daemon=True).start()
        else: send_mail(email, "Reset your BibGuard password", body)
    msg = ("If this email is registered, a reset link has been sent. Check your inbox and spam folder." if email_on() else
           "EMAIL IS NOT SET UP, so nothing was emailed. If the address is registered, the reset link is printed in the black terminal window where you ran python app.py. To send real emails run: python setup_wizard.py")
    return jsonify(ok=True, message=msg)

@app.post("/api/reset")
def api_reset():
    d = request.get_json(silent=True) or {}
    pw = d.get("password") or ""
    if len(pw) < 8: return jsonify(error="Password must be at least 8 characters."), 400
    h = hashlib.sha256((d.get("token") or "").encode()).hexdigest()
    with db() as c:
        r = c.execute("SELECT uid,exp FROM resets WHERE hash=?", (h,)).fetchone()
        if not r or r[1] < time.time(): return jsonify(error="This reset link is invalid or has expired. Request a new one."), 400
        c.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(pw), r[0]))
        c.execute("DELETE FROM resets WHERE uid=?", (r[0],))
    return jsonify(ok=True)

@app.post("/api/change-password")
def api_change_password():
    uid, d = session.get("uid"), request.get_json(silent=True) or {}
    if not uid: return jsonify(error="Please log in."), 401
    if len(d.get("new") or "") < 8: return jsonify(error="New password must be at least 8 characters."), 400
    with db() as c:
        r = c.execute("SELECT pw FROM users WHERE id=?", (uid,)).fetchone()
        if not r or not check_password_hash(r[0], d.get("old") or ""): return jsonify(error="Current password is wrong."), 401
        c.execute("UPDATE users SET pw=? WHERE id=?", (generate_password_hash(d["new"]), uid))
    return jsonify(ok=True)

# ---------- payments ----------
def add_credits(c, uid, files, cents, ref, cur=None):
    try:
        c.execute("INSERT INTO payments(uid,ref,files,cents,ts,cur) VALUES(?,?,?,?,?,?)", (uid, ref, files, cents, now_s(), (cur or PAY_CURRENCY).lower()))
    except sqlite3.IntegrityError:
        return False  # already processed (webhook + return page can both arrive)
    c.execute("UPDATE users SET credits=credits+? WHERE id=?", (files, uid)); return True

def settle(o, uid_expected=None):
    """Validate a Stripe Checkout Session and grant credits exactly once. Returns (status, files)."""
    if o.get("payment_status") != "paid": return "unpaid", 0
    md = o.get("metadata") or {}
    try:
        uid, files, price = int(o.get("client_reference_id") or 0), int(md.get("files", 0)), int(md.get("price_cents", -1))
    except ValueError:
        return "invalid", 0
    if uid_expected and uid != uid_expected: return "other-user", 0
    if not (1 <= files <= 100000) or int(o.get("amount_total") or -2) != price or (o.get("currency") or "").lower() != (md.get("currency") or "").lower():
        return "mismatch", 0           # amount or currency does not match what we asked Stripe to charge
    with db() as c:
        if not c.execute("SELECT 1 FROM users WHERE id=?", (uid,)).fetchone(): return "invalid", 0
        new = add_credits(c, uid, files, price, o["id"], o.get("currency"))
    return ("granted" if new else "already"), files

def providers():
    if not CREDITS_ENABLED: return []
    return [n for n, on in (("paypal", PAYPAL_ID and PAYPAL_SECRET), ("stripe", STRIPE_KEY), ("lemon", LEMON_URL), ("link", PAYMENT_LINK)) if on]

def pp_token():
    r = requests.post(PAYPAL_BASE + "/v1/oauth2/token", auth=(PAYPAL_ID, PAYPAL_SECRET), data={"grant_type": "client_credentials"}, timeout=20)
    r.raise_for_status(); return r.json()["access_token"]

@app.post("/api/checkout")
def api_checkout():
    if not CREDITS_ENABLED: return jsonify(error="Payments and credits are disabled during the free beta."), 403
    uid, email = session.get("uid"), user_email()
    if not uid or not email: return jsonify(error="Please log in first."), 401
    prov = (request.get_json(silent=True) or {}).get("provider") or (providers() or [""])[0]
    if prov not in providers(): return jsonify(error="Payments are not enabled on this site yet."), 503
    detail = ""
    try:
        if prov == "stripe":
            data = {"mode": "payment", "success_url": base_url() + "/?paid=1&session_id={CHECKOUT_SESSION_ID}", "cancel_url": base_url() + "/?paid=0",
                    "client_reference_id": str(uid), "customer_email": email,
                    "metadata[files]": str(PACK_FILES), "metadata[price_cents]": str(PACK_PRICE_CENTS), "metadata[currency]": PAY_CURRENCY,
                    "payment_intent_data[receipt_email]": email, "payment_intent_data[description]": f"BibGuard pack: {PACK_FILES} file checks",
                    "line_items[0][quantity]": "1", "line_items[0][price_data][currency]": PAY_CURRENCY,
                    "line_items[0][price_data][unit_amount]": str(PACK_PRICE_CENTS),
                    "line_items[0][price_data][product_data][name]": f"BibGuard pack: {PACK_FILES} file checks"}
            r = requests.post("https://api.stripe.com/v1/checkout/sessions", auth=(STRIPE_KEY, ""), data=data, timeout=20)
            if r.status_code == 200: return jsonify(url=r.json()["url"])
            print("STRIPE ERROR:", r.status_code, r.text[:400], flush=True); detail = f"Stripe {r.status_code}: {r.text[:300]}"
        elif prov == "paypal":
            body = {"intent": "CAPTURE", "purchase_units": [{"custom_id": str(uid), "description": f"BibGuard pack: {PACK_FILES} file checks",
                    "amount": {"currency_code": PAY_CURRENCY.upper(), "value": f"{PACK_PRICE_CENTS / 100:.2f}"}}],
                    "payment_source": {"paypal": {"experience_context": {"return_url": base_url() + "/?pp=1", "cancel_url": base_url() + "/?pp=0",
                    "user_action": "PAY_NOW", "brand_name": "BibGuard", "shipping_preference": "NO_SHIPPING"}}}}
            r = requests.post(PAYPAL_BASE + "/v2/checkout/orders", json=body, timeout=20,
                              headers={"Authorization": "Bearer " + pp_token(), "PayPal-Request-Id": secrets.token_hex(12)})
            if r.status_code in (200, 201):
                url = next((l["href"] for l in r.json().get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
                if url: return jsonify(url=url)
            print("PAYPAL ERROR:", r.status_code, r.text[:400], flush=True); detail = f"PayPal {r.status_code}: {r.text[:300]}"
        elif prov == "lemon":
            q = f"checkout[email]={quote(email)}&checkout[custom][user_id]={uid}"  # brackets stay literal on purpose
            return jsonify(url=LEMON_URL + ("&" if "?" in LEMON_URL else "?") + q, provider="lemon", manual=not LEMON_SECRET)
        else:
            return jsonify(url=PAYMENT_LINK, manual=True)
    except (requests.RequestException, KeyError, ValueError) as e:
        print("PAYMENT ERROR:", repr(e), flush=True); detail = repr(e)[:300]
    return jsonify(error="The payment provider returned an error. Please try again, or contact us.", detail=(detail if is_local() else "")), 502

@app.get("/api/paypal-return")
def api_paypal_return():
    """Customer came back from PayPal: capture the order on the server and verify it before giving credits."""
    if not CREDITS_ENABLED: return jsonify(error="Payments and credits are disabled during the free beta."), 403
    uid, oid = session.get("uid"), request.args.get("order_id", "")
    if not uid: return jsonify(error="Please log in."), 401
    if not (PAYPAL_ID and PAYPAL_SECRET) or not re.fullmatch(r"[A-Za-z0-9]{10,40}", oid): return jsonify(error="Invalid payment reference."), 400
    try:
        h = {"Authorization": "Bearer " + pp_token()}
        r = requests.post(f"{PAYPAL_BASE}/v2/checkout/orders/{oid}/capture", headers={**h, "Content-Type": "application/json", "PayPal-Request-Id": "cap-" + oid}, data="{}", timeout=20)
        if r.status_code not in (200, 201):   # maybe already captured: read the order instead
            r = requests.get(f"{PAYPAL_BASE}/v2/checkout/orders/{oid}", headers=h, timeout=20)
        o = r.json()
    except (requests.RequestException, ValueError, KeyError):
        return jsonify(error="Could not reach PayPal. Please try again."), 502
    try:
        pu = o["purchase_units"][0]; cap = pu["payments"]["captures"][0]
        ok = (o.get("status") == "COMPLETED" and cap.get("status") == "COMPLETED" and str(pu.get("custom_id") or cap.get("custom_id")) == str(uid)
              and cap["amount"]["currency_code"].lower() == PAY_CURRENCY and round(float(cap["amount"]["value"]) * 100) == PACK_PRICE_CENTS)
    except (KeyError, IndexError, TypeError, ValueError):
        ok = False
    if not ok: return jsonify(error="The PayPal payment could not be verified. If money was taken, contact us with your order number: " + oid), 400
    with db() as c: new = add_credits(c, uid, PACK_FILES, PACK_PRICE_CENTS, "pp-" + o["id"], PAY_CURRENCY)
    return jsonify(ok=True, status="granted" if new else "already", files=PACK_FILES)

@app.get("/api/confirm-payment")
def api_confirm_payment():
    """Called when the customer returns from Stripe. Asks Stripe directly (not the browser) whether the payment succeeded."""
    if not CREDITS_ENABLED: return jsonify(error="Payments and credits are disabled during the free beta."), 403
    uid, sid = session.get("uid"), request.args.get("session_id", "")
    if not uid: return jsonify(error="Please log in."), 401
    if not STRIPE_KEY or not re.fullmatch(r"cs_[A-Za-z0-9_]{10,200}", sid): return jsonify(error="Invalid payment reference."), 400
    try:
        r = requests.get(f"https://api.stripe.com/v1/checkout/sessions/{sid}", auth=(STRIPE_KEY, ""), timeout=20)
        if r.status_code != 200: return jsonify(error="Could not confirm the payment yet."), 502
        status, files = settle(r.json(), uid)
    except (requests.RequestException, ValueError):
        return jsonify(error="Could not reach the payment provider."), 502
    if status in ("granted", "already"): return jsonify(ok=True, status=status, files=files)
    return jsonify(error={"unpaid": "The payment is not completed (yet). If you paid, wait a minute and refresh.", "other-user": "This payment belongs to another account."}.get(status, "The payment could not be verified.")), 400

@app.post("/api/lemon-webhook")
def lemon_webhook():
    """Lemon Squeezy: Settings -> Webhooks -> URL = https://YOURSITE/api/lemon-webhook, event = order_created."""
    if not CREDITS_ENABLED: return jsonify(error="Payments and credits are disabled during the free beta."), 403
    if not LEMON_SECRET: return "webhook not configured", 503
    raw, sig = request.get_data(), request.headers.get("X-Signature", "")
    if not hmac.compare_digest(hmac.new(LEMON_SECRET.encode(), raw, hashlib.sha256).hexdigest(), sig): return "bad signature", 400
    ev = json.loads(raw); meta, d = ev.get("meta") or {}, ev.get("data") or {}; at = d.get("attributes") or {}
    if meta.get("event_name") == "order_created" and at.get("status") == "paid":
        if LEMON_VARIANT and str((at.get("first_order_item") or {}).get("variant_id")) != LEMON_VARIANT: return "other product", 200
        cu, uid = (meta.get("custom_data") or {}).get("user_id"), None
        with db() as c:
            if cu and str(cu).isdigit() and c.execute("SELECT 1 FROM users WHERE id=?", (int(cu),)).fetchone(): uid = int(cu)
            else:
                r = c.execute("SELECT id FROM users WHERE email=? AND verified=1", ((at.get("user_email") or "").lower(),)).fetchone()
                uid = r[0] if r else None
            if uid: add_credits(c, uid, PACK_FILES, int(at.get("total") or 0), f"ls-{d.get('id')}", at.get("currency") or "usd")
            else: print("LEMON: paid order without a matching user:", d.get("id"), at.get("user_email"), flush=True)
    return "ok", 200

@app.post("/api/stripe-webhook")
def stripe_webhook():
    if not CREDITS_ENABLED: return jsonify(error="Payments and credits are disabled during the free beta."), 403
    if not STRIPE_WH: return "webhook not configured", 503
    payload, sig = request.get_data(), request.headers.get("Stripe-Signature", "")
    parts = [x.split("=", 1) for x in sig.split(",") if "=" in x]
    t = next((v for k, v in parts if k == "t"), "0"); v1 = [v for k, v in parts if k == "v1"]
    good = hmac.new(STRIPE_WH.encode(), t.encode() + b"." + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(good, v) for v in v1) or abs(time.time() - int(t or 0)) > 300:
        return "bad signature", 400
    ev = json.loads(payload)
    if ev.get("type") in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        settle(ev["data"]["object"])
    return "ok", 200

@app.post("/api/diff")
def api_diff():
    a, b = request.files.get("original"), request.files.get("modified")
    if not a or not b: return jsonify(error="Upload both the original and the AI-modified file."), 400
    try:
        original, modified = parse_bib(read_upload(a)), parse_bib(read_upload(b))
        A = {e["key"]: dict(e["fields"], entry_type=e["type"]) for e in original}
        B = {e["key"]: dict(e["fields"], entry_type=e["type"]) for e in modified}
        if len(A) != len(original) or len(B) != len(modified):
            return jsonify(error="Remove duplicate BibTeX keys before comparing the files."), 400
    except ValueError as e:
        return jsonify(error=str(e)), 400
    if not A or not B: return jsonify(error="Both files must contain at least one readable BibTeX entry."), 400
    changes = []
    for k in A:
        if k not in B: changes.append({"key": k, "field": "(entry)", "old": "present", "new": "REMOVED"}); continue
        for fld in sorted(A[k].keys() | B[k].keys()):
            o, n = A[k].get(fld, ""), B[k].get(fld, "")
            if o != n:
                changes.append({"key": k, "field": fld, "old": o or "(none)", "new": n or "(none)"})
    for k in B:
        if k not in A: changes.append({"key": k, "field": "(entry)", "old": "absent", "new": "ADDED"})
    return jsonify(changes=changes, checked=len(A), changed=len({c["key"] for c in changes}))

@app.post("/api/feedback")
def api_feedback():
    d = request.get_json(silent=True) or {}
    msg = (d.get("message") or "").strip()[:2000]
    try: rating = max(0, min(5, int(d.get("rating") or 0)))
    except (TypeError, ValueError): rating = 0
    if not msg and not rating: return jsonify(error="Please add a rating or a message."), 400
    kind = d.get("kind") if d.get("kind") in ("review", "suggestion", "bug", "contact") else "review"
    with db() as c:
        n = c.execute("SELECT COUNT(*) FROM feedback WHERE ip=? AND ts>=?", (client_ip(), today())).fetchone()[0]
        if n >= 5: return jsonify(error="Too many messages today. Please try again tomorrow."), 429
        c.execute("INSERT INTO feedback(ts,ip,rating,kind,message,email) VALUES(?,?,?,?,?,?)",
                  (time.strftime("%Y-%m-%d %H:%M"), client_ip(), rating, kind, msg, (d.get("email") or "")[:200]))
    return jsonify(ok=True)

SEARCH_CAP = int(os.getenv("SEARCH_CAP_PER_DAY", "100"))
STYLE_RE = re.compile(r"^[a-z0-9\-]{2,80}$")

@app.get("/api/search")
def api_search():
    failures = set()
    token = LOOKUP_FAILURES.set(failures)
    try:
        return search_with_sources(failures)
    finally:
        LOOKUP_FAILURES.reset(token)

def search_with_sources(failures):
    q = (request.args.get("q") or "").strip()[:300]
    if len(q) < 4: return jsonify(error="Type a title, authors, or a DOI (at least 4 characters)."), 400
    key = f"s:{client_ip()}"
    with db() as c:
        if used_today(c, key) >= SEARCH_CAP: return jsonify(error="Daily search limit reached. Try again tomorrow."), 429
        c.execute("INSERT INTO usage VALUES(?,?,1) ON CONFLICT(ip,day) DO UPDATE SET files=files+1", (key, today()))
    m = re.search(r"10\.\d{4,9}/\S+", q)
    if m:
        d = clean_doi(m.group(0)); r = crossref_doi(d) or openalex_doi(d); found = [r] if r else []
    else:
        # Context variables do not automatically follow executor tasks.
        from contextvars import copy_context
        with ThreadPoolExecutor(2) as ex:
            a = ex.submit(copy_context().run, crossref_search, q, 6)
            b = ex.submit(copy_context().run, openalex_search, q, 6)
            found = a.result() + b.result()
    if not found and failures:
        return jsonify(error="Search sources are temporarily unavailable. Please try again."), 503
    seen, out = set(), []
    for r in found:
        k = norm(r["doi"]) or norm(r["title"])
        if k and k not in seen and r["title"]:
            seen.add(k); out.append(public(r))
    out = out[:8]
    if SCOPUS_KEY and out:
        with ThreadPoolExecutor(4) as ex:
            for r, v in zip(out, ex.map(lambda r: scopus_indexed(r["doi"]), out)): r["scopus"] = v
    return jsonify(results=out)

@app.get("/api/cite")
def api_cite():
    """Any CSL citation style, formatted by the DOI registry itself (content negotiation)."""
    doi = clean_doi(request.args.get("doi", "")); style = (request.args.get("style") or "").strip().lower()
    if not doi or not STYLE_RE.match(style): return jsonify(error="Enter a valid CSL style name, e.g. nature or ieee."), 400
    try:
        r = requests.get(f"https://doi.org/{quote(doi, safe='/')}", headers={**UA, "Accept": f"text/x-bibliography; style={style}; locale=en-US"}, timeout=15)
        r.encoding = "utf-8"
    except requests.RequestException:
        return jsonify(error="The citation service is not reachable right now. Try again."), 502
    if r.status_code != 200 or not r.text.strip():
        return jsonify(error="Style not found, or this DOI is not supported. Check the CSL style name."), 404
    return jsonify(citation=re.sub(r"\s+", " ", r.text).strip())

def esc(x): return html.escape("" if x is None else str(x))

@app.before_request
def require_admin():
    if request.path != "/admin" and not request.path.startswith("/admin/"):
        return
    if ADMIN_KEY == "change-me" or not ADMIN_KEY:
        return "Admin access is disabled.", 403
    auth = request.authorization
    if (not auth or auth.type != "basic" or auth.username != "admin"
            or not hmac.compare_digest((auth.password or "").encode(), ADMIN_KEY.encode())):
        return "Admin authentication required.", 401, {"WWW-Authenticate": 'Basic realm="BibGuard admin"'}

@app.get("/admin")
def admin():
    with db() as c:
        fb = c.execute("SELECT ts,kind,rating,message,email FROM feedback ORDER BY id DESC LIMIT 300").fetchall()
        avg = c.execute("SELECT AVG(rating),COUNT(*) FROM feedback WHERE rating>0").fetchone()
        vis = c.execute("SELECT COUNT(DISTINCT ip), COALESCE(SUM(files),0) FROM usage WHERE ip LIKE 'ip:%'").fetchone()
        users = c.execute("SELECT id,email,CASE verified WHEN 1 THEN 'yes' ELSE 'NO' END,created,last_login,free_used,credits FROM users ORDER BY id DESC LIMIT 500").fetchall()
        pays = c.execute("SELECT p.ts,u.email,p.files,p.cents,p.ref,COALESCE(p.cur,'usd') FROM payments p LEFT JOIN users u ON u.id=p.uid ORDER BY p.id DESC LIMIT 200").fetchall()
    grant_form = ""
    if CREDITS_ENABLED:
        csrf = session.get("admin_csrf")
        if not csrf:
            csrf = session["admin_csrf"] = secrets.token_urlsafe(24)
        grant_form = (
            f"<h2>Give credits manually</h2><form method=post action=/admin/grant>"
            f"<input type=hidden name=csrf value='{esc(csrf)}'>"
            "<input name=email placeholder='user email' required> "
            "<input name=files type=number value=30 min=1 required> <button>Add credits</button></form>"
        )
    table = lambda head, rows: "<table><tr>" + "".join(f"<th>{h}" for h in head) + "</tr>" + "".join("<tr>" + "".join(f"<td>{esc(x)}" for x in r) + "</tr>" for r in rows) + "</table>"
    rev = {}
    for p_ in pays: rev[p_[5].upper()] = rev.get(p_[5].upper(), 0) + p_[3] / 100
    revenue = ", ".join(f"{v:.2f} {k}" for k, v in rev.items()) or "0"
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>BibGuard admin</title>"
      "<style>body{font:15px system-ui;margin:24px;max-width:1100px}table{border-collapse:collapse;width:100%;margin:8px 0 28px}td,th{border:1px solid #ccd;padding:6px;text-align:left;vertical-align:top}th{background:#eef}input{padding:6px}</style>"
      f"<h1>BibGuard admin</h1><p><a href='/admin/diagnostics'><b>Run diagnostics (email, Google, payments)</b></a> &nbsp; <a href='/admin/test-email?to='>Send test email</a></p><p><b>Users:</b> {len(users)} | <b>Visitors (IPs):</b> {vis[0]} | <b>Files checked:</b> {vis[1]} | <b>Avg rating:</b> {avg[0] and round(avg[0],2)} ({avg[1]}) | <b>Revenue (listed payments):</b> {revenue}</p>"
      f"{grant_form}"
      f"<h2>Users</h2>{table(['ID','Email','Verified','Joined','Last login','Free used','Credits'], users)}"
      f"<h2>Payments</h2>{table(['Time','User','Files','Cents','Reference','Currency'], pays)}"
      f"<h2>Feedback</h2>{table(['Time','Type','Stars','Message','Email'], fb)}")

@app.get("/admin/test-email")
def admin_test_email():
    """Open /admin/test-email?to=you@mail.com to see the exact SMTP result."""
    st, err = send_mail(request.args.get("to", ""), "BibGuard test email", "If you can read this, your SMTP settings work.")
    return f"Result: {esc(st)}<br>{esc(err)}<br><br>sent = delivered to the mail server. dev = SMTP_HOST is empty. fail = see the error above."

@app.post("/admin/grant")
def admin_grant():
    if not CREDITS_ENABLED: return jsonify(error="Payments and credits are disabled during the free beta."), 403
    csrf = session.get("admin_csrf")
    if not csrf or not hmac.compare_digest(csrf.encode(), request.form.get("csrf", "").encode()):
        return "Invalid request token.", 403
    try: files = max(1, min(int(request.form.get("files", "0")), 100000))
    except ValueError: return "Bad number", 400
    with db() as c:
        r = c.execute("SELECT id FROM users WHERE email=?", (request.form.get("email", "").strip().lower(),)).fetchone()
        if not r: return "No user with that email.", 404
        add_credits(c, r[0], files, 0, f"manual-{time.time()}")
    return redirect("/admin")

@app.get("/admin/diagnostics")
def admin_diagnostics():
    """Live check of email, Google sign-in and payments, with plain-language fixes."""
    rows = []
    add = lambda name, ok, text, fix="": rows.append((name, ok, text, fix))
    if BREVO_KEY and BREVO_SENDER:
        try:
            r = requests.get("https://api.brevo.com/v3/account", headers={"api-key": BREVO_KEY}, timeout=12)
            add("Email: Brevo API key", r.status_code == 200, f"Brevo answered {r.status_code}",
                "" if r.status_code == 200 else "Brevo refused the key. In Brevo: SMTP & API -> API Keys -> generate a new key, then run python setup_wizard.py.")
        except Exception as e:
            add("Email: Brevo API key", False, repr(e), "Cannot reach Brevo. Check your internet connection.")
        add("Email: sender address", True, BREVO_SENDER, "This exact address must be VERIFIED in Brevo (Senders). Then open /admin/test-email to send a real test.")
    elif SMTP_HOST:
        try:
            srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) if SMTP_PORT == 465 else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15)
            with srv:
                if SMTP_PORT != 465: srv.starttls()
                if SMTP_USER: srv.login(SMTP_USER, SMTP_PASS)
            add("Email: SMTP login", True, f"Logged in to {SMTP_HOST} as {SMTP_USER}", "Open /admin/test-email to send a real test message.")
        except smtplib.SMTPAuthenticationError:
            add("Email: SMTP login", False, "The mail server refused the login", "Gmail needs a 16-letter APP PASSWORD (2-Step Verification must be ON), not your normal password.")
        except Exception as e:
            add("Email: SMTP login", False, repr(e), "Port 587 may be blocked by your network. Use Brevo (HTTPS) or Google sign-in instead.")
    else:
        add("Email", False, "No email provider configured", "Run python setup_wizard.py (Gmail app password or Brevo), or enable Google sign-in below.")
    if GOOGLE_CLIENT_ID:
        ok = GOOGLE_CLIENT_ID.endswith(".apps.googleusercontent.com")
        add("Google sign-in", ok, GOOGLE_CLIENT_ID[:28] + "...", "" if ok else "A Google Client ID ends with .apps.googleusercontent.com. Copy it again from Google Cloud Console -> Credentials.")
    else:
        add("Google sign-in", False, "Not configured (optional)", "Verifies emails without sending any email. Put GOOGLE_CLIENT_ID in .env (see README).")
    if PAYPAL_ID and PAYPAL_SECRET:
        try:
            pp_token(); add("Payment: PayPal", True, f"Credentials accepted ({PAYPAL_BASE.split('//')[1]})", "sandbox = test money only. Use PAYPAL_MODE=live for real money.")
        except Exception as e:
            add("Payment: PayPal", False, repr(e)[:200], "Client ID / Secret are wrong, or sandbox and live are mixed up. Copy both from developer.paypal.com -> Apps & Credentials (Sandbox tab for testing).")
    if STRIPE_KEY:
        try:
            r = requests.get("https://api.stripe.com/v1/balance", auth=(STRIPE_KEY, ""), timeout=12)
            add("Payment: Stripe", r.status_code == 200, f"Stripe answered {r.status_code}", "" if r.status_code == 200 else "The secret key (sk_test_... or sk_live_...) is wrong.")
        except Exception as e:
            add("Payment: Stripe", False, repr(e)[:200], "Cannot reach Stripe.")
    if LEMON_URL: add("Payment: Lemon Squeezy", bool(LEMON_SECRET), "Buy link set" + ("" if LEMON_SECRET else ", webhook secret MISSING"), "Credits are added automatically only if LEMON_WEBHOOK_SECRET is set and the webhook URL is public (https).")
    if PAYMENT_LINK: add("Payment: manual link", True, PAYMENT_LINK[:60], "Credits are added by you on the admin page after a payment.")
    if not providers(): add("Payments", False, "No payment provider configured", "Run python setup_wizard.py and choose PayPal (or Lemon Squeezy). Until then the Buy button only shows a contact message.")
    add("Website address (BASE_URL)", bool(BASE_URL), BASE_URL or "(empty)", "Set BASE_URL=http://localhost:5000 while testing, or your real https address online. PayPal/Stripe return links and reset emails use it.")
    out = "".join(f"<tr><td>{'OK' if ok else 'FIX'}<td>{esc(n)}<td>{esc(t)}<td>{esc(f)}</tr>" for n, ok, t, f in rows)
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>BibGuard diagnostics</title>"
            "<style>body{font:15px system-ui;margin:24px;max-width:1000px}table{border-collapse:collapse;width:100%}td{border:1px solid #ccd;padding:8px;vertical-align:top}tr td:first-child{font-weight:700}</style>"
            f"<h1>Diagnostics</h1><p>Green = works. FIX = read the last column. <a href='/admin'>Back to admin</a></p><table>{out}</table>"
            "<script>document.querySelectorAll('tr').forEach(r=>{const c=r.firstElementChild.textContent;r.style.background=c==='OK'?'#e7f6ec':'#fdecea'})</script>")

def smtp_selfcheck():
    """At start-up: try to log in to the mail server and print the result, so you know email works BEFORE users sign up."""
    try:
        srv = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=20) if SMTP_PORT == 465 else smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20)
        with srv:
            if SMTP_PORT != 465: srv.starttls()
            if SMTP_USER: srv.login(SMTP_USER, SMTP_PASS)
        print(f"\nEMAIL OK: logged in to {SMTP_HOST} as {SMTP_USER}. Verification codes will be emailed.\n", flush=True)
    except smtplib.SMTPAuthenticationError:
        print("\nEMAIL PROBLEM: the mail server refused the login. For Gmail use a 16-letter APP PASSWORD, not your normal password. Run: python setup_wizard.py\n", flush=True)
    except Exception as e:
        print(f"\nEMAIL PROBLEM: could not reach {SMTP_HOST}:{SMTP_PORT} ({e!r}). Check internet / settings. Run: python setup_wizard.py\n", flush=True)

def brevo_selfcheck():
    try:
        r = requests.get("https://api.brevo.com/v3/account", headers={"api-key": BREVO_KEY}, timeout=15)
        print("\nEMAIL OK: Brevo API key accepted. Verification codes will be emailed from " + BREVO_SENDER + ".\n" if r.status_code == 200 else
              f"\nEMAIL PROBLEM: Brevo refused the API key (status {r.status_code}). Run: python setup_wizard.py\n", flush=True)
    except Exception as e:
        print(f"\nEMAIL PROBLEM: could not reach Brevo ({e!r}).\n", flush=True)

if BREVO_KEY and BREVO_SENDER:
    threading.Thread(target=brevo_selfcheck, daemon=True).start()
elif SMTP_HOST:
    threading.Thread(target=smtp_selfcheck, daemon=True).start()
else:
    print("\n*** EMAIL IS NOT SET UP: 4-digit codes and reset links will only appear in THIS window and users will NOT receive them. ***\n*** Fix: run  python setup_wizard.py  (or use Google sign-in), then start again. ***\n", flush=True)
if GOOGLE_CLIENT_ID: print("Google sign-in: ON\n", flush=True)
if CREDITS_ENABLED and not providers(): print("*** PAYMENTS ARE NOT SET UP: the Buy credits button will show a 'contact us' message. Run python setup_wizard.py to add PayPal. ***\n", flush=True)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
