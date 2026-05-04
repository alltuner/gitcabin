<h1 align="center">gitcabin</h1>

<p align="center">
  <strong>A tiny self-hosted GitHub clone driven by the official <code>gh</code> CLI.</strong><br>
  All metadata stored in git itself — no separate database.
</p>

<p align="center">
  <a href="https://github.com/sponsors/alltuner">Sponsor</a>
</p>

<p align="center">
  <img src="https://img.shields.io/github/license/alltuner/gitcabin?color=5B2333" alt="License">
  <img src="https://img.shields.io/github/stars/alltuner/gitcabin?color=5B2333" alt="Stars">
</p>

> [!WARNING]
> **Pre-alpha sketch — not functional software yet.**
> This repo is a working sketch of what gitcabin *could* be, not a tool you can rely on. Most of what's here is design notes and structural code; deployment recipes work in narrow conditions but will break in real use. Read for the ideas; don't deploy for production. Feedback on the direction is very welcome — see [`docs/`](docs/) for what's been thought through and what's still open.

---

## What is gitcabin?

A tiny self-hosted GitHub clone driven by the official `gh` CLI, with all metadata stored in git itself.

### How it works

- `gh` has built-in support for arbitrary hosts via `GH_HOST`. The hostname `github.localhost` is special: `gh` sends to `http://api.github.localhost/` (REST) and `http://api.github.localhost/graphql` (GraphQL), so HTTPS is not required for local dev. For any other hostname `gh` forces HTTPS and uses the GitHub Enterprise URL shape (`https://<host>/api/v3/...` and `https://<host>/api/graphql`). gitcabin serves both shapes, so the same image works behind either path.
- Issues, PRs, and counters live in side refs of the bare git repo (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`). Code lives in normal `refs/heads/*` and `refs/tags/*`. The two namespaces never collide.
- The HTTP API server is the only writer of metadata refs. Plain `git clone`/`git push` only see code.

## Quickstart

The default deploy is local-only over HTTP via `github.localhost`. One command brings it up:

```sh
docker compose watch
```

Compose builds the image, runs the API container on `127.0.0.1:80` and the HTML dashboard on `127.0.0.1:8080`, and reloads on every source edit. Plain `docker compose up --build` works too if you don't want autoreload.

Then point `gh` at it:

```sh
echo "any-token" | gh auth login --hostname github.localhost --with-token
GH_HOST=github.localhost gh auth status
```

That's it. The token is unverified — gitcabin trusts whoever can reach the port.

Stop with `docker compose down`.

### Port 80 is already in use

`gh` hardcodes port 80 for `github.localhost`, so the proxy needs *some* port-80 binding to exist. If you already have something on `127.0.0.1:80`, give gitcabin a dedicated loopback IP:

```sh
# /etc/hosts (sudo required)
127.42.0.1   github.localhost api.github.localhost
```

```yaml
# compose.override.yml
services:
  gitcabin:
    ports:
      - "127.42.0.1:80:8000"
```

`127/8` is all loopback on Linux and macOS, so `127.42.0.1` is essentially free real estate. Run `docker compose up -d` and `gh` will dial port 80 on `127.42.0.1` instead of `127.0.0.1`.

## Browsing the data

The compose stack runs an HTML dashboard alongside the API on `127.0.0.1:8080`:

```sh
open http://localhost:8080/
```

The dashboard reads the same bare repos as the API and lets you browse issues, refs, commits, blames, and tree views. Code refs (`refs/heads/*`) and metadata refs (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`) are presented separately.

## Other deployment modes

The local-only quickstart is one of four documented setups:

| Mode | Audience | Cert work | Domain needed |
|---|---|---|---|
| Local-only (default) | the user, on this machine | none | `github.localhost` (built in) |
| Local with TLS | the user, on this machine | per-machine local CA, one-time `scripts/trust.sh` | `*.gitcabin.localhost` (built in) |
| Tailnet-shared | anyone on your tailnet | none (Tailscale provisions) | tailnet hostname (built in) |
| Public/team | anyone with DNS resolution | DNS-01 via Caddy | one you own |

See [`docs/installation.md`](docs/installation.md) for the TLS, Tailscale, and Caddy recipes, the exact `gh auth login` invocation each one pairs with, and trade-offs.

## Running natively (no Docker, no gh)

```sh
uv run gitcabin
```

Listens on `127.0.0.1:8000`. Useful for direct probing with curl / httpie, but `gh` won't reach it — `gh` dials port 80 (`github.localhost`) or 443 (anything else), never 8000.

## Development

```sh
uv sync                                    # install deps + editable gitcabin
uv run pytest                              # tests
uv run ruff check . && uv run ruff format --check .
```

## Support the project

gitcabin is an open source project built by [David Poblador i Garcia](https://davidpoblador.com/) through [All Tuner Labs](https://www.alltuner.com/).

If the direction interests you, consider supporting its development.

❤️ **Sponsor development**
https://github.com/sponsors/alltuner

☕ **One-time support**
https://buymeacoffee.com/alltuner

Your support helps fund the continued development of gitcabin and other open source developer tools such as [Factory Floor](https://github.com/alltuner/factoryfloor).

## License

[MIT](LICENSE)

---

<p align="center">
  Built by <a href="https://davidpoblador.com">David Poblador i Garcia</a> with the support of <a href="https://alltuner.com">All Tuner Labs</a>.<br>
  Made with ❤️ in Poblenou, Barcelona.
</p>
