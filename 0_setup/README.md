# 0_setup — the shared hosting kit for every site in this repo

This `sites` repo holds **all the websites** for the server, one folder per site
(`permitlify.com/`, `goldenproxies.com/`, ...). This `0_setup` folder is the
**shared infrastructure** that puts them all online on a single Windows server:

```
visitor --https--> Cloudflare --http:80--> Caddy (this folder) --> the right site
```

Cloudflare terminates HTTPS and forwards to the server on plain HTTP port 80.
**Caddy** owns port 80, looks at the requested domain, and reverse-proxies each
domain to its own site running on a private loopback port (Permitlify = 8000,
the next site = 8001, and so on). Each site runs as its **own auto-start Windows
service** under **waitress**.

## What's in this folder

| File                  | What it does                                                        |
|-----------------------|--------------------------------------------------------------------|
| `setup_caddy.bat`     | One-time: installs Caddy as the port-80 front-door service.         |
| `new_site.bat`        | Adds a site as its own waitress Windows service on its own port.    |
| `Caddyfile`           | The routing table: which domain → which loopback port.             |
| `reload_caddy.bat`    | Applies Caddyfile edits with zero downtime.                        |
| `serve_waitress.py`   | Generic launcher copied into any Django site that lacks one.        |
| `setup-goldenproxies.md` | A full worked example (adding goldenproxies.com).               |

> **Binaries are not committed.** Put **`nssm.exe`** in this folder before running
> anything (download from <https://nssm.cc>). `caddy.exe` is downloaded
> automatically by `setup_caddy.bat`. Both are git-ignored.

All `.bat` files must be run **as Administrator**.

## First-time server setup (run once)

```
0_setup\setup_caddy.bat
```

Installs Caddy on port 80 and health-checks the front door. It only sets up the
front door — it does **not** start any site. Wait for
**`SUCCESS. Caddy is the front door...`**, then start each site with
`new_site.bat` (next section). A domain shows a 502 until its site is started —
that's expected.

## Add a new site

1. Put the site's code in a sibling folder, e.g. `sites\mysite.com\`, with its
   **own** `.env` (its own `DJANGO_SECRET_KEY` and its own database).
2. Register it as a service on a free port:
   ```
   0_setup\new_site.bat MySite C:\Users\Administrator\Desktop\github\sites\mysite.com 8002
   ```
3. Add a block to `Caddyfile`:
   ```
   http://mysite.com, http://www.mysite.com {
           reverse_proxy 127.0.0.1:8002 {
                   header_up X-Forwarded-Proto https
           }
   }
   ```
4. Apply it (zero downtime):
   ```
   0_setup\reload_caddy.bat
   ```
5. **Cloudflare:** add an `A` record `@` → the server's public IP (proxy ON,
   orange cloud), another `A` record `www` → same IP (proxy ON), and set SSL/TLS
   mode to **Full**.

## Port registry (keep this updated)

| Site               | Service name   | Port |
|--------------------|----------------|------|
| permitlify.com     | `Permitlify`   | 8000 |
| goldenproxies.com  | `GoldenProxies`| 8001 |

Every new site needs a **unique** port and a **unique** service name.

## Security — firewall port 80 to Cloudflare only

The sites trust `X-Forwarded-Proto` from any caller (so Caddy can tell them the
request was HTTPS). That is only safe if the server's firewall allows inbound
**port 80 from Cloudflare's IP ranges only**
(<https://www.cloudflare.com/ips/>). Otherwise someone hitting the raw IP could
spoof the HTTPS header.
