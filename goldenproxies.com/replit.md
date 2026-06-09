# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.
Also includes a Django-based web app (GoldenProxies) that runs alongside the Node.js services.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Python**: 3.11 (for Django)

## Artifacts

### GoldenProxies (`artifacts/goldenproxies`)
- **Preview path**: `/`
- **Type**: Django 5 web app (Python)
- **Django project**: `artifacts/goldenproxies-django/`
- **Start command**: `bash /home/runner/workspace/artifacts/goldenproxies-django/start.sh`
- **Port**: 24289 (set via `PORT` env var)
- **Database**: SQLite (`artifacts/goldenproxies-django/db.sqlite3`) — wiped & recreated on each restart
- **Theme**: Gold & cream luxury proxy services — glassmorphism, gold shimmer buttons, Tailwind CDN
- **Auth**: Django built-in username/password auth (email used as username)
  - Super admin: `khemiri.mohamed.ensi@gmail.com` / password: `admin123` (auto-created on startup)
  - Admin panel: `/admin-panel/` (requires super admin email)

### Pages

**Public**
- `/` — Hero landing page
- `/pricing/` — Plan pricing
- `/contact/` — Contact form
- `/login/` — Sign in
- `/register/` — Create account

**Dashboard (login required)**
- `/dashboard/` — Overview
- `/dashboard/generator/` — Proxy generator
- `/dashboard/stats/` — Usage stats
- `/billing/` — Subscription management (plan picker, invoice history)
- `/billing/checkout/<plan>/<period>/` — Redirect to Whop checkout
- `/billing/success/` — Post-payment success page (activates subscription)
- `/billing/cancel/` — Cancel subscription (POST)
- `/billing/portal/` — Redirect to Whop hub
- `/billing/webhook/` — Whop webhook receiver (csrf-exempt)
- `/dashboard/support/` — Support tickets
- `/dashboard/settings/` — Profile settings

**Admin (super admin only)**
- `/admin-panel/` — Overview: KPIs, MRR, ARR, plan distribution, recent invoices
- `/admin-panel/users/` — User list with search; click through to user detail
- `/admin-panel/users/<id>/` — Per-user billing management (plan override, mode, Whop resync)
- `/admin-panel/invoices/` — All invoices with total revenue
- `/admin-panel/purchases/` — Purchase orders
- `/admin-panel/messages/` — Support inbox with reply composer
- `/admin-panel/whop-settings/` — Full Whop integration config (API key, plan IDs, checkout URLs, webhook info)
- `/admin-panel/whop-resync-all/` — Bulk resync all memberships from Whop

### Models
- `SystemSetting` — DB-backed key/value store for Whop settings (cached 60s in-process)
- `UserProfile` — Extends User with plan, Whop membership fields, billing dates, mode
- `Invoice` — Invoice records synced from Whop (auto-created on checkout success & webhook)
- `Purchase` — Individual proxy product purchases
- `SupportMessage` — Support tickets with priority and admin replies

### Whop Integration (`core/whop.py`)
- **Plans**: Starter ($29/mo, $23/mo annual), Pro ($99/mo, $79/mo annual), Business ($249/mo, $199/mo annual)
  - Internal key: `starter/pro/agency` (displays as Starter/Pro/Business)
- **Settings storage**: `SystemSetting` model, accessible via admin Whop Settings page
- **Billing flow**: checkout → Whop → success redirect → activate profile → webhook keeps in sync
- **Dev mode**: per-user `whop_mode='dev'` makes all plans $1 for testing
- **Webhook events handled**: `membership.went_valid`, `membership.was_revoked`, `membership.expired`, `membership.updated`, `membership.was_cancelled`, `membership.was_paused`
- **Signature verification**: HMAC-SHA256, secret stored in `SystemSetting(key='whop_webhook_secret')`

### Key Files
- `core/whop.py` — Whop API client, plan pricing, checkout URL builder, webhook helpers
- `core/views.py` — all views including full billing flow + admin
- `core/models.py` — database models
- `core/urls.py` — URL routing
- `core/forms.py` — auth & support forms
- `core/templatetags/core_extras.py` — `dict_get` and `get_item` template filters
- `core/templates/base.html` — master layout with glassmorphism CSS classes
- `core/templates/dashboard/base.html` — dashboard sidebar (Billing + admin Whop Settings links)
- `core/templates/dashboard/billing.html` — billing dashboard with plan picker + invoice table
- `core/templates/billing/success.html` — post-payment success page
- `core/templates/admin/whop_settings.html` — Whop config UI with webhook info
- `core/templates/admin/user_detail.html` — per-user billing management
- `core/templates/admin/invoices.html` — invoice history
- `goldenproxies/settings.py` — Django settings
- `start.sh` — deletes DB, runs makemigrations + migrate, auto-creates super admin, starts server

### API Server (`artifacts/api-server`)
- **Preview path**: `/api`
- **Type**: Express 5 Node.js server

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-server run dev` — run API server locally
- `cd artifacts/goldenproxies-django && python manage.py shell` — Django shell

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.
