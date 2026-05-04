# TLS and hostname strategy (design notes)

> **Status:** No option chosen yet. This file surveys the problem, the hard constraints, the options researched, and the per-option design detail. Anyone picking this up later should be able to make an informed call from this file alone — it leans on no option except those explicitly ruled out.

## What we're trying to solve

`gh` is the only client gitcabin needs to satisfy, and it has very specific opinions about the URL it will dial.

From [`internal/ghinstance/host.go:53-95`](https://github.com/cli/cli/blob/trunk/internal/ghinstance/host.go):

- The hostname `github.localhost` is special-cased to use plain HTTP on port 80.
- Every other hostname gets `https://` baked in and is dialed on port 443.
- There is no `--insecure-skip-verify`, `GH_INSECURE`, or analogous escape hatch.
- `HostnameValidator` actively rejects host strings containing `:`, so `gitcabin:8443` and friends are non-starters. gh always uses the scheme's default port.

That gives us exactly two doors:

1. **`github.localhost` over plain HTTP on port 80.** Works without any cert work or DNS setup. Already shipped as the default mode in [`installation.md`](installation.md). Single user, single machine. No further design needed.
2. **Any other hostname over HTTPS on port 443.** Requires a hostname resolvable on the user's machine, a TLS endpoint at port 443 of that hostname's resolved IP, and a cert trusted by the OS root store. Every step of that chain is a place a user can fail. **This is the design space this doc covers.**

The whole point of the design exercise is to compress those three steps — DNS, TLS endpoint, trusted cert — into something a user can complete with `docker compose up` and at most one signup.

## Hard constraints (simplicity-first)

User simplicity outranks everything else in this design. Maintainer convenience, infrastructure elegance, branding, even some forms of security hardening — if they fight user simplicity, they lose.

The constraints below are not negotiable. Any option that breaks one of them is ruled out, no matter how appealing on other axes.

| # | Constraint | What it means concretely |
|---|---|---|
| C1 | **No root / sudo on the user's machine.** | No `sudo`-prompted keychain installs, no `sudo ifconfig lo0 alias …`, no `sudo systemctl …`. The user's interaction is `docker compose up` and at most one tool signup in a browser. |
| C2 | **No user-side privileged-port binding.** | `docker compose` binding `127.0.0.1:443` is fine — the docker daemon (already privileged) does the bind. Asking the user to run a process with `cap_net_bind_service` is not. |
| C3 | **No custom CA root install on the client.** | The cert chain `gh` validates against has to be the OS-shipped public roots. No `mkcert -install`, no `bash trust.sh`, no manual `certutil.exe` step. This is what retires the per-machine local-CA mode. |
| C4 | **No domain ownership requirement on the user side.** | A path for operators who own a domain is fine to document, but the *default* must work for someone who does not. |
| C5 | **No hostname allocation step that requires us to be in the loop.** | Users picking `alice.<thing>.example.com` shouldn't have to email us, sign in to a portal, or wait for provisioning. |
| C6 | **`gh` must validate the cert without flags.** | No `SSL_CERT_FILE` wrapper as a default. (Documented as a power-user alternative is fine.) |

The user is on docker. They have a working OS keychain. They have an internet connection. They have a browser. That's it.

Maintainer-side cost matters but is a softer constraint. We're willing to run modest infrastructure to avoid pushing complexity onto users — but every always-on system we operate on the user's behalf is a single point of failure for the whole project, and contributors picking this up later inherit the operational debt. So we prefer "no maintainer infra" over "yes maintainer infra," all else equal.

## The options we have researched

Eight distinct approaches were investigated. Three remain live; the rest are ruled out. The live three are all viable; none has been chosen.

### Live options

| Option | What the user does | What we run | Cert source | Hostname shape |
|---|---|---|---|---|
| [Local-only HTTP](#l1-local-only-http-already-shipped) | `docker compose up` | nothing | none | `github.localhost` |
| [Shared wildcard cert](#l2-shared-wildcard-cert) | `docker compose up`; pick a subdomain | always-on Caddy + R2 (or equivalent) | LE via DNS-01 (we run it) | `*.local.gitcabin.com` |
| [Tailnet-shared (Tailscale)](#l4-tailnet-shared-tailscale) | install Tailscale CLI; mint auth key; `docker compose up` | nothing (Tailscale Inc. provides the control plane) | LE via DNS-01 (Tailscale runs it) | `<host>.<tailnet>.ts.net` |

The numbering preserves L1, L2, L4 to match the order in which the options were considered; "L3" and "L5" labels are intentionally absent because those slots map to options that have been ruled out.

### Ruled out

| Option | Reason it's out |
|---|---|
| **Per-machine local CA** (mkcert / Caddy local CA) | Violates C3. Every prior install required a sudo-prompted keychain step. This was the trigger for this entire redesign. |
| **L3 — DuckDNS / deSEC / dynv6 + per-user ACME** | Scoped out. Technically viable (zero maintainer infra, real LE certs via DNS-01, all hard constraints met), but the hostname is `*.duckdns.org` (or equivalent third-party), it adds a third-party uptime dependency in the renewal path, and it pushes the user through an external signup. Within the simplified scope of L1 + L2 + L4, it doesn't earn its keep. Recipe and trade-offs preserved in git history (commit `72b0767` if needed). |
| **L5 — Public/team (own domain)** | Scoped out. Violates C4 by definition (domain required), and the original framing — "for operators who already pass C4" — turned out to be carrying scope we don't want. Anyone who owns a domain and wants gitcabin reachable from anywhere can build a Caddy + DNS-01 recipe themselves; we're not the right project to document that. Also drops support for "GitHub Actions runners reaching gitcabin" as a use case — see [What's not in this doc](#whats-not-in-this-doc). |
| **mDNS / `gitcabin.local`** | No public CA issues certs for `.local` (CA/B Forum policy). The only way to get a "trusted" cert for `.local` is a private CA, which violates C3. |
| **Magic-DNS-only services without certs** (`lvh.me`, `localtest.me`, `local.gd`, `vcap.me`) | No cert path. gh forces HTTPS, so a hostname with no cert story is unusable as the ingress. Useful only for plain-HTTP local dev, where we already have `github.localhost`. |
| **Per-host LE via HTTP-01 on `nip.io` / `sslip.io`** | Requires LE servers to reach the user's machine on port 80 from the public internet. For "TLS on localhost" the IP encoded in the hostname has to be public, so it's not loopback. Plus no wildcard support — every gh hostname swap is a fresh issuance. |
| **Reverse tunnels with rotating hostnames** (CF Quick Tunnels, ngrok-free without dev-domain, Pinggy free, localhost.run free) | The hostname rotates per session, so `gh auth login --hostname …` is invalidated every restart. Plus the audience is "the public internet" not "this machine," which is a different deployment shape. |
| **Mesh networks without their own cert provisioning** (NetBird, ZeroTier, Nebula, Innernet, Tinc) | These give you connectivity, not trusted public TLS. To get a cert through them, you need a domain you own and an ACME runner — which lands in the (now-ruled-out) Public/team territory. |
| **Headscale (today)** | The `tailscale cert` / `tailscale serve` HTTPS provisioning that makes Tailscale the live option does not work the same way under Headscale. Tracked at [juanfont/headscale#1921](https://github.com/juanfont/headscale/issues/1921). Revisit if/when that lands. |

A couple of options were neither fully ruled out nor fully designed — they sit between live and dead:

- **Tailscale Funnel as an additional mode beyond L4.** Funnel exposes a tailnet service on `*.ts.net` to the public internet with a real cert. It would meet C1–C6 cleanly and give "public reach without owning a domain," which nothing else in this list does. **Not yet verified that `gh auth login --hostname <funnel-host>.ts.net` accepts it.** Worth a smoke test before promoting.
- **`SSL_CERT_FILE` wrapper around `gh`.** Genuinely good for power users (two-line shell wrapper, no cert distribution needed). Not a default because of C6, but worth documenting as an alternative path.

## Cross-cutting open questions

These affect more than one of the live options, so they live here rather than getting forked across per-design subsections.

### Will Let's Encrypt issue (and CT scanners flag) certs whose A record points to `127.0.0.1`?

**Issuance: yes.** Confirmed:

- LE's [`certificates-for-localhost`](https://letsencrypt.org/docs/certificates-for-localhost/) doc (last updated 2025-07-31) discusses this pattern explicitly and does not refuse it.
- crt.sh shows thousands of LE-issued certs for `*.traefik.me` (A → `127.0.0.1`) over multiple years across multiple LE intermediates.
- The CA/B Forum [Baseline Requirements](https://github.com/cabforum/servercert/blob/main/docs/BR.md) contain no prohibition based on the A-record value of the FQDN. DNS-01 validates control of the zone, not the IP the name resolves to.
- No bug tracker activity on caddyserver/caddy or acmesh-official/acme.sh suggests anyone has hit a wall on this.

**Scanner noise: low.** No evidence that Censys / crt.sh consumers / generic CT firehose monitors flag loopback-pinned certs as suspicious. (Inferred from absence of complaints from the precedent projects, not a positive vendor statement.)

**The actual risk is LE revocation if the private key is published.** This is the load-bearing finding. From the LE doc (verbatim):

> "Don't do this. It will put your users at risk, and your certificate may get revoked... in order to make it work, you had to ship the private key to your certificate with your native app. That means that anybody who downloads your native app gets a copy of the private key... This is considered a compromise of your private key, and your Certificate Authority (CA) is required to revoke your certificate if they become aware of it."

The localhost.direct project [has lived this](https://github.com/Upinel/localhost.direct/issues/18) with GlobalSign — the cert was revoked after a user leaked the unprotected private key. Issue [#19](https://github.com/Upinel/localhost.direct/issues/19) on the same repo is the open "violation of CA/B Baseline Requirements" complaint.

**Implications for our options:**

- **Shared wildcard cert (L2)** is the design that explicitly publishes the private key. It is therefore the design where this risk lands. Not a blocker, but the rotation cadence and the revocation runbook stop being optional polish — they become load-bearing. If LE revokes, every user's gitcabin handshake fails until we publish a fresh cert with a different private key (and even then, OCSP/CRL caches mean some users see failures for hours).
- **Tailnet-shared (L4)** does not publish anyone's private key. Tailscale holds the LE account; users don't see private keys. Not affected.

So this risk is unique to L2. It's manageable, but it raises L2's maintainer-side operational floor.

### Does `gh auth login --hostname <funnel>.ts.net` work cleanly?

Open. The Tailscale Funnel hostnames are real `*.ts.net` names with real LE certs, and `gh` should accept them the same way it accepts any HTTPS hostname. But this hasn't been verified end-to-end. A 5-minute smoke test would settle it. Worth doing before either committing to or ruling out a Funnel-based addition to L4.

### Will `tailscale cert` work in the recipe we plan to use?

**Confirmed yes.** [`infrastructure/production/docker-compose.yml`](../../infrastructure/production/docker-compose.yml) (in a sibling repo) runs two services exactly this way (`registry`, `oauth-relay`) with a `TS_SERVE_CONFIG` JSON that uses `${TS_CERT_DOMAIN}` (substituted by `tailscaled` at runtime). The pattern works in production. See the [Tailscale subsection](#l4-tailnet-shared-tailscale) for the deltas this audit surfaced for our recipe.

---

## L1 — Local-only HTTP (already shipped)

Default mode. `gh` dials `http://api.github.localhost/` over plain HTTP because `github.localhost` is the one hostname gh special-cases for plain HTTP. RFC 6761 reserves `*.localhost` to resolve to `127.0.0.1` natively on macOS, Linux, and Windows, so no DNS setup is needed.

**Constraint check:** C1–C6 all met trivially because there is no cert chain, no domain, no third party.

**What it doesn't cover:** anything that isn't "this single user, this single machine." For multi-device, sharing, or anyone-else-on-the-network access, we need L2 or L4.

Already documented in [`installation.md`](installation.md#local-only). Nothing more to design here.

---

## L2 — Shared wildcard cert

A single wildcard cert for `*.local.gitcabin.com` (final domain TBD), with:

- DNS authoritatively pinned to `127.0.0.1` for the wildcard.
- The cert and its private key published publicly.
- Users' compose overlay fetches the bundle on `compose up`, mounts it into a Caddy sidecar, and Caddy terminates TLS on `127.0.0.1:443`.
- gh runs as normal — `gh -h alice.local.gitcabin.com` validates the cert against the system's Let's Encrypt root and connects.

The user does not generate a key. The user does not run ACME. The user does not own a domain. They `curl` two files and start the proxy.

**Constraint check:** C1–C6 met. Maintainer side has the highest ongoing cost of any live option (always-on infra, rotation pipeline, revocation runbook).

### Why publishing the private key is a defensible threat model

A leaked private key is exploitable only by an attacker who can both (a) get clients to send traffic intended for `*.local.gitcabin.com` to a server they control, and (b) decrypt or sign for that traffic. The DNS pinning constraint forecloses (a) — any `*.local.gitcabin.com` hostname authoritatively resolves to `127.0.0.1` from our zone, so traffic never leaves the loopback interface. To intercept it, an attacker would need code execution on the same machine as the victim, at which point they have already won; TLS termination is not the protective layer.

The shared key is therefore as safe as the loopback assumption. The single load-bearing rule is: **never serve this cert on a non-loopback IP**. Compose's `127.0.0.1:443:443` enforces it; we should call this out in code review for any future overlay.

### Why publishing the private key is a real revocation risk

See [Cross-cutting open questions](#will-lets-encrypt-issue-and-ct-scanners-flag-certs-whose-a-record-points-to-127001) above. LE's stated policy is that they *will* revoke a cert whose private key has been distributed (BR §4.9.1.1 obliges them to). The localhost.direct project has been revoked once already. The mitigation is rotation: publish a fresh cert with a fresh key faster than OCSP/CRL caches let revocations bite. Architecture B/A both support this; it's the runbook that needs to exist.

### Alternatives within this design space

These are alternatives *within the shared-cert family* — variations on "a single cert that ships to many users."

| Option | Why it doesn't fit |
|---|---|
| `traefik.me` / `localhost.direct` directly | Works, but third-party dependency in the critical path of every gitcabin install. Outage there = outage for us. Branding is also wrong (`gitcabin.traefik.me` vs `gitcabin.local.gitcabin.com`). |
| Per-user dynamic certs via Caddy On-Demand TLS + shared CF token | Hands every install a token that can issue under our DNS zone. One leaked install compromises the zone. Hard no. |
| Per-user minting service (Cloudflare Worker that issues a cert per install) | Cleanest from a "no shared key" standpoint, but now we operate auth, rate limiting, abuse handling, and uptime. Several orders of magnitude more work than the always-on Caddy path for marginal security gain given the loopback constraint. |

### Two architectures for renewal infra

#### Architecture B — always-on Caddy + R2 sync

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

#### Architecture A — Cloudflare Worker

An alternative that keeps everything inside Cloudflare:

- A Worker on a cron trigger generates a fresh key, runs ACME against Let's Encrypt, performs DNS-01 via the same Cloudflare account, writes the bundle to R2.
- $0/month at our scale, no always-on host.

#### How they compare

| Dimension | Architecture B | Architecture A |
|---|---|---|
| Code surface | ~30 lines of Caddyfile | ~150 lines of Workers TypeScript |
| Reuse of battle-tested ACME | Caddy's stack | bespoke ACME client in Workers runtime |
| Always-on host needed | yes (Fly.io free tier or similar) | no |
| Debuggability | SSH + `caddy logs` | Worker tail + Cloudflare dashboard |
| Cognitive load for contributors | low (Caddy is widely known) | higher (custom ACME-in-Workers) |
| Failure modes | host dies → renewal stops | Worker quota / cron skip → renewal stops |

Architecture B's appeal is mostly off-the-shelf and easy to debug. Architecture A's appeal is no always-on infrastructure. Both viable; neither committed to.

### What gets published vs. what stays private

| Artifact | Where it lives | Visibility |
|---|---|---|
| Renewal host's persistent volume | wherever the renewal pipeline runs | private |
| Cloudflare API token (for ACME DNS-01) | renewal host's env | private |
| `cert.pem` (current) | R2 bucket on `cert.gitcabin.com` | public |
| `privkey.pem` (current) | R2 bucket on `cert.gitcabin.com` | public |
| Detached signature for the bundle | R2, signed with a long-lived gitcabin release key | public |
| Renewal scripts / Caddyfile | gitcabin-cert repo (separate from main gitcabin repo) | public |

Keeping the renewal infra in a separate `gitcabin-cert` repo (not in the main project repo) is intentional: the main project never gets a "private key in source" Secret Scanning warning, and contributors who fork gitcabin don't need to think about cert plumbing.

### How users consume the bundle

A `compose.shared-tls.yml` overlay (TBD, separate from `compose.yml`):

1. **Init container** runs `curl https://cert.gitcabin.com/cert.pem -o /certs/cert.pem` and same for `privkey.pem`. Verifies the detached signature against the bundled gitcabin release key. Bails if signature is wrong.
2. **Caddy sidecar** binds `127.0.0.1:443`, mounts `/certs/`, reverse-proxies to `gitcabin:8000`.
3. **User's gh invocation**: `gh auth login --hostname alice.local.gitcabin.com --with-token < token`.

The user picks any subdomain they want — there's no allocation, no central registry. `alice.local.gitcabin.com` and `bob.local.gitcabin.com` both work because they're both wildcard matches and they both resolve to `127.0.0.1`.

If `127.0.0.1:443` is already in use on the user's machine, the loopback-alias path (`127.0.13.1`, etc.) is *not* a clean escape hatch under C1: only Linux binds the full `127.0.0.0/8` to the loopback interface. macOS binds only `127.0.0.1` on `lo0` by default — additional addresses require `sudo ifconfig lo0 alias …` plus a LaunchDaemon for persistence. Windows behaves similarly. The sudo step violates C1, so we don't promise it. Users with a port-443 conflict either kill the conflicting service or fall back to L4.

### What's locked, what's open (for L2 specifically)

**Settled within this design** (calls that are made *if* we ship L2; not commitments to ship L2):

- Shared wildcard cert + key, rotated automatically.
- Bundle hosted under `cert.gitcabin.com` (or whichever final domain we pick), distributed separately from the main project repo.
- Detached signature on the bundle, verified by the user-side init container.
- Single wildcard SAN (`*.local.gitcabin.com`) pinned to `127.0.0.1`. No per-loopback-alias variants.

**Open:**

- **Domain.** `gitcabin.com` (preferred — self-documenting), `cabin.alltuner.com` (cheapest — already owned), or something else. Pending availability check.
- **Architecture A vs B.** Comparison table above; not yet decided.
- **Rotation cadence.** 60/75/90 days are the obvious slots. The LE-revocation finding above pushes toward the shorter end of that range; tighter still (weekly?) is possible but multiplies the risk that the renewal job fails at a bad moment.
- **Renewal-host hosting choice.** Fly.io free tier, an existing tailnet machine, Cloudflare Tunnel + Container — pick whichever the maintainer-on-call has the lowest cognitive overhead for.
- **Revocation runbook.** Documented fallback (ZeroSSL, Buypass) and an alert path so we notice within hours, not days. Architecture B makes the swap trivial; Architecture A needs a Worker-level switch.
- **Stale-cert UX on the user side.** If a user runs an install for the first time during the ~minute the bundle is being swapped, they see a TLS handshake fail. Init container should retry on 404/checksum-mismatch with backoff.
- **Who babysits the renewal pipeline?** L2 has the highest maintainer-side operational cost of any live option. Before shipping, someone needs to be on the hook for "renewal failed at 2am" alerts.

---

## L4 — Tailnet-shared (Tailscale)

A `tailscale/tailscale` sidecar puts gitcabin on the user's tailnet under `<host>.<tailnet>.ts.net`. Tailscale's coordination plane provisions and renews a real LE cert via DNS-01 (against TXT records under their `*.ts.net` zone). No host port is bound; access is gated by tailnet membership.

**Constraint check:** C1 violated *during install* (installing the Tailscale CLI itself takes sudo on most platforms). C2–C6 met. Once installed, no further sudo. The Tailscale CLI install is a one-time per-machine cost the user pays *outside* gitcabin's compose stack — comparable to "user already has docker installed" but distinct from it.

### What we run

Nothing on the maintainer side. Tailscale Inc. operates the coordination plane, the LE account, and the `*.ts.net` DNS. Free tier (as of early 2026): 6 users, unlimited devices, MagicDNS + HTTPS included.

### What the user does

1. Install Tailscale on each machine that needs to reach gitcabin (one-time, sudo).
2. Generate an auth key in the Tailscale admin panel.
3. Drop it in `.env` as `TS_AUTHKEY`.
4. Enable HTTPS in the admin console (one-time per tailnet — flips a bit that opts the tailnet's `*.ts.net` names into LE/CT issuance).
5. `docker compose -f compose.yml -f compose.tailscale.yml up -d`.
6. `gh auth login --hostname gitcabin.<tailnet>.ts.net`.

### Recipe (from the audit)

The recipe in current [`installation.md`](installation.md) is structurally correct. The audit of `~/repos/infrastructure` (which runs this pattern in production for other services) surfaced eight concrete deltas to import:

1. **Pin the image tag** (`tailscale/tailscale:v1.96`, not `:latest`). Renovate-bumped, no silent breaking restarts.
2. **`restart: unless-stopped`** on the sidecar. Without it, the sidecar doesn't recover after a host reboot.
3. **`devices: ["/dev/net/tun:/dev/net/tun"]`** alongside the caps. Belt-and-suspenders for hosts where the cap-only form fails.
4. **`AllowFunnel: false`** in `serve.json`. Cheap, explicit denial — prevents accidental public exposure.
5. **State as a named volume**, not a `./tailscale-state` bind mount. Avoids a uid-65532 permissions trap.
6. **Document the auth-key recipe**: reusable, 90-day, then disable key expiry on the node in the admin UI after first boot. Non-obvious; not in Tailscale's own docs.
7. **Document the MagicDNS + HTTPS prerequisites** explicitly. Neither is on by default; without both, `tailscale cert` returns nothing and 443 never serves.
8. **Drop `--ssh`** from `TS_EXTRA_ARGS` unless explicitly wanted. Turns Tailscale SSH into the auth path into the container.

`tailscale-serve.json` (with the AllowFunnel addition):

```json
{
  "TCP": { "443": { "HTTPS": true } },
  "Web": {
    "${TS_CERT_DOMAIN}:443": {
      "Handlers": { "/": { "Proxy": "http://127.0.0.1:8000" } }
    }
  },
  "AllowFunnel": { "${TS_CERT_DOMAIN}:443": false }
}
```

### Trade-offs

- **Cleanest for "this user, all their machines."** Multi-device works for free without us running anything.
- **Tailscale Inc. is in the trust path.** If their LE account breaks, cert provisioning stops. Tailnet Lock mitigates "compromised coordinator signs unauthorized nodes" but not "Tailscale's LE account fails." Headscale is the would-be escape hatch but can't yet provision certs the same way (see ruled-out section).
- **Audience is tailnet-only.** GitHub Actions runners, contractors not on the tailnet, hosted CI — none can reach it.
- **Sudo to install Tailscale itself** breaks pure C1 reading, but only outside `docker compose up`. Reasonable people will read this as "already-installed dependency, like docker."
- **CT-log opt-in.** Enabling HTTPS publishes machine names to public CT logs. One-time consent in the admin console.

### What's locked, what's open (for L4 specifically)

**Settled within this design:** the recipe deltas above, the `tailscale-serve.json` shape, the `network_mode: service:gitcabin` topology.

**Open:**

- Whether to ship a published gitcabin-tailscale image with the deltas pre-applied, vs documenting them in `installation.md` and letting users hand-edit.
- Tailscale Funnel as an additional mode beyond L4 (see open questions above).
- What happens if the user's free-tier device count exceeds the limit (it's currently unlimited devices, but if Tailscale changes pricing we want a doc note).

---

## What's not in this doc

- **Per-user authentication on top of gitcabin.** All three live options give TLS, not authn. gitcabin trusts whoever can reach the port. Adding authn is a separate design exercise.
- **The `uv run gitcabin` native path.** Listens on `127.0.0.1:8000`, doesn't satisfy gh's port-80/443 expectations, no TLS. Useful for direct probing only.
- **Public-internet reach (use cases that need anyone on the internet to dial gitcabin).** With L5 ruled out, there is no documented path for "GitHub Actions running in someone else's cloud needs to reach my gitcabin," "share a link with someone not on my tailnet," or similar. Anyone needing this is outside the scope of this project; build it on top.

## References

- gh source — host parsing and HTTPS-forcing: [`internal/ghinstance/host.go:53-95`](https://github.com/cli/cli/blob/trunk/internal/ghinstance/host.go).
- Let's Encrypt — [`certificates-for-localhost`](https://letsencrypt.org/docs/certificates-for-localhost/) (issuance, revocation policy, BR §4.9.1.1).
- Let's Encrypt — [revocation policy / CPS section 4.9](https://letsencrypt.org/repository/).
- CA/B Forum Baseline Requirements — [github.com/cabforum/servercert](https://github.com/cabforum/servercert/blob/main/docs/BR.md).
- crt.sh — [`*.traefik.me` certificate transparency log](https://crt.sh/?q=%25.traefik.me) (precedent for LE-on-127.0.0.1).
- Upinel/localhost.direct — [issue #18](https://github.com/Upinel/localhost.direct/issues/18) (GlobalSign revocation after key leak), [issue #19](https://github.com/Upinel/localhost.direct/issues/19) (open BR-violation complaint).
- traefik.me — prior art: https://traefik.me/, https://github.com/pyrou/traefik.me.
- Caddy ACME with DNS providers: https://caddyserver.com/docs/automatic-https.
- Tailscale pricing (free tier) — https://tailscale.com/pricing.
- Tailscale HTTPS / `tailscale cert` — https://tailscale.com/docs/how-to/set-up-https-certificates.
- Headscale issue — `tailscale cert` provisioning gap: https://github.com/juanfont/headscale/issues/1921.
- LE community thread on Mozilla revocations of native-app shippers: https://groups.google.com/d/msg/mozilla.dev.security.policy/pk039T_wPrI/tGnFDFTnCQAJ.
