# Git access (design notes)

> **Status:** not built. This doc enumerates the performance design space for smart-HTTP serving from gitcabin's existing FastAPI process so we can pick a starting point with eyes open.

Users need to `git clone`, `git fetch`, and `git push` against gitcabin's bare repos. The bind mount at `./data:/app/data` already lets a user on the same host run `git clone file:///$(pwd)/data/repos/me/cabin.git` — that's the trivial baseline. This doc is about the **smart-HTTP** layer that gives us cross-device access (paired with Tailscale) and a single port to firewall.

The constraint: gitcabin already runs on `127.0.0.1:18080`, single-port, granian + FastAPI. Adding git over HTTP means routing `/<owner>/<name>.git/...` URLs through the same process. We want this to feel *instant* on small repos and *not bottleneck* on large ones.

---

## The protocol surface

Smart-HTTP is three URLs per repo:

| URL | Method | Purpose |
|---|---|---|
| `/<o>/<n>.git/info/refs?service=git-upload-pack`   | GET  | advertise refs (clone/fetch discovery) |
| `/<o>/<n>.git/info/refs?service=git-receive-pack`  | GET  | advertise refs (push discovery) |
| `/<o>/<n>.git/git-upload-pack`                     | POST | stream packfile to client (clone/fetch) |
| `/<o>/<n>.git/git-receive-pack`                    | POST | accept packfile from client (push) |

The two POSTs are where ~all the bytes flow. They're long-lived streamed responses; latency on `/info/refs` matters for first-byte-time on `git clone`.

---

## Tier 1 — Free wins (config + careful streaming)

These aren't architecture decisions; they're requirements regardless of what backs the routes. Skip any of them and the rest is moot.

- **Async-friendly subprocess.** Use `asyncio.create_subprocess_exec` (or anyio equivalent), not `subprocess.run`. `git-upload-pack` for a kernel-sized repo can run for tens of seconds; blocking the event loop kills concurrency for the whole app.
- **Streaming I/O both directions.** Wrap response bodies in `StreamingResponse` with an async iterator that pumps chunks from the subprocess's stdout pipe. Don't `await proc.communicate()`. For POST upload, stream the request body into the subprocess's stdin instead of materializing the request body in memory.
- **No double compression.** `git-upload-pack` already compresses pack data. Don't apply gzip/brotli at the HTTP layer on the smart-HTTP responses — net loss. Set `Content-Encoding: identity` (or omit) on `*-pack` responses; let `info/refs` go through normal gzip if you want.
- **Protocol v2.** Modern git clients negotiate it via the `Git-Protocol: version=2` request header. Server-side support is automatic in any git ≥ 2.18; we just need to forward the header into the subprocess environment as `GIT_PROTOCOL`. v2 halves the round-trip count on `info/refs` advertising for repos with many refs.
- **HTTP keepalive.** Granian defaults to keepalive on; just don't fight it. A `git fetch` is `GET /info/refs` then `POST /git-upload-pack` — if those reuse the connection the second request skips TCP+TLS handshake, which is the dominant fixed cost on Tailscale.
- **Server-side pack indexes.** `commit-graph`, `multi-pack-index`, and bitmap reachability index speed up `upload-pack`'s pack generation by orders of magnitude on large histories. Run `git maintenance run --task=commit-graph --task=incremental-repack` on a cron, or just on every `gitcabin sync pull`. Free perf.
- **Bitmap reuse.** `pack.useBitmaps=true` (default) plus a bitmap-indexed pack (`git repack -bd` or `git multi-pack-index write --bitmap`) means clone responses can reuse the on-disk bitmap to compute "what objects to send" without walking the graph. The single biggest server-side win for clone-heavy workloads.

These are baseline. Whatever backend we pick has to support them.

---

## Tier 2 — Backend choice (the actual decision)

### Option A — `git http-backend` subprocess

The canonical answer. `git http-backend` is the CGI binary git ships with, and it does the protocol-handler dispatch internally. We expose a single FastAPI route per repo that sets the CGI env vars (`PATH_INFO`, `QUERY_STRING`, `REQUEST_METHOD`, `CONTENT_TYPE`, `GIT_PROJECT_ROOT`, `GIT_PROTOCOL`, etc.), pipes request body in, and streams response back.

Pros:
- Battle-tested. Every git server (Gitea, Gitolite, GitHub itself for this layer) does this or a close variant.
- Implements both `upload-pack` and `receive-pack` plus their sub-protocols (capability negotiation, sideband, packfile streaming) for free.
- Picks up every git-core perf improvement without our involvement.
- Bitmap / commit-graph / midx all "just work."

Cons:
- One `fork()` + `execve()` per request. ~5–10 ms on Linux, ~10–20 ms on macOS. Imperceptible for a `clone`, noticeable in tight CI loops doing dozens of fetches.
- CGI env-var contract is ugly to set up; needs care around `Content-Length` vs. `Transfer-Encoding: chunked` (`http-backend` wants the former).

### Option B — Direct `upload-pack` / `receive-pack` invocation

Skip `http-backend` and call `git upload-pack --stateless-rpc --advertise-refs <repo>` (for `info/refs`) or `git upload-pack --stateless-rpc <repo>` (for the POST), framing the smart-HTTP wrapper ourselves.

