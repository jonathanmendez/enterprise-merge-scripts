#requires -Version 5.1
<#
.SYNOPSIS
  Drive one Daily Merge from upstream/<branch> into enterprise-<branch>,
  sync l10n revisions, push to your fork, and open a PR.

.DESCRIPTION
  Mirrors the per-branch steps in "Daily Merges (x3)" of the Firefox
  Enterprise merge checklist. Halts cleanly on merge conflict; resume
  with -Resume after you commit the resolution.

  Reviewers are not assigned -- add them yourself in the GitHub UI.

.PARAMETER Branch
  main, beta, or release.
.PARAMETER Resume
  Continue after manually resolving and committing a merge conflict.
.PARAMETER DryRun
  Print mutating commands without running them. Read-only git commands
  still execute, but no merge, commit, push, or PR is performed.
.PARAMETER SkipPR
  Do everything except 'gh pr create'. Prints the gh command and leaves
  the PR body file in place so you can run it manually.

.EXAMPLE
  Merge-Enterprise.ps1 -Branch main
.EXAMPLE
  # After resolving and committing conflicts:
  Merge-Enterprise.ps1 -Branch main -Resume
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet("main", "beta", "release")]
    [string]$Branch,

    [switch]$Resume,
    [switch]$DryRun,
    [switch]$SkipPR,

    [string]$EnterpriseRemote = "enterprise",
    [string]$UpstreamRemote   = "upstream",
    [string]$OriginRemote     = "origin",
    [string]$EnterpriseRepo   = "mozilla/enterprise-firefox",
    [string]$PendingItemsUrl  = "https://docs.google.com/document/d/1PfqxfzGFmNuOUa1anLCMWkHNLFSY1DE2h7Yvk-VExyY/edit?tab=t.o6sj23jc0xws"
)

$ErrorActionPreference = "Stop"

# ----- console helpers -----
function Write-Step($msg) { Write-Host ""; Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Info($msg) { Write-Host "    $msg" }
function Write-Warn2($msg) { Write-Host "!!  $msg" -ForegroundColor Yellow }
function Write-Done($msg) { Write-Host "OK  $msg" -ForegroundColor Green }

# ----- git helpers -----
function Invoke-GitMut {
    [CmdletBinding()]
    param([Parameter(ValueFromRemainingArguments)][string[]]$GitArgs)
    if ($script:DryRun) {
        Write-Host "DRY: git $($GitArgs -join ' ')" -ForegroundColor DarkYellow
        return
    }
    & git @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE)"
    }
}

function Read-GitOut {
    [CmdletBinding()]
    param([Parameter(ValueFromRemainingArguments)][string[]]$GitArgs)
    $out = & git @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE)"
    }
    if ($null -eq $out) { return "" }
    if ($out -is [array]) { return ($out -join "`n") }
    return [string]$out
}

function Read-GitLines {
    [CmdletBinding()]
    param([Parameter(ValueFromRemainingArguments)][string[]]$GitArgs)
    $out = & git @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE)"
    }
    if ($null -eq $out) { return @() }
    return @($out)
}

function Save-State($state, $path) {
    if ($script:DryRun) { return }
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($path, ($state | ConvertTo-Json -Depth 5), $utf8NoBom)
}

