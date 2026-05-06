<p align="center">
  <img src="https://brand.alltuner.com/logos/gitcabin/horizontal.png" alt="gitcabin" width="500">
</p>

<p align="center">
  <strong>A tiny self-hosted GitHub clone driven by the official <code>gh</code> CLI.</strong><br>
  All metadata stored in git itself — no separate database.
</p>

<p align="center">
  <a href="https://www.gitcabin.com/">Website</a> ·
  <a href="https://alltuner.com/sponsor/">Sponsor</a>
</p>

<p align="center">
  <img src="https://img.shields.io/github/license/alltuner/gitcabin?color=5B2333" alt="License">
  <img src="https://img.shields.io/github/stars/alltuner/gitcabin?color=5B2333" alt="Stars">
</p>

> [!WARNING]
> **Developing in the open — not yet a launched product.**
> Most of what's here is design notes and structural code; recipes work in narrow conditions and will break in real use. Read for the ideas, follow along as it firms up, but don't deploy for production. Feedback on the direction is very welcome — see [`docs/`](docs/) for what's been thought through and what's still open.

---

## Get Started

The default deploy is local-only over HTTP via `github.localhost`. One command brings it up:

```sh
docker compose up --watch
```

Compose builds the image, runs a single `gitcabin` service bound to `127.0.0.1:18080`, streams its access logs to your terminal, and reloads on every source edit. Plain `docker compose up --build` works too if you don't want autoreload. (`docker compose watch` without `up` also works for the watcher, but only emits sync events — `up --watch` is what you want for active development because it streams container logs too.) The container fronts both the REST/GraphQL API and the HTML dashboard via Host-header dispatch — `cab`/`gh` traffic (Host: `api.github.localhost`) hits the API; browser traffic hits the dashboard. **One unprivileged port** — neither the host nor the daemon binds 80 or 443.

`cab` is the wrapper that points `gh`'s HTTP traffic at gitcabin and registers the host with `gh` on first use. Two ways to invoke it:

**Build the Go binary (host-side):**

```sh
cd cab && go build -o /usr/local/bin/cab . && cd ..
```

**Or alias the docker image (no Go toolchain needed):**

```sh
docker buildx build --platform linux/amd64,linux/arm64 -t alltuner/cab:dev cab/
alias cab='docker run --rm --network gitcabin_default \
  -v "$HOME/.config/gh:/home/cab/.config/gh" \
  alltuner/cab:dev'
```

