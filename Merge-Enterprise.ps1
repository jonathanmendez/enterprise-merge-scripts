#requires -Version 5.1
<#
.SYNOPSIS
  Drive one Daily Merge from upstream/<branch> into enterprise-<branch>,
  sync l10n revisions, push to your fork, and open a PR.

.DESCRIPTION
  Mirrors the per-branch steps in "Daily Merges (x3)" of the Firefox
  Enterprise merge checklist. Halts cleanly on merge conflict; resume
  with -Resume after you commit the resolution.

  In -DryRun mode every mutating action (git fetch/checkout/pull/merge/
  add/commit/push, file writes, state-file writes, gh pr create, even
  the browser open and the user prompt) is replaced with a "DRY: ..."
  line. Read-only git queries still run so the dry-run reflects real
  repo state.

  Reviewers are not assigned -- add them yourself in the GitHub UI.

.PARAMETER Branch
  main, beta, or release.
.PARAMETER Resume
  Continue after manually resolving and committing a merge conflict.
.PARAMETER DryRun
  Print mutating commands without running them.
.PARAMETER SkipPR
  Do everything except 'gh pr create'. Prints the gh command and leaves
  the PR body file in place so you can run it manually.

.EXAMPLE
  Merge-Enterprise.ps1 -Branch main
.EXAMPLE
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
function Write-Dry($msg)  { Write-Host "DRY: $msg" -ForegroundColor DarkYellow }

# ----- one mutating-git helper -----
# Honors $script:DryRun. Throws on non-zero exit unless -AllowFail; in
# that case the caller inspects $LASTEXITCODE.
function Invoke-Git {
    [CmdletBinding()]
    param(
        [Parameter(ValueFromRemainingArguments)][string[]]$GitArgs,
        [switch]$AllowFail
    )
    if ($script:DryRun) {
        Write-Dry ("git " + ($GitArgs -join ' '))
        $global:LASTEXITCODE = 0
        return
    }
    & git @GitArgs
    if ($LASTEXITCODE -ne 0 -and -not $AllowFail) {
        throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE)"
    }
}

# ----- one read-only-git helper -----
# Always runs (even in dry-run). Returns captured stdout as a single
# string, or as an array of lines with -Lines. Throws on non-zero exit
# unless -AllowFail.
function Get-Git {
    [CmdletBinding()]
    param(
        [Parameter(ValueFromRemainingArguments)][string[]]$GitArgs,
        [switch]$Lines,
        [switch]$AllowFail
    )
    $out = & git @GitArgs
    if ($LASTEXITCODE -ne 0 -and -not $AllowFail) {
        throw "git $($GitArgs -join ' ') failed (exit $LASTEXITCODE)"
    }
    if ($Lines) {
        if ($null -eq $out) { return @() }
        return ,@($out)
    }
    if ($null -eq $out) { return "" }
    if ($out -is [array]) { return ($out -join "`n") }
    return [string]$out
}

# ----- one non-git mutation helper -----
# Wraps Start-Process, Read-Host, file writes, etc. Dry-run prints the
# description; real run executes the scriptblock.
function Invoke-Action {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory, Position = 0)][string]$Description,
        [Parameter(Mandatory, Position = 1)][scriptblock]$Action
    )
    if ($script:DryRun) {
        Write-Dry $Description
        return
    }
    & $Action
}

