# Installation

gitcabin can be deployed three different ways today. They differ only in *who can reach it* and *whether TLS is involved*. The application is identical across all three; the only thing that changes is the proxy in front of it and the hostname you give to `gh`.

The design discussion behind these choices — including options that have been ruled out and options still being designed (a shared-wildcard-cert path and a DuckDNS path that don't require owning a domain) — lives in [`tls.md`](tls.md).

## Pick a mode

| Mode | Audience | Hostname | TLS | Setup steps |
|---|---|---|---|---|
| [Local-only](#local-only) | the user, on this machine | `github.localhost` | none (gh's HTTP shortcut) | 1 |
| [Tailnet-shared](#tailnet-shared) | anyone on your tailnet | `<machine>.<tailnet>.ts.net` | provisioned by Tailscale | 3 |
| [Public/team](#publicteam) | anyone with DNS resolution | `<your domain>` | provisioned by Caddy via DNS-01 | 4 |

If in doubt, start with **Local-only**. It's what the README quickstart sets up; everything else is opt-in.

> A previous "Local with TLS" mode used a per-machine local CA (`mkcert`-style). It has been retired — the sudo-prompted keychain install was the wrong UX trade-off for a project meant to be installable without admin privileges. See [`tls.md`](tls.md) for the design discussion of what may replace it.

---

## Local-only

The default. gh dials `http://api.github.localhost/` over plain HTTP (the one URL shape where gh skips TLS — see `internal/ghinstance/host.go` in the gh source). Only this machine can reach gitcabin, and there are no certs to manage.

### Setup

`compose.yml` ships with `127.0.0.1:80:8000` already, so:

```sh
docker compose up -d
```

### Point gh at it

```sh
echo "any-token" | gh auth login --hostname github.localhost --with-token
GH_HOST=github.localhost gh auth status
```

### Trade-offs

- **No cert work, no DNS work, no third parties** — `github.localhost` resolves to `127.0.0.1` natively on macOS and Linux because RFC 6761 reserves `*.localhost`.
- **Requires port 80 free on the loopback IP gh resolves to.** If `127.0.0.1:80` is taken, see the [README troubleshooting block](../README.md#port-80-is-already-in-use) for the loopback-alias workaround.
- **Single user.** Nobody else on your network can reach this — it's bound to `127.0.0.1` on purpose.

---

## Tailnet-shared

A [Tailscale](https://tailscale.com/) sidecar puts gitcabin on your tailnet under `<machine>.<tailnet>.ts.net`. Tailscale provisions and renews a real Let's Encrypt cert via its coordination server, with no DNS or ACME work on your side. Anyone on the tailnet can hit it; nobody off it can.

This is the right mode if your audience is "you across all your machines" or "your team, all of whom are already on Tailscale."

### One-time tailnet prep

Before any of the per-machine setup below, the tailnet itself needs two things turned on (admin console, once per tailnet, by whoever owns it):

- **MagicDNS** — required for tailnet hostnames to resolve to `*.<tailnet>.ts.net`.
- **HTTPS Certificates** — required for `tailscale cert` / `tailscale serve` to provision LE certs. Enabling this opts your tailnet's machine names into public Certificate Transparency logs (one-time consent in the admin console).

Without both, the sidecar comes up but `tailscale cert` returns nothing and 443 never serves.

### Setup

1. **Generate a Tailscale auth key** at https://login.tailscale.com/admin/settings/keys. Make it **reusable, 90-day, non-ephemeral** (an ephemeral key would make the node disappear on every container restart). Save it as `TS_AUTHKEY`. After the node first joins the tailnet, flip "Disable key expiry" on the node in the admin UI so node lifetime is decoupled from auth-key lifetime — otherwise the node disappears in 90 days when the key expires, even if it's still running.

2. **Create `tailscale-serve.json`** next to `compose.yml`:

   ```json
   {
     "TCP": {
       "443": { "HTTPS": true }
     },
     "Web": {
       "${TS_CERT_DOMAIN}:443": {
         "Handlers": {
           "/": { "Proxy": "http://127.0.0.1:8000" }
         }
       }
     },
     "AllowFunnel": {
       "${TS_CERT_DOMAIN}:443": false
     }
   }
   ```

   `${TS_CERT_DOMAIN}` is expanded by tailscaled at runtime to the FQDN it owns; you don't have to hardcode your tailnet name. The explicit `AllowFunnel: false` prevents accidental public exposure if anyone runs `tailscale funnel` against this port.

3. **Add a sidecar to `compose.yml`** (or to a `compose.tailscale.yml` overlay):

   ```yaml
   services:
     tailscale:
       image: tailscale/tailscale:v1.96
       restart: unless-stopped
       hostname: gitcabin
       environment:
         TS_AUTHKEY: ${TS_AUTHKEY:?Set TS_AUTHKEY in .env}
         TS_STATE_DIR: /var/lib/tailscale
         TS_SERVE_CONFIG: /config/serve.json
       volumes:
         - ts-state:/var/lib/tailscale
         - ./tailscale-serve.json:/config/serve.json:ro
       cap_add: [net_admin, sys_module]
       devices:
         - /dev/net/tun:/dev/net/tun
       network_mode: service:gitcabin

     gitcabin:
       # Drop the `ports:` block — the tailscale sidecar publishes the only
       # ingress now, on tailnet 443. Local-only access is gone.
       ports: !reset []

   volumes:
     ts-state:
   ```

   - `network_mode: service:gitcabin` puts the two containers in the same network namespace, so `127.0.0.1:8000` inside the tailscale container is gitcabin.
   - `image: tailscale/tailscale:v1.96` is pinned on purpose — `:latest` plus container auto-update is a recipe for a silent breaking restart.
   - `ts-state` is a named volume, not a bind mount, to avoid permissions traps with the uid the tailscale container runs as.
   - `devices: [/dev/net/tun:/dev/net/tun]` belt-and-suspenders the cap-only form, which fails on some hosts.

4. Bring it up:

   ```sh
   echo "TS_AUTHKEY=tskey-auth-..." > .env
   docker compose up -d
   docker compose logs tailscale | grep "https://"   # prints the FQDN
   ```

### Point gh at it

```sh
echo "any-token" | gh auth login --hostname gitcabin.<your-tailnet>.ts.net --with-token
GH_HOST=gitcabin.<your-tailnet>.ts.net gh auth status
```

gh will use the `/api/v3/...` and `/api/graphql` URL shape, which gitcabin serves alongside the bare paths.

### Trade-offs

- **Zero cert work.** Tailscale handles ACME entirely.
- **Anyone on the tailnet can reach it** with no further auth — gitcabin trusts whoever talks to it. Use ACLs in the Tailscale admin if you need finer control.
- **Audience is tailnet-only.** GitHub Actions runners, contractors not on your tailnet, and CI hosted elsewhere can't reach it.
- **One more moving part** (the tailscale container) and an auth key to store.

---

## Public/team

A [Caddy](https://caddyserver.com/) sidecar fronts gitcabin under a hostname *you* own (e.g. `git.example.com`), terminating TLS with a Let's Encrypt cert obtained via DNS-01 — so no inbound port 80/443 is required from the public internet. Anyone whose machine can resolve your hostname can reach gitcabin.

This is the mode for "team server" or "shared developer instance" deploys. It assumes you own a domain and have an API token for its DNS provider.

### Setup

The example below uses Cloudflare DNS. Other providers work the same way with a different Caddy plugin (see https://github.com/caddy-dns).

1. **Create a Cloudflare API token** with `Zone:DNS:Edit` permission for the zone serving your hostname. Save as `CF_API_TOKEN`.

2. **Create `Caddyfile`** next to `compose.yml`:

   ```
   git.example.com {
       reverse_proxy gitcabin:8000
       tls {
           dns cloudflare {env.CF_API_TOKEN}
       }
   }
   ```

3. **Create `Dockerfile.caddy`** to bake the Cloudflare DNS plugin into Caddy:

   ```dockerfile
   FROM caddy:2-builder AS builder
   RUN xcaddy build --with github.com/caddy-dns/cloudflare

   FROM caddy:2
   COPY --from=builder /usr/bin/caddy /usr/bin/caddy
   ```

4. **Add a sidecar to `compose.yml`** (or to a `compose.public.yml` overlay):

   ```yaml
   services:
     caddy:
       build:
         context: .
         dockerfile: Dockerfile.caddy
       restart: unless-stopped
       ports:
         - "443:443"
       volumes:
         - ./Caddyfile:/etc/caddy/Caddyfile:ro
         - caddy-data:/data
         - caddy-config:/config
       environment:
         CF_API_TOKEN: ${CF_API_TOKEN:?Set CF_API_TOKEN in .env}

     gitcabin:
       ports: !reset []   # caddy is the only ingress now

   volumes:
     caddy-data:
     caddy-config:
   ```

5. **Point your DNS** (`git.example.com` A/AAAA) at the host running this stack.

6. Bring it up:

   ```sh
   echo "CF_API_TOKEN=..." > .env
   docker compose up -d
   docker compose logs caddy | grep "obtained certificate"
   ```

### Point gh at it

```sh
echo "any-token" | gh auth login --hostname git.example.com --with-token
GH_HOST=git.example.com gh auth status
```

### Trade-offs

- **Real, browser-trusted TLS** — works for everyone, no client-side trust steps.
- **Reachable from anywhere** that resolves your hostname. If that's wider than you want, gate access at Caddy with basic auth or an OAuth proxy in front of `reverse_proxy`.
- **You need a domain and a DNS provider with an API.** Without that, this mode doesn't apply.
- **Port 443 must be free** on whatever host runs the stack.

---

## Verifying any mode works

After `gh auth login` in any mode, all three of these should succeed without error:

```sh
gh auth status
gh api /
gh api graphql -f query='query { viewer { login } }'
```

If `gh auth status` returns `connection refused`, the proxy isn't bound where gh is looking. If it returns a TLS error, the cert isn't trusted by your OS — that's a sign the wrong mode is configured (e.g. you're using a Caddy internal CA without installing the root). If it returns a 404, your URL prefix is wrong; gitcabin serves both `/` and `/api/v3` shapes, so a 404 means the proxy is rewriting paths in a way gitcabin doesn't expect.
