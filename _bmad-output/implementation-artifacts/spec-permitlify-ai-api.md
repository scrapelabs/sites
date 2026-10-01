---
title: Permitlify dedicated-key GPT-OSS API
type: feature
created: 2026-09-17
status: done
baseline_commit: 00e3eccc1a3f3c250b814ff609aea2d6fb3405e4
review_loop_iteration: 0
context: []
---

<frozen-after-approval reason="User selected option A: /ai/v1 with a dedicated API key">

## Intent

**Problem:** The GPT-OSS bundle has a localhost-only test script and no working online API route. Its launcher conflicts with Django's port 8000, while the installed model service uses port 8010 and an older Downloads directory.

**Approach:** Expose the OpenAI-compatible model API at `https://permitlify.com/ai/v1`, authenticated with a dedicated Bearer key. Supply a working `open_oss_20b/test.py` and README describing calls, credentials, deployment, and troubleshooting.

## Boundaries & Constraints

**Always:** Keep llama.cpp bound to loopback port 8010. Use HTTPS for remote clients. Protect every published model endpoint. Preserve streaming through Caddy. Keep secret values out of source, configuration, logs, and documentation. Preserve unrelated worktree changes.

**Ask First:** Destructive data changes, replacement of unrelated services, or changing the approved authentication contract.

**Never:** Publish llama.cpp administration/UI endpoints; introduce a database dependency for model authentication; commit or push without a request.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|----------|---------------|----------------------------|----------------|
| Model discovery | Valid Bearer key; GET /ai/v1/models | OpenAI model list | Real HTTP status retained |
| Chat | Valid key; POST /ai/v1/chat/completions | JSON answer or SSE stream | Upstream errors propagate |
| Responses API | Valid key; POST /ai/v1/responses | Upstream response | Optional smoke test for backend compatibility |
| Bad credentials | Missing, wrong, malformed, or query-string-only key | 401 JSON; no model invocation | No submitted credentials echoed |
| Missing configuration | Missing, unreadable, or malformed key hash | 503 JSON | Fail closed |
| Unpublished path | Valid key; /ai/slots or other llama.cpp path | 404 | Model management stays private |
| Test failure | Timeout, HTTP error, malformed JSON/SSE, or empty answer | Nonzero exit | Concise diagnostic; no interactive pause |

</frozen-after-approval>

## Code Map

- `0_setup/Caddyfile` -- Cloudflare-facing proxy, currently forwards all Permitlify requests to Django on port 8000.
- `permitlify.com/core/urls.py` -- Django routes; add an isolated forward-auth endpoint.
- `permitlify.com/core/ai_api.py` -- new constant-time hash-based authentication check; no inference in Django workers.
- `permitlify.com/open_oss_20b/` -- existing binary/model bundle, launchers, smoke client, documentation.

## Tasks & Acceptance

**Execution:**
- [x] `core/ai_api.py`, `core/urls.py`, `core/tests/test_ai_api.py` -- implement and test fail-closed Bearer authentication.
- [x] `0_setup/Caddyfile` -- forward-auth all /ai requests, allow only the three API endpoints/methods, strip /ai, proxy streaming to 8010, disable response caching, and bound request bodies.
- [x] `open_oss_20b/api_key.ps1`, `.gitignore` -- create a cryptographically random dedicated key; store the client credential using Windows user-bound DPAPI and publish only its SHA-256 verifier. Support loading and explicit rotation.
- [x] `open_oss_20b/start_server.bat`, `start_llama.bat` -- align loopback port 8010 and model alias gpt-oss-20b. Align the existing NSSM service with this bundle while preserving its four inference threads and context size.
- [x] `open_oss_20b/test.py`, `test_server.bat`, `tests/test_client.py` -- online-first, environment-configurable smoke client with bounded waits, failure exit codes, model discovery, chat, streaming, and optional Responses coverage.
- [x] `open_oss_20b/README.md` -- executable PowerShell/Python examples, credential loading/rotation, exact public/local URLs, service commands, rollback, and troubleshooting.
- [x] Review changes, run isolated unit checks and Caddy validation, deploy affected services, then prove missing/wrong keys fail and HTTPS model discovery/chat/streaming succeed.

