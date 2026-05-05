# Installation

Today gitcabin ships exactly one mode: **Local-only HTTP via `cab`** — single-machine access through an unprivileged-port proxy, no certs, no privileged ports. Multi-device access (Tailnet-shared) is a deferred design captured in [`tls.md`](tls.md) but not yet built.

This concentration is intentional. The single mode is solid enough to iterate on the rest of the project (sync, mutations, dashboard) without splitting attention across deployment plumbing. We'll come back to multi-device when the rest is steady.

> Earlier rounds of the project considered a "Local with TLS" mode (per-machine local CA), a "Public/team" mode (operator-owned domain), and a "Shared wildcard cert" mode. All three are ruled out — see [`tls.md`](tls.md) for the reasoning. The Tailnet-shared design is preserved there too, ready to lift when we resume.

---

## Local-only

The default. `gh` dials `http://api.github.localhost/` over plain HTTP (the one URL shape where `gh` skips TLS — see `internal/ghinstance/host.go` in the gh source). Only this machine can reach gitcabin, and there are no certs to manage.

The wrinkle: `gh` hardcodes port 80 for `github.localhost`, which would normally force the host to bind a privileged port. We sidestep that with `cab` — a 90-line shell wrapper (`scripts/cab`, [design notes](cab.md)) that points `gh`'s `HTTP_PROXY` at gitcabin's unprivileged port (8080 by default) so cleartext requests round-trip through loopback in absolute-URI form. No host-side privileged-port binding, no `vmnetd`, no `/etc/hosts` workarounds.

### Setup

`compose.yml` binds the gitcabin API to `127.0.0.1:8080` and the dashboard to `127.0.0.1:8081` (both unprivileged). Service names are `gitcabin-api` and `gitcabin-dashboard`:

```sh
docker compose up -d
```

Build `cab` and put it on your PATH (one-time):

```sh
cd cab && go build -o /usr/local/bin/cab . && cd ..
```

Or alias it as a docker image (no Go toolchain on the host):

```sh
docker buildx build --platform linux/amd64,linux/arm64 -t alltuner/cab:dev cab/
alias cab='docker run --rm --network gitcabin_default \
  -v "$HOME/.config/gh:/home/cab/.config/gh" \
  alltuner/cab:dev'
```

### Use it

The very first `cab` command auto-registers `github.localhost` with `gh` (writes a placeholder token to `~/.config/gh/hosts.yml`); you don't have to think about it. Just go:

```sh
cab repo init me/cabin                       # one-time bare-repo init via the running container
cab issue create -R me/cabin --title "..."   # plain gh from here on
cab status                                   # any time you want to verify the setup
```

If you ever want to force re-registration (eg after `cab logout`), `cab login` does it explicitly.

### Trade-offs

- **No cert work, no DNS work, no third parties** — `github.localhost` resolves to `127.0.0.1` natively on macOS, Linux, and Windows because RFC 6761 reserves `*.localhost`.
- **No privileged ports anywhere on the host.** The vmnetd-style "Docker can't bind port 80" failure mode is gone for this mode.
- **Users have to remember to use `cab` instead of plain `gh`.** A bare `gh issue create -R me/cabin --title x` would dial `127.0.0.1:80` and fail (we don't bind it). The wrapper is short and cheap to symlink onto PATH; users who forget hit a clear error.
- **Single user.** Nobody else on your network can reach this — it's bound to `127.0.0.1` on purpose. Multi-device access is a future mode (see [`tls.md`](tls.md)'s deferred Tailnet-shared design).

---

## Verifying it works

After `cab login` (or any first invocation that auto-registers), all three of these should succeed without error:

```sh
cab auth status
cab api /
cab api graphql -f query='query { viewer { login } }'
```

If `cab auth status` returns `connection refused`, the proxy isn't reachable on `127.0.0.1:8080` — verify the compose stack is running. If it returns a 404, the proxy is forwarding to something other than gitcabin; double-check `compose.yml`. There is no TLS error path in this mode (it's plain HTTP throughout).
