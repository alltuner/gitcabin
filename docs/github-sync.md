# GitHub sync (design notes)

> **Status:** partially implemented as of commit `9e6383d`. The identity / provenance / can_edit model below is built and tested; pull-side sync of issues, PRs, and comments works end-to-end against real GitHub (smoke-tested against `alltuner/gitcabin-sync-smoke`); push-side covers issues + comments but not PRs. Outstanding gaps are tracked as GitHub issues — links inline below.

The goal of GitHub sync is bidirectional mirroring between gitcabin's metadata refs (`refs/issues/*`, `refs/prs/*`, `refs/meta/*`) and a real GitHub repository. A user can browse and act on issues / PRs / comments in gitcabin's local UI, and changes propagate to/from GitHub.com.

This doc focuses specifically on **authorship attribution and edit affordances**: which items in the local UI should expose edit / delete actions, and which should be read-only because acting on them would lie about who wrote them on GitHub.

## What's built

| Capability | Module | State |
|---|---|---|
| Provenance + gh ids on issues / comments | `gitcabin.storage.issues` | done |
| Per-repo sync config at `refs/meta/sync` | `gitcabin.sync.config` | done |
| `gh api` wrapper with runner injection | `gitcabin.sync.gh` | done |
| Pull issues + comments | `gitcabin.sync.pull` | done |
| Pull PRs | `gitcabin.sync.pull` | done |
| Push local-only issues + comments | `gitcabin.sync.push` | done |
| Push local-only PRs | `gitcabin.sync.push` | done (auto-pushes the head branch first when it lives in the bare repo — see below) |
| `can_edit` / `can_delete` rules | `gitcabin.permissions` | done |
| Mutation enforcement (closeIssue) | `gitcabin.graphql_schema` | done |
| Mutation enforcement (updateIssue, updateComment, deleteComment) | — | not built ([#15](https://github.com/alltuner/gitcabin/issues/15)) |
| GraphQL surfaces synced issues + viewer_can_* | `gitcabin.graphql_schema` | done |
| GraphQL surfaces synced PRs + viewer_can_* | `gitcabin.graphql_schema` | done |
| CLI `gitcabin sync identity / link / pull / push` | `gitcabin.cli` | done |
| Resumable push (crash safety) | — | not built ([#12](https://github.com/alltuner/gitcabin/issues/12)) |
| Push-then-pull orchestration | `gitcabin.cli` (`gitcabin sync sync`) | done |
| `viewer_repo_role` auto-fetch | partially built | broken ([#16](https://github.com/alltuner/gitcabin/issues/16)) |
| Web dashboard reads viewer_can_* | `gitcabin.web.routes` | done (close/reopen gated; synced badge rendered) |
| `gh_author_id` for rename stability | `gitcabin.storage.issues`, `gitcabin.sync.pull` | done |

End-to-end smoke test verified at commit `9e6383d` against `alltuner/gitcabin-sync-smoke`: pull recovered both issues + the comment + the closed-state of issue 2; push of a local draft created issue 3 upstream (with its comment), renumbered locally from `refs/issues/local/1` to `refs/issues/3`, and stamped `provenance: SYNCED_BIDIR` + the upstream `gh_issue_id`.

### PR push: branch upload

`push_local_prs` POSTs to `/repos/<o>/<r>/pulls` with the local PR's `head` and `base` branch labels. GitHub responds 422 ("head ref does not exist") if the named branch isn't on the upstream repo, so the push step has to upload the branch first.

For each local PR, before the POST runs:

1. Extract the bare branch name from `head_ref` (`branch` or `<viewer>:branch`). A `<other>:branch` cross-fork label is skipped — gitcabin doesn't have a remote for someone else's fork.
2. If `refs/heads/<branch>` exists in the bare repo, push it to `https://<host>/<owner>/<name>.git` using gh's credential helper (`gh auth git-credential`) so the token never lands in argv. If the branch isn't local, skip — the user is on the manual-push path and the upstream branch is already there.

The injectable `push_branch` parameter on `push_local_prs` is the test seam: tests pass a recording fake instead of shelling out.

```sh
# bare repo already mirrors the GitHub remote
# (cab repo init <owner>/<name> handles this)
# create a draft PR locally via createPullRequest mutation
gitcabin sync push me/cabin                  # uploads branch + POSTs the PR
```

Cross-fork PRs (`head_ref="other:branch"`) still need the legacy manual workflow — push the branch yourself first, then run `gitcabin sync push`.

---

## The core problem

When a gitcabin install syncs from a GitHub repo, the local store ends up with a mix of items:

1. **Items I authored.** I created issue #42 either locally or via the GitHub web UI; my login is on it.
2. **Items others authored.** Someone else opened issue #41 or commented on issue #42; their login is on it. gitcabin pulled it down on sync.
3. **Items created locally that haven't synced yet.** I just typed a comment in gitcabin's dashboard. It exists in `refs/issues/<n>` but doesn't have a GitHub-side counterpart yet.
4. **Items the user has admin rights over but didn't author.** I own the repo or have triage access; on GitHub I could delete or hide someone else's comment. Whether gitcabin should expose that affordance is a design choice.

The local UI today doesn't differentiate. Every issue, every comment renders the same. We need a model that lets the UI decide, per item, what actions are valid.

The key constraint: **gitcabin must never silently impersonate another user's identity on GitHub.** If I'm logged in to gh as `alice` and I edit Bob's comment locally, the gh-mediated push to GitHub would either fail (GitHub rejects edits to other people's comments) or, worse, succeed under `alice`'s identity and overwrite Bob's words. Both outcomes are unacceptable. The UI must prevent the action upstream of the sync layer.

---

## Identity: who is "me"?

Three pieces of identity sit in this project:

- **The gh-side login** — whatever GitHub account the user authenticated as via `gh auth login`. Discoverable by calling `gh api user` (or `viewer { login }` over GraphQL) at sync time and caching it.
- **The gitcabin-side viewer login** — currently `Settings.viewer_login`, defaulting to `david`. Used by gitcabin's own GraphQL `viewer` resolver, which is what gh queries to identify the active user against gitcabin.
- **The author field on stored items** — `author: str` on `IssueDocument`, `CommentDocument`, etc. (see `src/gitcabin/storage/issues.py`). Today this is whatever the API caller passed, with no validation against any external truth.

For sync to work coherently, these three need to relate to one another in a defined way:

- The gh-side login is the **authoritative identity** for anything that lives on GitHub.
- The gitcabin-side viewer login should match the gh-side login when sync is configured. (For local-only deploys with no GitHub sync, the gitcabin-side login can be anything the user picks.)
- The author field on stored items should, after sync, equal the gh-side login of whoever created the item on GitHub.

When the configured gitcabin viewer login *doesn't* match the gh-side login (e.g. user types `david` into Settings but gh is authenticated as `dpoblador`), we surface a setup warning and refuse to sync until they're reconciled. Allowing them to diverge silently is how items end up authored under the wrong identity.

---

## Provenance: where did this item come from?

Every stored item gets a small provenance record alongside its author. Three states:

| Provenance | Meaning |
|---|---|
| `local-only`         | Created in gitcabin, never synced. No upstream counterpart yet. |
| `synced-from-github` | Pulled from GitHub during sync. Upstream is canonical. |
| `synced-bidir`       | Created locally, then successfully pushed to GitHub. Upstream now also has it. |

Storage shape: a small `provenance` field on `IssueDocument` / `CommentDocument` plus the GitHub-side numeric ID where applicable (`gh_issue_id`, `gh_comment_id`). For a `local-only` issue, the gh ID is null until first push.

Provenance plus author plus the viewer's gh-side login is enough to compute the editability of any item.

---

## Edit affordance rules

The UI consults a single helper — call it `can_edit(item, viewer)` — for every action that mutates content (edit body, edit title, delete comment, close issue, reopen, etc.). The rules:

### Issues

| Author | Provenance | Repo role of viewer | Can edit body / title? | Can close / reopen? | Can lock / hide? |
|---|---|---|---|---|---|
| viewer | `local-only`         | any                                          | yes                    | yes                 | n/a (no upstream) |
| viewer | `synced-bidir`       | any                                          | yes                    | yes                 | yes (via gh) |
| viewer | `synced-from-github` | any                                          | yes                    | yes                 | yes (via gh) |
| other  | `synced-from-github` | viewer is owner / has triage                 | **no** (would impersonate) | yes (admin action, allowed) | yes (moderation, allowed) |
| other  | `synced-from-github` | viewer is none of the above                  | no                     | no                  | no |

The principle: **content edits require authorship**. Admin actions (closing, locking, hiding) require *either* authorship *or* a privileged role. gitcabin's UI checks both before showing the action.

### Comments

Same matrix, simpler — there's no "close/reopen" on comments; just edit and delete:

| Author | Provenance | Repo role | Can edit? | Can delete? |
|---|---|---|---|---|
| viewer | any                   | any                       | yes       | yes |
| other  | any                   | viewer is owner / admin   | **no**    | yes (moderation) |
| other  | any                   | viewer is none            | no        | no |

Note the asymmetry: a repo owner can *delete* someone else's comment (a moderation action GitHub supports — "remove off-topic comment"), but cannot *edit* it. Editing changes attributed words; deletion just removes them. That distinction needs to be represented in the rule, not papered over.

### PRs

Out of scope for this first cut. Same model but with extra cases (merge button, request review, dismiss review). Pin once issues are working.

---

## How the viewer's repo role is known

GitHub exposes a viewer's permission on a repo via the GraphQL `Repository.viewerPermission` field (`READ`, `TRIAGE`, `WRITE`, `MAINTAIN`, `ADMIN`). gitcabin caches this per-repo at sync time alongside the rest of the repo metadata (it's a single field added to whatever object stores repo-level state).

The cache is refreshed:

- On each full sync.
- On any 403/404 response from a write attempt (defensive — the user's role may have been revoked).

We don't store role per-comment or per-issue; it's a property of the (viewer, repo) pair, not of the item.

For local-only repos that have never been linked to a GitHub repo, `viewerPermission` is implicitly `ADMIN` — the user owns the local bare repo, period.

---

## Where this gets enforced in the layers

Three layers, three responsibilities:

1. **Storage layer** (`src/gitcabin/storage/issues.py`) — stores authorship and provenance faithfully. Doesn't enforce edit rules; that's a UI/API concern. A storage-layer caller asking to mutate Bob's comment under `author=alice` will succeed at the storage level. Don't try to make storage refuse this — it's the wrong layer (and would break the sync inbound path, where we *do* legitimately write items authored by Bob).

2. **API layer** (REST + GraphQL) — enforces `can_edit(item, viewer)` on every mutation. Returns 403 if the viewer's identity doesn't match the rules. This is the security boundary. Tests live here.

3. **UI layer** (`src/gitcabin/web/`) — calls `can_edit(...)` per item when rendering and conditionally renders the edit/delete affordances. This is for UX, not security — the API will enforce regardless of what the UI shows.

The `can_edit` helper lives once, in a place both layers can import (probably `src/gitcabin/permissions.py` or similar), so the UI and the API agree by construction.

---

## Edge cases worth pinning explicitly

- **Renamed upstream user.** GitHub allows users to rename. If `alice` becomes `alice-renamed`, items authored by her still carry `alice` in gitcabin's store. We should resolve via the stable numeric `user.id` from GitHub's API where possible. Display the current login GitHub returns.
- **Deleted upstream user.** GitHub displays items by deleted users with a "ghost" placeholder. We mirror that behavior: store a tombstone author rather than deleting the item.
- **Items authored under a bot account.** GitHub Apps act under their own identity. Their items are not editable by humans (including the repo owner) via the API. Treat bot-authored items as effectively another user.
- **Items edited on GitHub after sync but before push.** Race condition. The next sync should detect the upstream change (GitHub returns `updated_at`) and either pull the new content or surface a conflict. Strategy TBD — probably "GitHub wins" for the first cut, with a UI badge on items that had local edits overwritten.
- **The viewer login changes mid-session.** Re-authentication with a different GitHub account would invalidate every "viewer == author" check on items already loaded. The UI should treat the viewer login as session-scoped and re-render on auth change.

---

## What this design doesn't do

- **No multi-user collaboration in gitcabin itself.** gitcabin is single-user (per the broader project framing). This authorship model is about *honest representation* of items synced from a multi-user GitHub repo, not about hosting multi-user collaboration locally.
- **No partial-edit support.** If a comment is half-authored by the viewer and half-quoted from someone else, that's outside scope. Authorship is whole-or-nothing per item.
- **No granular moderation UI for repo admins.** Admins get the same actions GitHub gives them (delete items, hide them, lock issues), but we don't expose a separate "moderation queue" view in this first cut.

---

## Open questions

- **Where does the gh-side login get queried from?** We could call `gh api user` via subprocess at startup, or query our own GraphQL `viewer` over the gh-mediated path. The latter is circular if gh's auth points at gitcabin (which it does in the local-only deploy mode). For the sync-with-real-GitHub case, the gh-side login is whoever is authenticated against `github.com` in the same gh installation — we'd need a separate gh invocation with `GH_HOST=github.com`.
- **How do we handle a user who has multiple GitHub accounts in their gh config?** `gh auth status` lists them. Pick the one matching the sync target's host, or surface a chooser.
- **Do we ever allow the viewer to *override* their displayed identity** ("post as bot," etc.)? Cleanest answer is no, at least for v1.
- **Storage migration.** Existing `IssueDocument` / `CommentDocument` instances don't have `provenance` or `gh_issue_id` fields. Add them as optional with sensible defaults (`provenance: "local-only"`, gh IDs null) and let the sync layer backfill on first sync.
