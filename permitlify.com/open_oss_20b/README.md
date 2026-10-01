# GPT-OSS 20B API on Permitlify

Run the local **gpt-oss-20b** GGUF model using llama.cpp and call it online through
an OpenAI-compatible API protected by a **dedicated Bearer API key**.

| Setting | Value |
|---|---|
| Public API base URL | `https://permitlify.com/ai/v1` |
| Model ID | `gpt-oss-20b` (or discover it with `GET /models`) |
| Local model API | `http://127.0.0.1:8010/v1` |
| Windows model service | `GptOss20B` |
| Application port | `8000` -- reserved for Django/Waitress |

## Quick start: test the online API

Open **PowerShell as Administrator**, under the Windows account that provisioned
the key. From this `open_oss_20b` directory, run:

```powershell
# The site virtual environment already includes requests.
# On another computer: python -m pip install requests

# Creates the key once, or loads the existing key into this process's environment.
.\api_key.ps1
python .\test.py
```

`test.py` defaults to `https://permitlify.com/ai/v1` and reads the credential from
`PERMITLIFY_AI_API_KEY`. A successful run prints:

```text
Model discovery: OK
JSON chat: OK (non-empty, completed assistant answer)
Streaming chat: OK (answer, completion and [DONE])
All requested smoke tests passed.
```

It discovers the model before calling it, checks for an actual assistant answer
(reasoning alone is insufficient), and verifies the streaming completion marker.
It prints check results rather than raw response bodies. Exit codes: **0** success,
**1** failed check/configuration/network request, **2** invalid arguments. Python
does not pause; `test_server.bat` forwards arguments and preserves the exit code.
Set `PERMITLIFY_AI_PAUSE=1` if you want the batch wrapper to wait before closing.

```powershell
python .\test.py --no-stream
python .\test.py --prompt "What is the capital of France? Answer briefly." --max-tokens 256
python .\test.py --timeout 120 --responses  # additionally check the Responses API
python .\test.py --api-url http://127.0.0.1:8010/v1 --no-stream
python .\test.py --help
```

The default timeout is 120 seconds per connect/read operation, with an additional
streaming progress deadline. The Responses API is opt-in because support depends
on the llama.cpp build. Selecting it makes an unsupported endpoint a test failure.
The client requires HTTPS and a key for remote hosts, allows unauthenticated
loopback HTTP, and does not follow redirects or use environment proxy/.netrc
settings. Set credentials in the environment rather than `--api-key` so they do
not appear in shell history or process arguments. URLs cannot contain credentials,
query strings, or fragments.

## Credential storage and rotation

`api_key.ps1` creates a cryptographically random 256-bit token with a `plai_`
prefix. It stores files in the Git-ignored, access-restricted `.secrets/`:

- `api-key.dpapi`: client credential encrypted with **Windows user-bound DPAPI**;
  only the provisioning Windows account on this machine can decrypt it.
- `api-key.sha256`: the SHA-256 verifier read by Django. The API server does not
  need the plaintext key. An absent/unreadable/invalid verifier fails closed with
  HTTP 503.
- `api-key.lock`: an exclusive cross-process lock; concurrent helper invocations
  fail with a retry message. The file stays in place after the lock is released.

The helper loads `$env:PERMITLIFY_AI_API_KEY` without printing the token. Run the
script **in the same PowerShell session** as Python; running a separate
`powershell.exe -File ...` process will not set the parent session's environment.
If execution policy blocks it, invoke `Set-ExecutionPolicy -Scope Process Bypass`
for that session before running the helper.

For another computer, transfer the key using your secret manager and set
`PERMITLIFY_AI_API_KEY` in that client's environment. The encrypted DPAPI file is
not portable. The dedicated key is separate from Permitlify account/Agency API
keys. Do not put it in browser JavaScript or a URL.

```powershell
.\api_key.ps1 -Rotate   # explicitly replace the key; immediately revokes the old key
python .\test.py       # uses the newly loaded credential
```

