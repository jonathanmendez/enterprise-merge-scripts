"""enterprise_merge_lib.py -- Shared library for Firefox Enterprise merge
and promotion scripts.

Imported by merge-enterprise.py and promote-enterprise.py. Provides:
  - constants (PENDING_ITEMS_URL, VERSION_FILES, ...)
  - color/log helpers (step, info, warn, done, dry)
  - the MergeError exception
  - the GitOps mixin (_git/_git_out/_git_lines/_git_check/_action)
  - module-level unlink_quiet, save_json, update_l10n_revisions
  - the Merger class (also invokable as a library from promote)
"""

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

PENDING_ITEMS_URL = (
    "https://docs.google.com/document/d/"
    "1PfqxfzGFmNuOUa1anLCMWkHNLFSY1DE2h7Yvk-VExyY/edit?tab=t.o6sj23jc0xws"
)

VERSION_FILES = [
    "browser/config/version.txt",
    "browser/config/version_display.txt",
    "config/milestone.txt",
    "mobile/android/version.txt",
]

ENT_L10N_REL = "browser/locales/enterprise-l10n-changesets.json"
UPSTREAM_L10N_REL = "browser/locales/l10n-changesets.json"


# ----- Color output -------------------------------------------------

if os.name == "nt":
    # Side-effect: enables ANSI processing on Windows 10+ consoles.
    os.system("")


def _use_color():
    if os.environ.get("NO_COLOR") is not None:
        return False
    return sys.stdout.isatty()


if _use_color():
    C_CYAN = "\033[36m"
    C_YELLOW = "\033[33m"
    C_GREEN = "\033[32m"
    C_DIM = "\033[2;33m"
    C_RESET = "\033[0m"
else:
    C_CYAN = C_YELLOW = C_GREEN = C_DIM = C_RESET = ""


def step(msg):  print(f"\n{C_CYAN}==> {msg}{C_RESET}")
def info(msg):  print(f"    {msg}")
def warn(msg):  print(f"{C_YELLOW}!!  {msg}{C_RESET}")
def done(msg):  print(f"{C_GREEN}OK  {msg}{C_RESET}")
def dry(msg):   print(f"{C_DIM}DRY: {msg}{C_RESET}")


class MergeError(Exception):
    """Fail with a clean, user-facing error message."""


def unlink_quiet(p: Path):
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def save_json(data, path: Path):
    """Pretty-print `data` as JSON to `path` with LF line endings, no BOM."""
    path.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


class GitOps:
    """Mixin providing git/action helpers. Subclasses must set
    self.dry_run before any helper is invoked."""

    dry_run: bool = False

    def _git(self, *args, allow_fail=False):
        """Mutating git. Honors self.dry_run. Returns exit code; raises
        MergeError on non-zero unless allow_fail."""
        if self.dry_run:
            dry("git " + " ".join(shlex.quote(a) for a in args))
            return 0
        rc = subprocess.run(["git", *args]).returncode
        if rc != 0 and not allow_fail:
            raise MergeError(f"git {' '.join(args)} failed (exit {rc})")
        return rc

    def _git_out(self, *args, allow_fail=False) -> str:
        """Read-only git. Always runs (even in --dry-run). Returns
        captured stdout with trailing newline stripped."""
        r = subprocess.run(["git", *args], capture_output=True, text=True)
        if r.returncode != 0 and not allow_fail:
            raise MergeError(
                f"git {' '.join(args)} failed (exit {r.returncode}): "
                f"{r.stderr.strip()}"
            )
        return r.stdout.rstrip("\n")

    def _git_lines(self, *args, allow_fail=False) -> list:
        out = self._git_out(*args, allow_fail=allow_fail)
        return out.splitlines() if out else []

    def _git_check(self, *args) -> int:
        """Read-only git that returns the exit code, suppressing output.
        For verifications whose failure mode is signalled by a non-zero
        exit (rev-parse --verify, merge-base --is-ancestor, remote
        get-url). Always runs, even in --dry-run."""
        return subprocess.run(
            ["git", *args], capture_output=True, text=True
        ).returncode

    def _action(self, description, fn):
        """Non-git mutation. Honors self.dry_run."""
        if self.dry_run:
            dry(description)
            return None
        return fn()


