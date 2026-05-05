# `cab` — the gitcabin gh wrapper

`cab` is a six-line shell script (`scripts/cab`) that lets `gh` reach a local gitcabin without touching a privileged port. It exists because `gh` hardcodes port 80 for the `github.localhost` shortcut, and binding port 80 on the host has a long tail of failure modes (Docker Desktop's `vmnetd` helper not running, port-80 conflicts with other services, rootless docker without `cap_net_bind_service`, etc.).

This doc is the design rationale; daily usage lives in the [README](../README.md#using-gitcabin).

## What it does

```sh
exec env -u no_proxy -u NO_PROXY \
  HTTP_PROXY="http://127.0.0.1:${GITCABIN_PORT:-8080}" \
  GH_HOST="${GITCABIN_HOST:-github.localhost}" \
  gh "$@"
```

That's it. Three pieces of environment, then exec into `gh`.

## Why this works

`gh` is a Go program. Go's `net/http` honors `HTTP_PROXY` for `http://...` URLs by default. When `gh -h github.localhost` constructs `http://api.github.localhost/`, the request is sent to the proxy in [absolute-form per RFC 7230](https://www.rfc-editor.org/rfc/rfc7230#section-5.3.2) — so:

- `gh` dials `127.0.0.1:8080` (where gitcabin happens to be listening).
- The HTTP request line reads `GET http://api.github.localhost/ HTTP/1.1`.
- gitcabin (granian + hyper underneath) accepts absolute-form Request-URIs and routes by path.
- Cleartext, no certs, no privileged ports anywhere on the host.

`HTTP_PROXY` only applies to `http://` URLs. `gh`'s separate calls to real `github.com` over HTTPS go through `HTTPS_PROXY` (which we don't set), so a wrapped `cab` invocation can't accidentally tunnel real-GitHub requests through gitcabin.

## Why we unset `NO_PROXY`

Many development environments default `NO_PROXY=localhost,127.0.0.1`. With that set, Go's `httpproxy` package decides the request is for an explicitly-no-proxy host and bypasses the proxy entirely — falling back to a direct dial of `127.0.0.1:80`, which puts us right back in the privileged-port problem. The wrapper unsets both casing variants (`NO_PROXY` and `no_proxy`) for the duration of the `gh` invocation.

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

It also obsoletes the **L2 shared-cert plan** that previously lived in `docs/tls.md`. L2's value proposition was "real LE cert for `*.local.gitcabin.com`, no domain ownership, no CA install." `cab` gives you the same outcome (`gh` reaches gitcabin under a built-in hostname, no domain, no CA) without operating any cert pipeline. L2 was always going to carry rotation runbooks, R2 plumbing, and revocation risk; `cab` carries six lines of shell.

L4 (Tailnet-shared) is currently deferred. When we resume it, `cab` won't be in that path — multi-device clients can't `HTTP_PROXY` back to a laptop's loopback, so the Tailscale design uses real LE certs on tailnet 443 and skips the wrapper entirely. The two modes are independent.

## Limitations to be honest about

1. **`cab` is not transparent.** Plain `gh issue create -R me/cabin --title x` (without the wrapper) will dial port 80 and fail. Users have to remember to use `cab`. We mitigate by making `cab` short and easy to symlink onto `PATH`.

2. **`gh` extensions might not honor `HTTP_PROXY`.** Most do (they go through `gh`'s standard HTTP transport), but a custom extension that shells out, or uses a non-stdlib HTTP client, could ignore it. Out of our control.

3. **Granian needs to accept absolute-form Request-URIs.** Hyper does, per RFC, and we've smoke-tested with `curl -x` against a running granian. If a future server change breaks this, the wrapper breaks; we'd notice immediately because every `cab` call would 400.

4. **HTTPS-mode deployments (L4) bypass the wrapper.** Those use TLS end-to-end through CONNECT tunneling and can't use a plain HTTP proxy. `cab` is for the local HTTP path only.

5. **First-time auth still goes through `gh auth login`.** `cab auth login --with-token` registers the host in `~/.config/gh/hosts.yml` exactly like a plain `gh auth login --hostname github.localhost --with-token` would. The wrapper just hides the `--hostname` flag.

## Env knobs

| Variable | Default | Purpose |
|---|---|---|
| `GITCABIN_PORT` | `8080` | Host port the local gitcabin instance binds. Override if the default conflicts. |
| `GITCABIN_HOST` | `github.localhost` | Hostname `gh` uses for the local instance. Override only if you've registered gitcabin under a different name. |

If you set `GITCABIN_PORT` for `cab`, also set the matching mapping in `compose.override.yml` so gitcabin actually binds that port.

## Why "cab"

Three letters, mnemonic of the project name (gitcabin → cab), free of common Unix-system collisions. We considered the two-letter `ch` (visual cousin of `gh`) but it collides with the niche [SoftIntegration Ch shell](https://www.softintegration.com/); `cab` reads more naturally for the project and avoids that.

## Why a shell script and not a binary / Python entry point

`cab` does almost nothing — clear two env vars, set two more, `exec gh`. The choice of implementation language matters only insofar as it affects cold-start time. Measured on macOS (Python 3.14, gh 2.92, M-series CPU; 20-run averages):

| Variant | Wall time | Overhead vs raw `gh` |
|---|---|---|
| Raw `gh --version` (no wrapper) | 29ms | — |
| Bare shell wrapper (`exec env … gh`) | 55ms | +26ms |
| **Current `cab` (shell + auto-register check)** | 85ms | +56ms |
| Python wrapper (no auto-register) | 47ms | +18ms |
| Hypothetical Go binary (estimated, ~3ms cold start) | ~32ms | +3ms |
| Hypothetical Rust binary (estimated, ~2ms cold start) | ~31ms | +2ms |
| Hypothetical C binary | ~30ms | +1ms |

Two real findings shifted my expectations:

1. **Python beats bare-shell by ~8ms**, because Python 3.14 starts faster than the shell `env` + exec dance does in `sh`. The "shell is always faster" priors were wrong.

2. **The biggest cost in `cab` today is the `gh auth status` check inside `ensure_auth`** (~30ms per invocation). The wrapper itself is ~26ms; the auto-register check more than doubles that. Caching the registration state after first successful call would drop `cab` to ~55ms — same speed as Python, half the gap to a native binary.

So the order-of-magnitude question isn't "shell vs binary" — it's "how often do we re-check auth?"

### The optimization path

1. **Cache the auth check** (cheapest win, ~10 lines of shell): write `~/.cache/cab/<host>.registered` after first successful `gh auth login`; subsequent invocations skip the check. Brings `cab` to ~55ms. **Tracked at [#22](https://github.com/alltuner/gitcabin/issues/22) once filed.**

2. **Native binary** (Rust or Go, see #19): drops to ~30–35ms, indistinguishable from raw `gh`. Worth it once the surface justifies — see decision criteria below.

### Rust vs Go for a future native cab

| Dimension | Rust | Go |
|---|---|---|
| Cold start | ~1–3ms | ~3–5ms |
| Binary size (release, stripped) | ~500KB–1MB | ~3–5MB |
| Build complexity | Cargo, well-trodden | go build, well-trodden |
| Cross-compilation | `cargo build --target=…` (rustup adds targets) | `GOOS=linux GOARCH=arm64 go build` (built in) |
| Distribution (Homebrew, scoop, GitHub Releases) | trivial | trivial |
| Async / concurrency story | Tokio if needed | goroutines, no extra runtime |
| Memory safety | guaranteed | GC + memory-safe-by-default |
| **Integration with gh's own code** | would have to reimplement HTTP plumbing | **could `import` cli/cli/internal/* directly** if Go modules permit, eventually skipping the gh subprocess entirely |

For *this* tool, the integration angle is what tilts the scales toward Go — even though Rust would be a hair faster.

The "skip the gh subprocess" option (Go embedding gh's transport directly) takes the latency floor below 30ms because we no longer pay `gh`'s own cold start (~29ms). Best case: cab does what gh would have done, in-process, in ~5ms. That's the actual ceiling of what's possible without rearchitecting gitcabin's API to be reachable directly (which would defeat the purpose — gh's special-casing of `github.localhost` is what we're piggybacking on).

### Decision criteria

We move to a native binary when one of these is true:

- `cab` grows beyond pure passthrough (interactive prompts, JSON manipulation, structured `cab status` output, multi-host management).
- We start shipping `cab` independently of the gitcabin clone (curl-installer, Homebrew tap).
- Measured user-visible latency becomes a persistent complaint.

Until then, the shell version is the right answer — small, readable, debuggable with `set -x`, and within ~50ms of the theoretical ceiling.

## Distribution

Today: clone the repo, symlink `scripts/cab` to a directory on your PATH. Works fine for developers, painful for end users.

Cleaner paths we may ship later:

- **`curl | sh` installer** — host the script at a stable URL (`gitcabin.com/cab`, raw GitHub) so users can `curl -fsSL https://gitcabin.com/cab -o /usr/local/bin/cab && chmod +x /usr/local/bin/cab`. Same pattern `gh`, `direnv`, `mise` use.
- **Homebrew tap** — `brew install alltuner/tap/cab`. Most polished on macOS; maintenance overhead is a tap repo plus a formula pinning the tag.
- **`cab` as a docker image** — a small container that bundles `gh` plus the wrapper, joins the gitcabin compose network, and proxies through `gitcabin:8000` instead of `127.0.0.1:8080`. Users don't need to install `gh` on the host at all:

  ```sh
  alias cab='docker run --rm \
    --network gitcabin_default \
    -e GITCABIN_PORT=8000 -e GITCABIN_HOST=gitcabin \
    ghcr.io/alltuner/cab'
  ```

  **No credentials need to leave the host.** gitcabin doesn't verify tokens, so the cab image can bake a placeholder `hosts.yml` at build time and run completely self-contained. There's no need to mount `~/.config/gh` from the host into the container — the auto-register mechanism (or a pre-baked hosts.yml) handles the registration locally inside each ephemeral container. That's a deliberate property of the design: `cab` only ever talks to gitcabin (HTTP, loopback, placeholder token), so there's nothing meaningful to inherit.

  Trade-off: ~300ms container startup per invocation (vs ~10ms for the shell script), but zero host-side dependencies beyond docker. Could be worth shipping for users who run gitcabin from compose and have docker but not gh.

  **Note for the future `gitcabin sync` work:** the *server* needs real `github.com` credentials when sync runs (the calls hit `api.github.com` over HTTPS). Today sync runs on the host (`uv run python -m gitcabin sync …`) and inherits the host's `~/.config/gh/` naturally. When sync moves into the gitcabin container, we'd mount `~/.config/gh:/home/app/.config/gh:ro` on that service — same pattern `act` and other gh-driven container tools use. That's a server-container concern, not a cab-container concern.

- **Published gitcabin server image** — orthogonal: if `docker run ghcr.io/alltuner/gitcabin` works for the server, the user doesn't need to clone the repo at all. Pair with the dockerized `cab` and the entire toolchain becomes "two `docker run`s + a shell alias."

None of these are blockers. They're "v0.1 polish" once we have a release worth installing without `git clone`.
