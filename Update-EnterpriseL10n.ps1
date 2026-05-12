#requires -Version 5.1
<#
.SYNOPSIS
  Sync the `revision` field of every locale in
  browser/locales/enterprise-l10n-changesets.json to the value found for
  that same locale in upstream/main:browser/locales/l10n-changesets.json.

.DESCRIPTION
  Run from inside a Firefox Enterprise checkout (or any subdirectory).
  Mutates the enterprise l10n file in place. Preserves the original
  formatting (4-space indent, LF newlines, no BOM) and changes only the
  revision lines, mirroring the historical "Update enterprise-l10n-..."
  commits which produce minimal one-line-per-locale diffs.

  Exits 0 whether or not anything changed. Use the printed summary to
  decide whether to commit.

.PARAMETER UpstreamRemote
  Name of the git remote pointing at the canonical Firefox repo.
.PARAMETER UpstreamRef
  Ref on the upstream remote to read l10n-changesets.json from.
  Defaults to "main" since enterprise tracks upstream/main revisions for
  all three branches.
#>
[CmdletBinding()]
param(
    [string]$UpstreamRemote = "upstream",
    [string]$UpstreamRef    = "main"
)

$ErrorActionPreference = "Stop"

$repoRoot = (& git rev-parse --show-toplevel) 2>$null
if ($LASTEXITCODE -ne 0 -or -not $repoRoot) {
    throw "Not in a git repository."
}
$repoRoot = $repoRoot.Trim()

$entPath = Join-Path $repoRoot "browser/locales/enterprise-l10n-changesets.json"
if (-not (Test-Path $entPath)) {
    throw "Enterprise l10n file not found: $entPath"
}

$upstreamSpec = "${UpstreamRemote}/${UpstreamRef}:browser/locales/l10n-changesets.json"
$upstreamRaw  = & git show $upstreamSpec 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Could not read $upstreamSpec. Did you 'git fetch $UpstreamRemote'?"
}
$upstreamJson = ($upstreamRaw -join "`n") | ConvertFrom-Json

$entText  = Get-Content $entPath -Raw
$entData  = $entText | ConvertFrom-Json
$entLines = $entText -split "`r?`n"

$locales      = @($entData.PSObject.Properties.Name)
$localeIdx    = 0
$changedCount = 0

for ($i = 0; $i -lt $entLines.Count; $i++) {
    if ($entLines[$i] -match '^(\s+)"revision":\s+"([0-9a-fA-F]+)"(.*)$') {
        if ($localeIdx -ge $locales.Count) {
            throw "More 'revision' lines than locales in $entPath (file may be malformed)."
        }
        $locale = $locales[$localeIdx]
        $localeIdx++
        $indent = $matches[1]
        $oldRev = $matches[2]
        $suffix = $matches[3]
        $upstreamProp = $upstreamJson.PSObject.Properties[$locale]
        if (-not $upstreamProp) {
            Write-Warning "Locale '$locale' missing from $upstreamSpec; leaving revision unchanged."
            continue
        }
        $newRev = $upstreamJson.$locale.revision
        if (-not $newRev) {
            Write-Warning "Locale '$locale' has no revision in upstream file; skipping."
            continue
        }
        if ($newRev -ne $oldRev) {
            Write-Host ("  {0}: {1} -> {2}" -f $locale, $oldRev, $newRev)
            $entLines[$i] = "${indent}`"revision`": `"${newRev}`"${suffix}"
            $changedCount++
        }
    }
}

if ($localeIdx -ne $locales.Count) {
    Write-Warning "Found $localeIdx 'revision' lines but $($locales.Count) locales in JSON; file may be malformed."
}

if ($changedCount -eq 0) {
    Write-Host "No revision updates needed."
    return
}

$utf8NoBom = New-Object System.Text.UTF8Encoding $false
$content   = ($entLines -join "`n")
if (-not $content.EndsWith("`n")) { $content += "`n" }
[System.IO.File]::WriteAllText($entPath, $content, $utf8NoBom)

Write-Host "Updated $changedCount locale revision(s) in $entPath"