Rotation atomically replaces the verifier; no service restart is required.
Ordinary loading checks that the credential matches the verifier and leaves both
files intact. Missing or mismatched state requires explicit `-Rotate` recovery.
Previously issued credentials have no automatic expiry; rotate them to revoke
access. If only the verifier exists, the helper requires `-Rotate` rather than
silently replacing an existing credential.

## Calling the API from Python

### requests

```python
import os
import requests

response = requests.post(
    "https://permitlify.com/ai/v1/chat/completions",
    headers={"Authorization": f"Bearer {os.environ['PERMITLIFY_AI_API_KEY']}"},
    json={
        "model": "gpt-oss-20b",
        "messages": [{"role": "user", "content": "What is the capital of France?"}],
        "max_tokens": 256,
        "temperature": 0,
    },
    timeout=120,
    allow_redirects=False,
)
response.raise_for_status()
print(response.json()["choices"][0]["message"]["content"])
```

### OpenAI SDK

Install the optional client with `python -m pip install openai`.

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="https://permitlify.com/ai/v1",
    api_key=os.environ["PERMITLIFY_AI_API_KEY"],
    timeout=120,
    max_retries=0,
)
reply = client.chat.completions.create(
    model="gpt-oss-20b",
    messages=[{"role": "user", "content": "Say hello in one sentence."}],
    max_tokens=256,
)
print(reply.choices[0].message.content)

stream = client.chat.completions.create(
    model="gpt-oss-20b",
    messages=[{"role": "user", "content": "Count from 1 to 5."}],
    max_tokens=256,
    stream=True,
)
for chunk in stream:
    if chunk.choices:
        print(chunk.choices[0].delta.content or "", end="", flush=True)
print()
```

## Published endpoints

All require `Authorization: Bearer <dedicated-key>`; use paths without a trailing
slash. The proxy publishes only:

| Method | Public path | Purpose |
|---|---|---|
| GET | `/ai/v1/models` | Discover loaded models |
| POST | `/ai/v1/chat/completions` | JSON or SSE streaming chat |
| POST | `/ai/v1/responses` | Responses API, subject to backend support |

Other `/ai` paths/methods return 404 after authentication. Missing/wrong keys
return 401 JSON. Query-string keys and website login cookies are not accepted.
Model management/UI endpoints stay private. Requests are limited to **1 MB**,
responses are marked `Cache-Control: no-store`, and wildcard CORS is removed.

## Deployment and service operations

Traffic flows through **Cloudflare HTTPS -> Caddy -> Django forward-auth ->
llama.cpp**. Django only checks the key hash; Caddy sends inference directly to
port 8010, including SSE streaming. The key and website cookies are removed
before forwarding to llama.cpp. Existing server-side loopback consumers can
continue using `http://127.0.0.1:8010/v1` without a key.

**Transport:** the site's existing Cloudflare-to-origin connection is HTTP on
port 80, as documented in `../../0_setup/README.md`. Public HTTPS terminates at
Cloudflare; this configuration does not provide end-to-end TLS to the origin.
Protecting that hop requires Cloudflare Full (strict) with an origin certificate,
or an encrypted Cloudflare Tunnel, as a separate infrastructure change.

Relevant configuration:

- `../../0_setup/Caddyfile`: authenticated `/ai` routing and private-path blocking.
- `../core/ai_api.py`: constant-time Bearer verifier, independent of the database.
- `../core/urls.py`: private `/_internal/ai-auth/` route (blocked by Caddy).
- `api_key.ps1`: local credential provisioning/loading/rotation.

### Model startup

The model file is `models/gpt-oss-20b-mxfp4.gguf` (~12 GB). If needed, download it
once with the Hugging Face CLI:

```powershell
hf download ggml-org/gpt-oss-20b-GGUF --include "*.gguf" --local-dir models
```

`start_server.bat` (also callable as `start_llama.bat`) runs:

