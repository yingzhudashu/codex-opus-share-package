param([switch]$SkipCodexModel)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$codexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$claudeHome = Join-Path $env:USERPROFILE '.claude'
$toolsDir = Join-Path $codexHome 'tools'
$globalAgents = Join-Path $codexHome 'AGENTS.md'
$configToml = Join-Path $codexHome 'config.toml'
$bridgeSource = Join-Path $packageRoot 'claude_brain_mcp.py'
$bridgeTarget = Join-Path $toolsDir 'claude_brain_mcp.py'
$stamp = '# BEGIN codex-opus-share-package'
$endStamp = '# END codex-opus-share-package'

New-Item -ItemType Directory -Force -Path $codexHome, $toolsDir | Out-Null
$backupDir = Join-Path $codexHome ('backups\opus-share-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
foreach ($path in @($globalAgents, $configToml)) { if (Test-Path $path) { Copy-Item $path $backupDir } }

$rules = Get-Content (Join-Path $packageRoot 'AGENTS.md') -Raw
$rules = $rules -replace '(?s)^.*?<!-- project-doc -->\r?\n', ''
$block = "$stamp`r`n$rules`r`n$endStamp"
$existing = if (Test-Path $globalAgents) { Get-Content $globalAgents -Raw } else { '' }
if ($existing -match [regex]::Escape($stamp)) {
    $existing = [regex]::Replace($existing, '(?s)\# BEGIN codex-opus-share-package.*?\# END codex-opus-share-package', [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $block })
} else {
    $existing = ($existing.TrimEnd() + "`r`n`r`n" + $block + "`r`n")
}
Set-Content -Path $globalAgents -Value $existing -Encoding UTF8
Copy-Item $bridgeSource $bridgeTarget -Force

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { throw 'Python 3 is required for the claude_brain bridge.' }
& $python.Source -m py_compile $bridgeTarget
if (-not $SkipCodexModel) { Write-Host 'Model settings are preserved; set model gpt-6.1-sol and reasoning effort in Codex if desired.' }
codex mcp remove claude_brain 2>$null | Out-Null
codex mcp add claude_brain -- $python.Source $bridgeTarget | Out-Null

Write-Host "Installed global rules and claude_brain MCP."
Write-Host "Backup: $backupDir"
Write-Host "Claude Code must be installed and authenticated separately; no API key was copied."
Write-Host "Restart Codex or create a new session to load the global MCP."