def update_l10n_revisions(ops: GitOps, upstream_remote: str) -> int:
    """Sync `revision` of every locale in the enterprise l10n file from
    {upstream_remote}/main:{UPSTREAM_L10N_REL}, preserving original
    formatting (minimal diff). Returns the count of changed revisions
    (caller decides whether to git add + commit). Requires
    ops.repo_root and ops._git_out."""
    upstream_spec = f"{upstream_remote}/main:{UPSTREAM_L10N_REL}"
    try:
        upstream_text = ops._git_out("show", upstream_spec)
    except MergeError as e:
        raise MergeError(
            f"Could not read {upstream_spec}. "
            f"Did you 'git fetch {upstream_remote}'? ({e})"
        )
    upstream = json.loads(upstream_text)

    ent_path = ops.repo_root / ENT_L10N_REL
    original = ent_path.read_text(encoding="utf-8")
    ent_data = json.loads(original)

    # The enterprise file's "revision" lines appear in the same order
    # as the locales in the parsed JSON. Walk both in lockstep and
    # patch each revision in place.
    locales = list(ent_data.keys())
    lines = original.split("\n")
    locale_idx = 0
    changed = 0
    pat = re.compile(r'^(\s+)"revision":\s+"([0-9a-fA-F]+)"(.*)$')
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        if locale_idx >= len(locales):
            raise MergeError(
                f"More 'revision' lines than locales in {ENT_L10N_REL} (malformed)."
            )
        locale = locales[locale_idx]
        locale_idx += 1
        indent, old_rev, suffix = m.group(1), m.group(2), m.group(3)
        up_entry = upstream.get(locale)
        if not up_entry:
            warn(f"Locale '{locale}' not present in upstream; skipping.")
            continue
        new_rev = up_entry.get("revision")
        if not new_rev:
            warn(f"Locale '{locale}' has no revision in upstream; skipping.")
            continue
        if new_rev != old_rev:
            print(f"  {locale}: {old_rev} -> {new_rev}")
            lines[i] = f'{indent}"revision": "{new_rev}"{suffix}'
            changed += 1
    if locale_idx != len(locales):
        warn(
            f"Found {locale_idx} 'revision' lines but {len(locales)} locales "
            "in JSON; file may be malformed."
        )
    if changed == 0:
        info("No revision updates needed.")
        return 0
    new_content = "\n".join(lines)
    if not new_content.endswith("\n"):
        new_content += "\n"
    ent_path.write_text(new_content, encoding="utf-8", newline="\n")
    info(f"Updated {changed} locale revision(s) in {ENT_L10N_REL}")
    return changed


# ===================================================================
# Merger -- daily-merge orchestration
# ===================================================================