# ----- pre-flight -----
$repoRoot = (Get-Git rev-parse --show-toplevel).Trim()
Push-Location $repoRoot
try {
    $gitDir = (Get-Git rev-parse --git-dir).Trim()
    if (-not [System.IO.Path]::IsPathRooted($gitDir)) {
        $gitDir = Join-Path $repoRoot $gitDir
    }
    $stateFile = Join-Path $gitDir "enterprise-merge-state.json"

    foreach ($r in @($EnterpriseRemote, $UpstreamRemote, $OriginRemote)) {
        # Stderr suppressed: we'll re-raise with a cleaner message.
        $null = & git remote get-url $r 2>$null
        if ($LASTEXITCODE -ne 0) {
            throw "Git remote '$r' not found."
        }
    }

    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw "gh (GitHub CLI) is required. Install: winget install --id GitHub.cli"
    }

    $entBranchLocal = "enterprise-$Branch"

    $porcelain = Get-Git -Lines status --porcelain
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
        $currentRef = (Get-Git rev-parse --abbrev-ref HEAD).Trim()
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
        Invoke-Action "open $PendingItemsUrl in default browser" {
            Start-Process $PendingItemsUrl | Out-Null
        }
        Invoke-Action "wait for user to confirm review of pending items" {
            $null = Read-Host "Press Enter after reviewing (Ctrl+C to abort)"
        }
    }

    # ----- step 2: fetch -----
    if (-not $Resume) {
        Write-Step "Fetching $UpstreamRemote and $EnterpriseRemote"
        Invoke-Git fetch $UpstreamRemote
        Invoke-Git fetch $EnterpriseRemote
    }

    # ----- step 3: checkout + pull -----
    if (-not $Resume) {
        Write-Step "Checking out $entBranchLocal"
        Invoke-Git checkout $entBranchLocal
        Invoke-Git pull --ff-only $EnterpriseRemote $entBranchLocal
    }

    # ----- step 4: merge -----
    if (-not $Resume) {
        $preMergeSha = (Get-Git rev-parse HEAD).Trim()
        Write-Step "Merging $UpstreamRemote/$Branch into $entBranchLocal"
        Write-Info "Pre-merge SHA: $preMergeSha"

        Invoke-Git -AllowFail merge --no-edit "$UpstreamRemote/$Branch"
        if ($LASTEXITCODE -eq 0) {
            $conflictFiles = @()
        } else {
            $conflictFiles = Get-Git -Lines diff --name-only --diff-filter=U
            if ($conflictFiles.Count -eq 0) {
                throw "git merge failed (exit $LASTEXITCODE) but no conflict files detected."
            }
        }

        $state = [PSCustomObject]@{
            branch      = $Branch
            started     = (Get-Date).ToUniversalTime().ToString("o")
            preMergeSha = $preMergeSha
            conflicts   = @($conflictFiles)
            prBranch    = $null
        }
        Invoke-Action "write merge state to $stateFile" {
            $utf8NoBom = New-Object System.Text.UTF8Encoding $false
            [System.IO.File]::WriteAllText($stateFile, ($state | ConvertTo-Json -Depth 5), $utf8NoBom)
        }

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
    Invoke-Action "run Update-EnterpriseL10n.ps1 -UpstreamRemote $UpstreamRemote" {
        & "$PSScriptRoot\Update-EnterpriseL10n.ps1" -UpstreamRemote $UpstreamRemote
    }
    if ($DryRun) {
        Write-Dry "check whether $l10nPath changed, and 'git add' + 'git commit' if so"
    } else {
        Get-Git -AllowFail diff --quiet -- $l10nPath | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Invoke-Git add -- $l10nPath
            Invoke-Git commit -m "Update enterprise-l10n-changesets.json revisions to upstream/main"
            Write-Done "Committed l10n revision update."
        } else {
            Write-Info "No l10n revision changes."
        }
    }

    # ----- step 6: release version swap -----
    $versionChanged = $false
    if ($Branch -eq "release") {
        $verPath     = Join-Path $repoRoot "browser/config/version.txt"
        $verDispPath = Join-Path $repoRoot "browser/config/version_display.txt"
        $ver         = (Get-Content $verPath -Raw).Trim()
        if ($ver -match '^(\d+)\.0\.(\d+)$') {
            $major   = $matches[1]
            $dot     = $matches[2]
            $newVer  = "$major.$dot.0"
            Write-Step "Release version pattern detected"
            Write-Info "version.txt:         $ver  ->  $newVer"
            $verDisp    = (Get-Content $verDispPath -Raw).Trim()
            $newVerDisp = $verDisp
            if ($verDisp -match '^(\d+)\.0\.(\d+)$') {
                $newVerDisp = "$($matches[1]).$($matches[2]).0"
                Write-Info "version_display.txt: $verDisp  ->  $newVerDisp"
            }
            if ($DryRun) {
                Write-Dry "would prompt for version swap; if confirmed, would write files and commit"
            } else {
                $ans = Read-Host "Apply swap? [y/N]"
                if ($ans -match '^[Yy]') {
                    Invoke-Action "write $newVer to $verPath" {
                        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
                        [System.IO.File]::WriteAllText($verPath, "$newVer`n", $utf8NoBom)
                    }
                    if ($newVerDisp -ne $verDisp) {
                        Invoke-Action "write $newVerDisp to $verDispPath" {
                            $utf8NoBom = New-Object System.Text.UTF8Encoding $false
                            [System.IO.File]::WriteAllText($verDispPath, "$newVerDisp`n", $utf8NoBom)
                        }
                        Invoke-Git add browser/config/version.txt browser/config/version_display.txt
                    } else {
                        Invoke-Git add browser/config/version.txt
                    }
                    Invoke-Git commit -m "Update version to $newVer"
                    $versionChanged = $true
                    Write-Done "Committed version swap."
                } else {
                    Write-Info "Skipped version swap."
                }
            }
        }
    }

    # ----- step 7: detect .taskcluster.yml change -----
    $tcChanged = $false
    if ($DryRun) {
        Write-Dry "check whether .taskcluster.yml changed in this merge"
    } else {
        $changedInMerge = Get-Git -Lines diff --name-only $state.preMergeSha HEAD
        $tcChanged = $changedInMerge -contains ".taskcluster.yml"
        if ($tcChanged) {
            Write-Warn2 ".taskcluster.yml changed in this merge -- ping relduty after the PR merges."
        }
    }

    # ----- step 8: main-only push -----
    if ($Branch -eq "main") {
        Write-Step "Pushing $UpstreamRemote/main -> ${EnterpriseRemote}:main"
        Invoke-Git push $EnterpriseRemote "$UpstreamRemote/main:main"
    }

    # ----- step 9: choose PR branch name (or reuse from state) -----
    if ($state.prBranch) {
        $prBranch = $state.prBranch
        Write-Step "Reusing PR branch from state: $prBranch"
    } else {
        $today  = (Get-Date).ToString("yyyyMMdd")
        $prefix = "$Branch-merge_$today"
        Write-Step "Determining next PR suffix for $prefix"
        $heads = Get-Git -Lines ls-remote --heads $OriginRemote "$prefix*"
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
        Invoke-Action "persist prBranch=$prBranch to state file" {
            $utf8NoBom = New-Object System.Text.UTF8Encoding $false
            [System.IO.File]::WriteAllText($stateFile, ($state | ConvertTo-Json -Depth 5), $utf8NoBom)
        }
    }

    # ----- step 10: push PR branch -----
    Write-Step "Pushing $entBranchLocal -> ${OriginRemote}:${prBranch}"
    Invoke-Git push $OriginRemote "${entBranchLocal}:${prBranch}"

    # ----- step 11: build PR body + open PR -----
    $originUrl = (Get-Git remote get-url $OriginRemote).Trim()
    if ($originUrl -notmatch 'github\.com[:/]([^/]+)/[^/]+?(?:\.git)?$') {
        throw "Could not parse origin owner from URL: $originUrl"
    }
    $originOwner = $matches[1]

    $suffix  = $prBranch -replace ("^" + [regex]::Escape($Branch) + "-merge_"), ""
    $prTitle = "Enterprise $Branch merge $suffix"

    $conflicts = @($state.conflicts)
    if ($conflicts.Count -gt 0) {
        $bullets       = ($conflicts | ForEach-Object { "*   $_" }) -join "`n"
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

    $bodyFile = Join-Path $env:TEMP "enterprise-merge-pr-body-$prBranch.md"
    Invoke-Action "write PR body to $bodyFile" {
        $utf8NoBom = New-Object System.Text.UTF8Encoding $false
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
    } else {
        Invoke-Action ("gh " + ($ghArgs -join ' ')) {
            Write-Step "Creating PR via gh"
            $prUrl = & gh @ghArgs
            if ($LASTEXITCODE -ne 0) {
                throw "gh pr create failed (exit $LASTEXITCODE). State file preserved at $stateFile; resolve and re-run with -Resume."
            }
            Write-Done "PR opened: $prUrl"
            Remove-Item $stateFile -ErrorAction SilentlyContinue
            Remove-Item $bodyFile -ErrorAction SilentlyContinue
        }
        if ($DryRun) {
            Write-Host "Body would have been:"
            Write-Host "-----"
            Write-Host $body
            Write-Host "-----"
        }
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
