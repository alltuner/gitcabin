<p align="center">
  <img src="https://brand.alltuner.com/logos/gitcabin/horizontal.png" alt="gitcabin" width="500">
</p>

<p align="center">
  <strong>A tiny self-hosted GitHub clone driven by the official <code>gh</code> CLI.</strong><br>
  All metadata stored in git itself — no separate database.
</p>

<p align="center">
  <a href="https://alltuner.com/sponsor">Sponsor</a>
</p>

<p align="center">
  <img src="https://img.shields.io/github/license/alltuner/gitcabin?color=5B2333" alt="License">
  <img src="https://img.shields.io/github/stars/alltuner/gitcabin?color=5B2333" alt="Stars">
</p>

> [!WARNING]
> **Pre-alpha sketch — not functional software yet.**
> This repo is a working sketch of what gitcabin *could* be, not a tool you can rely on. Most of what's here is design notes and structural code; deployment recipes work in narrow conditions but will break in real use. Read for the ideas; don't deploy for production. Feedback on the direction is very welcome — see [`docs/`](docs/) for what's been thought through and what's still open.

---

## Get Started

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

---

## What is gitcabin?

A tiny self-hosted GitHub clone driven by the official `gh` CLI, with all metadata stored in git itself.

### How it works

- `gh` has built-in support for arbitrary hosts via `GH_HOST`. The hostname `github.localhost` is special: `gh` sends to `http://api.github.localhost/` (REST) and `http://api.github.localhost/graphql` (GraphQL), so HTTPS is not required for local dev. For any other hostname `gh` forces HTTPS and uses the GitHub Enterprise URL shape (`https://<host>/api/v3/...` and `https://<host>/api/graphql`). gitcabin serves both shapes, so the same image works behind either path.
- Issues, PRs, and counters live in side refs of the bare git repo (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`). Code lives in normal `refs/heads/*` and `refs/tags/*`. The two namespaces never collide.
- The HTTP API server is the only writer of metadata refs. Plain `git clone`/`git push` only see code.

## Using gitcabin

Once it's running and `gh` is authenticated, everything goes through the regular `gh` commands. Set `GH_HOST=github.localhost` for the session (or pass `-h github.localhost` per call) and the rest looks like talking to GitHub.

### Working with issues

Repos are created on first use — there's no separate `gh repo create` step yet. Just create an issue and gitcabin will initialize the bare repo on disk.

```sh
export GH_HOST=github.localhost

# Create an issue. The repo is initialized lazily.
gh issue create -R me/cabin --title "First issue" --body "Try things out"

# List issues. State filters work; ordering options are accepted but ignored.
gh issue list -R me/cabin
gh issue list -R me/cabin --state closed

# View one, optionally with its comments.
gh issue view 1 -R me/cabin
gh issue view 1 -R me/cabin --comments

# Edit your own issue. Title or body, separately or together.
gh issue edit 1 -R me/cabin --title "Renamed"
gh issue edit 1 -R me/cabin --body "Updated body"

# Close. Reopening locally isn't wired up yet; manage state with edit.
gh issue close 1 -R me/cabin
```

### Comments

```sh
gh issue comment 1 -R me/cabin --body "A reply"

# Edit your own comment.
gh api graphql -f query='
  mutation U($id: ID!, $body: String!) {
    updateIssueComment(input: {id: $id, body: $body}) {
      issueComment { body }
    }
  }
' -F id=<comment-id> -F body="Edited reply"

# Delete your own comment (or any comment if you have ADMIN on a synced repo).
gh api graphql -f query='
  mutation D($id: ID!) {
    deleteIssueComment(input: {id: $id}) { clientMutationId }
  }
' -F id=<comment-id>
```

`gh issue comment --edit-last` and `gh issue comment --delete` are the friendlier wrappers — both work as soon as gh's version supports `updateIssueComment` / `deleteIssueComment` (gh 2.92+).

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

The compose stack runs an HTML dashboard alongside the API on `127.0.0.1:8080`:

```sh
open http://localhost:8080/
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

**Push local-only issues to GitHub.** Walks `refs/issues/local/*`, posts each to GitHub, gets back the upstream number, and renumbers the ref to match. The local ref is dropped only after the new synced ref is fully populated.

```sh
gitcabin sync push me/cabin
# pushed 1 issues
```

After push, the issue's `provenance` becomes `SYNCED_BIDIR` and its author is rewritten to the gh-side login (whoever gh authenticated as on github.com). The original local number is gone — `gh issue view 41` works, `gh issue view <old-local>` doesn't.

**Sync mode trade-offs you should know:**

- *Re-pull clobbers local edits on synced items.* If you close a synced issue locally and then run `pull` again, GitHub's open state will overwrite yours. A push-then-pull orchestration is tracked at [#13](https://github.com/alltuner/gitcabin/issues/13).
- *Push isn't crash-safe yet.* If `push` dies between the upstream POST and the local renumbering, retrying creates a duplicate issue on GitHub. Tracked at [#12](https://github.com/alltuner/gitcabin/issues/12).
- *PR push isn't built.* You can pull PRs to read them, but creating a PR from gitcabin needs local-branch infrastructure that doesn't exist yet ([#14](https://github.com/alltuner/gitcabin/issues/14)).
- *`gh_author_id` isn't tracked.* If a GitHub user renames, items they authored on synced issues will keep their old login locally until the next pull updates the display ([#18](https://github.com/alltuner/gitcabin/issues/18)).

The full design and outstanding gaps live in [`docs/github-sync.md`](docs/github-sync.md).

## Other deployment modes

The local-only quickstart is one of two documented setups:

| Mode | Audience | Cert work | Domain needed |
|---|---|---|---|
| Local-only (default) | the user, on this machine | none | `github.localhost` (built in) |
| Tailnet-shared | anyone on your tailnet | none (Tailscale provisions) | tailnet hostname (built in) |

See [`docs/installation.md`](docs/installation.md) for the Tailscale recipe, the exact `gh auth login` invocation it pairs with, and trade-offs. The design discussion behind these modes — including options ruled out (per-machine local CA, public/team-with-own-domain, DuckDNS) and a shared-wildcard-cert path still under design — lives in [`docs/tls.md`](docs/tls.md).

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
