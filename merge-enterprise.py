#!/usr/bin/env python3
"""merge-enterprise.py -- Daily Merge automation for Firefox Enterprise.

Drives one merge from upstream/<branch> into enterprise-<branch>, syncs
l10n revisions, pushes to your fork, and opens a PR via gh.

Mirrors the per-branch steps in the Firefox Enterprise "Daily Merges
(x3)" checklist. Halts cleanly on merge conflict; resume with --resume
after committing the resolution.

In --dry-run mode every mutating action (git fetch/checkout/pull/merge/
add/commit/push, file writes, state-file writes, gh pr create, even the
browser open and the user prompt) is replaced with a "DRY: ..." line.
Read-only git queries still run so the dry-run reflects real repo state.

Reviewers are not assigned -- add them yourself in the GitHub UI.

Requires: git, gh (GitHub CLI), Python 3.8+.

Usage:
    merge-enterprise.py --branch main
    merge-enterprise.py --branch main --dry-run
    merge-enterprise.py --branch main --resume     # after resolving conflicts
    merge-enterprise.py --branch main --skip-pr    # stop before gh pr create
    merge-enterprise.py --branch release --tag FIREFOX_150_0_2_BUILD2
"""

import argparse
import sys

from enterprise_merge_lib import Merger, MergeError, PENDING_ITEMS_URL


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="merge-enterprise.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--branch", required=True, choices=["main", "beta", "release"])
    p.add_argument("--resume", action="store_true",
                   help="continue after manually resolving and committing a merge conflict")
    p.add_argument("--dry-run", action="store_true",
                   help="print mutating commands without running them")
    p.add_argument("--skip-pr", action="store_true",
                   help="do everything except 'gh pr create'; print the gh command")
    p.add_argument("--tag", default="",
                   help="merge a specific tag instead of upstream/<branch>; "
                        "tag must be an ancestor of upstream/<branch>")
    p.add_argument("--enterprise-remote", default="enterprise")
    p.add_argument("--upstream-remote", default="upstream")
    p.add_argument("--origin-remote", default="origin")
    p.add_argument("--enterprise-repo", default="mozilla/enterprise-firefox")
    p.add_argument("--pending-items-url", default=PENDING_ITEMS_URL)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        Merger(
            branch=args.branch,
            tag=args.tag,
            resume=args.resume,
            dry_run=args.dry_run,
            skip_pr=args.skip_pr,
            enterprise_remote=args.enterprise_remote,
            upstream_remote=args.upstream_remote,
            origin_remote=args.origin_remote,
            enterprise_repo=args.enterprise_repo,
            pending_items_url=args.pending_items_url,
        ).run()
    except MergeError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
