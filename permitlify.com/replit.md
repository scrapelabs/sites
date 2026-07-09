# PermitDaily (Permitlify)

## Overview
PermitDaily is a Django platform for building-permit intelligence and AI-scored lead generation for contractors. It ingests municipal permit data, normalizes it into Postgres, scores lead quality, and exposes filtered permit dashboards, admin tools, billing, support, and CRM/API integrations.

## User Preferences
The user prefers iterative development, concise communication, and simple language for day-to-day updates. Ask before significant architectural changes or new external paid dependencies. Do not make changes to `db.json` unless explicitly requested.

## PR / GitHub Workflow Rules
1. Always check GitHub PR state before opening or pushing to a PR.
2. If the previous PR is already merged, open a new PR off `main`.
3. If the previous PR is still open, push follow-up commits to the same branch.
4. The user merges PRs quickly, so never assume a PR is still open.
5. Continue using the single-atomic-commit-via-tree-API pattern and always reply with the PR link.

## System Architecture

### UI/UX Decisions
The UI uses Django templates with vanilla CSS and JavaScript, including DM Sans, Plus Jakarta Sans, and JetBrains Mono. Inner app pages use a sticky topbar and fixed sidebar. Core UI elements include cards, permit tables, score indicators, status badges, modals, settings tabs, profile forms, and admin dashboards.

### Technical Implementations
The backend uses Django 5.2 and Python 3.11+. Sessions are file-backed under `sessions/`. Protected views use login/admin decorators. The primary database is Supabase Postgres via `psycopg` and `psycopg_pool`.

## Feature Specifications
* **Authentication & User Management:** Users live in the `users` table with unique emails. Google Sign-In uses a stdlib OAuth authorization-code flow. Runtime Google OAuth settings are managed from `/admin-panel/google-settings/`.
* **Permit Management:** Scraped permit records live in `permits` with scoring, status, trade, dates, contact fields, and raw scrape lineage. `/permits/` uses DataTables server-side processing with parameterized SQL and city subscription authorization.
* **Notifications:** Database-backed notification center with pagination, type filters, opened state, Slack, and generic webhook delivery preferences.
* **Support:** Full ticketing flow for user tickets and admin ticket management.
* **API Portal:** Agency-only API key management, playground, and permit usage statistics.
* **CRM Integrations:** HubSpot, GoHighLevel, and Zapier integrations for pushing lead data.
* **Billing Receipts (Whop):** Payment-success emails are deduped per `(user_id, membership_id, plan)` across redirect and webhook paths.
* **Admin Blog Editor:** `/admin-panel/blog/` uses local Playwright rendering for source URL scraping, then Claude rewrite to produce reviewed HTML blog drafts. Blog settings manage Claude fields plus optional `datacenter_proxy` for browser scraping.

## Scraper System
* `/admin-panel/scrapers/` is a server-side DataTable for configured Accela sources with state/city filters and global run controls.
* `/admin-panel/scrapers/<sid>/` shows scraper detail, permit data, run controls, logs, source modal access, and local scraper-agent settings.
* The scraper path is deterministic-first: HTTP/Playwright page fetching, Accela parser fallbacks, local/OSS model enrichment where useful, and Claude only for remaining extraction gaps.
* The default run branch uses the local scraper agent to paginate list pages, open detail pages, upsert rows incrementally, and write progress to `scraper_runs`.
* The Accela finder at `/admin-panel/scrapers/accela-search/` uses local web search, DO/local OSS inference, and Playwright verification to discover usable `*.accela.com` search URLs.
* `junk_permits` records non-actionable rows so reruns skip known no-contact permit numbers before spending fetch or LLM work again.
* Cross-source permit dedup uses `permits.dedup_hash` and normalized identity/contact/value fields.

## External Dependencies
* **Database:** Supabase Postgres via `psycopg` and `psycopg_pool`.
* **Browser Rendering:** Playwright with local Chrome/Edge fallback and optional `DATACENTER_PROXY` / `datacenter_proxy`.
* **AI:** Local GPT-OSS/DO-compatible inference for scraper enrichment and Claude for blog rewriting or extraction fallback where configured.
* **CRM:** HubSpot OAuth, GoHighLevel OAuth, and Zapier webhooks.
* **Notifications:** Slack webhooks and generic JSON webhooks.
