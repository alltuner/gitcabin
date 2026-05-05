# `cab` — the gitcabin gh wrapper

`cab` is a small Go binary (`cab/main.go`) that lets `gh` reach a local gitcabin without touching a privileged port. It exists because `gh` hardcodes port 80 for the `github.localhost` shortcut, and binding port 80 on the host has a long tail of failure modes (Docker Desktop's `vmnetd` helper not running, port-80 conflicts with other services, rootless docker without `cap_net_bind_service`, etc.).

This doc is the design rationale; daily usage lives in the [README](../README.md#using-gitcabin).

## What it does

For every invocation, `cab`:

1. Reads `~/.config/gh/hosts.yml` to confirm `github.localhost` is registered with `gh`. If not, it writes the registration entry directly (with a placeholder token — gitcabin doesn't verify tokens, so any non-empty value works).
2. Sets `HTTP_PROXY` to gitcabin's unprivileged port and `GH_HOST` to `github.localhost`.
3. `syscall.Exec`s into `gh` with the user's args, replacing the cab process so signals, exit codes, and stdio behave exactly like running `gh` directly.

For specific cab subcommands (`status`, `login`, `logout`, `repo init`) the binary handles them natively rather than passing through to `gh`.

## Why this works

`gh` is a Go program. Go's `net/http` honors `HTTP_PROXY` for `http://...` URLs by default. When `gh -h github.localhost` constructs `http://api.github.localhost/`, the request is sent to the proxy in [absolute-form per RFC 7230](https://www.rfc-editor.org/rfc/rfc7230#section-5.3.2):

- `gh` dials the proxy URL (`127.0.0.1:8080` on host, `gitcabin-api:8000` inside the docker network).
- The HTTP request line reads `GET http://api.github.localhost/ HTTP/1.1`.
- gitcabin (granian + hyper underneath) accepts absolute-form Request-URIs and routes by path.
- Cleartext, no certs, no privileged ports anywhere on the host.

`HTTP_PROXY` only applies to `http://` URLs. `gh`'s separate calls to real `github.com` over HTTPS go through `HTTPS_PROXY` (which we don't set), so a wrapped `cab` invocation can't accidentally tunnel real-GitHub requests through gitcabin.

## Why we clear `NO_PROXY`

Many development environments default `NO_PROXY=localhost,127.0.0.1`. With that set, Go's `httpproxy` package decides the request is for an explicitly-no-proxy host and bypasses the proxy entirely — falling back to a direct dial of `127.0.0.1:80`, which puts us right back in the privileged-port problem. cab strips both casing variants (`NO_PROXY` and `no_proxy`) from the env it hands to `gh`.

## What this kills in the design space

Before `cab`, the L1 (local-only HTTP) mode required:

- Docker daemon binding port 80 on the host (privileged).
- macOS users not having `vmnetd` glitches; Linux users not running rootless docker.
- The `127.42.0.1` `/etc/hosts` workaround for users who already had something on `127.0.0.1:80`.

After `cab`:

- gitcabin binds an unprivileged port (8080 by default).
- Any port-80 conflict is irrelevant because we don't bind 80 anywhere.
- `vmnetd` is not in the path.
- The `/etc/hosts` workaround is unnecessary and has been deleted from the README.

It also obsoletes the **L2 shared-cert plan** that previously lived in `docs/tls.md`. L2's value proposition was "real LE cert for `*.local.gitcabin.com`, no domain ownership, no CA install." `cab` gives you the same outcome (`gh` reaches gitcabin under a built-in hostname, no domain, no CA) without operating any cert pipeline.

L4 (Tailnet-shared) is currently deferred. When we resume it, `cab` won't be in that path — multi-device clients can't `HTTP_PROXY` back to a laptop's loopback, so the Tailscale design uses real LE certs on tailnet 443 and skips the wrapper entirely. The two modes are independent.

## Why no `gh` subprocess for auth ops

`gh auth login`, `gh auth status`, and `gh auth logout` all just read or write `~/.config/gh/hosts.yml`. `cab` does the same directly. That removes ~30ms of gh cold start from every `cab login` / `cab status` / `cab logout` and from the auth check on every passthrough.

We do still `syscall.Exec` to `gh` for the actual passthrough — the gh-shaped commands (`issue create`, `issue list`, etc.) are precisely what `gh`'s UI is built for, and reimplementing it would be a different project. See "Why we kept gh as a subprocess for passthrough" below.

## Why we kept `gh` as a subprocess for passthrough

We considered importing `cli/cli` directly to skip the subprocess. The blocker is Go's `internal` package rule: cli/cli puts essentially all of its useful code under `internal/`, which our module can't import. Workarounds (forking cli/cli, vendoring select files, reimplementing every command) all ship with parity tracking and maintenance debt that outweighs the latency win.

Measured: cab Go binary at ~35ms cold start, raw gh at ~39ms (the syscall.Exec replaces the cab process before paying cab's own costs twice). The wrapper overhead is already gone — we're at gh's own latency floor for everything that goes through gh.

For non-gh-shaped operations (`cab repo init`, `cab status`, `cab login`, future `cab sync` commands), we already implement them natively in Go. That's where the C-style "skip gh entirely" decision is correct, and we apply it. For gh-shaped passthrough (`cab issue create` etc.), gh's UI is the point — reimplementing it is a different project.

## Distribution

Two paths today; both build from `cab/`:

### Native binary on the host

```sh
cd cab && go build -o /usr/local/bin/cab . && cd ..
```

Single static binary, ~9 MB, no runtime dependencies beyond `gh` on `$PATH`. Cold start ~35 ms.

### Docker image

```sh
docker buildx build --platform linux/amd64,linux/arm64 -t alltuner/cab:dev cab/
alias cab='docker run --rm --network gitcabin_default \
  -v "$HOME/.config/gh:/home/cab/.config/gh" \
  alltuner/cab:dev'
```

The image bundles `gh` (pinned to the `GH_VERSION` build arg, default 2.92.0) so users don't need it on the host. The mounted `~/.config/gh` is intentional — gitcabin doesn't verify tokens, but the volume keeps `cab login` state consistent across docker invocations and across host-side `gh` use.

The image is multi-arch (`linux/amd64` + `linux/arm64`). To build without buildx (single arch, faster local iteration):

```sh
docker build -t alltuner/cab:dev cab/
```

## Cleaner paths we may ship later

- **`curl | sh` installer** — host the binary at a stable URL (`gitcabin.com/cab`, GitHub Releases) so users can `curl -fsSL https://gitcabin.com/cab/install.sh | sh`. Requires committing to a stable URL + a release pipeline.
- **Homebrew tap** — `brew install alltuner/tap/cab`. Most polished on macOS; maintenance overhead is a tap repo plus a formula pinning the tag.
- **Published gitcabin server image** — orthogonal: if `docker run ghcr.io/alltuner/gitcabin` works for the server, the user doesn't need to clone the repo at all. Pair with the `cab` docker image and the entire toolchain becomes "two `docker run`s + a shell alias."

None of these are blockers. They're "v0.1 polish" once we have a release worth installing without `git clone`.

## Limitations to be honest about

1. **`cab` is not transparent.** Plain `gh issue create -R me/cabin --title x` (without the wrapper) will dial port 80 and fail. Users have to remember to use `cab`. The Go binary is short and easy to drop on PATH.

2. **`gh` extensions might not honor `HTTP_PROXY`.** Most do (they go through `gh`'s standard HTTP transport), but a custom extension that shells out, or uses a non-stdlib HTTP client, could ignore it. Out of our control.

3. **Granian needs to accept absolute-form Request-URIs.** Hyper does, per RFC, and we've smoke-tested with `curl -x` against a running granian. If a future server change breaks this, the wrapper breaks; every `cab` call would 400.

4. **HTTPS-mode deployments (L4) bypass the wrapper.** Those use TLS end-to-end through CONNECT tunneling and can't use a plain HTTP proxy. `cab` is for the local HTTP path only.

5. **`cab repo init` only works when cab can reach the gitcabin data directory** (host-side use, where `./data/repos/...` is writable). The dockerized `cab` can't init repos because the data volume isn't mounted there. Future: a `createRepository` GraphQL mutation in gitcabin would let dockerized cab init repos via API.

## Env knobs

| Variable | Default | Purpose |
|---|---|---|
| `GITCABIN_PROXY` | (unset) | Full proxy URL. Overrides `GITCABIN_PORT`. Used in the docker image to point at `http://gitcabin-api:8000`. |
| `GITCABIN_PORT` | `8080` | Host port the local gitcabin instance binds. Used to compute `GITCABIN_PROXY` when the latter isn't set. |
| `GITCABIN_HOST` | `github.localhost` | Hostname `gh` uses for the local instance. Almost never needs to change — `github.localhost` is the one hostname gh special-cases for plain HTTP. |
| `GITCABIN_DATA_DIR` | `./data` | Where `cab repo init` looks for the gitcabin data directory. Only relevant on the host. |

## Why "cab"

Three letters, mnemonic of the project name (gitcabin → cab), free of common Unix-system collisions. We considered the two-letter `ch` (visual cousin of `gh`) but it collides with the niche [SoftIntegration Ch shell](https://www.softintegration.com/); `cab` reads more naturally for the project and avoids that.

## Performance

20-run averages on macOS (Python 3.14, gh 2.92, M-series):

| Variant | Wall time | Overhead vs raw `gh` |
|---|---|---|
| Raw `gh --version` (no wrapper) | 29 ms | — |
| **Go cab `--version` (current)** | 35 ms | +6 ms |
| Bare shell wrapper (legacy) | 55 ms | +26 ms |
| Shell wrapper with `gh auth status` check (legacy) | 85 ms | +56 ms |
| Python wrapper | 47 ms | +18 ms |

Two findings worth recording:

1. **The Go binary essentially disappears from the latency budget.** It actually beats raw `gh` slightly because `syscall.Exec` replaces the process before any cab-specific code paths run twice. We're at gh's own startup floor.

2. **The biggest cost in the previous shell version wasn't language overhead — it was `gh auth status` running on every invocation.** The Go version reads `~/.config/gh/hosts.yml` directly, so the auth check is a single file stat (~0.1 ms) instead of a 30 ms gh subprocess. That's where the 50 ms saving came from.

Going further requires reimplementing gh's commands in pure Go (see "Why we kept gh as a subprocess" above). Not worth the effort while the wrapper is already at gh's floor.
