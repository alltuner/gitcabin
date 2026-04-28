# Shared wildcard certificate (design notes)

> **Status:** decision in progress. Architecture B is the current lean. Nothing is implemented yet — this doc captures the reasoning so we don't re-derive it later.

## The problem we're trying to solve

Once gitcabin moves beyond "the user's own laptop, on `github.localhost`," `gh` forces HTTPS. From [`internal/ghinstance/host.go:53-95`](https://github.com/cli/cli/blob/trunk/internal/ghinstance/host.go) in the gh source:

- Only `github.localhost` is special-cased to use plain HTTP.
- Every other host gets `https://` baked in.
- There is no `--insecure-skip-verify`, `GH_INSECURE`, or similar escape hatch.
- `HostnameValidator` actively rejects any host string containing a `:`, so `gitcabin:8443` and friends are non-starters. gh always dials the scheme's default port (443 for HTTPS).

That means anyone who wants gitcabin reachable from `gh` under a hostname *other than* `github.localhost` needs:

1. A hostname resolvable on their machine.
2. A TLS endpoint at port 443 of that hostname's IP.
3. A cert for that hostname trusted by `gh`'s TLS stack (i.e. by the OS root store).

For an open-source project meant to be installed by anyone with `docker compose up`, every step in that chain is a place users can fail. The whole point of this design exercise is to compress those three steps into "download the bundle and run compose."

## What we want from the solution

- **Zero admin/sudo on the user's machine.** No `mkcert -install`, no keychain manipulation, no `certbot` runs on the user side.
- **Zero domain ownership requirement on the user side.** Most gitcabin users will not own a domain or have an API token for one.
- **Zero always-on infrastructure that the user has to trust.** Anything we host on their behalf is a single point of failure for the whole project.
- **Real TLS, not bypass tricks.** `gh` should validate the chain like it would for github.com. Self-signed-with-`SSL_CERT_FILE` works for power users but isn't a defaults-friendly story.
- **Cleanly OSS-able.** Whatever infrastructure we run shouldn't leak through gitcabin's repo as committed secrets, and shouldn't tie the project to David's personal accounts forever.

## The chosen approach

A single wildcard cert for `*.local.gitcabin.com` (final domain TBD — see [Open questions](#open-questions)), with:

- DNS authoritatively pinned to `127.0.0.1` for the wildcard.
- The cert *and its private key* published publicly.
- Users' compose overlay fetches the bundle on `compose up`, mounts it into a Caddy sidecar, and Caddy terminates TLS on `127.0.0.1:443`.
- gh runs as normal — `gh -h alice.local.gitcabin.com` validates the cert against the system's Let's Encrypt root and connects.

The user does not generate a key. The user does not run ACME. The user does not own a domain. They `curl` two files and start the proxy.

### Why publishing the private key is acceptable here

This is the part that looks alarming if you skim it. The argument:

A leaked private key is exploitable only by an attacker who can both (a) get clients to send traffic intended for `*.local.gitcabin.com` to a server they control, and (b) decrypt or sign for that traffic. The DNS pinning constraint forecloses (a) — any `*.local.gitcabin.com` hostname authoritatively resolves to `127.0.0.1` from our zone, so traffic never leaves the loopback interface. To intercept it, an attacker would need code execution on the same machine as the victim, at which point they have already won; TLS termination is not the protective layer.

The shared key is therefore as safe as the loopback assumption. The single load-bearing rule is: **never serve this cert on a non-loopback IP**. Compose's `127.0.0.1:443:443` enforces it, and we should call this out in code review for any future overlay.

Prior art: [`traefik.me`](https://traefik.me/) (run by the Traefik team since ~2017) and [`localhost.direct`](https://github.com/Upinel/localhost.direct) have been doing exactly this without Let's Encrypt revocation. Their threat model write-ups are a useful sanity check.

### What rotation buys us

Even granting "leaked key on loopback isn't catastrophic," fresh keys every cycle are still better than long-lived ones. If a key is published every ~60 days, the window during which any specific copy of the key can be (mis)used in some unforeseen way is bounded. Quarterly rotation is the floor; 60 days is the lean.

Rotation also weakens the "but you publish private keys" optic, because the answer is no longer "we publish a long-lived key" — it's "we publish a key that will be replaced before any reasonable adversary can build infrastructure around it."

## Alternatives considered (and why they lose)

| Option | Why it doesn't fit |
|---|---|
| `mkcert` + OS keychain install | Requires sudo/admin on first run, per-OS UX divergence, leaves keychain entries behind. Power-user mode at best. |
| Caddy internal CA + manual root install | Same admin requirement, harder than `mkcert` because we'd ship the install script ourselves. |
| `traefik.me` / `localhost.direct` directly | Works, but third-party dependency in the critical path of every gitcabin install. Outage there = outage for us. Branding is also wrong (`gitcabin.traefik.me` vs `gitcabin.local.gitcabin.com`). |
| Per-user dynamic certs via Caddy On-Demand TLS + shared CF token | Hands every install a token that can issue under our DNS zone. One leaked install compromises the zone. Hard no. |
| Per-user minting service (Cloudflare Worker that issues a cert per install) | Cleanest from a "no shared key" standpoint, but now we operate auth, rate limiting, abuse handling, and uptime. Several orders of magnitude more work than option F for marginal security gain given the loopback constraint. |
| `SSL_CERT_FILE` wrapper around `gh` | Genuinely good for power users (two-line shell wrapper, no cert distribution), but doesn't solve the "users want this to work without us shipping a wrapper script" case. Worth documenting as an alternative path; not the default. |

## How rotation runs (Architecture B — current lean)

A small always-on Caddy instance, hosted on something cheap (Fly.io free tier, Cloudflare's container product, or an existing alltuner box), holds the master cert. Caddy's built-in ACME client renews on its normal schedule via DNS-01 against Cloudflare:

```caddyfile
{
    storage file_system /data
    acme_dns cloudflare {env.CF_API_TOKEN}
}

*.local.gitcabin.com {
    tls internal_issuer {
        dns cloudflare {env.CF_API_TOKEN}
    }
    respond "cert host"
}
```

A sidecar process watches `/data/certs/` with `inotifywait` and syncs new bundles to a public Cloudflare R2 bucket fronted by `cert.gitcabin.com`. Users' compose curls from there.

### Why Architecture B beats Architecture A (Cloudflare Worker)

Both architectures were on the table. Architecture A would have kept everything inside Cloudflare:

- A Worker on a cron trigger generates a fresh key, runs ACME against Let's Encrypt, performs DNS-01 via the same Cloudflare account, writes the bundle to R2.
- $0/month at our scale, no always-on host.

We're picking B because:

- **Caddy's ACME is battle-tested.** The ~30-line Caddyfile in B is shorter and more obviously correct than the ~150 lines of Workers TypeScript in A.
- **The "always-on host" cost is real but small.** Fly.io's free tier or an existing tailnet machine handles it; we don't need a second piece of serverless infrastructure.
- **Easier to debug.** SSH into the host, tail Caddy's logs, see what's happening. Workers cron failures are harder to inspect.
- **Easier to reason about for contributors.** Caddy is a known quantity in DevOps; bespoke ACME-in-Workers is not.

Architecture A is a fine fallback if the always-on requirement ever becomes annoying.

### What gets published vs. what stays private

| Artifact | Where it lives | Visibility |
|---|---|---|
| Caddy host's persistent volume | wherever Caddy runs | private |
| Cloudflare API token (for ACME DNS-01) | Caddy host's env | private |
| `cert.pem` (current) | R2 bucket on `cert.gitcabin.com` | public |
| `privkey.pem` (current) | R2 bucket on `cert.gitcabin.com` | public |
| Detached signature for the bundle | R2, signed with a long-lived gitcabin release key | public |
| Renewal scripts / Caddyfile | gitcabin-cert repo (separate from main gitcabin repo) | public |

Keeping the renewal infra in a separate `gitcabin-cert` repo (not in `alltuner/gitcabin` itself) is intentional: the main project repo never gets a "private key in source" Secret Scanning warning, and contributors who fork gitcabin don't need to think about cert plumbing.

## How users consume the bundle

A `compose.shared-tls.yml` overlay (TBD, separate from `compose.yml`):

1. **Init container** runs `curl https://cert.gitcabin.com/cert.pem -o /certs/cert.pem` and same for `privkey.pem`. Verifies the detached signature against the bundled gitcabin release key. Bails if signature is wrong.
2. **Caddy sidecar** binds `127.0.0.1:443`, mounts `/certs/`, reverse-proxies to `gitcabin:8000`.
3. **User's gh invocation**: `gh auth login --hostname alice.local.gitcabin.com --with-token < token`.

The user picks any subdomain they want — there's no allocation, no central registry. `alice.local.gitcabin.com` and `bob.local.gitcabin.com` both work because they're both wildcard matches and they both resolve to `127.0.0.1`.

For users whose `127.0.0.1:443` is already taken, we ship a second SAN on the same cert — e.g. `*.lo2.local.gitcabin.com` pinned to `127.42.0.1` — and they edit a single env var in their `.env` to flip between loopback IPs. (Cheap, and addresses the same class of conflict the README's port-80 troubleshooting block does.)

## What's locked, what's open

### Locked in

- Shared wildcard cert + key, rotated automatically.
- Architecture B (Caddy + R2 sync) over Architecture A (Worker + R2).
- Bundle hosted under `cert.gitcabin.com` (or whichever final domain we pick), distributed separately from the main project repo.
- Detached signature on the bundle, verified by the user-side init container.
- Two SAN variants on the wildcard cert to cover the loopback-conflict escape hatch.

### Open questions

- **Domain.** Is it `gitcabin.com` (preferred — self-documenting, doubles as project branding), `cabin.alltuner.com` (cheapest — already owned), or something else? Pending domain availability check.
- **Number of loopback variants.** `*.local.<domain>` pinned to 127.0.0.1 plus `*.lo2.<domain>` to 127.42.0.1 covers most cases; do we want a third (lo3, lo4) for users with deeper port stacking? Probably not, but the wildcard cert can include up to 100 SANs at no marginal cost.
- **Rotation cadence.** 60 vs. 75 vs. 90 days. LE max is 90; tighter rotation is more security but more chances for the renewal job to fail at a bad moment. 60 feels right.
- **Hosting choice for the Caddy renewal box.** Fly.io free tier, an existing alltuner-internal Tailscale node, a Cloudflare Tunnel + Container — all viable. Pick whichever has the lowest cognitive overhead for whoever ends up maintaining it.
- **"What if Let's Encrypt revokes" runbook.** We need a documented fallback (ZeroSSL, Buypass) and an alert path so we notice within hours, not days. Architecture B makes this trivial — flip a Caddy directive — but we should write the runbook before the first revocation, not after.
- **Stale-cert UX on the user side.** If a user runs an install for the first time during the ~minute the bundle is being swapped, they see a TLS handshake fail. The init container should retry on 404/checksum-mismatch with backoff. Define the exact behavior before shipping.

## Out of scope

- The public/team Caddy + DNS-01 recipe in [installation.md](installation.md). That's for operators who own a domain and want to run gitcabin behind it; this doc is about users who don't.
- The Tailscale-shared recipe. Different audience, different infra.
- Any per-user authentication on top of gitcabin. The shared cert gives TLS, not authn — gitcabin still trusts whoever can reach the port.

## References

- gh source — host parsing and HTTPS-forcing: [`internal/ghinstance/host.go:53-95`](https://github.com/cli/cli/blob/trunk/internal/ghinstance/host.go).
- traefik.me — prior art: https://traefik.me/.
- localhost.direct — second prior art with a more thorough write-up: https://github.com/Upinel/localhost.direct.
- Caddy ACME with DNS providers: https://caddyserver.com/docs/automatic-https.
- Let's Encrypt revocation policy (CPS section 4.9): https://letsencrypt.org/repository/.
