# ABOUTME: CLI subcommands for gitcabin sync — identity, link, pull, push.
# ABOUTME: Single entry point parsed by `gitcabin sync ...` from __main__.

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from gitcabin.config import Settings
from gitcabin.storage.repo import BareRepo
from gitcabin.sync.config import SyncConfig, read_config, write_config
from gitcabin.sync.gh import GhClient, gh_login
from gitcabin.sync.pull import pull_comments, pull_issues, pull_prs
from gitcabin.sync.push import push_local_issues


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gitcabin sync")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("identity", help="show the gitcabin viewer and gh-side login")

    p_link = sub.add_parser("link", help="link a local repo to a GitHub repo")
    p_link.add_argument("local", help="local repo as <owner>/<name>")
    p_link.add_argument("--gh", required=True, help="GitHub repo as <owner>/<name>")
    p_link.add_argument(
        "--login",
        help="gh-side login expected for this sync target (defaults to current gh user)",
    )
    p_link.add_argument(
        "--role",
        choices=["READ", "TRIAGE", "WRITE", "MAINTAIN", "ADMIN"],
        help="cached repo role; if omitted, fetched from GitHub",
    )

    p_pull = sub.add_parser("pull", help="pull issues, PRs, and comments from GitHub")
    p_pull.add_argument("local", help="local repo as <owner>/<name>")

    p_push = sub.add_parser("push", help="push local-only issues + comments to GitHub")
    p_push.add_argument("local", help="local repo as <owner>/<name>")

    p_sync = sub.add_parser(
        "sync",
        help="push local changes then pull from GitHub (push-only / pull-only with flags)",
    )
    p_sync.add_argument("local", help="local repo as <owner>/<name>")
    direction = p_sync.add_mutually_exclusive_group()
    direction.add_argument(
        "--push-only",
        action="store_true",
        help="skip the pull step (alias for `gitcabin sync push <local>`)",
    )
    direction.add_argument(
        "--pull-only",
        action="store_true",
        help="skip the push step (alias for `gitcabin sync pull <local>`)",
    )

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    settings = Settings.from_env()

    if args.cmd == "identity":
        return _cmd_identity(settings)
    if args.cmd == "link":
        return _cmd_link(settings, args.local, args.gh, args.login, args.role)
    if args.cmd == "pull":
        return _cmd_pull(settings, args.local)
    if args.cmd == "push":
        return _cmd_push(settings, args.local)
    if args.cmd == "sync":
        return _cmd_sync(
            settings, args.local, push_only=args.push_only, pull_only=args.pull_only
        )
    return 2


def _cmd_identity(settings: Settings) -> int:
    """Print the gitcabin viewer login alongside the gh-side login on github.com.

    Mismatches are surfaced as a hint; we don't fail because the user might
    legitimately want a different gitcabin-side identity for local-only repos.
    """
    print(f"gitcabin viewer_login: {settings.viewer_login}")
    try:
        gh = gh_login(GhClient())
    except Exception as e:
        print(f"gh login lookup failed: {e}", file=sys.stderr)
        return 1
    print(f"github.com gh login:   {gh}")
    if gh != settings.viewer_login:
        print(
            "\nthese differ. for sync, set GITCABIN_VIEWER_LOGIN to the gh value,\n"
            "or pass --login on `gitcabin sync link` to override per repo.",
            file=sys.stderr,
        )
    return 0


def _cmd_link(
    settings: Settings,
    local: str,
    gh: str,
    login: str | None,
    role: str | None,
) -> int:
    """Write a SyncConfig that pairs the local repo with a GitHub repo.

    The login defaults to the current gh user on github.com — that's the
    identity gh will use when push runs against this config. Role is
    optional; if omitted we fetch it from /repos/<o>/<r>'s
    viewer_permission via the gh client.
    """
    repo = _open_local(settings, local)
    if repo is None:
        print(f"unknown local repo: {local}", file=sys.stderr)
        return 1
    gh_owner, gh_name = _split_slash(gh, "--gh")
    if gh_owner is None or gh_name is None:
        return 2

    client = GhClient()
    if login is None:
        try:
            login = gh_login(client)
        except Exception as e:
            print(f"gh login lookup failed: {e}", file=sys.stderr)
            return 1

    if role is None:
        try:
            payload = client.get_json(f"repos/{gh_owner}/{gh_name}")
            role = _role_from_repo_payload(payload) or "READ"
        except Exception:
            # Fetching the role is a nice-to-have; treat failure as READ so
            # the user retains the option to escalate manually via --role on
            # a follow-up `gitcabin sync link`.
            role = "READ"

    config = SyncConfig(
        gh_owner=gh_owner,
        gh_name=gh_name,
        gh_viewer_login=login,
        viewer_repo_role=role,
    )
    write_config(repo, config)
    print(f"linked {local} -> {gh_owner}/{gh_name} (role={role}, login={login})")
    return 0