**Acceptance Criteria:**
- Given the user's approved dedicated-key design, when a remote Python client uses the documented base URL and credential, then a real model answer is returned over HTTPS.
- Given normal website traffic, when the AI route is installed, then the Permitlify and GoldenProxies homepages still respond successfully.
- Given the installed model service, when it is restarted, then its executable/model paths use the requested repository bundle and model discovery advertises a stable alias.

## Design Notes

Caddy runs a lightweight Django forward-auth check and then streams directly from llama.cpp. This preserves existing unauthenticated loopback consumers and keeps slow inference out of Waitress workers. The server stores a verifier of a 256-bit random API token, not the token itself. The local test credential is encrypted with Windows DPAPI for the provisioning user; rotation replaces the verifier atomically and takes effect without restarting Django. The key does not grant Permitlify account access.

## Verification

- `python manage.py test core.tests.test_ai_api` -- isolated authentication tests, no application database access.
- `python -m unittest discover -s open_oss_20b/tests -v` -- smoke-client error and stream regression tests.
- `python manage.py check` -- Django system checks pass.
- `0_setup/caddy.exe validate --config 0_setup/Caddyfile --adapter caddyfile` -- valid proxy configuration.
- `python test.py` with the provisioned environment key -- real HTTPS model discovery, chat, and streaming; exit 0 only on success.
- HTTP probes -- absent/invalid keys return 401; unpublished routes return 404; existing homepages remain healthy.

### Verified results (2026-09-17)

- Django authentication: 7 tests passed; Django system check passed using the site's `.venv`.
- Smoke client: 31 offline tests passed.
- Key provisioning: 8 Windows PowerShell 5.1 tests passed, including process contention, rotation, and exact ACL enforcement.
- Actual isolated Caddy: 11 integration tests covering 95 HTTP cases passed, including body limits and incremental SSE delivery.
- Live `python test.py --responses`: model discovery, JSON chat, streaming, and Responses API all passed over public HTTPS.
- Public requests without a key and with a wrong key: 401. Authenticated management and direct internal-auth paths: 404. Responses have no-store and no wildcard CORS.
- README requests example: completed answer correctly identifies Paris.
- NSSM GptOss20B now runs the repository bundle on loopback 8010, advertising `gpt-oss-20b`; legacy loopback model ID still returns 200.
- Permitlify public homepage: 200. GoldenProxies origin through Caddy: 200; public automated probe receives Cloudflare's challenge (403, `Cf-Mitigated: challenge`). All four Windows services are running.
- Review fixes: matched Caddy deny handler, serialized key lifecycle, verifier consistency checks, exact private ACLs, and opt-in batch pause. Security follow-up found no blockers in these corrections.
- Existing Cloudflare-to-origin HTTP transport is documented and tracked in `deferred-work.md` for a coordinated infrastructure change.

## Suggested Review Order

**Public boundary**
- Authenticate before publishing the three model API routes; preserve native streaming.
  [`Caddyfile:32`](../../0_setup/Caddyfile#L32)
- Authorize using a constant-time verifier check without database access.
  [`ai_api.py:28`](../../permitlify.com/core/ai_api.py#L28)

**Credential lifecycle and client**
- Protect credential storage and serialize rotation without reviving old verifiers.
  [`api_key.ps1:21`](../../permitlify.com/open_oss_20b/api_key.ps1#L21)
- Execute bounded, non-interactive model checks with meaningful failure status.
  [`test.py:339`](../../permitlify.com/open_oss_20b/test.py#L339)
- Follow executable online calling and deployment instructions.
  [`README.md:1`](../../permitlify.com/open_oss_20b/README.md#L1)

**Regression coverage**
- Verify fail-closed authentication and immediate revocation.
  [`test_ai_api.py:8`](../../permitlify.com/core/tests/test_ai_api.py#L8)
- Exercise malformed streams, HTTP failures, and credential-safe diagnostics.
  [`test_client.py:99`](../../permitlify.com/open_oss_20b/tests/test_client.py#L99)
- Exercise real Windows credential lifecycle, locking, and access controls.
  [`test_api_key.ps1:91`](../../permitlify.com/open_oss_20b/tests/test_api_key.ps1#L91)
