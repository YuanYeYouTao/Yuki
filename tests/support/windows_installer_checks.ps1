param([string]$Source, [string]$DownloadOrigin)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$PSNativeCommandArgumentPassing = 'Legacy'
$Tokens = $null; $Errors = $null
$Ast = [System.Management.Automation.Language.Parser]::ParseFile($Source, [ref]$Tokens, [ref]$Errors)
if ($Errors.Count) { throw ($Errors | Out-String) }
foreach ($Function in $Ast.FindAll({param($Node) $Node -is [System.Management.Automation.Language.FunctionDefinitionAst]}, $true)) {
    Invoke-Expression $Function.Extent.Text
}
$Root = Join-Path ([IO.Path]::GetTempPath()) ('yuki-installer-fixture-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $Root | Out-Null
$Utf8 = [Text.UTF8Encoding]::new($false)
$Messages = [Collections.Generic.List[string]]::new()
function Write-Host($Object, $ForegroundColor) { $script:Messages.Add([string]$Object) }
function Assert($Value, [string]$Message) { if (-not $Value) { throw $Message } }
function Must-Fail([scriptblock]$Operation, [string]$Message) {
    $Rejected = $false
    try { & $Operation } catch { $Rejected = $true }
    Assert $Rejected $Message
}
function Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path).Hash.ToLowerInvariant() }
try {
    if ([Environment]::OSVersion.Platform -ne [PlatformID]::Win32NT) {
        $NativeOutput = Join-Path $Root 'quoted path.txt'
        Invoke-Checked '/bin/bash' @('-c', 'printf "%s\n" "$1" > "$2"', 'bash', 'literal "$1" with spaces', $NativeOutput) 'Native quote fixture'
        Assert ((Get-Content -Raw $NativeOutput).TrimEnd("`n") -eq 'literal "$1" with spaces') 'Native quoting changed.'
        Must-Fail { Invoke-Checked '/usr/bin/false' @() } 'Native exit failure was ignored.'
    }
    $StatePath = Join-Path $Root 'state.json'
    Save-State ([pscustomobject]@{ value = 1 }); Save-State ([pscustomobject]@{ value = 2 })
    Assert ((Get-Content -Raw $StatePath | ConvertFrom-Json).value -eq 2) 'Atomic state replacement failed.'

    $Old = Join-Path $Root 'old'; $New = Join-Path $Root 'new'; $Stored = Join-Path $Root 'stored'
    New-Item -ItemType Directory $Old, $New | Out-Null
    [IO.File]::WriteAllText((Join-Path $Old 'script.ps1'), 'old installer', $Utf8)
    [IO.File]::WriteAllText((Join-Path $New 'script.ps1'), 'new installer', $Utf8)
    $OldManifest = [pscustomobject]@{ files = [pscustomobject]@{ 'script.ps1' = (Hash (Join-Path $Old 'script.ps1')) } }
    [IO.File]::WriteAllText((Join-Path $Old 'manifest.json'), ($OldManifest | ConvertTo-Json), $Utf8)
    $NewManifest = [pscustomobject]@{ files = [pscustomobject]@{ 'script.ps1' = (Hash (Join-Path $New 'script.ps1')) }; previous_files = $OldManifest.files; previous_manifest_sha256 = (Hash (Join-Path $Old 'manifest.json')) }
    [IO.File]::WriteAllText((Join-Path $New 'manifest.json'), ($NewManifest | ConvertTo-Json), $Utf8)
    Copy-VerifiedPayload $Old $Stored $OldManifest
    function Copy-Item([string]$LiteralPath, [string]$Destination) {
        if ([IO.Path]::GetFileName($LiteralPath) -eq 'manifest.json') { throw 'fixture interrupted manifest publication' }
        Microsoft.PowerShell.Management\Copy-Item -LiteralPath $LiteralPath -Destination $Destination
    }
    Must-Fail { Copy-VerifiedPayload $New $Stored $NewManifest } 'Interrupted upgrade did not stop.'
    Remove-Item Function:\Copy-Item
    Assert ((Get-Content -Raw (Join-Path $Stored 'script.ps1')) -eq 'new installer') 'Exact old script did not upgrade.'
    Copy-VerifiedPayload $New $Stored $NewManifest
    Copy-VerifiedPayload $New $Stored $NewManifest
    Assert ((Hash (Join-Path $Stored 'manifest.json')) -eq (Hash (Join-Path $New 'manifest.json'))) 'Upgrade retry failed.'
    [IO.File]::WriteAllText((Join-Path $Stored 'script.ps1'), 'operator edit', $Utf8)
    Must-Fail { Copy-VerifiedPayload $New $Stored $NewManifest } 'Operator script was overwritten.'
    Assert ((Get-Content -Raw (Join-Path $Stored 'script.ps1')) -eq 'operator edit') 'Operator edit was lost.'

    $InstallRoot = Join-Path $Root 'D-data'; $LegacyRoot = Join-Path $Root 'C-data'
    New-Item -ItemType Directory $InstallRoot, $LegacyRoot | Out-Null
    $WslConfig = Join-Path $Root '.wslconfig'
    $OriginalSettings = "# operator comments`r`n[wsl2]`r`nmemory=4GB`r`nswapFile=C:\\old\\swap.vhdx`r`n[experimental]`r`nautoMemoryReclaim=gradual`r`n"
    [IO.File]::WriteAllText($WslConfig, $OriginalSettings, $Utf8)
    Assert (Set-WslSwapOnDataDrive $WslConfig) 'Existing swap did not change to the requested drive.'
    $ChangedSettings = [IO.File]::ReadAllText($WslConfig)
    Assert ($ChangedSettings.Contains("# operator comments`r`n[wsl2]`r`nmemory=4GB") -and $ChangedSettings.Contains("[experimental]`r`nautoMemoryReclaim=gradual")) 'Unrelated WSL configuration changed.'
    Assert (-not (Set-WslSwapOnDataDrive $WslConfig)) 'Swap setup was not idempotent.'
    $FreshConfig = Join-Path $Root 'fresh.wslconfig'
    Assert (Set-WslSwapOnDataDrive $FreshConfig) 'New WSL swap config was not created.'
    Assert (-not (Set-WslSwapOnDataDrive $FreshConfig)) 'New WSL swap config did not remain stable.'
    [IO.File]::WriteAllText($WslConfig, "[wsl2]`nswapFile=one`nswapFile=two", $Utf8)
    Must-Fail { Set-WslSwapOnDataDrive $WslConfig } 'Duplicate swap setting was rewritten.'
    Assert ([IO.File]::ReadAllText($WslConfig).Contains('swapFile=two')) 'Rejected WSL config was changed.'
    $State = [pscustomobject]@{ bundle_id = 'fixture-owner'; legacy_root = $LegacyRoot; admin_qq = '123456'; completed = $false }
    [IO.File]::WriteAllText((Join-Path $LegacyRoot 'deployment-state.json'), ($State | ConvertTo-Json), $Utf8)
    $State | Add-Member legacy_state_sha256 (Hash (Join-Path $LegacyRoot 'deployment-state.json'))
    New-Item -ItemType Directory (Join-Path $LegacyRoot 'wsl') | Out-Null
    [IO.File]::WriteAllText((Join-Path $LegacyRoot 'wsl/ext4.vhdx'), 'live disk must not be copied by Windows tools', $Utf8)
    [IO.File]::WriteAllText((Join-Path $LegacyRoot 'management.txt'), 'private fixture credential', $Utf8)
    Copy-LegacyInstallation $State
    Assert (Test-Path (Join-Path $InstallRoot 'management.txt')) 'Private file did not migrate.'
    Assert (-not (Test-Path (Join-Path $InstallRoot 'wsl'))) 'Live WSL disk was copied.'
    [IO.File]::WriteAllText((Join-Path $LegacyRoot 'retry.txt'), 'migration retry fixture', $Utf8)
    function Copy-Item([string]$LiteralPath, [string]$Destination) {
        Microsoft.PowerShell.Management\Copy-Item -LiteralPath $LiteralPath -Destination $Destination
        if ([IO.Path]::GetFileName($LiteralPath) -eq 'retry.txt') { throw 'fixture interrupted migration copy' }
    }
    Must-Fail { Copy-LegacyInstallation $State } 'Interrupted migration was accepted.'
    Assert (-not (Test-Path (Join-Path $InstallRoot 'retry.txt'))) 'Interrupted migration published a partial file.'
    Assert (@(Get-ChildItem $InstallRoot -Filter 'retry.txt.*').Count -eq 0) 'Interrupted migration left temporary content.'
    Remove-Item Function:\Copy-Item
    Copy-LegacyInstallation $State
    Assert ((Get-Content -Raw (Join-Path $InstallRoot 'retry.txt')) -eq 'migration retry fixture') 'Migration copy could not resume.'
    New-Item -ItemType SymbolicLink -Path (Join-Path $LegacyRoot 'unsafe-link') -Target (Join-Path $Root 'outside') | Out-Null
    Must-Fail { Copy-LegacyInstallation $State } 'Legacy link was followed.'
    Remove-Item -LiteralPath (Join-Path $LegacyRoot 'unsafe-link')
    [IO.File]::WriteAllText((Join-Path $InstallRoot 'management.txt'), 'operator edit', $Utf8)
    Must-Fail { Copy-LegacyInstallation $State } 'Changed destination was overwritten.'
    [IO.File]::WriteAllText((Join-Path $InstallRoot 'management.txt'), 'private fixture credential', $Utf8)
    [IO.File]::WriteAllText((Join-Path $LegacyRoot '.hidden'), 'hidden file retained', $Utf8)
    Copy-LegacyInstallation $State
    Assert (Test-Path (Join-Path $InstallRoot '.hidden')) 'Hidden file did not migrate.'
    [IO.File]::WriteAllText((Join-Path $LegacyRoot '.hidden'), 'late source edit', $Utf8)

    $Distro = 'Yuki-Bocchi'; $Wsl = 'Owner-Stub'; $Calls = [Collections.Generic.List[string]]::new()
    function Owner-Stub { $global:LASTEXITCODE = 0; return 'WSL version: 2.7.7.0' }
    Assert (-not (Test-WslMoveVersion)) 'Old WSL move version was accepted.'
    function Owner-Stub { $global:LASTEXITCODE = 0; return 'WSL version: 3.0.1.0' }
    Assert (Test-WslMoveVersion) 'Pinned WSL move version was rejected.'
    function Owner-Stub { $global:LASTEXITCODE = 0; return 'unknown version' }
    Assert (-not (Test-WslMoveVersion)) 'Unknown WSL version was accepted.'
    function Owner-Stub { $global:LASTEXITCODE = 0; return 'fixture-owner' }
    $script:Storage = Join-Path $LegacyRoot 'wsl'
    function Get-WslStoragePath { return $script:Storage }
    function Invoke-Wsl($User, $Arguments) { $script:Calls.Add('stop owned services') }
    function Invoke-Checked($Executable, $Arguments, $Label) {
        $script:Calls.Add(($Arguments -join ' '))
        if ($Arguments[0] -eq '--manage') { $script:Storage = $Arguments[-1] }
    }
    Move-OwnedWsl $State
    Assert ($Calls.Count -eq 3 -and $Calls[2].Contains('--manage Yuki-Bocchi --move')) 'Owned move used wrong command or order.'
    $Count = $Calls.Count; Move-OwnedWsl $State
    Assert ($Calls.Count -eq $Count) 'Move retry repeated the operation.'
    $script:Storage = Join-Path $Root 'foreign-disk'
    Must-Fail { Move-OwnedWsl $State } 'Unowned storage was moved.'
    Assert ($Calls.Count -eq $Count) 'Rejected storage dispatched commands.'
    $script:Storage = Join-Path $InstallRoot 'wsl'
    function Owner-Stub { $global:LASTEXITCODE = 0; return 'different-owner' }
    Must-Fail { Move-OwnedWsl $State } 'Different WSL owner was adopted.'
    function Owner-Stub { $global:LASTEXITCODE = 0; return 'fixture-owner' }
    [IO.File]::WriteAllText((Join-Path $LegacyRoot '.install.lock'), 'unknown legacy lock is preserved', $Utf8)
    $Lock = [IO.File]::Open((Join-Path $InstallRoot '.install.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try { Remove-VerifiedLegacyCopies $State $NewManifest } finally { $Lock.Dispose() }
    Assert (Test-Path (Join-Path $LegacyRoot '.install.lock')) 'Unknown legacy lock was removed.'
    Remove-VerifiedLegacyCopies $State $NewManifest
    Assert (-not (Test-Path (Join-Path $LegacyRoot 'management.txt'))) 'Verified migrated duplicate was not cleaned.'
    Assert ((Get-Content -Raw (Join-Path $LegacyRoot '.hidden')) -eq 'late source edit') 'Late source edit was deleted.'
    Assert (Test-Path (Join-Path $LegacyRoot 'wsl/ext4.vhdx')) 'Cleanup touched the old live disk.'
    $script:Storage = Join-Path $LegacyRoot 'wsl'
    Must-Fail { Remove-VerifiedLegacyCopies $State $NewManifest } 'Cleanup ran before D: storage verification.'

    $MsiExit = 3010; $MsiArguments = ''
    function Start-Process([string]$FilePath, [string]$ArgumentList, [switch]$PassThru) {
        $script:MsiArguments = $ArgumentList
        $Fake = [pscustomobject]@{ ExitCode = $script:MsiExit; polls = 0; disposed = $false }
        $Fake | Add-Member ScriptMethod WaitForExit { param($Milliseconds) $this.polls++; return $this.polls -gt 1 }
        $Fake | Add-Member ScriptMethod Dispose { $this.disposed = $true }
        return $Fake
    }
    Assert ((Install-WslCore 'fixture path.msi') -eq 3010) 'MSI reboot code was lost.'
    Assert ($MsiArguments.Contains('"/passive"') -and $MsiArguments.Contains('"/L*V!"') -and $MsiArguments.Contains('D-data')) 'MSI progress/log arguments missing.'
    Assert (@($Messages | Where-Object { $_.StartsWith('WSL installer: running') }).Count -gt 0) 'MSI heartbeat missing.'
    $MsiExit = 1618; Must-Fail { Install-WslCore 'fixture.msi' } 'Concurrent MSI result was ignored.'
    $MsiExit = 1603; Must-Fail { Install-WslCore 'fixture.msi' } 'Failed MSI result was accepted.'

    if ($DownloadOrigin) {
        $Payload = [Text.Encoding]::UTF8.GetBytes('download fixture')
        $Digest = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash($Payload)).Replace('-', '').ToLowerInvariant()
        $Destination = Join-Path $Root 'download.msi'
        Get-VerifiedDownload 'fixture' ($DownloadOrigin + '/good') $Destination $Digest 1 10
        Assert ((Hash $Destination) -eq $Digest) 'Real download checksum failed.'
        Get-VerifiedDownload 'fixture' ($DownloadOrigin + '/missing') $Destination $Digest 1 10
        [IO.File]::WriteAllText($Destination, 'old partial artifact', $Utf8)
        foreach ($Endpoint in @('/wrong', '/stall-header', '/stall-body', '/slow')) {
            Must-Fail { Get-VerifiedDownload 'fixture' ($DownloadOrigin + $Endpoint) $Destination $Digest 1 1 } "Download $Endpoint did not fail."
            Assert ((Get-Content -Raw $Destination) -eq 'old partial artifact') 'Failed download overwrote the original artifact.'
            Assert (@(Get-ChildItem $Root -Filter '*.download.*').Count -eq 0) 'Failed download leaked temporary content.'
        }
        Assert (@($Messages | Where-Object { $_.StartsWith('fixture download:') }).Count -gt 0) 'Download size/rate progress missing.'
    }
    'PowerShell installer regressions passed: atomic upgrade/retry, preserved edits, C-to-D files, D: swap/unrelated settings/idempotency, no VHD copy, owned WSL move/idempotency, guarded cleanup, MSI progress/logs/errors, real HTTP progress/checksum/idle/total timeout.'
} finally { Remove-Item -LiteralPath $Root -Recurse -Force }
