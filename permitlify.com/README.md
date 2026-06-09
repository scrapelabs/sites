# Permitlify

AI-scored building permit alerts for US contractors. Daily permit intelligence delivered by email — built for roofing, HVAC, plumbing, electrical, and general contractors across all 50 states.

---

## What's Inside

| Area | What's built |
|------|-------------|
| **Marketing site** | Homepage, Pricing, How It Works, Our Story, Blog (7 articles), Careers, Press, Contact, Support, Privacy, Terms |
| **Auth** | Login, Sign-up, Logout — file sessions, SHA-256 hashed passwords |
| **Dashboard** | Permit feed with AI scores (0–100), trade badges, permit tier (🔥 Hot / Warm / Cold) |
| **Permit Feed** | Filterable table of sample permits with score bars and export controls |
| **Settings** | 4 tabs: Billing, Coverage, Alerts, API |
| **Billing tab** | Hero charge card, usage meters, plan selector (Monthly/Annual), payment methods, billing address (saves to DB), invoice history with CSV export and PDF print |
| **Profile** | Name, company, phone, email, password change |
| **Admin panel** | User management, plan stats, ban/unban, delete users |
| **REST API** | 4 endpoints — list permits, get permit, list cities, AI score |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.11 · Django 5.2 |
| Database | TinyDB (file-based JSON — zero migrations, zero SQL server) |
| Sessions | Django file-based sessions |
| Frontend | Plain HTML · CSS custom properties · Vanilla JS |
| Fonts | Plus Jakarta Sans · DM Sans · JetBrains Mono (Google Fonts) |
| Icons | Inline SVG throughout — no icon library dependency |
| Build step | **None** — no Node, no bundler, no compile step |

---

## Quick Start

### 1. Prerequisites

Python 3.11 or newer:

```bash
python3 --version   # must be 3.11+
```

### 2. Unzip and enter the project

```bash
unzip permitlify.zip
cd permitlify
```

### 3. Create a virtual environment

```bash
# macOS / Linux
python3 -m venv venv
source venv/bin/activate

# Windows (Command Prompt)
python -m venv venv
venv\Scripts\activate

# Windows (PowerShell)
python -m venv venv
venv\Scripts\Activate.ps1
```

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

Only two packages are required — Django and TinyDB:

```bash
pip install "django>=5.2" "tinydb>=4.8"
```

### 5. Start the server

```bash
python manage.py runserver 0.0.0.0:5000
```

Open **http://localhost:5000** in your browser.

That's it. No database setup, no migrations, no environment variables needed for local development. The `db.json` file and `sessions/` directory are created automatically on first run.

---

## Demo Account

A demo user is seeded automatically the first time the server starts:

| Field | Value |
|-------|-------|
| Email | `mk@permitdaily.com` |
| Password | `demo1234` |
| Plan | Agency |
| API Key | `pl_test_k7x2m9n4p8q1r5s3v6w0` |

This account also has **admin access** — you'll see the Admin Dashboard link in the sidebar.

---

## Project Structure

```
permitlify/
├── core/
│   ├── views.py             # All views: public pages, auth, dashboard, billing, API
│   ├── urls.py              # URL routing for all app pages and API endpoints
│   ├── db.py                # TinyDB helpers: create_user, update_user, authenticate, ban
│   ├── decorators.py        # @login_required decorator (session-based)
│   └── blog_articles.py     # 7 full-length blog articles (600–900 words each)
├── templates/
│   └── core/
│       ├── base.html            # Authenticated app shell (sidebar, topbar, plan card)
│       ├── public_base.html     # Public marketing shell (nav, footer)
│       ├── index.html           # Homepage — hero, features, testimonials, pricing CTA
│       ├── pricing.html         # Pricing — 3 tiers, monthly/annual toggle
│       ├── api_docs.html        # Developer docs — endpoints, auth, code samples
│       ├── dashboard.html       # App dashboard — permit cards, score meters
│       ├── permits.html         # Permit feed table — filterable, sortable
│       ├── settings.html        # Settings — Billing / Coverage / Alerts / API tabs
│       ├── invoice_print.html   # Standalone printable invoice (opens in new tab)
│       ├── profile.html         # Profile editor — name, company, phone, password
│       ├── admin_dashboard.html # Admin panel — user table, stats, ban/delete
│       ├── login.html           # Login — split-screen layout
│       ├── signup.html          # Sign-up — split-screen layout
│       ├── blog.html            # Blog index — featured + grid cards
│       ├── blog_post.html       # Blog article — full content + related sidebar
│       ├── how_it_works.html    # How It Works — step-by-step feature explainer
│       ├── careers.html         # Careers
│       ├── press.html           # Press Kit
│       ├── contact.html         # Contact form
│       ├── support.html         # Support / FAQ
│       ├── privacy.html         # Privacy Policy
│       └── terms.html           # Terms of Service
├── permitdaily/
│   ├── settings.py          # Django configuration
│   ├── urls.py              # Root URL conf (includes core.urls)
│   └── wsgi.py
├── static/                  # Static files directory (empty — styles are inline)
├── db.json                  # TinyDB user store (auto-created on first run)
├── sessions/                # File session storage (auto-created on first run)
├── requirements.txt         # Django + TinyDB
├── manage.py
└── README.md
```

