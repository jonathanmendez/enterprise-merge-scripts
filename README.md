# enterprise-firefox merge scripts

Automation for the Firefox Enterprise daily-merge and train-promotion workflows.
Drives `git`, syncs the enterprise l10n file, and opens PRs via `gh`. Halts cleanly
on conflicts; resumes after manual resolution.

## Disclaimer

The scripts were created with guidance by claude in the pursuit of automating happy paths and common pitfalls.
No effort has been made to make the code clean and readable, and only limited effort has been put toward robustness
(I feel confident in being able to reset the state of my git repo if something goes wrong). Use at your own risk.

## Files

| File | Purpose |
|---|---|
| `enterprise_merge_lib.py` | Shared library (helpers, `Merger` class, l10n updater). Not invoked directly. |
| `merge-enterprise.py` | Daily merge from `upstream/<branch>` (or a tag) into `enterprise-<branch>`. |
| `promote-enterprise.py` | Train promotion (`main`→`beta` or `beta`→`release`) of the enterprise branches. |

## Requirements

- Python 3.8+
- `git`
- [`gh`](https://cli.github.com/), authenticated via `gh auth login`
- Three git remotes (names overridable via flags):
  - `upstream` — `mozilla-firefox/firefox`
  - `enterprise-firefox` — `mozilla/enterprise-firefox`
  - `origin` — your fork of `enterprise-firefox`

## Setup

Clone this repo somewhere on your `PATH` (e.g. `~/bin`) and make the scripts
executable:

```sh
chmod +x merge-enterprise.py promote-enterprise.py
```

Invoke as `python merge-enterprise.py ...` or `python promote-enterprise.py`.

## Daily merge

```sh
merge-enterprise.py --branch main           # one of main / beta / release
merge-enterprise.py --branch main --dry-run # preview without mutating
merge-enterprise.py --branch main --resume  # after resolving conflicts
merge-enterprise.py --branch release --tag FIREFOX_150_0_2_BUILD2  # tag-pinned
```

On conflict the script prints the files, exits, and tells you to commit and
re-run with `--resume`. PR title/body/label match the existing convention; you
add reviewers in the GitHub UI.

## Promotion

```sh
promote-enterprise.py --branch beta --version 152             # beta -> release
promote-enterprise.py --branch beta --version 152 --dry-run   # preview
promote-enterprise.py --branch beta --version 152 --continue  # resume after pause
```

If any sub-step pauses (inner merge conflict, `merge -X theirs` conflict,
`cherry-pick` conflict), you resolve in-tree and re-run with `--continue`. Although
the promotion script invokes merges, if the merge script puases for conflicts, no
need to invoke `merge-enterprise.py --resume` manually — `promote-enterprise.py --continue` drives it.

## State files

Held in `.git/` of the working repo:

- `.git/enterprise-merge-state.json` — merge-enterprise's in-flight state
- `.git/enterprise-promote-state.json` — promote-enterprise's phase + cached `configs_sha`

Both are deleted on successful completion. Delete manually to start over.

## Flags common to both scripts

| Flag | Effect |
|---|---|
| `--dry-run` | Prints `DRY: ...` for every mutation; reads still run. |
| `--skip-pr` | Pushes the branch but doesn't open the PR; prints the `gh` invocation. |
| `--enterprise-remote NAME`, `--upstream-remote NAME`, `--origin-remote NAME` | Override remote names. |
| `--enterprise-repo OWNER/REPO` | Override the GitHub repo for `gh pr create` (default `mozilla/enterprise-firefox`). |

See `--help` on either script for the full list.