class Merger(GitOps):
    """Drive one daily merge (or tag-pinned merge) from upstream into
    enterprise-<branch>. Importable; promote-enterprise.py uses this
    directly for its step 1a/1b."""

    def __init__(self, *, branch, tag="", resume=False, dry_run=False,
                 skip_pr=False, enterprise_remote="enterprise-firefox",
                 upstream_remote="upstream", origin_remote="origin",
                 enterprise_repo="mozilla/enterprise-firefox",
                 pending_items_url=PENDING_ITEMS_URL,
                 skip_pending_items=False):
        self.branch = branch
        self.resume = resume
        self.dry_run = dry_run
        self.skip_pr = skip_pr
        self.tag = tag
        self.enterprise_remote = enterprise_remote
        self.upstream_remote = upstream_remote
        self.origin_remote = origin_remote
        self.enterprise_repo = enterprise_repo
        self.pending_items_url = pending_items_url
        self.skip_pending_items = skip_pending_items
        self.ent_branch_local = f"enterprise-{self.branch}"

        self.repo_root: Path = None
        self.state_file: Path = None
        self.state: dict = None

    def _save_state(self):
        if self.dry_run:
            return
        save_json(self.state, self.state_file)

    # ----- main flow -----

    def run(self):
        self._preflight()
        self._step1_pending_items()
        self._step2_fetch()
        self._step3_checkout_pull()
        if not self.resume:
            if not self._step4_merge():
                return  # conflict -- caller checks state_file presence
        self._step5_l10n()
        version_changed = self._step6_version_swap()
        tc_changed = self._step7_taskcluster_check()
        if self._is_noop():
            return
        self._step8_main_push()
        pr_branch = self._step9_pr_branch_name()
        self._step10_push_pr_branch(pr_branch)
        self._step11_open_pr(pr_branch)
        self._summary(pr_branch, version_changed, tc_changed)

    def _is_noop(self) -> bool:
        if self.dry_run:
            return False
        current_sha = self._git_out("rev-parse", "HEAD")
        if current_sha != self.state["preMergeSha"]:
            return False
        step("No new commits since pre-merge state; skipping push and PR")
        info(
            f"upstream/{self.branch} was already fully merged and no "
            "l10n updates were needed."
        )
        unlink_quiet(self.state_file)
        step("Summary")
        print(f"  Branch:  {self.branch}")
        print(f"  Status:  no-op")
        return True

    def _preflight(self):
        try:
            self.repo_root = Path(self._git_out("rev-parse", "--show-toplevel"))
        except MergeError:
            raise MergeError("Not in a git repository.")
        os.chdir(self.repo_root)

        git_dir = Path(self._git_out("rev-parse", "--git-dir"))
        if not git_dir.is_absolute():
            git_dir = self.repo_root / git_dir
        self.state_file = git_dir / "enterprise-merge-state.json"

        for r in (self.enterprise_remote, self.upstream_remote, self.origin_remote):
            if self._git_check("remote", "get-url", r) != 0:
                raise MergeError(f"Git remote '{r}' not found.")

        if not self.skip_pr and shutil.which("gh") is None:
            raise MergeError(
                "gh (GitHub CLI) is required. Install: https://cli.github.com/"
            )

        porcelain = self._git_lines("status", "--porcelain")
        if self.resume:
            if porcelain:
                raise MergeError(
                    "--resume requires a clean working tree "
                    "(did you commit the merge resolution?)."
                )
            if not self.state_file.exists():
                raise MergeError(
                    f"--resume specified but no state file at {self.state_file}."
                )
            self.state = json.loads(self.state_file.read_text(encoding="utf-8"))
            if self.state["branch"] != self.branch:
                raise MergeError(
                    f"State file is for branch '{self.state['branch']}', not '{self.branch}'."
                )
            state_tag = self.state.get("tag")
            if self.tag:
                if not state_tag:
                    raise MergeError(
                        f"State was created without a tag, but --tag '{self.tag}' was given. "
                        "Drop --tag to resume, or delete state to start over."
                    )
                if state_tag != self.tag:
                    raise MergeError(
                        f"State was created with --tag '{state_tag}', "
                        f"but --tag '{self.tag}' was given."
                    )
            elif state_tag:
                self.tag = state_tag
            current = self._git_out("rev-parse", "--abbrev-ref", "HEAD")
            if current != self.ent_branch_local:
                raise MergeError(
                    f"--resume expects HEAD on '{self.ent_branch_local}' but it is on '{current}'."
                )
            step(f"Resuming merge of {self.branch} (started {self.state['started']})")
            conflicts = self.state.get("conflicts") or []
            info(f"Recorded conflicts: {', '.join(conflicts) if conflicts else '(none)'}")
        else:
            if porcelain:
                raise MergeError("Working tree is not clean. Commit or stash before running.")
            if self.state_file.exists():
                raise MergeError(
                    f"State file already exists at {self.state_file}. "
                    "Use --resume to continue or delete it to start over."
                )

    # ----- step 1 -----
    def _step1_pending_items(self):
        if self.resume or self.skip_pending_items:
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

    # ----- step 2 -----
    def _step2_fetch(self):
        if self.resume:
            return
        step(f"Fetching {self.upstream_remote} and {self.enterprise_remote}")
        if self.tag:
            self._git("fetch", "--tags", self.upstream_remote)
        else:
            self._git("fetch", self.upstream_remote)
        self._git("fetch", self.enterprise_remote)

    # ----- step 3 -----
    def _step3_checkout_pull(self):
        if self.resume:
            return
        step(f"Checking out {self.ent_branch_local}")
        self._git("switch", self.ent_branch_local)
        self._git("pull", "--ff-only", self.enterprise_remote, self.ent_branch_local)

    # ----- step 4 -----
    def _step4_merge(self) -> bool:
        """Returns False on conflict (state saved, caller exits)."""
        if self.tag:
            if self._git_check("rev-parse", "--verify", "--quiet",
                               f"refs/tags/{self.tag}") != 0:
                raise MergeError(
                    f"Tag '{self.tag}' not found locally (did the fetch include tags?)."
                )
            if self._git_check("merge-base", "--is-ancestor",
                               f"refs/tags/{self.tag}",
                               f"{self.upstream_remote}/{self.branch}") != 0:
                raise MergeError(
                    f"Tag '{self.tag}' is not reachable from "
                    f"{self.upstream_remote}/{self.branch}."
                )
            merge_source = f"refs/tags/{self.tag}"
            merge_display = self.tag
        else:
            merge_source = f"{self.upstream_remote}/{self.branch}"
            merge_display = merge_source

        pre_merge_sha = self._git_out("rev-parse", "HEAD")
        step(f"Merging {merge_display} into {self.ent_branch_local}")
        info(f"Pre-merge SHA: {pre_merge_sha}")

        rc = self._git("merge", "--no-edit", merge_source, allow_fail=True)
        if rc == 0:
            conflicts = []
        else:
            conflicts = self._git_lines("diff", "--name-only", "--diff-filter=U")
            if not conflicts:
                raise MergeError(
                    f"git merge failed (exit {rc}) but no conflict files detected."
                )

        self.state = {
            "branch": self.branch,
            "started": datetime.now(timezone.utc).isoformat(),
            "preMergeSha": pre_merge_sha,
            "conflicts": conflicts,
            "prBranch": None,
            "tag": self.tag or None,
        }
        self._action(f"write merge state to {self.state_file}", self._save_state)

        if conflicts:
            warn(f"Merge produced {len(conflicts)} conflict(s):")
            for f in conflicts:
                print(f"      {f}")
            print()
            print("Resolve them, then:")
            print("  git add <files>")
            print("  git commit")
            print(f"  re-run with: --branch {self.branch} --resume")
            return False
        done("Merge completed cleanly.")
        return True

    # ----- step 5 -----
    def _step5_l10n(self):
        step(
            f"Syncing enterprise-l10n-changesets.json revisions "
            f"from {self.upstream_remote}/main"
        )
        self._action(
            f"sync revisions in {ENT_L10N_REL}",
            lambda: update_l10n_revisions(self, self.upstream_remote),
        )
        if self.dry_run:
            dry(f"check whether {ENT_L10N_REL} changed and 'git add' + 'git commit' if so")
            return
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

    # ----- step 6 -----
    def _step6_version_swap(self) -> bool:
        if self.branch != "release":
            return False
        primary = self.repo_root / VERSION_FILES[0]
        ver = primary.read_text(encoding="utf-8").strip()
        m = re.match(r"^(\d+)\.0\.(\d+)$", ver)
        if not m:
            return False
        major, dot = m.group(1), m.group(2)
        new_ver = f"{major}.{dot}.0"
        pattern = re.compile(r"^" + re.escape(ver) + r"$", re.M)

        affected = []
        for rel in VERSION_FILES:
            full = self.repo_root / rel
            if not full.exists():
                warn(f"Version file not found: {rel} (skipping)")
                continue
            if pattern.search(full.read_text(encoding="utf-8")):
                affected.append(rel)

        step("Release version pattern detected")
        info(f"{ver}  ->  {new_ver}")
        info(f"Files containing '{ver}' on its own line:")
        for f in affected:
            info(f"  {f}")

        if not affected:
            warn(f"No version files contain '{ver}' on its own line; skipping swap.")
            return False
        if self.dry_run:
            dry(
                f"would prompt for version swap; if confirmed, would rewrite "
                f"{len(affected)} file(s) and commit"
            )
            return False
        ans = input(f"Apply swap to {len(affected)} file(s)? [y/N]: ").strip()
        if not ans.lower().startswith("y"):
            info("Skipped version swap.")
            return False
        for rel in affected:
            full = self.repo_root / rel
            self._action(
                f"swap {ver} -> {new_ver} in {rel}",
                lambda full=full, pattern=pattern, new_ver=new_ver: full.write_text(
                    pattern.sub(new_ver, full.read_text(encoding="utf-8")),
                    encoding="utf-8",
                    newline="\n",
                ),
            )
        self._git("add", "--", *affected)
        self._git("commit", "-m", f"Update version to {new_ver}")
        done(f"Committed version swap across {len(affected)} file(s).")
        return True

    # ----- step 7 -----
    def _step7_taskcluster_check(self) -> bool:
        if self.dry_run:
            dry("check whether .taskcluster.yml changed in this merge")
            return False
        changed = self._git_lines(
            "diff", "--name-only", self.state["preMergeSha"], "HEAD"
        )
        tc = ".taskcluster.yml" in changed
        if tc:
            warn(
                ".taskcluster.yml changed in this merge -- "
                "ping relduty after the PR merges."
            )
        return tc

    # ----- step 8 -----
    def _step8_main_push(self):
        if self.branch != "main":
            return
        step(
            f"Pushing {self.upstream_remote}/main -> {self.enterprise_remote}:main"
        )
        self._git(
            "push", self.enterprise_remote,
            f"{self.upstream_remote}/main:main",
        )

    # ----- step 9 -----
    def _step9_pr_branch_name(self) -> str:
        if self.state.get("prBranch"):
            step(f"Reusing PR branch from state: {self.state['prBranch']}")
            return self.state["prBranch"]
        if self.tag:
            pr_branch = f"{self.branch}-merge_{self.tag}"
            step(f"PR branch (tag-based): {pr_branch}")
        else:
            today = datetime.now(timezone.utc).strftime("%Y%m%d")
            prefix = f"{self.branch}-merge_{today}"
            step(f"Determining next PR suffix for {prefix}")
            heads = self._git_lines(
                "ls-remote", "--heads", self.origin_remote, f"{prefix}*"
            )
            pat = re.compile(r"refs/heads/" + re.escape(prefix) + r"(\d{2})$")
            used = []
            for line in heads:
                m = pat.search(line)
                if m:
                    used.append(int(m.group(1)))
            nn = max(used) + 1 if used else 0
            pr_branch = f"{prefix}{nn:02d}"
            info(f"PR branch: {pr_branch}")
        self.state["prBranch"] = pr_branch
        self._action(
            f"persist prBranch={pr_branch} to state file",
            self._save_state,
        )
        return pr_branch

    # ----- step 10 -----
    def _step10_push_pr_branch(self, pr_branch):
        step(f"Pushing {self.ent_branch_local} -> {self.origin_remote}:{pr_branch}")
        self._git(
            "push", self.origin_remote,
            f"{self.ent_branch_local}:{pr_branch}",
        )

    # ----- step 11 -----
    def _step11_open_pr(self, pr_branch):
        origin_url = self._git_out("remote", "get-url", self.origin_remote)
        m = re.search(r"github\.com[:/]([^/]+)/[^/]+?(?:\.git)?$", origin_url)
        if not m:
            raise MergeError(f"Could not parse origin owner from URL: {origin_url}")
        origin_owner = m.group(1)

        suffix = re.sub(rf"^{re.escape(self.branch)}-merge_", "", pr_branch)
        pr_title = f"Enterprise {self.branch} merge {suffix}"
        merge_source_label = self.tag if self.tag else f"{self.upstream_remote}/{self.branch}"

        conflicts = self.state.get("conflicts") or []
        if conflicts:
            bullets = "\n".join(f"*   {f}" for f in conflicts)
            conflict_block = f"Resolved conflicts:\n\n{bullets}"
        else:
            conflict_block = "No merge conflicts."

        # Two trailing spaces after "NO BUG" = markdown hard line break.
        body = (
            "### Description\n"
            "\n"
            "Bugzilla: NO BUG  \n"
            f"Daily merge from `{merge_source_label}` to "
            f"`{self.ent_branch_local}`\n"
            "\n"
            f"{conflict_block}\n"
        )

        body_path = Path(tempfile.gettempdir()) / f"enterprise-merge-pr-body-{pr_branch}.md"
        self._action(
            f"write PR body to {body_path}",
            lambda: body_path.write_text(body, encoding="utf-8", newline="\n"),
        )

        label = f"branch:{self.branch}"
        gh_args = [
            "pr", "create",
            "--repo", self.enterprise_repo,
            "--base", self.ent_branch_local,
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
            unlink_quiet(self.state_file)
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
                    "resolve and re-run with --resume."
                )
            done(f"PR opened: {r.stdout.strip()}")
            unlink_quiet(self.state_file)
            unlink_quiet(body_path)

        self._action(f"gh {quoted_gh}", do_gh)
        if self.dry_run:
            print("Body would have been:")
            print("-----")
            print(body)
            print("-----")

    # ----- summary -----
    def _summary(self, pr_branch, version_changed, tc_changed):
        step("Summary")
        print(f"  Branch:    {self.branch}")
        print(f"  PR branch: {pr_branch}")
        print(f"  Conflicts: {len(self.state.get('conflicts') or [])}")
        if version_changed:
            warn("  Version was swapped to xxx.y.0; verify before merging.")
        if tc_changed:
            warn(
                "  .taskcluster.yml changed -- after PR merges, ping "
                "relduty in #releaseduty:"
            )
            warn(
                "    'rebuild hooks after a change to the "
                "enterprise-firefox .taskcluster.yml'"
            )
        print("  Next:      assign reviewers, watch CI, then merge in the GitHub UI.")
