param([switch]$SkipCodexModel)

$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$codexHome = [System.IO.Path]::GetFullPath(
    $(if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' })
)
$toolsDir = Join-Path $codexHome 'tools'
$globalAgents = Join-Path $codexHome 'AGENTS.md'
$bridgeSource = Join-Path $packageRoot 'claude_brain_mcp.py'
$bridgeTarget = Join-Path $toolsDir 'claude_brain_mcp.py'
$stamp = '# BEGIN codex-opus-share-package'
$endStamp = '# END codex-opus-share-package'

function Invoke-CodexCapture {
    param([string[]]$Arguments)
    $output = @(& $script:codexPath @Arguments 2>$null)
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0) {
        throw "Codex CLI command failed (exit code $exitCode). No command output was retained."
    }
    return ($output -join [Environment]::NewLine)
}

function Write-Utf8File {
    param([string]$Path, [string]$Content)
    $encoding = [System.Text.UTF8Encoding]::new($false)
    [System.IO.File]::WriteAllText($Path, $Content, $encoding)
}

function Move-FileIntoPlace {
    param([string]$Source, [string]$Destination)
    if (Test-Path -LiteralPath $Destination -PathType Leaf) {
        # File.Replace requires a valid backup path on some .NET/PowerShell
        # versions even though newer APIs document this argument as nullable.
        # Keep that transient backup beside the destination so replacement
        # remains same-volume and atomic, then remove it after success.
        $transientBackup = $Destination + '.' + [guid]::NewGuid().ToString('N') + '.replace-backup'
        try {
            [System.IO.File]::Replace($Source, $Destination, $transientBackup)
        } finally {
            Remove-Item -LiteralPath $transientBackup -Force -ErrorAction SilentlyContinue
        }
    } else {
        [System.IO.File]::Move($Source, $Destination)
    }
}

function Test-OwnedRegistration {
    param($Server)
    if (-not $Server) { return $false }
    $transport = $Server.transport
    $registeredArgs = @($transport.args)
    return (
        $transport.type -eq 'stdio' -and
        [string]::Equals([string]$transport.command, $script:pythonPath, [System.StringComparison]::OrdinalIgnoreCase) -and
        $registeredArgs.Count -eq (1 + $script:pythonPrefix.Count) -and
        [string]::Equals([string]$registeredArgs[-1], $script:bridgeTarget, [System.StringComparison]::OrdinalIgnoreCase) -and
        (($script:pythonPrefix.Count -eq 0) -or [string]::Equals([string]$registeredArgs[0], '-3', [System.StringComparison]::OrdinalIgnoreCase)) -and
        -not $transport.env -and
        @($transport.env_vars).Count -eq 0
    )
}

if (-not (Test-Path -LiteralPath $bridgeSource -PathType Leaf)) {
    throw 'Bridge source file is missing from the package.'
}

$python = $null
$pythonPrefix = @()
$pythonVersionText = ''
$pythonCandidates = @(
    (Get-Command python -ErrorAction SilentlyContinue),
    (Get-Command py -ErrorAction SilentlyContinue)
) | Where-Object { $_ -and $_.CommandType -eq 'Application' }
foreach ($candidate in $pythonCandidates) {
    $candidatePrefix = if ($candidate.Name -match '^py(?:\.exe)?$') { @('-3') } else { @() }
    # Avoid embedded quote characters: Windows PowerShell 5.1 strips them
    # while forwarding native arguments, which would corrupt the Python code.
    $candidateVersionText = (& $candidate.Source @candidatePrefix -c 'import sys; print(chr(46).join(str(x) for x in sys.version_info[:3]))' 2>$null | Out-String).Trim()
    $candidateVersionExit = $LASTEXITCODE
    if ($candidateVersionText) { $pythonVersionText = $candidateVersionText }
    if ($candidateVersionExit -eq 0 -and $candidateVersionText -match '^(?<major>\d+)\.(?<minor>\d+)\.(?<patch>\d+)$') {
        $parsedVersion = [version]::new(
            [int]$Matches.major,
            [int]$Matches.minor,
            [int]$Matches.patch
        )
        if ($parsedVersion -ge [version]'3.10') {
            $python = $candidate
            $pythonPrefix = @($candidatePrefix)
            break
        }
    }
}
if (-not $python) {
    $found = if ($pythonVersionText) { $pythonVersionText } else { 'unknown' }
    throw "Python 3.10 or newer is required; no supported executable was found (last detected version: $found). No project files were changed."
}
$pythonPath = $python.Source
$script:pythonPath = $pythonPath
$script:pythonPrefix = @($pythonPrefix)

