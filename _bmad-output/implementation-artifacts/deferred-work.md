- source_spec: `spec-permitlify-ai-api.md`
  summary: Encrypt the existing Cloudflare-to-origin hop with Full (strict) TLS or a Cloudflare Tunnel.
  evidence: `0_setup/Caddyfile` already routes all sites over origin HTTP port 80. Public HTTPS therefore terminates at Cloudflare, including traffic to the new AI endpoint; changing this requires coordinated Cloudflare/origin infrastructure configuration.