---

## All Pages & Routes

### Public (no login required)

| URL | Page |
|-----|------|
| `/` | Homepage |
| `/pricing/` | Pricing — Starter / Pro / Agency |
| `/how-it-works/` | Feature walkthrough |
| `/developers/` | API documentation |
| `/blog/` | Blog index (7 articles) |
| `/blog/<slug>/` | Individual blog article |
| `/careers/` | Careers page |
| `/press/` | Press kit |
| `/contact/` | Contact form |
| `/support/` | Support & FAQ |
| `/privacy/` | Privacy Policy |
| `/terms/` | Terms of Service |
| `/login/` | Login |
| `/signup/` | Sign up |

### Authenticated app

| URL | Page |
|-----|------|
| `/dashboard/` | Dashboard — permit feed + stats |
| `/permits/` | Full permit table with filters |
| `/settings/` | Settings — Billing, Coverage, Alerts, API tabs |
| `/profile/` | Profile editor |
| `/admin-panel/` | Admin dashboard *(admin accounts only)* |

### Billing sub-routes

| URL | Action |
|-----|--------|
| `POST /settings/billing-address/` | Save billing address to DB (AJAX) |
| `/settings/invoices/export/` | Download invoices as CSV |
| `/settings/invoices/<id>/pdf/` | Open printable invoice page |

### REST API

| URL | Action |
|-----|--------|
| `GET /api/v1/permits/` | List permits — filter by `trade`, `city`, `min_score`, `tier` |
| `GET /api/v1/permits/<number>/` | Get single permit |
| `GET /api/v1/cities/` | List monitored cities |
| `POST /api/v1/score/` | AI-score a permit |

---

## Billing Features (Settings → Billing Tab)

### Plan cards
- Starter $29 / Pro $99 / Agency $249 per month
- Annual toggle shows 20% discounted prices ($23 / $79 / $199)
- Current plan is highlighted; upgrade triggers a checkout confirmation
- Downgrade shows a confirmation warning before proceeding

### Payment methods
- Two visual credit card renders (primary Visa + secondary Mastercard)
- Make Default swaps the DEFAULT badge live in the DOM
- Delete card shows a confirmation dialog

### Billing address
- Five editable fields: Company, Tax ID/EIN, Street, City, State/ZIP
- Edit → Save posts to `POST /settings/billing-address/` via AJAX with CSRF token
- Values are persisted in TinyDB and pre-populated on next page load