$codex = Get-Command codex -ErrorAction SilentlyContinue
if (-not $codex -or @('Application', 'ExternalScript') -notcontains [string]$codex.CommandType) {
    throw 'Codex CLI executable was not found. No project files were changed.'
}
$script:codexPath = $codex.Source
$script:bridgeTarget = $bridgeTarget
$null = Invoke-CodexCapture -Arguments @('mcp', '--help')

# Query and parse MCP state before touching the user's rules or installed bridge.
# The JSON may contain local environment settings; it is only held in memory and
# is never written to the console, a backup, or a log.
$mcpJson = Invoke-CodexCapture -Arguments @('mcp', 'list', '--json')
try {
    $configuredServers = @($mcpJson | ConvertFrom-Json -ErrorAction Stop)
} catch {
    throw 'Codex MCP listing was not valid JSON. No project files were changed.'
}
$existingServer = @($configuredServers | Where-Object { $_.name -eq 'claude_brain' }) | Select-Object -First 1
if ($existingServer) {
    if (-not (Test-OwnedRegistration -Server $existingServer)) {
        throw 'An MCP named claude_brain already exists with a different configuration. It was left unchanged; inspect it and choose a deliberate migration before installing.'
    }
    if ($existingServer.enabled -eq $false) {
        throw 'The existing claude_brain MCP is disabled. It was left unchanged; enable or migrate it deliberately before installing.'
    }
}

# Validate markers and the bridge in temporary storage before writing user state.
$existing = if (Test-Path -LiteralPath $globalAgents -PathType Leaf) {
    [System.IO.File]::ReadAllText($globalAgents, [System.Text.Encoding]::UTF8)
} else { '' }
$startCount = [regex]::Matches($existing, [regex]::Escape($stamp)).Count
$endCount = [regex]::Matches($existing, [regex]::Escape($endStamp)).Count
if ($startCount -ne $endCount -or $startCount -gt 1) {
    throw 'Global AGENTS.md has unbalanced or duplicate package markers. It was left unchanged.'
}

$sourceRules = [System.IO.File]::ReadAllText((Join-Path $packageRoot 'AGENTS.md'), [System.Text.Encoding]::UTF8)
$projectDocMarker = '<!-- project-doc -->'
$markerIndex = $sourceRules.IndexOf($projectDocMarker, [System.StringComparison]::Ordinal)
if ($markerIndex -ge 0) {
    $afterMarker = $markerIndex + $projectDocMarker.Length
    if ($afterMarker -lt $sourceRules.Length -and $sourceRules[$afterMarker] -eq [char]13) { $afterMarker++ }
    if ($afterMarker -lt $sourceRules.Length -and $sourceRules[$afterMarker] -eq [char]10) { $afterMarker++ }
    $rules = $sourceRules.Substring($afterMarker).TrimEnd()
} else {
    # The current share package stores only rules in AGENTS.md; older wrappers
    # may append project-specific prose after an explicit boundary marker.
    $rules = $sourceRules.Trim()
}
$block = "$stamp`r`n$rules`r`n$endStamp"
if ($startCount -eq 1) {
    $pattern = '(?s)' + [regex]::Escape($stamp) + '.*?' + [regex]::Escape($endStamp)
    $updatedAgents = [regex]::Replace($existing, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($match) $block }, 1)
} else {
    if ($existing.Length -eq 0) {
        $separator = ''
    } elseif ($existing.EndsWith("`n") -or $existing.EndsWith("`r")) {
        $separator = "`r`n"
    } else {
        $separator = "`r`n`r`n"
    }
    $updatedAgents = $existing + $separator + $block + "`r`n"
}

$tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ('codex-opus-install-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
try {
    $tempBridge = Join-Path $tempRoot 'claude_brain_mcp.py'
    Copy-Item -LiteralPath $bridgeSource -Destination $tempBridge
    & $pythonPath @pythonPrefix -m py_compile $tempBridge
    if ($LASTEXITCODE -ne 0) { throw 'Bridge compilation failed during preflight. No user files were changed.' }
} finally {
    Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
}

$backupDir = Join-Path $codexHome ('backups\opus-share-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $codexHome, $toolsDir -Force | Out-Null
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null
$hadAgents = Test-Path -LiteralPath $globalAgents -PathType Leaf
$hadBridge = Test-Path -LiteralPath $bridgeTarget -PathType Leaf
if ($hadAgents) { Copy-Item -LiteralPath $globalAgents -Destination (Join-Path $backupDir 'AGENTS.md') }
if ($hadBridge) { Copy-Item -LiteralPath $bridgeTarget -Destination (Join-Path $backupDir 'claude_brain_mcp.py') }

New-Item -ItemType Directory -Path $codexHome, $toolsDir -Force | Out-Null
$tempAgents = Join-Path $codexHome ('.AGENTS.' + [guid]::NewGuid().ToString('N') + '.tmp')
$tempInstalledBridge = Join-Path $toolsDir ('.claude_brain_mcp.' + [guid]::NewGuid().ToString('N') + '.tmp')
$registrationAttempted = $false
try {
    Write-Utf8File -Path $tempAgents -Content $updatedAgents
    Copy-Item -LiteralPath $bridgeSource -Destination $tempInstalledBridge
    Move-FileIntoPlace -Source $tempAgents -Destination $globalAgents
    Move-FileIntoPlace -Source $tempInstalledBridge -Destination $bridgeTarget

    if (-not $existingServer) {
        $registrationAttempted = $true
        $addArguments = @('mcp', 'add', 'claude_brain', '--', $pythonPath) + $pythonPrefix + @($bridgeTarget)
        $null = Invoke-CodexCapture -Arguments $addArguments
        $updatedMcpJson = Invoke-CodexCapture -Arguments @('mcp', 'list', '--json')
        try {
            $updatedServers = @($updatedMcpJson | ConvertFrom-Json -ErrorAction Stop)
        } catch {
            throw 'Codex returned invalid MCP JSON after registration.'
        }
        $registeredServer = @($updatedServers | Where-Object { $_.name -eq 'claude_brain' }) | Select-Object -First 1
        if (-not (Test-OwnedRegistration -Server $registeredServer)) {
            throw 'Codex did not report the expected MCP registration after add.'
        }
    }
} catch {
    $failure = $_
    $mcpRollbackWarning = ''
    if ($registrationAttempted) {
        try {
            $rollbackMcpJson = Invoke-CodexCapture -Arguments @('mcp', 'list', '--json')
            $rollbackServers = @($rollbackMcpJson | ConvertFrom-Json -ErrorAction Stop)
            $partialRegistration = @($rollbackServers | Where-Object { $_.name -eq 'claude_brain' }) | Select-Object -First 1
            if (Test-OwnedRegistration -Server $partialRegistration) {
                $null = Invoke-CodexCapture -Arguments @('mcp', 'remove', 'claude_brain')
            } elseif ($partialRegistration) {
                $mcpRollbackWarning = ' A different MCP appeared during rollback and was left untouched; inspect `codex mcp list`.'
            }
        } catch {
            $mcpRollbackWarning = ' Codex MCP state could not be confirmed; inspect `codex mcp list` before retrying.'
        }
    }
    if ($hadAgents) {
        Copy-Item -LiteralPath (Join-Path $backupDir 'AGENTS.md') -Destination $globalAgents -Force
    } else {
        Remove-Item -LiteralPath $globalAgents -Force -ErrorAction SilentlyContinue
    }
    if ($hadBridge) {
        Copy-Item -LiteralPath (Join-Path $backupDir 'claude_brain_mcp.py') -Destination $bridgeTarget -Force
    } else {
        Remove-Item -LiteralPath $bridgeTarget -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $tempAgents, $tempInstalledBridge -Force -ErrorAction SilentlyContinue
    throw "Installation failed; local files were rolled back. Recovery copies: $backupDir. $($failure.Exception.Message)$mcpRollbackWarning"
}

if (-not $SkipCodexModel) { Write-Host 'Model settings are preserved; set model gpt-6.1-sol and reasoning effort in Codex if desired.' }
Write-Host 'Installed global rules and claude_brain MCP.'
Write-Host "Backup: $backupDir"
Write-Host 'Claude Code must be installed and authenticated separately; no API key was copied.'
Write-Host 'Restart Codex or create a new session to load the global MCP.'