# ----- pre-flight -----
$repoRoot = (Read-GitOut rev-parse --show-toplevel).Trim()
Push-Location $repoRoot
try {
    $gitDir = (Read-GitOut rev-parse --git-dir).Trim()
    if (-not [System.IO.Path]::IsPathRooted($gitDir)) {
        $gitDir = Join-Path $repoRoot $gitDir
    }
    $stateFile = Join-Path $gitDir "enterprise-merge-state.json"

    foreach ($r in @($EnterpriseRemote, $UpstreamRemote, $OriginRemote)) {
        $null = & git remote get-url $r 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Git remote '$r' not found."
        }
    }

    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "gh (GitHub CLI) is required. Install: winget install --id GitHub.cli"
    }

    $entBranchLocal = "enterprise-$Branch"

    $porcelain = Read-GitLines status --porcelain
    if ($Resume) {
        if ($porcelain.Count -gt 0) {
            throw "-Resume requires a clean working tree (did you commit the merge resolution?)."
        }
        if (-not (Test-Path $stateFile)) {
            throw "-Resume specified but no state file at $stateFile."
        }
        $state = Get-Content $stateFile -Raw | ConvertFrom-Json
        if ($state.branch -ne $Branch) {
            throw "State file is for branch '$($state.branch)', not '$Branch'."
        }
        $currentRef = (Read-GitOut rev-parse --abbrev-ref HEAD).Trim()
        if ($currentRef -ne $entBranchLocal) {
            throw "-Resume expects HEAD on '$entBranchLocal' but it is on '$currentRef'."
        }
        Write-Step "Resuming merge of $Branch (started $($state.started))"
        $confCount = @($state.conflicts).Count
        Write-Info ("Recorded conflicts: {0}" -f $(if ($confCount -eq 0) { "(none)" } else { ($state.conflicts -join ', ') }))
    } else {
        if ($porcelain.Count -gt 0) {
            throw "Working tree is not clean. Commit or stash before running."
        }
        if (Test-Path $stateFile) {
            throw "State file already exists at $stateFile. Use -Resume to continue or delete it to start over."
        }
    }

    # ----- step 1: pending items -----
    if (-not $Resume) {
        Write-Step "Opening 'Pending important items' doc"
        if ($DryRun) {
            Write-Host "DRY: Start-Process $PendingItemsUrl" -ForegroundColor DarkYellow
        } else {
            Start-Process $PendingItemsUrl | Out-Null
        }
        $null = Read-Host "Press Enter after reviewing (Ctrl+C to abort)"
    }

    # ----- step 2: fetch -----
    if (-not $Resume) {
        Write-Step "Fetching $UpstreamRemote and $EnterpriseRemote"
        Invoke-GitMut fetch $UpstreamRemote
        Invoke-GitMut fetch $EnterpriseRemote
    }

    # ----- step 3: checkout + pull -----
    if (-not $Resume) {
        Write-Step "Checking out $entBranchLocal"
        Invoke-GitMut checkout $entBranchLocal
        Invoke-GitMut pull --ff-only $EnterpriseRemote $entBranchLocal
    }

    # ----- step 4: merge -----
    if (-not $Resume) {
        $preMergeSha = (Read-GitOut rev-parse HEAD).Trim()
        Write-Step "Merging $UpstreamRemote/$Branch into $entBranchLocal"
        Write-Info "Pre-merge SHA: $preMergeSha"

        if ($DryRun) {
            Write-Host "DRY: git merge --no-edit $UpstreamRemote/$Branch" -ForegroundColor DarkYellow
            $conflictFiles = @()
        } else {
            & git merge --no-edit "$UpstreamRemote/$Branch"
            $mergeExit = $LASTEXITCODE
            if ($mergeExit -eq 0) {
                $conflictFiles = @()
            } else {
                $conflictFiles = Read-GitLines diff --name-only --diff-filter=U
                if ($conflictFiles.Count -eq 0) {
                    throw "git merge failed (exit $mergeExit) but no conflict files detected."
                }
            }
        }

        $state = [PSCustomObject]@{
            branch      = $Branch
            started     = (Get-Date).ToUniversalTime().ToString("o")
            preMergeSha = $preMergeSha
            conflicts   = @($conflictFiles)
            prBranch    = $null
        }
        Save-State $state $stateFile

        if ($conflictFiles.Count -gt 0) {
            Write-Warn2 "Merge produced $($conflictFiles.Count) conflict(s):"
            foreach ($f in $conflictFiles) { Write-Host "      $f" }
            Write-Host ""
            Write-Host "Resolve them, then:"
            Write-Host "  git add <files>"
            Write-Host "  git commit"
            Write-Host "  Merge-Enterprise.ps1 -Branch $Branch -Resume"
            return
        }
        Write-Done "Merge completed cleanly."
    }

    # ----- step 5: l10n -----
    Write-Step "Syncing enterprise-l10n-changesets.json revisions from $UpstreamRemote/main"
    $l10nPath = "browser/locales/enterprise-l10n-changesets.json"
    if ($DryRun) {
        Write-Host "DRY: & '$PSScriptRoot\Update-EnterpriseL10n.ps1' -UpstreamRemote $UpstreamRemote" -ForegroundColor DarkYellow
    } else {
        & "$PSScriptRoot\Update-EnterpriseL10n.ps1" -UpstreamRemote $UpstreamRemote
        & git diff --quiet -- $l10nPath
        if ($LASTEXITCODE -ne 0) {
            Invoke-GitMut add -- $l10nPath
            Invoke-GitMut commit -m "Update enterprise-l10n-changesets.json revisions to upstream/main"
            Write-Done "Committed l10n revision update."
        } else {
            Write-Info "No l10n revision changes."
        }
    }

    # ----- step 6: release version swap -----
    $versionChanged = $false
    if ($Branch -eq "release" -and -not $DryRun) {
        $verPath     = Join-Path $repoRoot "browser/config/version.txt"
        $verDispPath = Join-Path $repoRoot "browser/config/version_display.txt"
        $ver = (Get-Content $verPath -Raw).Trim()
        if ($ver -match '^(\d+)\.0\.(\d+)$') {
            $major  = $matches[1]
            $dot    = $matches[2]
            $newVer = "$major.$dot.0"
            Write-Step "Release version pattern detected"
            Write-Info "version.txt:         $ver  ->  $newVer"
            $verDisp    = (Get-Content $verDispPath -Raw).Trim()
            $newVerDisp = $verDisp
            if ($verDisp -match '^(\d+)\.0\.(\d+)$') {
                $newVerDisp = "$($matches[1]).$($matches[2]).0"
                Write-Info "version_display.txt: $verDisp  ->  $newVerDisp"
            }
            $ans = Read-Host "Apply swap? [y/N]"
            if ($ans -match '^[Yy]') {
                $utf8NoBom = New-Object System.Text.UTF8Encoding $false
                [System.IO.File]::WriteAllText($verPath, "$newVer`n", $utf8NoBom)
                if ($newVerDisp -ne $verDisp) {
                    [System.IO.File]::WriteAllText($verDispPath, "$newVerDisp`n", $utf8NoBom)
                    Invoke-GitMut add browser/config/version.txt browser/config/version_display.txt
                } else {
                    Invoke-GitMut add browser/config/version.txt
                }
                Invoke-GitMut commit -m "Update version to $newVer"
                $versionChanged = $true
                Write-Done "Committed version swap."
            } else {
                Write-Info "Skipped version swap."
            }
        }
    }

    # ----- step 7: detect .taskcluster.yml change -----
    $tcChanged = $false
    if (-not $DryRun) {
        $changedInMerge = Read-GitLines diff --name-only $state.preMergeSha HEAD
        $tcChanged = $changedInMerge -contains ".taskcluster.yml"
        if ($tcChanged) {
            Write-Warn2 ".taskcluster.yml changed in this merge -- ping relduty after the PR merges."
        }
    }

    # ----- step 8: main-only push to enterprise -----
    if ($Branch -eq "main") {
        Write-Step "Pushing $UpstreamRemote/main -> $EnterpriseRemote:main"
        Invoke-GitMut push $EnterpriseRemote "$UpstreamRemote/main:main"
    }

    # ----- step 9: choose PR branch name (or reuse from state) -----
    if ($state.prBranch) {
        $prBranch = $state.prBranch
        Write-Step "Reusing PR branch from state: $prBranch"
    } else {
        $today  = (Get-Date).ToString("yyyyMMdd")
        $prefix = "$Branch-merge_$today"
        Write-Step "Determining next PR suffix for $prefix"
        $heads = Read-GitLines ls-remote --heads $OriginRemote "$prefix*"
        $used = @()
        foreach ($line in $heads) {
            if ($line -match ("refs/heads/" + [regex]::Escape($prefix) + "(\d{2})$")) {
                $used += [int]$matches[1]
            }
        }
        $nn = 0
        if ($used.Count -gt 0) {
            $nn = ([int]($used | Measure-Object -Maximum).Maximum) + 1
        }
        $prBranch = "{0}{1:D2}" -f $prefix, $nn
        Write-Info "PR branch: $prBranch"
        $state.prBranch = $prBranch
        Save-State $state $stateFile
    }

    # ----- step 10: push PR branch -----
    Write-Step "Pushing $entBranchLocal -> ${OriginRemote}:${prBranch}"
    Invoke-GitMut push $OriginRemote "${entBranchLocal}:${prBranch}"

    # ----- step 11: build body + open PR -----
    $originUrl = (Read-GitOut remote get-url $OriginRemote).Trim()
    if ($originUrl -notmatch 'github\.com[:/]([^/]+)/[^/]+?(?:\.git)?$') {
        throw "Could not parse origin owner from URL: $originUrl"
    }
    $originOwner = $matches[1]

    # Title format from PR #883: "Enterprise <branch> merge <YYYYMMDDNN>"
    $suffix = $prBranch -replace "^$([regex]::Escape($Branch))-merge_", ""
    $prTitle = "Enterprise $Branch merge $suffix"

    $conflicts = @($state.conflicts)
    if ($conflicts.Count -gt 0) {
        $bullets = ($conflicts | ForEach-Object { "*   $_" }) -join "`n"
        $conflictBlock = "Resolved conflicts:`n`n$bullets"
    } else {
        $conflictBlock = "No merge conflicts."
    }

    # Two trailing spaces after "NO BUG" = markdown hard line break.
    $hardBreak = "  "
    $body = @"
