#!/usr/bin/env python3
"""Fold every outstanding change in a repository into one commit and push it.

A single-file, stdlib-only companion to ``update_manifests.py``: instead of
leaving you with a dirty tree after a batch update, it stages everything,
writes one commit whose subject names the manifests that changed, folds any
commit that has not been pushed yet into that same commit, merges whatever
landed on the remote in the meantime, and pushes.

The repository is the path argument (default: the current directory); any
subdirectory of a work tree is resolved to its root. The script is meant to
live outside the repository it drives, so it never becomes part of the commit
it creates.

Usage
-----
    python commit_push.py D:\\path\\to\\repo            # commit, squash, merge, push
    python commit_push.py                              # same, for the current directory
    python commit_push.py -m "bucket: ..."             # explicit commit subject
    python commit_push.py -n                           # dry run, print the plan only
    python commit_push.py --no-squash                  # keep unpublished commits apart
    python commit_push.py --rebase                     # rebase instead of merge
    python commit_push.py --no-push                    # commit locally only
    python commit_push.py --no-fetch                   # do not talk to the remote first

What it does
------------
1. ``git add -A`` -- modified, deleted and untracked files alike, still
   honouring ``.gitignore``.
2. Commits them. Without ``-m`` the subject is derived from the change set:
   ``bucket: update Foo, Bar`` when manifests changed, ``repo: update ...``
   otherwise; files outside ``bucket/`` are mentioned in the body.
3. Fetches, then folds every commit that has not been pushed yet into that one
   commit, so the branch goes out as a single commit (``--no-squash`` keeps
   them). Only unpublished history is rewritten -- the squash resets ``--soft``
   to ``<remote>/<branch>`` and never force-pushes.
4. Merges ``<remote>/<branch>`` when the remote moved on (``--rebase`` keeps
   the history linear instead), then pushes.

The script refuses to run in the middle of a merge, rebase, cherry-pick,
revert or bisect, so a half-finished operation is never folded into a commit.

Exit codes: 0 = nothing to do, or committed and pushed; 1 = failure.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# The work tree being driven; main() replaces this with the resolved --repo path.
REPO = Path.cwd()
MANIFEST_DIR = "bucket"
# How many names a generated subject / body lists before collapsing the rest.
SUBJECT_NAMES = 5
BODY_NAMES = 8


# --------------------------------------------------------------------------- #
# console helpers
# --------------------------------------------------------------------------- #
def _init_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def info(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(_c(msg, "32"), flush=True)


def warn(msg: str) -> None:
    print(_c(f"warning: {msg}", "33"), file=sys.stderr, flush=True)


def error(msg: str) -> None:
    print(_c(f"error: {msg}", "31"), file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# git layer
# --------------------------------------------------------------------------- #
class GitError(Exception):
    """Raised when a git command the script depends on fails."""


def resolve_repo(path: str) -> Path:
    """Resolve a directory (or any path inside one) to its work-tree root."""
    candidate = Path(path).expanduser()
    if not candidate.is_dir():
        raise GitError(f"{candidate} is not a directory")
    result = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    top = result.stdout.strip()
    if result.returncode != 0 or not top:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(f"{candidate} is not inside a git repository: {detail}")
    return Path(top).resolve()


def _run(args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def git(*args: str, check: bool = True, raw: bool = False) -> str:
    """Run git in the repository root and return stdout.

    ``raw`` keeps the output byte-for-byte (used for ``-z`` path lists); the
    default trims the trailing newline every plumbing command adds.
    """
    result = _run(list(args))
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise GitError(f"git {' '.join(args)} failed: {detail}")
    out = result.stdout or ""
    return out if raw else out.strip()


def try_git(*args: str) -> Tuple[int, str]:
    """Run git without raising; return ``(exit code, stdout + stderr)``."""
    result = _run(list(args))
    return result.returncode, (result.stdout + result.stderr).strip()


def current_branch() -> Optional[str]:
    # --quiet makes a detached HEAD exit with status 1 instead of printing "HEAD".
    branch = git("symbolic-ref", "--short", "--quiet", "HEAD", check=False)
    return branch or None


def operation_in_progress() -> Optional[str]:
    """Name the git operation whose state files are present, if any."""
    git_dir = Path(git("rev-parse", "--absolute-git-dir"))
    markers = (
        ("rebase-merge", "rebase"),
        ("rebase-apply", "rebase"),
        ("MERGE_HEAD", "merge"),
        ("CHERRY_PICK_HEAD", "cherry-pick"),
        ("REVERT_HEAD", "revert"),
        ("BISECT_LOG", "bisect"),
    )
    for marker, label in markers:
        if (git_dir / marker).exists():
            return label
    return None


def upstream_of(remote: str, branch: str) -> Optional[str]:
    """Return the tracking ref for ``branch``, falling back to ``<remote>/<branch>``."""
    ref = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
    if ref and "/" in ref:
        return ref
    candidate = f"{remote}/{branch}"
    if git("rev-parse", "--verify", "--quiet", candidate, check=False):
        return candidate
    return None


def diverged(upstream: Optional[str]) -> Tuple[int, int]:
    """Return ``(ahead, behind)`` relative to ``upstream``."""
    if not upstream:
        return 0, 0
    out = git("rev-list", "--left-right", "--count", f"{upstream}...HEAD")
    behind, ahead = out.split()
    return int(ahead), int(behind)


def fetch(remote: str) -> None:
    """Refresh ``remote``; a missing remote or a network error is only a warning."""
    if remote not in git("remote", check=False).split():
        warn(f"remote {remote!r} is not configured -- skipping fetch")
        return
    code, out = try_git("fetch", "--prune", remote)
    if code != 0:
        warn(f"git fetch {remote} failed -- continuing with the local refs\n{out}")


def changed_paths(*revision_args: str) -> List[str]:
    """Paths of a diff, NUL-separated so spaces and non-ASCII names survive."""
    out = git("diff", "--name-only", "-z", *revision_args, raw=True)
    return [path for path in out.split("\0") if path]


def pending_paths() -> List[str]:
    """Every path the working tree would commit, without touching the index."""
    seen = {}
    for args in (
        ("diff", "--name-only", "-z"),                     # unstaged edits
        ("diff", "--cached", "--name-only", "-z"),         # already staged
        ("ls-files", "--others", "--exclude-standard", "-z"),  # untracked
    ):
        out = git(*args, raw=True)
        for path in out.split("\0"):
            if path:
                seen.setdefault(path, None)
    return list(seen)


# --------------------------------------------------------------------------- #
# commit message
# --------------------------------------------------------------------------- #
def manifest_name(path: str) -> Optional[str]:
    """``bucket/Foo.json`` -> ``Foo``; anything else -> ``None``."""
    parts = path.split("/")
    if len(parts) == 2 and parts[0] == MANIFEST_DIR and parts[1].endswith(".json"):
        return parts[1][: -len(".json")]
    return None


def _listing(items: Sequence[str], limit: int) -> str:
    shown = list(items[:limit])
    extra = len(items) - len(shown)
    return ", ".join(shown) + (f" (+{extra} more)" if extra else "")


def build_message(paths: Sequence[str]) -> Tuple[str, Optional[str]]:
    """Derive a subject and optional body from the files that changed."""
    manifests = sorted({name for name in map(manifest_name, paths) if name}, key=str.lower)
    others = sorted(path for path in paths if manifest_name(path) is None)

    if manifests:
        subject = f"{MANIFEST_DIR}: update {_listing(manifests, SUBJECT_NAMES)}"
    elif others:
        subject = f"repo: update {_listing([p.rstrip('/') for p in others], SUBJECT_NAMES)}"
    else:
        subject = "repo: update"

    body = None
    if manifests and others:
        body = f"Also touched: {_listing(others, BODY_NAMES)}"
    return subject, body


def make_commit(subject: str, body: Optional[str]) -> None:
    args = ["commit", "-m", subject]
    if body:
        args += ["-m", body]
    git(*args)


# --------------------------------------------------------------------------- #
# actions
# --------------------------------------------------------------------------- #
def stage_and_commit(message: Optional[str]) -> bool:
    """Stage the whole tree and commit it. Returns False when there was nothing."""
    pending = pending_paths()
    if not pending:
        return False

    git("add", "-A")
    staged = changed_paths("--cached")
    if not staged:
        warn("nothing staged -- .gitignore may cover every change")
        return False

    subject, body = (message, None) if message else build_message(staged)
    make_commit(subject, body)
    ok(f"committed {len(staged)} file(s): {subject}")
    return True


def integrate(upstream: str, rebase: bool) -> None:
    """Bring the branch up to date with the remote after fetching."""
    if rebase:
        git("rebase", upstream)
        ok(f"rebased onto {upstream}")
    else:
        git("merge", "--no-edit", upstream)
        ok(f"merged {upstream}")


def squash(upstream: str, message: Optional[str]) -> bool:
    """Fold unpublished commits into one. Returns True when it rewrote history."""
    count = int(git("rev-list", "--count", f"{upstream}..HEAD"))
    if count <= 1:
        return False

    # The final tree is the whole point; a soft reset keeps index and worktree.
    if message:
        subject, body = message, None
    else:
        subject, body = build_message(changed_paths(upstream, "HEAD"))
    git("reset", "--soft", upstream)
    make_commit(subject, body)
    ok(f"squashed {count} unpublished commit(s): {subject}")
    return True


def push(remote: str, branch: str, upstream: Optional[str]) -> None:
    if upstream:
        code, out = try_git("push")
    else:
        code, out = try_git("push", "--set-upstream", remote, branch)
    if code != 0:
        hint = ""
        if "rejected" in out or "non-fast-forward" in out or "fetch first" in out:
            hint = "\nthe remote moved on -- rerun the script to merge it"
        raise GitError(f"git push failed: {out}{hint}")
    ok(f"pushed {branch} to {upstream or remote}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    global REPO
    _init_stdout()
    parser = argparse.ArgumentParser(
        description="Fold every outstanding change into one commit and push it.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("repo", nargs="?", default=".", help="repository to commit in (default: current directory)")
    parser.add_argument("-m", "--message", help="commit subject (default: derived from the changed files)")
    parser.add_argument("-n", "--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--no-squash", action="store_true", help="keep unpublished commits instead of folding them")
    parser.add_argument("--rebase", action="store_true", help="rebase onto the remote instead of merging it")
    parser.add_argument("--no-push", action="store_true", help="stop after committing")
    parser.add_argument("--no-fetch", action="store_true", help="skip the fetch and use the local remote refs")
    parser.add_argument("--remote", default="origin", help="remote to fetch from and push to (default: origin)")
    args = parser.parse_args(argv)

    try:
        REPO = resolve_repo(args.repo)

        branch = current_branch()
        if branch is None:
            error("HEAD is detached -- check out a branch before committing")
            return 1

        state = operation_in_progress()
        if state:
            error(f"a {state} is in progress -- finish or abort it first")
            return 1

        if not args.no_fetch:
            fetch(args.remote)

        upstream = upstream_of(args.remote, branch)
        ahead, behind = diverged(upstream)

        pending = pending_paths()
        subject, body = (args.message, None) if args.message else build_message(pending)
        unpublished = int(git("rev-list", "--count", f"{upstream}..HEAD")) if upstream else 0
        squashable = unpublished > 1 and not args.no_squash

        if args.dry_run:
            info(f"repo:   {REPO}")
            info(f"{branch} -> {upstream or '(no upstream)'}  ahead {ahead}, behind {behind}")
            if pending:
                info(f"pending: {len(pending)} file(s)")
                for path in pending:
                    info(f"  {path}")
                info(f"subject: {subject}")
                if body:
                    info(f"body:    {body}")
            else:
                info("pending: nothing")
            if squashable:
                info(f"squash: {unpublished} unpublished commit(s) -> 1")
            if behind:
                info(f"{'rebase' if args.rebase else 'merge'}: {upstream}")
            if not args.no_push and (pending or unpublished or behind):
                info(f"push:   {branch} -> {upstream or args.remote}")
            info("dry run: working tree untouched")
            return 0

        committed = stage_and_commit(args.message)

        if upstream and behind:
            integrate(upstream, args.rebase)
            behind = 0

        squashed = False
        if upstream and not args.no_squash:
            squashed = squash(upstream, args.message)

        if not committed and not squashed and not behind and ahead == 0:
            ok("nothing to do -- working tree and branch are up to date")
            return 0

        if args.no_push:
            if committed or squashed:
                info("committed locally -- --no-push given, not pushing")
            else:
                waiting = int(git("rev-list", "--count", f"{upstream}..HEAD")) if upstream else 0
                info(f"{waiting} commit(s) waiting to be pushed -- --no-push given, not pushing")
            return 0

        push(args.remote, branch, upstream)
        return 0

    except GitError as exc:
        error(str(exc))
        return 1
    except OSError as exc:
        error(f"cannot run git: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
