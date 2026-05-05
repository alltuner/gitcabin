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

`cab` does almost nothing — clear two env vars, set two more, `exec gh`. Anything heavier than POSIX shell pays overhead for no benefit:

| Option | Cold start | Footprint | Why we passed |
|---|---|---|---|
| **POSIX shell (current)** | ~10ms | ~90 lines of `sh` | nothing simpler exists |
| Python entry-point | ~80ms | depends on gitcabin install | Python startup is felt on every interactive call |
| Single-file Python script | ~80ms | one `.py` file | no speed gain over the shell version, larger code |
| Native Go / Rust binary | ~5ms | 1–3 MB binary + cross-lang build pipeline | fastest, but a build pipeline for 90 lines of logic is a bad trade |
| `uvx` / `pipx` distributable | ~80–150ms | runtime resolver overhead | same speed cost as the Python entry-point, more layers |

The five-millisecond difference between shell and a static binary doesn't matter for an interactive wrapper today. The seventy-millisecond gap to anything that boots a Python interpreter does — typing `cab issue list` and feeling a beat of latency before `gh` even starts is exactly the user-perceptible drag we're trying to remove from the project.

A **native Rust or Go rewrite** is on the table once the surface area justifies it. It would buy us:

- ~5 ms cold start (matches `gh` itself).
- A single static binary that's trivial to drop on `$PATH` or ship via Homebrew / scoop / Cargo / go install.
- Strong typing for the pieces the shell version fakes (proxy URL parsing, JSON-aware status checks, future `cab status` output formatting).

Tracked as a follow-up; the cost isn't justified at 90 lines of plumbing, and the shell version is already correct. When `cab` grows interactive prompts, JSON manipulation, structured config, or anything that's awkward in `sh`, we move.

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
