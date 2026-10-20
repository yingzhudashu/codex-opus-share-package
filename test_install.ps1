$ErrorActionPreference = 'Stop'

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw $Message }
}

$packageRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$installer = Join-Path $packageRoot 'install.ps1'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('codex-opus-installer-test-' + [guid]::NewGuid().ToString('N'))
$fakeBin = Join-Path $testRoot 'bin'
$codexHome = Join-Path $testRoot 'codex-home'
$statePath = Join-Path $testRoot 'mcp-state.json'
$logPath = Join-Path $testRoot 'codex-calls.log'
$originalPath = $env:PATH
$originalUserProfile = $env:USERPROFILE
$originalCodexHome = $env:CODEX_HOME
$originalState = $env:CODEX_STUB_STATE
$originalNextState = $env:CODEX_STUB_NEXT_STATE
$originalLog = $env:CODEX_STUB_LOG
$originalFailAdd = $env:CODEX_STUB_FAIL_ADD

try {
    New-Item -ItemType Directory -Path $fakeBin, $codexHome | Out-Null
    $env:CODEX_HOME = $codexHome
    $env:CODEX_STUB_STATE = $statePath
    $nextStatePath = Join-Path $testRoot 'mcp-state-after-add.json'
    $env:CODEX_STUB_NEXT_STATE = $nextStatePath
    $env:CODEX_STUB_LOG = $logPath
    $env:CODEX_STUB_FAIL_ADD = '0'
    $env:PATH = $fakeBin + [IO.Path]::PathSeparator + $originalPath

    $codexLines = @(
        '@echo off',
        'if /i "%1 %2"=="mcp --help" exit /b 0',
        'if /i "%1 %2 %3"=="mcp list --json" goto LIST',
        'if /i "%1 %2"=="mcp add" goto ADD',
        'if /i "%1 %2"=="mcp remove" goto REMOVE',
        'exit /b 2',
        ':LIST',
        'if exist "%CODEX_STUB_STATE%" (type "%CODEX_STUB_STATE%") else (echo [])',
        'exit /b 0',
        ':ADD',
        'echo add>>"%CODEX_STUB_LOG%"',
        'if exist "%CODEX_STUB_NEXT_STATE%" copy /Y "%CODEX_STUB_NEXT_STATE%" "%CODEX_STUB_STATE%" >nul',
        'if "%CODEX_STUB_FAIL_ADD%"=="1" exit /b 7',
        'exit /b 0',
        ':REMOVE',
        'echo remove>>"%CODEX_STUB_LOG%"',
        'echo []>"%CODEX_STUB_STATE%"',
        'exit /b 0'
    )
    Set-Content -LiteralPath (Join-Path $fakeBin 'codex.cmd') -Value $codexLines -Encoding Ascii

    $agentsPath = Join-Path $codexHome 'AGENTS.md'
    $configPath = Join-Path $codexHome 'config.toml'
    $toolsPath = Join-Path $codexHome 'tools'
    $bridgeTarget = Join-Path $toolsPath 'claude_brain_mcp.py'
    $originalAgents = "User-owned rules`r`n"
    $originalBridge = 'preexisting bridge bytes'
    $originalConfig = "# existing non-secret fixture`r`nmodel = 'keep'`r`n"
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
    $ownedServer = [ordered]@{
        name = 'claude_brain'
        enabled = $true
        transport = [ordered]@{
            type = 'stdio'
            command = $pythonPath
            args = @($bridgeTarget)
            env = $null
            env_vars = @()
        }
    }
    ConvertTo-Json -InputObject @($ownedServer) -Depth 8 | Set-Content -LiteralPath $nextStatePath -Encoding UTF8
    Set-Content -LiteralPath $agentsPath -Value $originalAgents -NoNewline -Encoding UTF8
    New-Item -ItemType Directory -Path $toolsPath | Out-Null
    Set-Content -LiteralPath $bridgeTarget -Value $originalBridge -NoNewline -Encoding Ascii
    Set-Content -LiteralPath $configPath -Value $originalConfig -NoNewline -Encoding UTF8
    $originalAgentsHash = (Get-FileHash -LiteralPath $agentsPath -Algorithm SHA256).Hash
    $originalBridgeHash = (Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash

    # Simulate a post-write registration failure; both files must be rolled back.
    $env:CODEX_STUB_FAIL_ADD = '1'
    $rollbackObserved = $false
    try { & $installer -SkipCodexModel } catch {
        $rollbackObserved = $_.Exception.Message -match 'rolled back'
    }
    Assert-True $rollbackObserved 'Expected a simulated registration failure and rollback.'
    Assert-True ((Get-FileHash -LiteralPath $agentsPath -Algorithm SHA256).Hash -eq $originalAgentsHash) 'AGENTS.md bytes were not restored exactly.'
    Assert-True ((Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash -eq $originalBridgeHash) 'Bridge bytes were not restored exactly.'
    Assert-True ((Get-Content -LiteralPath $logPath) -contains 'remove') 'Partial MCP registration was not cleaned up after an installation error.'

    # A fresh install must preserve unmanaged rules and install the exact source.
    $env:CODEX_STUB_FAIL_ADD = '0'
    Set-Content -LiteralPath $statePath -Value '[]' -Encoding UTF8
    & $installer -SkipCodexModel
    $installedAgents = [IO.File]::ReadAllText($agentsPath, [Text.Encoding]::UTF8)
    Assert-True ($installedAgents.Contains('User-owned rules')) 'Unmanaged global rules were lost.'
    Assert-True ($installedAgents.StartsWith($originalAgents, [StringComparison]::Ordinal)) 'Installer changed original global-rule bytes before appending its block.'
    Assert-True ([regex]::Matches($installedAgents, [regex]::Escape('# BEGIN codex-opus-share-package')).Count -eq 1) 'Managed rule marker is missing or duplicated.'
    Assert-True ((Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath (Join-Path $packageRoot 'claude_brain_mcp.py') -Algorithm SHA256).Hash) 'Installed bridge differs from package source.'
    Assert-True (@(Get-ChildItem -LiteralPath $codexHome -Recurse -Filter '*.replace-backup' -ErrorAction SilentlyContinue).Count -eq 0) 'A transient atomic-replacement backup was left behind.'
    Assert-True ([IO.File]::ReadAllText($configPath, [Text.Encoding]::UTF8) -eq $originalConfig) 'Installer directly changed config.toml.'
    $addCount = @(Get-Content -LiteralPath $logPath | Where-Object { $_ -eq 'add' }).Count
    Assert-True ($addCount -eq 2) 'Expected one failed and one successful registration attempt.'

    # Updating the managed block must preserve unrelated text before and after it.
    Add-Content -LiteralPath $agentsPath -Value 'User-owned trailing rules' -Encoding UTF8
    $beforeRepeatAgents = [IO.File]::ReadAllText($agentsPath, [Text.Encoding]::UTF8)
    $prefixBeforeRepeat = $beforeRepeatAgents.Substring(0, $beforeRepeatAgents.IndexOf('# BEGIN codex-opus-share-package', [StringComparison]::Ordinal))
    $suffixBeforeRepeat = $beforeRepeatAgents.Substring($beforeRepeatAgents.IndexOf('# END codex-opus-share-package', [StringComparison]::Ordinal) + '# END codex-opus-share-package'.Length)

    # Reinstalling an identical MCP must not remove or add it again.
    & $installer -SkipCodexModel
    $addCountAfterRepeat = @(Get-Content -LiteralPath $logPath | Where-Object { $_ -eq 'add' }).Count
    Assert-True ($addCountAfterRepeat -eq $addCount) 'An identical MCP registration was unnecessarily replaced.'
    $afterRepeatAgents = [IO.File]::ReadAllText($agentsPath, [Text.Encoding]::UTF8)
    Assert-True ($afterRepeatAgents.StartsWith($prefixBeforeRepeat, [StringComparison]::Ordinal)) 'Updating the managed block changed preceding user rules.'
    Assert-True ($afterRepeatAgents.EndsWith($suffixBeforeRepeat, [StringComparison]::Ordinal)) 'Updating the managed block changed trailing user rules.'
    Assert-True ($afterRepeatAgents.Contains('User-owned trailing rules')) 'Trailing user rules were lost during the update.'

    # A conflicting entry with the same name must be rejected before file writes.
    $beforeAgents = (Get-FileHash -LiteralPath $agentsPath -Algorithm SHA256).Hash
    $beforeBridge = (Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash
    $foreignServer = [ordered]@{
        name = 'claude_brain'
        enabled = $true
        transport = [ordered]@{ type = 'stdio'; command = 'custom-server.exe'; args = @('custom-server.py'); env = $null; env_vars = @() }
    }
    ConvertTo-Json -InputObject @($foreignServer) -Depth 8 | Set-Content -LiteralPath $statePath -Encoding UTF8
    $conflictObserved = $false
    try { & $installer -SkipCodexModel } catch { $conflictObserved = $_.Exception.Message -match 'different configuration' }
    Assert-True $conflictObserved 'Expected a conflicting MCP to be rejected.'
    Assert-True ((Get-FileHash -LiteralPath $agentsPath -Algorithm SHA256).Hash -eq $beforeAgents) 'Conflict rejection changed AGENTS.md.'
    Assert-True ((Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash -eq $beforeBridge) 'Conflict rejection changed the installed bridge.'

    # Unbalanced markers are rejected without altering either installed file.
    Set-Content -LiteralPath $statePath -Value '[]' -Encoding UTF8
    $validAgents = [IO.File]::ReadAllText($agentsPath, [Text.Encoding]::UTF8)
    [IO.File]::WriteAllText($agentsPath, $validAgents + "`r`n# BEGIN codex-opus-share-package`r`n", [Text.UTF8Encoding]::new($false))
    $beforeBadMarkerAgents = (Get-FileHash -LiteralPath $agentsPath -Algorithm SHA256).Hash
    $beforeBadMarkerBridge = (Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash
    $badMarkersRejected = $false
    try { & $installer -SkipCodexModel } catch { $badMarkersRejected = $_.Exception.Message -match 'unbalanced or duplicate' }
    Assert-True $badMarkersRejected 'Expected unbalanced package markers to be rejected.'
    Assert-True ((Get-FileHash -LiteralPath $agentsPath -Algorithm SHA256).Hash -eq $beforeBadMarkerAgents) 'Marker preflight changed AGENTS.md.'
    Assert-True ((Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash -eq $beforeBadMarkerBridge) 'Marker preflight changed the bridge.'
    [IO.File]::WriteAllText($agentsPath, $validAgents, [Text.UTF8Encoding]::new($false))

    # Unsupported Python interpreters must fail before any user-file change.
    $pythonStub = @('@echo off', 'echo 3.9.0', 'exit /b 0')
    Set-Content -LiteralPath (Join-Path $fakeBin 'python.cmd') -Value $pythonStub -Encoding Ascii
    Set-Content -LiteralPath (Join-Path $fakeBin 'py.cmd') -Value $pythonStub -Encoding Ascii
    $env:PATH = $fakeBin
    $beforePythonAgents = (Get-FileHash -LiteralPath $agentsPath -Algorithm SHA256).Hash
    $beforePythonBridge = (Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash
    $unsupportedPythonRejected = $false
    try { & $installer -SkipCodexModel } catch { $unsupportedPythonRejected = $_.Exception.Message -match 'Python 3.10 or newer' }
    Assert-True $unsupportedPythonRejected 'Expected Python 3.9 to be rejected during preflight.'
    Assert-True ((Get-FileHash -LiteralPath $agentsPath -Algorithm SHA256).Hash -eq $beforePythonAgents) 'Python preflight changed AGENTS.md.'
    Assert-True ((Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash -eq $beforePythonBridge) 'Python preflight changed the bridge.'

    # Use a supported fake interpreter for the no-prior-files scenarios so the
    # test remains independent of the host interpreter after the 3.9 preflight
    # case above.
    $supportedPythonStub = @(
        '@echo off',
        'if /i "%1"=="-c" echo 3.10.0',
        'exit /b 0'
    )
    Set-Content -LiteralPath (Join-Path $fakeBin 'python.cmd') -Value $supportedPythonStub -Encoding Ascii
    Remove-Item -LiteralPath (Join-Path $fakeBin 'py.cmd') -Force -ErrorAction SilentlyContinue
    $freshPythonPath = Join-Path $fakeBin 'python.cmd'
    $ownedServer.transport.command = $freshPythonPath
    ConvertTo-Json -InputObject @($ownedServer) -Depth 8 | Set-Content -LiteralPath $nextStatePath -Encoding UTF8

    # When there was no prior installation, a failed first install must remove
    # both new files and clean only the just-created registration.
    Remove-Item -LiteralPath $agentsPath, $bridgeTarget -Force
    Set-Content -LiteralPath $statePath -Value '[]' -Encoding UTF8
    $env:CODEX_STUB_FAIL_ADD = '1'
    $freshRollbackObserved = $false
    try { & $installer -SkipCodexModel } catch {
        $freshRollbackObserved = $_.Exception.Message -match 'rolled back'
    }
    Assert-True $freshRollbackObserved 'Expected a failed first install to be rolled back.'
    Assert-True (-not (Test-Path -LiteralPath $agentsPath)) 'Failed first install left a newly created AGENTS.md behind.'
    Assert-True (-not (Test-Path -LiteralPath $bridgeTarget)) 'Failed first install left a newly installed bridge behind.'
    Assert-True ((ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($statePath))).Count -eq 0) 'Failed first install left its MCP registration behind.'

    # A successful first install must also work when neither destination exists.
    $env:CODEX_STUB_FAIL_ADD = '0'
    & $installer -SkipCodexModel
    Assert-True (Test-Path -LiteralPath $agentsPath -PathType Leaf) 'First install did not create AGENTS.md.'
    Assert-True ((Get-FileHash -LiteralPath $bridgeTarget -Algorithm SHA256).Hash -eq (Get-FileHash -LiteralPath (Join-Path $packageRoot 'claude_brain_mcp.py') -Algorithm SHA256).Hash) 'First install did not create the exact bridge source.'

    # Exercise the Codex npm distribution shape surfaced by PowerShell 5.1:
    # Get-Command reports codex.ps1 as an ExternalScript, not an Application.
    Move-Item -LiteralPath (Join-Path $fakeBin 'codex.cmd') -Destination (Join-Path $fakeBin 'codex.cmd.disabled')
    $codexScript = @(
        'param([Parameter(ValueFromRemainingArguments=$true)][string[]]$CodexArgs)',
        '$ErrorActionPreference = ''Stop''',
        'if ($CodexArgs.Count -eq 2 -and $CodexArgs[0] -eq ''mcp'' -and $CodexArgs[1] -eq ''--help'') { return }',
        'if ($CodexArgs.Count -eq 3 -and $CodexArgs[0] -eq ''mcp'' -and $CodexArgs[1] -eq ''list'' -and $CodexArgs[2] -eq ''--json'') { if (Test-Path -LiteralPath $env:CODEX_STUB_STATE) { [IO.File]::ReadAllText($env:CODEX_STUB_STATE) } else { ''[]'' }; return }',
        'if ($CodexArgs.Count -ge 3 -and $CodexArgs[0] -eq ''mcp'' -and $CodexArgs[1] -eq ''add'') { Add-Content -LiteralPath $env:CODEX_STUB_LOG -Value ''add-ps1''; Copy-Item -LiteralPath $env:CODEX_STUB_NEXT_STATE -Destination $env:CODEX_STUB_STATE -Force; return }',
        'throw ''Unexpected Codex arguments in ExternalScript test.'''
    )
    Set-Content -LiteralPath (Join-Path $fakeBin 'codex.ps1') -Value $codexScript -Encoding UTF8
    $resolvedCodex = Get-Command codex -ErrorAction Stop
    Assert-True ($resolvedCodex.CommandType -eq 'ExternalScript') 'PowerShell did not resolve the codex.ps1 fixture as an ExternalScript.'
    Set-Content -LiteralPath $statePath -Value '[]' -Encoding UTF8
    $ps1AddCountBefore = @(Get-Content -LiteralPath $logPath | Where-Object { $_ -eq 'add-ps1' }).Count
    & $installer -SkipCodexModel
    $ps1AddCountAfter = @(Get-Content -LiteralPath $logPath | Where-Object { $_ -eq 'add-ps1' }).Count
    Assert-True ($ps1AddCountAfter -eq ($ps1AddCountBefore + 1)) 'codex.ps1 did not receive and complete the MCP add command.'
    Assert-True (@(ConvertFrom-Json -InputObject ([IO.File]::ReadAllText($statePath))).Count -eq 1) 'codex.ps1 MCP add did not register the expected server.'

    Write-Output 'Installer integration checks passed: rollback with and without prior files, unmanaged-rule preservation, fresh/repeat install, codex.ps1 ExternalScript registration, collision refusal, malformed-marker refusal, Python-version preflight, and atomic-replacement cleanup.'
} finally {
    $env:PATH = $originalPath
    $env:USERPROFILE = $originalUserProfile
    $env:CODEX_HOME = $originalCodexHome
    $env:CODEX_STUB_STATE = $originalState
    $env:CODEX_STUB_NEXT_STATE = $originalNextState
    $env:CODEX_STUB_LOG = $originalLog
    $env:CODEX_STUB_FAIL_ADD = $originalFailAdd
    Remove-Item -LiteralPath $testRoot -Recurse -Force -ErrorAction SilentlyContinue
}