Pros:
- Same upstream binaries — same perf profile as A.
- One less subprocess layer; a hair lower latency than `http-backend` (no CGI parsing).
- Direct control over response headers + status codes (cleaner error mapping into FastAPI exceptions).

Cons:
- We own the smart-HTTP framing (`pkt-line`, the `# service=...` advertisement banner, sideband demux on errors). Roughly 100 lines of careful code, but easy to get wrong.
- Have to reimplement what `http-backend` does for free.

### Option C — `dulwich` in-process

Pure-Python git implementation. `dulwich.server.HTTPGitApplication` is a WSGI app implementing smart-HTTP end-to-end. We'd mount it under FastAPI (or convert to ASGI).

Pros:
- No subprocess. No `fork()`, no pipe overhead, no env-var dance. Lowest latency on small operations.
- Easy to introspect: hook into the protocol from Python (e.g., reject pushes that violate gitcabin invariants).
- One dependency, no native binaries — important if we ever ship a Windows install.

Cons:
- Pack generation is **much slower than git-core** on large repos. Python loops vs. C loops. Pack-rev list, delta computation, zlib in Python — for a kernel-sized repo, dulwich is several × slower than `git upload-pack`. For repos under a few hundred MB it's fine.
- No bitmap / commit-graph use. dulwich computes pack contents from scratch each time, leaving the biggest perf win on the table.
- Smaller community; obscure protocol bugs land here months later than in git-core.

### Option D — `pygit2` (libgit2)

C library bindings. Fast object access, but **libgit2 doesn't implement smart-HTTP server-side packfile generation directly** — its server-side pack negotiation is incomplete. We'd be building most of the protocol ourselves on top of libgit2 primitives.

Pros: fast object reads, in-process.
Cons: more code than B, less coverage than A or C, libgit2 quirks. Not recommended unless we have a specific reason.

### Option E — `gitoxide` (`gix`) via Rust extension

Even faster than libgit2 for some operations, server-side smart-HTTP is in flux. Worth revisiting in a year; not now.

---

## Tier 3 — Advanced (probably overkill at our scale)

- **Persistent worker pool.** Pre-fork a few `git upload-pack` processes idling on a control socket; dispatch requests to them via fd-passing. Eliminates fork cost. Useful if request rate is high enough that fork latency is in the p50 — single-user, not.
- **`info/refs` cache.** Memoize the advertisement output keyed on (`repo`, `service`, `HEAD-of-refs/`). Invalidate on any ref update. Wins ~5 ms per request, irrelevant unless someone is hammering the endpoint.
- **Pre-built clone packs.** GitHub does this — pre-compute "the pack you'd send to a fresh client" for the default ref tip and serve it as a static file on the first hit. Lots of code, lots of edge cases (filtering, partial clones, shallow). Not for us.
- **HTTP/2 + multiplexing.** Granian supports it. Doesn't help individual git operations (single request body) but helps when a client opens multiple repos in parallel. Default on; no work.
- **Partial clone / `--filter`.** Server-side support is in `upload-pack` already (Tier 1 makes it transparent). Big win for clones of large repos. Free.

---

## Recommendation

**Start with Option A (`git http-backend`)**, do all of Tier 1 from day one, and ship a `gitcabin maintenance run` task that maintains commit-graph + bitmaps on every bare repo. That gets us:

- Battle-tested protocol coverage (no subtle bugs we have to chase).
- Whatever git-core ships, we get for free — including `protocol v2`, `--filter`, etc.
- Performance dominated by disk + network on big repos, by fork cost on small ones, both acceptable.

**If profiling later shows fork is the bottleneck**, the cleanest upgrade path is **Option B** — direct `upload-pack` invocation skips `http-backend`'s CGI shim while keeping the same battle-tested binaries. We'd write the pkt-line framing ourselves; that's a few hours of work, not days.

**Don't reach for `dulwich` (Option C)** unless we hit a hard requirement that needs in-process protocol introspection (e.g., pre-receive hooks written in Python that need to inspect the incoming pack mid-stream). It's tempting because "no subprocess" sounds clean, but we'd be giving up the bitmap/commit-graph wins that matter on big repos.

The order to build:

1. FastAPI route handlers for the four URLs, calling `git http-backend` via async subprocess.
2. Stream both ways. No buffering.
3. Forward `Git-Protocol` header into env.
4. `gitcabin maintenance` CLI command running `git maintenance run --task=commit-graph --task=incremental-repack` per bare repo.
5. Smoke-test `git clone http://localhost:18080/me/cabin.git` round-trip works.
6. Decide on auth — for loopback + Tailscale, "none" is the simplest correct answer. Bearer-token gate when we open it wider.

That's the cheapest way to get to "feels instant for everything I'd realistically clone" and leave the upgrade lanes open.

---

## What this doc deliberately doesn't decide

- **Auth model.** Loopback today; revisit when binding to non-loopback. Tailscale identity is a strong default if/when we go multi-device.
- **Push permissions.** Even with auth, do we accept any push, or do we want pre-receive hooks to enforce gitcabin invariants (e.g., reject writes to `refs/issues/*` from raw `git push`)? Worth a separate design pass.
- **`gitcabin maintenance` scheduling.** Cron, on-pull, on-quiescence — pick when we build it.
- **Dumb-HTTP fallback.** Skip; smart-HTTP is universal in 2026.