Either way, the very first `cab` command auto-registers `github.localhost` with `gh` (writes a placeholder token to `~/.config/gh/hosts.yml` — gitcabin doesn't verify tokens, anyone who can reach the port is the owner) and then runs whatever you asked:

```sh
cab repo init me/cabin                             # init a fresh repo
cab issue create -R me/cabin --title "First issue" --body "Try things out"
cab issue list -R me/cabin
```

`cab` sets `HTTP_PROXY` to `127.0.0.1:18080` (gitcabin's unprivileged port; or `gitcabin:8000` from inside the docker network) and `GH_HOST` to `github.localhost`, then `exec`s `gh`. `gh` honors `HTTP_PROXY` for `http://...` URLs (its calls to real `github.com` over HTTPS are unaffected), so a single `cab issue create -R me/cabin --title ...` Just Works without ever touching a privileged port — no `vmnetd`, no port-80 conflicts, no `/etc/hosts` edits. See [`docs/cab.md`](docs/cab.md) for the design.

Stop with `docker compose down`.

---

## What is gitcabin?

A tiny self-hosted GitHub clone driven by the official `gh` CLI, with all metadata stored in git itself.

### How it works

- `gh` has built-in support for arbitrary hosts via `GH_HOST`. The hostname `github.localhost` is special: `gh` sends to `http://api.github.localhost/` (REST) and `http://api.github.localhost/graphql` (GraphQL), so HTTPS is not required for local dev. For any other hostname `gh` forces HTTPS and uses the GitHub Enterprise URL shape (`https://<host>/api/v3/...` and `https://<host>/api/graphql`). gitcabin serves both shapes, so the same image works behind either path.
- Issues, PRs, and counters live in side refs of the bare git repo (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`). Code lives in normal `refs/heads/*` and `refs/tags/*`. The two namespaces never collide.
- The HTTP API server is the only writer of metadata refs. Plain `git clone`/`git push` only see code.

## Using gitcabin

Once the stack is up, every operation is `cab <whatever-gh-subcommand>`. The wrapper points `gh`'s HTTP traffic at the unprivileged proxy port and registers the host with `gh` on first use; otherwise it's a transparent passthrough.

### Working with issues

A new repo needs a one-time bare-repo init on disk because `gh` validates the repo exists before sending mutations. `cab repo init` does it via the running container:

```sh
cab repo init me/cabin
```

After that, the rest is plain `gh`:

```sh
# Create an issue.
cab issue create -R me/cabin --title "First issue" --body "Try things out"

# List issues. State filters work; ordering options are accepted but ignored.
cab issue list -R me/cabin
cab issue list -R me/cabin --state closed

# View one, optionally with its comments.
cab issue view 1 -R me/cabin
cab issue view 1 -R me/cabin --comments

# Edit your own issue. Title or body, separately or together.
cab issue edit 1 -R me/cabin --title "Renamed"
cab issue edit 1 -R me/cabin --body "Updated body"

# Close. The reopen mutation isn't exposed over GraphQL yet (the
# dashboard reopens via its own POST endpoint — see /issues/<n>/reopen).
cab issue close 1 -R me/cabin
```

### Comments

```sh
cab issue comment 1 -R me/cabin --body "A reply"

# Edit your own comment.
cab api graphql -f query='
  mutation U($id: ID!, $body: String!) {
    updateIssueComment(input: {id: $id, body: $body}) {
      issueComment { body }
    }
  }
' -F id=<comment-id> -F body="Edited reply"

# Delete your own comment (or any comment if you have ADMIN on a synced repo).
cab api graphql -f query='
  mutation D($id: ID!) {
    deleteIssueComment(input: {id: $id}) { clientMutationId }
  }
' -F id=<comment-id>
```

`cab issue comment --edit-last` and `cab issue comment --delete` are the friendlier wrappers — both work as soon as gh's version supports `updateIssueComment` / `deleteIssueComment` (gh 2.92+).

### Editability — who can change what

The same rules GitHub uses, enforced by the API:

| Action | When viewer == author | When viewer != author |
|---|---|---|
| Edit issue title / body | yes | **no, never** — even ADMIN. Editing someone else's words is impersonation. |
| Close / reopen issue | yes | only with TRIAGE / WRITE / MAINTAIN / ADMIN role |
| Edit comment body | yes | **no, never** — same rule |
| Delete comment | yes | only with ADMIN (moderation) |

These checks fire in the API layer, so they hold whether you go through `gh`, raw GraphQL, or the dashboard. The GraphQL types also expose the booleans (`viewerCanUpdate`, `viewerCanCloseOrReopen`, `viewerCanDelete`) so a UI can hide affordances ahead of time.

For repos that have never been linked to a GitHub upstream, the viewer is implicitly ADMIN — you own the bare repo on your disk. For [linked repos](#mirroring-a-github-repo), the role is the one cached in the sync config (which mirrors GitHub's repo permission for that user).

### Browsing the data

The dashboard lives at the same port as the API (`127.0.0.1:18080`), routed by Host header — browsers hit the dashboard, `cab`/`gh` hits the API:

```sh
open http://localhost:18080/
```

The dashboard reads the same bare repos as the API and lets you browse issues, refs, commits, blames, and tree views. Code refs (`refs/heads/*`) and metadata refs (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`) are presented separately.

### Mirroring a GitHub repo

gitcabin can pull issues, PRs, and comments from a real GitHub repository, and push back local-only issues you drafted in gitcabin. The sync subsystem is opt-in per repo.

**Identity check first.** gitcabin's `viewer_login` (defaults to `david`) must match the GitHub login `gh` is authenticated as on `github.com`. Mismatch surfaces a hint:

```sh
$ gitcabin sync identity
gitcabin viewer_login: david
github.com gh login:   davidpoblador

these differ. for sync, set GITCABIN_VIEWER_LOGIN to the gh value,
or pass --login on `gitcabin sync link` to override per repo.
```

Set `GITCABIN_VIEWER_LOGIN=davidpoblador` in your environment (or `compose.override.yml`) so identity matches the gh-side login.

**Link a local repo to its GitHub counterpart.** The role (READ / TRIAGE / WRITE / MAINTAIN / ADMIN) is fetched from GitHub automatically; pass `--role` to override.

```sh
gitcabin sync link me/cabin --gh alice/cabin
# linked me/cabin -> alice/cabin (role=ADMIN, login=davidpoblador)
```

Linking writes a sync config to `refs/meta/sync` inside the local bare repo.

**Pull from GitHub.** Pulls issues into `refs/issues/<gh-number>`, PRs into `refs/prs/<gh-number>`, and comments under each ref's `comments/` subtree. Re-pulls overwrite — GitHub wins when there's a conflict.

```sh
gitcabin sync pull me/cabin
# pulled 12 issues, 3 PRs, 47 comments
```

**Push local-only issues to GitHub.** Walks `refs/issues/local/*`, posts each to GitHub, gets back the upstream number, and renumbers the ref to match. The local ref is dropped only after the new synced ref is fully populated. Each upstream side effect (issue POST, then each comment POST) is durably recorded in `refs/meta/sync-pending` before the next runs, so a crash mid-push can resume without re-publishing items GitHub already accepted.

```sh
gitcabin sync push me/cabin
# pushed 1 issues
```

After push, the issue's `provenance` becomes `SYNCED_BIDIR` and its author is rewritten to the gh-side login (whoever gh authenticated as on github.com). The original local number is gone — `gh issue view 41` works, `gh issue view <old-local>` doesn't.

**Push, then pull, in one command.** Re-pull is GitHub-wins, so running pull on its own can clobber local-only items that were never pushed. `gitcabin sync sync` runs the push first so local-only drafts land upstream before pull rewrites the synced refs. `--push-only` and `--pull-only` are escape hatches for the one-direction case.

```sh
gitcabin sync sync me/cabin
# pushed 0 issues
# pulled 12 issues, 3 PRs, 47 comments
```

**Run sync inside the docker container.** `gh` is installed in the runtime image and the host's `~/.config/gh` is bind-mounted read-only, so `docker compose exec` reuses your host login without re-authenticating. On macOS, where `gh auth login` stores the token in Keychain rather than `hosts.yml`, pass it through with `-e GH_TOKEN`:

```sh
docker compose exec -e GH_TOKEN=$(gh auth token --hostname github.com) \
  gitcabin gitcabin sync sync me/cabin
```

**Sync mode trade-offs you should know:**

- *Re-pull is GitHub-wins.* `gitcabin sync sync` mitigates the common case (local-only items pushed before pull) but doesn't yet detect *edits* to already-synced items. Closing a synced issue locally still gets clobbered on the next pull.
- *PR push for cross-fork branches.* `gitcabin sync push` creates same-repo PRs end-to-end (pushing the head branch through `gh auth git-credential` first), but cross-fork PRs (`head_ref="other:branch"`) still need the manual `git push` workflow because gitcabin has no remote for someone else's fork.
- *PR push isn't crash-safe yet.* The issue path (`_push_one`) records pending state to `refs/meta/sync-pending` and resumes cleanly; the PR path (`_push_one_pr`) doesn't yet — same shape, not wired in.

The full design and outstanding gaps live in [`docs/github-sync.md`](docs/github-sync.md).

## Other deployment modes

Today there's exactly one shipping mode: **Local-only HTTP via `cab`** (the quickstart above). The current implementation is solid enough that we're prioritizing iteration speed over deployment-mode breadth — multi-device access via Tailscale is documented as a deferred design in [`docs/tls.md`](docs/tls.md) but not yet built.

The design discussion behind this single-mode decision — including options ruled out (per-machine local CA, public/team-with-own-domain, DuckDNS, shared-wildcard-cert) and the deferred Tailnet-shared mode — lives in [`docs/tls.md`](docs/tls.md).

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

## License

[MIT](LICENSE)

## Support the project

gitcabin is an open source project built by [David Poblador i Garcia](https://davidpoblador.com/) through [All Tuner Labs](https://www.alltuner.com/).

If this project was useful to you, [consider supporting its development](https://alltuner.com/sponsor).

---

<p align="center">
  Built by <a href="https://davidpoblador.com">David Poblador i Garcia</a> with the support of <a href="https://alltuner.com">All Tuner Labs</a>.<br>
  Made with ❤️ in Poblenou, Barcelona.
</p>