def _cmd_pull(settings: Settings, local: str) -> int:
    """Pull issues, then PRs, then comments — in that order.

    Comments dispatch by ref existence (issue vs PR), so issues and PRs need
    to land first. Updates last_synced_at on success.
    """
    repo = _open_local(settings, local)
    if repo is None:
        print(f"unknown local repo: {local}", file=sys.stderr)
        return 1
    config = read_config(repo)
    if config is None:
        print(f"{local} is not linked. run `gitcabin sync link` first.", file=sys.stderr)
        return 1

    client = GhClient()
    issues = pull_issues(repo, client, config)
    prs = pull_prs(repo, client, config)
    comments = pull_comments(repo, client, config)

    _stamp_synced(repo, config)

    print(f"pulled {len(issues)} issues, {len(prs)} PRs, {len(comments)} comments")
    return 0


def _cmd_push(settings: Settings, local: str, *, client: GhClient | None = None) -> int:
    """Push every refs/issues/local/<n> issue (and its comments) to GitHub.

    `client` is the injection seam for tests — the CLI default constructs a
    real GhClient that shells out to `gh api`.
    """
    repo = _open_local(settings, local)
    if repo is None:
        print(f"unknown local repo: {local}", file=sys.stderr)
        return 1
    config = read_config(repo)
    if config is None:
        print(f"{local} is not linked. run `gitcabin sync link` first.", file=sys.stderr)
        return 1

    if client is None:
        client = GhClient()

    pushed = push_local_issues(repo, client, config)
    # Re-read because push may have updated the config (e.g. renumbering local
    # refs onto upstream slots, last-pushed bookkeeping).
    config = read_config(repo) or config
    _stamp_synced(repo, config)
    print(f"pushed {len(pushed)} issues")
    return 0


def _cmd_sync(
    settings: Settings,
    local: str,
    *,
    push_only: bool,
    pull_only: bool,
    client: GhClient | None = None,
) -> int:
    """Push then pull. Default order keeps local edits from being clobbered.

    Pull is GitHub-wins; any locally created issue / comment that hadn't been
    pushed yet gets passed over silently if pull happens first. Pushing first
    means those local-only items are upstream by the time pull overwrites
    things, so the only blobs pull rewrites are ones whose authoritative copy
    really does live on GitHub. `--push-only` and `--pull-only` are escape
    hatches when the user wants one half of the round-trip.

    `client` is the injection seam for tests — the CLI default constructs a
    real GhClient that shells out to `gh api`.
    """
    repo = _open_local(settings, local)
    if repo is None:
        print(f"unknown local repo: {local}", file=sys.stderr)
        return 1
    config = read_config(repo)
    if config is None:
        print(f"{local} is not linked. run `gitcabin sync link` first.", file=sys.stderr)
        return 1

    if client is None:
        client = GhClient()

    if not pull_only:
        pushed = push_local_issues(repo, client, config)
        # Re-read the config because push may have updated it (e.g. renumbering
        # local refs onto upstream slots, last-pushed bookkeeping).
        config = read_config(repo) or config
        _stamp_synced(repo, config)
        print(f"pushed {len(pushed)} issues")

    if push_only:
        return 0

    issues = pull_issues(repo, client, config)
    prs = pull_prs(repo, client, config)
    comments = pull_comments(repo, client, config)
    _stamp_synced(repo, config)
    print(f"pulled {len(issues)} issues, {len(prs)} PRs, {len(comments)} comments")
    return 0


# ---- helpers ----------------------------------------------------------- #


def _stamp_synced(repo: BareRepo, config: SyncConfig) -> None:
    """Bump SyncConfig.last_synced_at to now and persist."""
    write_config(
        repo,
        config.model_copy(
            update={"last_synced_at": datetime.now(tz=UTC).isoformat()}
        ),
    )


def _split_slash(s: str, flag: str) -> tuple[str | None, str | None]:
    parts = s.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        print(f"{flag} expects <owner>/<name>; got {s!r}", file=sys.stderr)
        return (None, None)
    return parts[0], parts[1]


def _role_from_repo_payload(payload: object) -> str | None:
    """Map a `/repos/<o>/<r>` REST response to a RepoRole string.

    The REST endpoint returns a `permissions` object with boolean flags rather
    than the GraphQL viewerPermission enum, so we walk the booleans from
    most-privileged to least to find the highest role the viewer holds.
    """
    if not isinstance(payload, dict):
        return None
    perms = payload.get("permissions")
    if not isinstance(perms, dict):
        return None
    for flag, role in (
        ("admin", "ADMIN"),
        ("maintain", "MAINTAIN"),
        ("push", "WRITE"),
        ("triage", "TRIAGE"),
        ("pull", "READ"),
    ):
        if perms.get(flag):
            return role
    return None


def _open_local(settings: Settings, local: str) -> BareRepo | None:
    owner, name = _split_slash(local, "<local>")
    if owner is None or name is None:
        return None
    path = (Path(settings.data_dir) / "projects" / owner / name).with_suffix(".git")
    if not path.is_dir():
        return None
    return BareRepo.open_or_init(path)


if __name__ == "__main__":
    sys.exit(main())
