# Add goldenproxies.com to the server

A step-by-step runbook for hosting **goldenproxies.com** alongside Permitlify on
the same Windows server, behind Caddy + Cloudflare.

**Settings used in this doc** (change if you prefer):

| Thing          | Value                                                          |
|----------------|----------------------------------------------------------------|
| Service name   | `GoldenProxies`                                                |
| Code folder    | `...\github\sites\goldenproxies.com`                           |
| Port (private) | `8001`  (Permitlify uses 8000 — every site needs its own port) |
| Domains        | `goldenproxies.com`, `www.goldenproxies.com`                   |

---

## 0. One-time prerequisite (skip if already done)

Caddy must be the port-80 "front door" before any site routing works. You only
ever do this **once** for the whole server:

```
cd C:\Users\Administrator\Desktop\github\sites
git pull
0_setup\setup_caddy.bat
```

Wait for it to print **`SUCCESS. Caddy is the front door...`**. This only sets up
the front door — it does not start any site (you do that in step 2). A domain
will show **502** until its site is started; that's expected.

---

## 1. Put the goldenproxies.com code on the server

Place the site's code in a folder **inside** the `sites` repo (next to the other
sites and the `0_setup` folder):

```
cd C:\Users\Administrator\Desktop\github\sites
git clone <your-goldenproxies-repo-url> goldenproxies.com
```

(Or copy the files there manually — the folder must end up at
`C:\Users\Administrator\Desktop\github\sites\goldenproxies.com`.)

### Give it its own .env

Each site is fully independent. Inside the `goldenproxies.com` folder, create a
`.env` with **its own**:

* `DJANGO_SECRET_KEY` (a fresh random value — do NOT reuse Permitlify's)
* its **own database** connection (a separate database, not Permitlify's)
* any API keys that site needs

---

## 2. Register it as its own auto-start service

Open **Command Prompt as Administrator**, then:

```
cd C:\Users\Administrator\Desktop\github\sites
0_setup\new_site.bat GoldenProxies C:\Users\Administrator\Desktop\github\sites\goldenproxies.com 8001
```

This single command does everything to run the site under **waitress as a
Windows service**, exactly like Permitlify:

* builds the site's virtual environment,
* installs its `requirements.txt`,
* installs **waitress** (the WSGI server),
* if the project has no `serve_waitress.py`, copies in a generic launcher that
  auto-detects the Django settings module from `manage.py` (so you don't have to
  configure anything),
* registers + starts the `GoldenProxies` service on `127.0.0.1:8001`, set to
  auto-start on reboot.

Wait for it to print that it's serving on `http://127.0.0.1:8001`.

> **Not a Django app?** The above assumes a Django project (like Permitlify). If
> goldenproxies.com is a different stack (Node, static, PHP, plain Flask, etc.),
> run `nssm edit GoldenProxies` and point **Application** + **Arguments** at that
> app's own start command, keeping the port at `8001`. Ask if you want this adjusted.

---

## 3. Tell Caddy about the domain

Open this file in a text editor:

```
C:\Users\Administrator\Desktop\github\sites\0_setup\Caddyfile
```

Add this block (anywhere below the Permitlify block):

```
http://goldenproxies.com, http://www.goldenproxies.com {
        reverse_proxy 127.0.0.1:8001 {
                header_up X-Forwarded-Proto https
        }
}
```

Save the file.

---

## 4. Apply the change (zero downtime)

```
cd C:\Users\Administrator\Desktop\github\sites
0_setup\reload_caddy.bat
```

It validates the Caddyfile first; if you made a typo it tells you and keeps the
old config live (nothing breaks).

---

## 5. Point the domain at the server (Cloudflare)

In Cloudflare for `goldenproxies.com`:

1. Add the domain to your Cloudflare account (if it isn't already).
2. Create an **A record**: name `@` -> your server's **public IP**, proxy
   **ON** (orange cloud).
3. Add another **A record**: name `www` -> same IP, proxy ON.
4. SSL/TLS mode: **Full** (or **Flexible** if the origin only listens on :80).

DNS can take a few minutes to propagate.

---

## 6. Verify it works

On the server, check the app and the front door directly:

```
:: the app itself
curl -s -o NUL -w "%{http_code}\n" -H "Host: goldenproxies.com" -H "X-Forwarded-Proto: https" http://127.0.0.1:8001/

:: through Caddy on port 80
curl -s -o NUL -w "%{http_code}\n" -H "Host: goldenproxies.com" http://127.0.0.1
```

A `200` (or `301/302` to a real page) means it's working. Then open
`https://goldenproxies.com` in an **Incognito window** (avoids cached redirects).

---

## Troubleshooting

| Symptom                                  | Fix                                                                                   |
|------------------------------------------|---------------------------------------------------------------------------------------|
| `301 ... (from disk cache)` in browser   | Browser cache, not the server. Hard refresh (Ctrl+Shift+R) or use Incognito.          |
| Site not loading after step 4            | Run `nssm restart GoldenProxies`, then check `goldenproxies.com\logs\waitress.err.log`. |
| Caddy not answering                       | `nssm restart Caddy`, then check `sites\0_setup\caddy.err.log`.                       |
| Endless HTTPS redirect loop              | Make sure the Caddy block has `header_up X-Forwarded-Proto https`.                     |
| "service already exists" on re-run       | Safe — `new_site.bat` removes and re-installs the service each time.                   |

## Useful service commands

```
nssm status GoldenProxies
nssm restart GoldenProxies
nssm stop GoldenProxies
nssm edit GoldenProxies      :: open the GUI to change command/port/env
```

---

## Adding even more sites later

Repeat steps 1-5 for each new website, changing **three things** every time:

* a new **service name** (e.g. `SiteThree`)
* a new **port** (`8002`, `8003`, ...)
* a new **Caddyfile block** with that domain + port

Everything else stays the same.
