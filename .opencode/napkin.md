## 2026-09-17
- [runtime] Permitlify uses `permitlify.com/.venv/Scripts/python.exe`; the system Python lacks application dependencies such as psycopg. Use the site venv for `manage.py check` and Django tests.
- [model API] User chose a dedicated Bearer-key API at `https://permitlify.com/ai/v1`. Caddy forward-auth calls `core/ai_api.py`, then streams directly to loopback 8010. `open_oss_20b/api_key.ps1` manages user-bound encrypted credentials and a server-side verifier; never copy credential contents into notes.
- [Windows service] GptOss20B now runs the repository's `permitlify.com/open_oss_20b` bundle, with model alias `gpt-oss-20b`, 4 threads, and context 8192. The prior Downloads deployment is no longer the active service path.
- [Caddy] A top-level `respond` can sort after a catch-all `handle`; private-route denials should use matched `handle` blocks. Verified with isolated proxy tests covering 95 requests.
- [transport] Public TLS terminates at Cloudflare; the existing origin connection uses HTTP port 80. Full (strict) TLS or a tunnel requires coordinated origin/Cloudflare configuration. See the model README and BMAD deferred work.