### Invoice history
- **Export All** → downloads `permitlify-invoices.csv` immediately
- **Preview / PDF ↓** → opens a printable invoice in a new tab
- Invoice amounts match the user's active plan price
- Print dialog lets you save as PDF; invoice fills the full page (Letter, 0.55" margins)

### Toast notifications
- All billing actions show a slide-up toast (success / info / warn / error)
- Destructive actions (pause, downgrade, delete card) show a confirmation dialog first

---

## REST API Reference

**Base URL (local):** `http://localhost:5000/api/v1/`

**Demo key:** `pl_test_k7x2m9n4p8q1r5s3v6w0`

Pass the key as a query parameter or header:

```
?api_key=pl_test_k7x2m9n4p8q1r5s3v6w0
Authorization: Bearer pl_test_k7x2m9n4p8q1r5s3v6w0
```

### GET /api/v1/permits/

Optional filters:

| Param | Example | Description |
|-------|---------|-------------|
| `trade` | `Roofing` | Filter by trade type |
| `city` | `Fort Worth` | Filter by city |
| `min_score` | `75` | Minimum AI score (0–100) |
| `tier` | `hot` | `hot` / `warm` / `cold` |

```bash
curl "http://localhost:5000/api/v1/permits/?api_key=pl_test_k7x2m9n4p8q1r5s3v6w0&tier=hot"
curl "http://localhost:5000/api/v1/permits/?api_key=pl_test_k7x2m9n4p8q1r5s3v6w0&trade=Roofing&min_score=80"
```

### GET /api/v1/permits/\<number\>/

```bash
curl "http://localhost:5000/api/v1/permits/FW-2026-04192/?api_key=pl_test_k7x2m9n4p8q1r5s3v6w0"
```

### GET /api/v1/cities/

```bash
curl "http://localhost:5000/api/v1/cities/?api_key=pl_test_k7x2m9n4p8q1r5s3v6w0"
```

### POST /api/v1/score/

Body (JSON):

```json
{ "trade": "Roofing", "value": "$18,500", "city": "Fort Worth" }
```

```bash
curl -X POST "http://localhost:5000/api/v1/score/?api_key=pl_test_k7x2m9n4p8q1r5s3v6w0" \
  -H "Content-Type: application/json" \
  -d '{"trade": "Roofing", "value": "$18,500", "city": "Fort Worth"}'
```

---

## Admin Panel

Access at `/admin-panel/` when logged in as an admin account.

**Admin emails** are configured in `core/views.py`:

```python
ADMIN_EMAILS = {'mk@permitdaily.com'}
```

Add any email address to this set to grant admin access. The Admin Dashboard link appears automatically in the sidebar for admin accounts.

Admin capabilities:
- View all registered users with plan, join date, and status
- Delete users (with self-delete protection)
- Ban email addresses (blocks signup and login)
- Unban email addresses
- Plan distribution stats (Starter / Pro / Agency counts)

---

## Authentication System

- Passwords hashed with SHA-256 + salt (`permitdaily_salt_v1`)
- Sessions stored as files in the `sessions/` directory
- `@login_required` decorator redirects unauthenticated users to `/login/`
- No Django auth framework — fully custom, lightweight session system

To change the password salt (recommended for production):

```python
# core/db.py  line ~18
SALT = 'your-custom-salt-here'
```

Note: changing the salt invalidates all existing passwords.

---

## Deploying to Production

Permitlify is a standard Django WSGI app — deploy anywhere Python runs.

### Railway *(recommended — free tier available)*

```bash
# 1. Push to GitHub
# 2. railway.app → New Project → Deploy from GitHub repo
# 3. Set environment variable:
SECRET_KEY=<your-secret-key>
# Railway auto-detects Django and sets PORT automatically
```

### Render

1. Connect your GitHub repo at [render.com](https://render.com)
2. **Build command:** `pip install -r requirements.txt`
3. **Start command:** `gunicorn permitdaily.wsgi --bind 0.0.0.0:$PORT`
4. Add env var: `SECRET_KEY=<your-secret-key>`

Install gunicorn first:
```bash
pip install gunicorn
echo "gunicorn>=21.0" >> requirements.txt
```

### Heroku

```bash
echo "web: gunicorn permitdaily.wsgi" > Procfile
pip install gunicorn && echo "gunicorn>=21.0" >> requirements.txt
heroku create my-permitlify
heroku config:set SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
git push heroku main
heroku open
```

### VPS / Ubuntu

```bash
pip install gunicorn
gunicorn permitdaily.wsgi:application --bind 0.0.0.0:8000 --workers 2
```

Use Nginx as a reverse proxy in front of Gunicorn. Point your domain at port 8000.

---

## Environment Variables

| Variable | Default | Required in prod? |
|----------|---------|-------------------|
| `SECRET_KEY` | weak dev key | **Yes** — generate a new one |
| `DEBUG` | `True` | Set to `False` |
| `ALLOWED_HOSTS` | `['*']` | Set to your actual domain |

Generate a strong secret key:

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Set them in `permitdaily/settings.py` or via environment variable:

```python
import os
SECRET_KEY = os.environ.get('SECRET_KEY', 'fallback-only-for-dev')
DEBUG = os.environ.get('DEBUG', 'True') == 'True'
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '*').split(',')
```

---

## Pricing Configuration

Plan prices are set in `core/views.py`:

```python
PLAN_PRICE = {'starter': 29, 'pro': 99, 'agency': 249}
```

Checkout links (Lemon Squeezy) are referenced in the billing JS inside `templates/core/settings.html`. Update them to point to your real product checkout URLs.

---

## Customising the Demo Data

**Permit feed:** Edit `SAMPLE_PERMITS` in `core/views.py` — each permit is a plain Python dict.

**City list:** Edit `SAMPLE_CITIES` in `core/views.py`.

**Blog articles:** Edit `core/blog_articles.py` — each article is a dict with `slug`, `title`, `author`, `date`, `body` (HTML string), and `tags`.

---

## requirements.txt

```
Django==5.2.13
tinydb==4.8.2
```

---

## License

MIT — see `LICENSE` for details.