```powershell
.\llama\llama-server.exe -m models\gpt-oss-20b-mxfp4.gguf --host 127.0.0.1 --port 8010 --threads 4 --ctx-size 8192 --alias gpt-oss-20b --jinja
```

Use the existing `GptOss20B` service in production. Do not start a second manual
server while that service owns port 8010. The API alias is fixed independently
of the model file's path. The custom `server.py` is a separate legacy Transformers
backend; it is not used by this deployment.

### Align the existing NSSM model service with this bundle

Run the following from this directory in an elevated PowerShell session during
an idle inference window. Record the existing NSSM application, directory,
parameters, and log paths first for rollback (`nssm get GptOss20B <setting>`).

```powershell
$nssm = Resolve-Path ..\..\0_setup\nssm.exe
& $nssm stop GptOss20B
& $nssm set GptOss20B Application "$PWD\llama\llama-server.exe"
& $nssm set GptOss20B AppDirectory "$PWD"
& $nssm set GptOss20B AppParameters '-m models\gpt-oss-20b-mxfp4.gguf --host 127.0.0.1 --port 8010 --threads 4 --ctx-size 8192 --alias gpt-oss-20b --jinja'
New-Item -ItemType Directory -Force .\logs | Out-Null
& $nssm set GptOss20B AppStdout "$PWD\logs\server.out.log"
& $nssm set GptOss20B AppStderr "$PWD\logs\server.err.log"
& $nssm start GptOss20B
```

Wait for model loading to finish before testing; readiness is
`http://127.0.0.1:8010/health`. Performance and startup time depend on available
RAM/CPU. Keep at least 16 GB RAM available for this model, plus application load.

### Activate the public route

From this directory, provision the key, restart Django to load the auth route,
then validate and reload Caddy:

```powershell
.\api_key.ps1
$nssm = Resolve-Path ..\..\0_setup\nssm.exe
$caddy = Resolve-Path ..\..\0_setup\caddy.exe
$config = Resolve-Path ..\..\0_setup\Caddyfile
& $nssm restart Permitlify
& $caddy validate --config $config --adapter caddyfile
if ($LASTEXITCODE -eq 0) {
    & $caddy reload --config $config --adapter caddyfile --address localhost:2019
}
python .\test.py
```

**Rollback:** restore the previous Caddy site block, validate, and reload Caddy
to remove the public route. Restore the saved NSSM settings and restart only
`GptOss20B` if rolling back the model bundle. Check model readiness and the
Permitlify homepage afterward. Removing the verifier disables authenticated
access immediately with 503; it does not open the API.

## Troubleshooting and offline checks

| Symptom | Check |
|---|---|
| 401 | Load the key in the same shell; use the dedicated Bearer key, not a Permitlify account key. |
| 404 | Base URL must end in `/ai/v1`; check method/path and Caddy reload. |
| 413 | Reduce the JSON request body below 1 MB. |
| 502 / 503 | Check `GptOss20B` and `Permitlify`; verify the key hash exists and is readable by the service. |
| Timeout / Cloudflare 524 | Check model queue/CPU and reduce prompt/output size. A longer client timeout cannot override Cloudflare's origin timeout; streaming can help once output begins. |
| Empty/truncated answer | GPT-OSS spends output tokens on reasoning; increase `--max-tokens`. |
| DPAPI decryption error | Run the helper as the provisioning Windows user on the same machine; otherwise explicitly rotate and update clients. |

Use these checks without running inference (commands from the `permitlify.com`
parent directory):

```powershell
.\.venv\Scripts\python.exe manage.py test core.tests.test_ai_api
.\.venv\Scripts\python.exe manage.py check
python -m unittest discover -s open_oss_20b\tests -v
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File open_oss_20b\tests\test_api_key.ps1
```

The repository ignores downloaded binaries, weights, logs, and credentials under
this bundle. Only source, launchers, tests, and documentation belong in Git.
