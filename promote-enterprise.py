#!/usr/bin/env python3
"""promote-enterprise.py -- Promote a Firefox Enterprise branch.

Mirrors an upstream Firefox train promotion (main->beta or beta->release)
on the enterprise-firefox branches. Reuses merge-enterprise.py for the
two preparatory tag-merges (step 1a, 1b), then does the actual promotion
via the "ours"-merge dance that mimics Lando's merge_onto, plus the
post-promotion "Update configs" commit and l10n sync.

Halts cleanly on any conflict (in the preparatory merges or in the
promotion's own merge / cherry-pick steps). Resume with --continue
after committing or `--continue`-ing the relevant git operation.

In --dry-run mode every mutating action is replaced with a "DRY: ..."
line. Read-only git queries still run.

Requires: git, gh (GitHub CLI), Python 3.8+.

Usage:
    promote-enterprise.py --branch beta --version 152
    promote-enterprise.py --branch beta --version 152 --dry-run
    promote-enterprise.py --branch beta --version 152 --continue
    promote-enterprise.py --branch beta --version 152 --skip-pr
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from enterprise_merge_lib import (
    GitOps, Merger, MergeError,
    PENDING_ITEMS_URL, VERSION_FILES, ENT_L10N_REL,
    step, info, warn, done, dry,
    save_json, unlink_quiet, update_l10n_revisions,
)


CONFIGS_COMMIT_MESSAGE = "No Bug - Update configs after merge day operations a=release"
TEMP_MERGE_BRANCH = "theirs-merge-temp-branch"

PHASE_ORDER = [
    "step_1a",
    "step_1b",
    "wait_for_prs",
    "step_2_initial",
    "step_2_configs",
    "step_2_cherry",
    "step_2_finish",
]


class Promoter(GitOps):
    """Drive one promotion of enterprise-<src> to enterprise-<dest>."""

    def __init__(self, args):
        self.branch = args.branch
        self.version = int(args.version)
        self.resume = args.resume  # the --continue flag
        self.dry_run = args.dry_run
        self.skip_pr = args.skip_pr
        self.enterprise_remote = args.enterprise_remote
        self.upstream_remote = args.upstream_remote
        self.origin_remote = args.origin_remote
        self.enterprise_repo = args.enterprise_repo
        self.pending_items_url = args.pending_items_url

        # Step 0a: derived terms
        if self.branch == "main":
            self.upstream_dest = "beta"
        elif self.branch == "beta":
            self.upstream_dest = "release"
        else:
            raise MergeError("--branch must be 'main' or 'beta' for promotion")
        self.upstream_src = self.branch
        self.promoted_version = self.version
        self.previous_version = self.version - 1
        self.ent_src = f"enterprise-{self.upstream_src}"
        self.ent_dest = f"enterprise-{self.upstream_dest}"
        dest_up = self.upstream_dest.upper()
        self.src_tag = f"FIREFOX_{dest_up}_{self.promoted_version}_BASE"
        self.dest_tag = f"FIREFOX_{dest_up}_{self.previous_version}_END"

        self.repo_root: Path = None
        self.git_dir: Path = None
        self.state_file: Path = None
        self.merger_state_file: Path = None
        self.state: dict = None

    def _save_state(self):
        if self.dry_run:
            return
        save_json(self.state, self.state_file)

    # ----- main flow -----

    def run(self):
        self._preflight()
        self._show_pending_items()
        self._step0_fetch_and_validate()
        self._dispatch_phases()

    def _show_pending_items(self):
        """Open the Pending Items doc and wait for confirmation. The
        inner Merger invocations in step 1a/1b are told to skip this
        (skip_pending_items=True), so promote surfaces it once up front
        in case any item is relevant to the promotion."""
        if self.resume:
            return
        step("Opening 'Pending important items' doc")
        self._action(
            f"open {self.pending_items_url} in default browser",
            lambda: webbrowser.open(self.pending_items_url),
        )
        self._action(
            "wait for user to confirm review of pending items",
            lambda: input("Press Enter after reviewing (Ctrl+C to abort): "),
        )

    def _dispatch_phases(self):
        actions = [
            ("step_1a",         self._do_step_1a),
            ("step_1b",         self._do_step_1b),
            ("wait_for_prs",    self._do_wait_for_prs),
            ("step_2_initial",  self._do_step_2_initial),
            ("step_2_configs",  self._do_step_2_configs),
            ("step_2_cherry",   self._do_step_2_cherry),
            ("step_2_finish",   self._do_step_2_finish),
        ]
        try:
            start_idx = next(
                i for i, (name, _) in enumerate(actions)
                if name == self.state["phase"]
            )
        except StopIteration:
            raise MergeError(f"Unknown phase in state: '{self.state['phase']}'")

        for name, fn in actions[start_idx:]:
            self.state["phase"] = name
            self._action(
                f"persist phase={name} to state file",
                self._save_state,
            )
            fn()
        unlink_quiet(self.state_file)
        self._summary()

    # ----- preflight -----

    def _preflight(self):
        try:
            self.repo_root = Path(self._git_out("rev-parse", "--show-toplevel"))
        except MergeError:
            raise MergeError("Not in a git repository.")
        os.chdir(self.repo_root)

        gd = Path(self._git_out("rev-parse", "--git-dir"))
        if not gd.is_absolute():
            gd = self.repo_root / gd
        self.git_dir = gd
        self.state_file = gd / "enterprise-promote-state.json"
        self.merger_state_file = gd / "enterprise-merge-state.json"

        for r in (self.enterprise_remote, self.upstream_remote, self.origin_remote):
            if self._git_check("remote", "get-url", r) != 0:
                raise MergeError(f"Git remote '{r}' not found.")

        if not self.skip_pr and shutil.which("gh") is None:
            raise MergeError(
                "gh (GitHub CLI) is required. Install: https://cli.github.com/"
            )

        if self.resume:
            if not self.state_file.exists():
                raise MergeError(
                    f"--continue specified but no state file at {self.state_file}."
                )
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if (self.state["branch"] != self.branch
                    or self.state["version"] != self.version):
                raise MergeError(
                    f"State is for --branch {self.state['branch']} --version {self.state['version']}, "
                    f"but --branch {self.branch} --version {self.version} was given."
                )
            step(
                f"Resuming promotion of {self.upstream_src} -> {self.upstream_dest} "
                f"(version {self.version}) at phase '{self.state['phase']}'"
            )
        else:
            if self.state_file.exists():
                raise MergeError(
                    f"State file already exists at {self.state_file}. "
                    "Use --continue to resume, or delete it to start over."
                )
            if self.merger_state_file.exists():
                raise MergeError(
                    f"Leftover merge-enterprise state at {self.merger_state_file}. "
                    "Finish or delete it before starting a promotion."
                )
            porcelain = self._git_lines("status", "--porcelain")
            if porcelain:
                raise MergeError(
                    "Working tree is not clean. Commit or stash before running."
                )
            self.state = {
                "branch": self.branch,
                "version": self.version,
                "configs_sha": None,
                "phase": "step_1a",
            }

    # ----- step 0 -----

    def _step0_fetch_and_validate(self):
        step(
            f"Step 0: validating promotion of "
            f"{self.ent_src} (v{self.previous_version}) -> "
            f"{self.ent_dest} (v{self.promoted_version})"
        )
        info(f"src_tag:  {self.src_tag}  (must be ancestor of {self.upstream_remote}/{self.upstream_src})")
        info(f"dest_tag: {self.dest_tag} (must be ancestor of {self.upstream_remote}/{self.upstream_dest})")

        # Fetch upstream tags and enterprise remote so subsequent checks
        # see the current state. Only on the initial run -- on --continue
        # we trust the state captured by the first invocation. (Validation
        # below still runs each time as a sanity check, using local refs.)
        if self.resume:
            info("Skipping fetch (--continue: using state captured by initial run).")
        else:
            self._git("fetch", "--tags", self.upstream_remote)
            self._git("fetch", self.enterprise_remote)

        # 0b: <version> should be the current major version in <ent-src>.
        if not self.dry_run:
            ent_src_ver = self._git_out(
                "show",
                f"{self.enterprise_remote}/{self.ent_src}:{VERSION_FILES[0]}",
            ).strip()
            m = re.match(r"^(\d+)", ent_src_ver)
            if not m:
                raise MergeError(
                    f"Could not parse major version from {self.enterprise_remote}/{self.ent_src}:"
                    f"{VERSION_FILES[0]} (got '{ent_src_ver}')."
                )
            ent_src_major = int(m.group(1))
            if ent_src_major != self.promoted_version:
                raise MergeError(
                    f"Version mismatch: {self.enterprise_remote}/{self.ent_src} is at "
                    f"v{ent_src_major} ({ent_src_ver}), but --version {self.promoted_version} was given."
                )
            info(f"Version check: {self.enterprise_remote}/{self.ent_src} is at {ent_src_ver}  (OK)")

        # 0c: ancestry checks and configs-commit search.
        if self.dry_run:
            dry("verify upstream tag ancestry and locate configs commit")
            return
        for tag, ref in (
            (self.dest_tag, f"{self.upstream_remote}/{self.upstream_dest}"),
            (self.src_tag,  f"{self.upstream_remote}/{self.upstream_src}"),
        ):
            if self._git_check("rev-parse", "--verify", "--quiet", f"refs/tags/{tag}") != 0:
                raise MergeError(
                    f"Tag '{tag}' not found locally (the fetch should have included it)."
                )
            if self._git_check("merge-base", "--is-ancestor", f"refs/tags/{tag}", ref) != 0:
                raise MergeError(f"Tag '{tag}' is not an ancestor of {ref}.")
            info(f"Ancestry: {tag} is an ancestor of {ref}  (OK)")

        # --ancestry-path restricts to commits that are both descendants
        # of <dest-tag> and ancestors of upstream/<upstream-dest> -- i.e.
        # commits made directly on the dest line of history. Without it,
        # the range pulls in older "Update configs" commits inherited
        # from the source branch's history via the promotion merge (note
        # that Lando's merge_onto pattern in step 2 inverts first-parent
        # direction, so --first-parent here doesn't help). The configs
        # commit we want was made directly on <upstream-dest> after the
        # promotion merge, which lies on the ancestry path.
        configs_shas = self._git_lines(
            "log",
            f"refs/tags/{self.dest_tag}..{self.upstream_remote}/{self.upstream_dest}",
            "--ancestry-path",
            "--grep", CONFIGS_COMMIT_MESSAGE,
            "-F",
            "--format=%H",
        )
        if not configs_shas:
            raise MergeError(
                f"Could not find a commit with message {CONFIGS_COMMIT_MESSAGE!r} "
                f"in {self.upstream_remote}/{self.upstream_dest} after {self.dest_tag}."
            )
        if len(configs_shas) > 1:
            raise MergeError(
                f"Found {len(configs_shas)} commits matching {CONFIGS_COMMIT_MESSAGE!r} "
                f"in {self.upstream_remote}/{self.upstream_dest} after {self.dest_tag}; "
                f"expected exactly one. SHAs: {', '.join(configs_shas)}"
            )
        cached = self.state.get("configs_sha")
        if cached and cached != configs_shas[0]:
            warn(f"configs_sha changed since previous run: {cached} -> {configs_shas[0]}")
        self.state["configs_sha"] = configs_shas[0]
        info(f"Configs commit: {self.state['configs_sha']}")

    # ----- step 1a / 1b -----

    def _invoke_merger(self, *, branch, tag, label):
        """Run merge-enterprise's Merger as a library. Resumes the inner
        operation if its state file is present. Raises MergeError if the
        inner Merger ends in a conflict state."""
        inner_resume = self.merger_state_file.exists()
        if inner_resume:
            info(f"Resuming inner merge for {label} (merge-enterprise state file present).")
        inner = Merger(
            branch=branch,
            tag=tag,
            resume=inner_resume,
            dry_run=self.dry_run,
            skip_pr=False,
            enterprise_remote=self.enterprise_remote,
            upstream_remote=self.upstream_remote,
            origin_remote=self.origin_remote,
            enterprise_repo=self.enterprise_repo,
            pending_items_url=self.pending_items_url,
            skip_pending_items=True,
        )
        inner.run()
        if self.merger_state_file.exists():
            raise MergeError(
                f"merge-enterprise hit a conflict during {label} "
                f"(merge of {tag} into enterprise-{branch}). "
                "Resolve, commit, then re-run with --continue."
            )

    def _do_step_1a(self):
        step(f"Step 1a: merge {self.src_tag} into {self.ent_src}")
        self._invoke_merger(branch=self.upstream_src, tag=self.src_tag, label="step 1a")
        done(f"Step 1a complete.")

    def _do_step_1b(self):
        step(f"Step 1b: merge {self.dest_tag} into {self.ent_dest}")
        self._invoke_merger(branch=self.upstream_dest, tag=self.dest_tag, label="step 1b")
        done(f"Step 1b complete.")

    def _do_wait_for_prs(self):
        step("Step 1c: waiting for the two preparation PRs to be merged")
        info("Both step-1 PRs must be merged on GitHub before promotion can proceed.")
        info(f"  - PR for {self.ent_src}  (merge of {self.src_tag})")
        info(f"  - PR for {self.ent_dest} (merge of {self.dest_tag})")
        self._action(
            "wait for user to confirm both PRs are merged",
            lambda: input("Press Enter once both PRs are merged (Ctrl+C to abort): "),
        )

    # ----- step 2 -----

    def _do_step_2_initial(self):
        """Steps 2a-2g: fetch, switch to ent-dest, then do the 'ours'-
        strategy merge that fast-forwards ent-dest to mirror ent-src."""
        step("Step 2 (initial): fast-forward enterprise-<dest> to enterprise-<src>")

        # 2a: fetch enterprise to pick up the just-merged step-1 PRs.
        self._git("fetch", self.enterprise_remote)

        # 2b: switch to ent-dest and fast-forward to remote.
        self._git("switch", self.ent_dest)
        self._git("pull", "--ff-only", self.enterprise_remote, self.ent_dest)

        # 2c: clean up any stale temp branch from a previous attempt,
        # then create theirs-merge-temp-branch from enterprise-remote/ent-src.
        self._git("branch", "-D", TEMP_MERGE_BRANCH, allow_fail=True)
        self._git(
            "switch", "-c", TEMP_MERGE_BRANCH,
            "--", f"{self.enterprise_remote}/{self.ent_src}",
        )

        # 2d: merge ent-dest INTO the temp branch with the "ours" strategy.
        # Result: a merge commit whose tree matches the temp branch (i.e.
        # ent-src content), but with ent-dest as a second parent.
        self._git(
            "merge", "--no-ff", "-s", "ours",
            "-m", f"Promote {self.ent_src} to {self.ent_dest}",
            self.ent_dest,
        )

        # 2e: capture the new merge commit SHA.
        if self.dry_run:
            merge_sha = "<dry-run-merge-sha>"
        else:
            merge_sha = self._git_out("rev-parse", "HEAD")
        info(f"Promote merge commit: {merge_sha}")

        # 2f: force-update ent-dest to point at the new merge commit.
        self._git("branch", "-f", self.ent_dest, merge_sha)

        # 2g: switch back to ent-dest.
        self._git("switch", self.ent_dest)

        # Clean up temp branch (no longer needed).
        self._git("branch", "-D", TEMP_MERGE_BRANCH, allow_fail=True)

    def _do_step_2_configs(self):
        """Step 2h-2i: apply the upstream "Update configs after merge day"
        commit via `git merge -X theirs`."""
        configs_sha = self.state["configs_sha"]
        step(f"Step 2 (configs): merge -X theirs {configs_sha[:12]}")

        # Idempotency: if HEAD's parents already include the configs commit,
        # this substep was completed (possibly via manual conflict resolution).
        if not self.dry_run and self._head_has_parent(configs_sha):
            info("Configs commit already merged into HEAD; skipping.")
            return

        # Refuse to start if there's an in-progress merge/cherry-pick.
        self._refuse_inprogress(
            "Resolve and commit any in-progress merge/cherry-pick before continuing."
        )

        rc = self._git("merge", "-X", "theirs", configs_sha, allow_fail=True)
        if rc != 0:
            conflicts = self._git_lines("diff", "--name-only", "--diff-filter=U")
            warn(f"`git merge -X theirs {configs_sha[:12]}` had conflicts:")
            for f in conflicts:
                print(f"      {f}")
            print()
            print("Resolve them, then:")
            print("  git add <files>")
            print("  git commit")
            print(f"  promote-enterprise.py --branch {self.branch} --version {self.version} --continue")
            raise MergeError("step_2_configs paused for conflict resolution.")

    def _do_step_2_cherry(self):
        """Step 2j: cherry-pick the same configs commit to actually apply
        its changes (the merge -X theirs may not have applied them due to
        the a->b->a flip in upstream history)."""
        configs_sha = self.state["configs_sha"]
        step(f"Step 2 (cherry-pick): cherry-pick {configs_sha[:12]}")

        # Idempotency: if HEAD's commit message already references the
        # cherry-pick of configs_sha, skip.
        if not self.dry_run and self._head_is_cherry_pick_of(configs_sha):
            info("Configs commit already cherry-picked at HEAD; skipping.")
            return

        self._refuse_inprogress(
            "Resolve and continue any in-progress merge/cherry-pick before continuing."
        )

        # -x appends a "(cherry picked from commit <sha>)" trailer so
        # _head_is_cherry_pick_of can detect a completed cherry-pick on
        # --continue.
        rc = self._git("cherry-pick", "-x", configs_sha, allow_fail=True)
        if rc != 0:
            conflicts = self._git_lines("diff", "--name-only", "--diff-filter=U")
            warn(f"`git cherry-pick -x {configs_sha[:12]}` had conflicts:")
            for f in conflicts:
                print(f"      {f}")
            print()
            print("Resolve them, then:")
            print("  git add <files>")
            print("  git cherry-pick --continue")
            print(f"  promote-enterprise.py --branch {self.branch} --version {self.version} --continue")
            raise MergeError("step_2_cherry paused for conflict resolution.")

    def _do_step_2_finish(self):
        """Steps 2k + 2l: l10n sync, push, open PR."""
        # 2k: l10n sync.
        step(
            f"Step 2 (l10n): sync enterprise-l10n-changesets.json revisions "
            f"from {self.upstream_remote}/main"
        )
        self._action(
            f"sync revisions in {ENT_L10N_REL}",
            lambda: update_l10n_revisions(self, self.upstream_remote),
        )
        if self.dry_run:
            dry(f"check whether {ENT_L10N_REL} changed and 'git add' + 'git commit' if so")
        else:
            rc = self._git("diff", "--quiet", "--", ENT_L10N_REL, allow_fail=True)
            if rc != 0:
                self._git("add", "--", ENT_L10N_REL)
                self._git(
                    "commit", "-m",
                    "Update enterprise-l10n-changesets.json revisions to upstream/main",
                )
                done("Committed l10n revision update.")
            else:
                info("No l10n revision changes.")

        # 2l: push and PR.
        pr_branch = f"promote-{self.promoted_version}-{self.upstream_dest}"
        step(f"Step 2l: push {self.ent_dest} -> {self.origin_remote}:{pr_branch}")
        self._git(
            "push", self.origin_remote,
            f"{self.ent_dest}:{pr_branch}",
        )

        # PR title / body / label.
        origin_url = self._git_out("remote", "get-url", self.origin_remote)
        m = re.search(r"github\.com[:/]([^/]+)/[^/]+?(?:\.git)?$", origin_url)
        if not m:
            raise MergeError(f"Could not parse origin owner from URL: {origin_url}")
        origin_owner = m.group(1)

        pr_title = f"Promote {self.ent_src} to {self.ent_dest} ({self.promoted_version})"
        # Two trailing spaces after "NO BUG" = markdown hard line break.
        body = (
            "### Description\n"
            "\n"
            "Bugzilla: NO BUG  \n"
            f"Promote `{self.ent_src}` to `{self.ent_dest}` for Firefox {self.promoted_version}.\n"
        )
        body_path = Path(tempfile.gettempdir()) / f"enterprise-promote-pr-body-{pr_branch}.md"
        self._action(
            f"write PR body to {body_path}",
            lambda: body_path.write_text(body, encoding="utf-8", newline="\n"),
        )

        label = f"branch:{self.upstream_dest}"
        gh_args = [
            "pr", "create",
            "--repo", self.enterprise_repo,
            "--base", self.ent_dest,
            "--head", f"{origin_owner}:{pr_branch}",
            "--title", pr_title,
            "--body-file", str(body_path),
            "--label", label,
        ]
        quoted_gh = " ".join(shlex.quote(a) for a in gh_args)

        if self.skip_pr:
            step("Skipping PR creation (--skip-pr)")
            print("Run this when ready:")
            print(f"  gh {quoted_gh}")
            print(f"Body file (kept for the command above): {body_path}")
            return

        def do_gh():
            step("Creating PR via gh")
            r = subprocess.run(["gh", *gh_args], capture_output=True, text=True)
            if r.returncode != 0:
                if r.stderr:
                    print(r.stderr, file=sys.stderr)
                raise MergeError(
                    f"gh pr create failed (exit {r.returncode}). "
                    f"State file preserved at {self.state_file}; "
                    "resolve and re-run with --continue."
                )
            done(f"PR opened: {r.stdout.strip()}")
            unlink_quiet(body_path)

        self._action(f"gh {quoted_gh}", do_gh)
        if self.dry_run:
            print("Body would have been:")
            print("-----")
            print(body)
            print("-----")

    # ----- helpers -----

    def _head_has_parent(self, sha):
        """True if `sha` (full or prefix) is one of HEAD's parent commits."""
        try:
            full_sha = self._git_out("rev-parse", sha)
        except MergeError:
            return False
        parents_line = self._git_out("rev-list", "--parents", "-1", "HEAD")
        parents = parents_line.split()[1:]
        return full_sha in parents

    def _head_is_cherry_pick_of(self, sha):
        """True if HEAD's commit message contains the cherry-pick trailer
        for `sha` (full or prefix)."""
        try:
            full_sha = self._git_out("rev-parse", sha)
        except MergeError:
            return False
        msg = self._git_out("log", "-1", "--format=%B")
        return f"(cherry picked from commit {full_sha})" in msg

    def _refuse_inprogress(self, msg):
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD"):
            if (self.git_dir / marker).exists():
                raise MergeError(f".git/{marker} exists. {msg}")

    # ----- summary -----

    def _summary(self):
        step("Promotion complete")
        print(f"  Source:        {self.ent_src} (v{self.promoted_version})")
        print(f"  Destination:   {self.ent_dest} (was v{self.previous_version})")
        print(f"  src_tag:       {self.src_tag}")
        print(f"  dest_tag:      {self.dest_tag}")
        print(f"  configs_sha:   {self.state['configs_sha']}")
        print("  Next:          assign reviewers, watch CI, then merge the promotion PR.")


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="promote-enterprise.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--branch", required=True, choices=["main", "beta"],
                   help="source branch of the upstream promotion "
                        "(main for main->beta, beta for beta->release)")
    p.add_argument("--version", required=True, type=int,
                   help="the major version being promoted (e.g. 152)")
    p.add_argument("--continue", dest="resume", action="store_true",
                   help="resume a paused promotion from its saved state")
    p.add_argument("--dry-run", action="store_true",
                   help="print mutating commands without running them")
    p.add_argument("--skip-pr", action="store_true",
                   help="do everything except the final 'gh pr create' for the promotion PR; "
                        "the two step-1 PRs are always created")
    p.add_argument("--enterprise-remote", default="enterprise")
    p.add_argument("--upstream-remote", default="upstream")
    p.add_argument("--origin-remote", default="origin")
    p.add_argument("--enterprise-repo", default="mozilla/enterprise-firefox")
    p.add_argument("--pending-items-url", default=PENDING_ITEMS_URL)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    try:
        Promoter(args).run()
    except MergeError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
