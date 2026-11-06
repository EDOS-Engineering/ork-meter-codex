# Ork Meter for Codex: install and pair in one line (Windows PowerShell).
#
#   irm https://raw.githubusercontent.com/EDOS-Engineering/ork-meter-codex/main/install.ps1 | iex
#
# It adds this marketplace and the plugin to Codex with Codex's own commands,
# then runs the installed plugin's setup, which installs the release pinned in
# release.json (verified by SHA-256), pairs this computer in your browser and
# turns on capture for every Codex session. Nothing here needs administrator
# rights.
$ErrorActionPreference = 'Stop'

if (-not (Get-Command codex -ErrorAction SilentlyContinue)) {
    Write-Error 'Ork Meter for Codex needs the Codex CLI first: https://developers.openai.com/codex/cli'
    return
}

$python = $null
foreach ($candidate in @(@('py', '-3'), @('python3'), @('python'))) {
    if (Get-Command $candidate[0] -ErrorAction SilentlyContinue) {
        $arguments = @($candidate | Select-Object -Skip 1)
        & $candidate[0] @arguments -c 'import sys; sys.exit(sys.version_info < (3, 8))' 2>$null
        if ($LASTEXITCODE -eq 0) { $python = $candidate; break }
    }
}
if (-not $python) {
    Write-Error 'Ork Meter for Codex needs Python 3. Install it with: winget install Python.Python.3.13'
    return
}
$pythonArgs = @($python | Select-Object -Skip 1)

Write-Host 'Adding the Ork Meter marketplace and plugin to Codex...'
codex plugin marketplace add EDOS-Engineering/ork-meter-codex | Out-Host
if ($LASTEXITCODE -ne 0) { Write-Error 'codex plugin marketplace add failed'; return }
codex plugin marketplace upgrade edos-codex | Out-Host
$installed = codex plugin add ork-meter-codex@edos-codex --json | Out-String
if ($LASTEXITCODE -ne 0) { Write-Error 'codex plugin add failed'; return }

# The installed plugin's root comes from Codex itself; never guess a version.
$bootstrap = Join-Path (Join-Path ($installed | ConvertFrom-Json).installedPath 'scripts') 'ork_codex.py'
if (-not (Test-Path -LiteralPath $bootstrap -PathType Leaf)) {
    Write-Error 'Codex did not report where it installed the plugin'
    return
}

& $python[0] @pythonArgs -I -B $bootstrap setup @args