### Description

Bugzilla: NO BUG$hardBreak
Daily merge from ``$UpstreamRemote/$Branch`` to ``$entBranchLocal``

$conflictBlock
"@

    $bodyFile  = Join-Path $env:TEMP "enterprise-merge-pr-body-$prBranch.md"
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    if (-not $DryRun) {
        [System.IO.File]::WriteAllText($bodyFile, $body, $utf8NoBom)
    }

    $label  = "branch:$Branch"
    $ghArgs = @(
        "pr", "create",
        "--repo",      $EnterpriseRepo,
        "--base",      $entBranchLocal,
        "--head",      "${originOwner}:${prBranch}",
        "--title",     $prTitle,
        "--body-file", $bodyFile,
        "--label",     $label
    )

    if ($SkipPR) {
        Write-Step "Skipping PR creation (-SkipPR)"
        Write-Host "Run this when ready:"
        Write-Host "  gh $($ghArgs -join ' ')"
        Write-Host "Body file: $bodyFile"
    } elseif ($DryRun) {
        Write-Host "DRY: gh $($ghArgs -join ' ')" -ForegroundColor DarkYellow
        Write-Host "Body would have been:"
        Write-Host "-----"
        Write-Host $body
        Write-Host "-----"
    } else {
        Write-Step "Creating PR via gh"
        $prUrl = & gh @ghArgs
        if ($LASTEXITCODE -ne 0) {
            throw "gh pr create failed (exit $LASTEXITCODE). State file preserved at $stateFile; resolve and re-run with -Resume."
        }
        Write-Done "PR opened: $prUrl"
        Remove-Item $stateFile -ErrorAction SilentlyContinue
        Remove-Item $bodyFile -ErrorAction SilentlyContinue
    }

    # ----- summary -----
    Write-Step "Summary"
    Write-Host "  Branch:    $Branch"
    Write-Host "  PR branch: $prBranch"
    Write-Host "  Conflicts: $($conflicts.Count)"
    if ($versionChanged) { Write-Warn2 "  Version was swapped to xxx.y.0; verify before merging." }
    if ($tcChanged) {
        Write-Warn2 "  .taskcluster.yml changed -- after PR merges, ping relduty in #releaseduty:"
        Write-Warn2 "    'rebuild hooks after a change to the enterprise-firefox .taskcluster.yml'"
    }
    Write-Host "  Next:      assign reviewers, watch CI, then merge in the GitHub UI."
}
finally {
    Pop-Location
}
