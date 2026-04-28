# Security Policy

## Supported versions

gitcabin is pre-1.0. Only the latest published release on `main` is supported. There are no LTS branches and no backports to older tags.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for security vulnerabilities. Two private channels:

- **GitHub Security Advisories** — preferred. Open a draft advisory at https://github.com/alltuner/gitcabin/security/advisories/new. This keeps the report private until we've coordinated a fix.
- **Email** — david@poblador.com if you can't use the advisory flow.

Include:

- A description of the issue and its impact.
- Steps to reproduce, or a proof-of-concept.
- Affected versions, if known.
- Whether you've already disclosed this to anyone else.

We'll acknowledge receipt within a few days, give you a rough triage estimate, and keep you in the loop on the fix and disclosure timeline. If we agree on a fix, we'll credit you in the release notes unless you'd rather stay anonymous.

## Scope

In scope:

- The gitcabin server (REST + GraphQL).
- The HTML dashboard.
- The shipped Docker images on `ghcr.io/alltuner/gitcabin`.
- The default `compose.yml` and any `compose.*.yml` overlays we publish.

Out of scope (not because they don't matter, but because they're not the right place for a vulnerability report):

- gh CLI behavior — that's https://github.com/cli/cli.
- Caddy / Tailscale / other sidecars used in deployment recipes — report upstream.
- Choices documented as known trade-offs in `docs/shared-cert.md` (e.g. shared-key TLS variants and their stated DNS-trust constraints). If you find a *new* attack on those models that we haven't documented, that *is* in scope.

## Threat model context

gitcabin is designed to run on a user's own machine and trust whoever can reach the listening port. It does not implement per-user authentication on top of `gh`'s tokens; the deployment mode controls who can reach it. Reports based on "anyone who can connect can do anything" are working as designed and not vulnerabilities — see `docs/installation.md` for the deployment recipes that constrain reach.
