[CmdletBinding()]
param([switch]$Resume)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Distro = 'Yuki-Bocchi'
$InstallRoot = 'D:\Yuki-Bocchi'
$LegacyRoot = Join-Path $env:LOCALAPPDATA 'Yuki-Bocchi'
$BundleRoot = Join-Path $InstallRoot 'bundle'
$StatePath = Join-Path $InstallRoot 'deployment-state.json'
$Wsl = Join-Path $env:WINDIR 'System32\wsl.exe'
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function ConvertTo-NativeArgument([string]$Value) {
    # Windows CRT quoting, independent of PowerShell 5.1's legacy argument join.
    # Bash receives its literal embedded quotes, spaces and trailing backslashes.
    $Escaped = [regex]::Replace($Value, '(\\*)"', '$1$1\"')
    $Escaped = [regex]::Replace($Escaped, '(\\+)$', '$1$1')
    return '"' + $Escaped + '"'
}

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$Label = 'Command') {
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = $Executable
    $Info.Arguments = (@($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $Info.UseShellExecute = $false
    $Process = [Diagnostics.Process]::Start($Info)
    $Timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        while (-not $Process.WaitForExit(15000)) {
            Write-Host ("{0}: running, elapsed {1:mm\:ss}." -f $Label, $Timer.Elapsed) -ForegroundColor DarkCyan
        }
        if ($Process.ExitCode -ne 0) { throw "Command failed: $Executable (exit $($Process.ExitCode))." }
    } finally { $Process.Dispose() }
}

function Get-VerifiedDownload([string]$Label, [string]$Url, [string]$Path, [string]$Expected,
                              [int]$IdleSeconds = 120, [int]$MaximumSeconds = 3600) {
    if ((Test-Path -LiteralPath $Path) -and (Get-FileHash -LiteralPath $Path).Hash.ToLowerInvariant() -eq $Expected) {
        Write-Host "$Label download already verified; reusing it." -ForegroundColor Green
        return
    }
    $Temporary = $Path + '.download.' + [Guid]::NewGuid().ToString('N')
    $Response = $null; $InputStream = $null; $OutputStream = $null
    $Timer = [Diagnostics.Stopwatch]::StartNew(); $LastReport = -10.0
    Write-Host "Downloading $Label from $(([uri]$Url).Host); waiting for HTTPS response..." -ForegroundColor Cyan
    try {
        $Request = [Net.HttpWebRequest]::Create($Url)
        $Request.Timeout = $IdleSeconds * 1000
        $Request.ReadWriteTimeout = $IdleSeconds * 1000
        $Response = $Request.GetResponse()
        $InputStream = $Response.GetResponseStream()
        $OutputStream = [IO.File]::Open($Temporary, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::Read)
        $Buffer = New-Object byte[] 65536
        $Downloaded = 0L
        while (($Count = $InputStream.Read($Buffer, 0, $Buffer.Length)) -gt 0) {
            $OutputStream.Write($Buffer, 0, $Count); $Downloaded += $Count
            if ($Timer.Elapsed.TotalSeconds -gt $MaximumSeconds) { throw 'Download maximum duration exceeded.' }
            if ($Timer.Elapsed.TotalSeconds - $LastReport -ge 10) {
                $LastReport = $Timer.Elapsed.TotalSeconds
                $Rate = $Downloaded / [Math]::Max(1, $LastReport) / 1KB
                $Status = '{0:N1} MB, {1:N0} KB/s, elapsed {2:mm\:ss}' -f ($Downloaded / 1MB), $Rate, $Timer.Elapsed
                if ($Response.ContentLength -gt 0) {
                    $Percent = [Math]::Min(100, [int](100.0 * $Downloaded / $Response.ContentLength))
                    $Status += ', total {0:N1} MB ({1}%)' -f ($Response.ContentLength / 1MB), $Percent
                    Write-Progress -Activity "Downloading $Label" -Status $Status -PercentComplete $Percent
                }
                Write-Host "$Label download: $Status"
            }
        }
        $OutputStream.Dispose(); $OutputStream = $null
        if ((Get-FileHash -LiteralPath $Temporary).Hash.ToLowerInvariant() -ne $Expected) {
            throw 'Downloaded checksum does not match the pinned release.'
        }
        Move-Item -LiteralPath $Temporary -Destination $Path -Force
        Write-Host "$Label download and SHA256 passed." -ForegroundColor Green
    } catch {
        throw "$Label download failed ($($_.Exception.GetType().Name)). Check HTTPS access to $(([uri]$Url).Host) and retry. No data for $IdleSeconds seconds stops the download."
    } finally {
        if ($OutputStream) { $OutputStream.Dispose() }
        if ($InputStream) { $InputStream.Dispose() }
        if ($Response) { $Response.Dispose() }
        if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary }
        Write-Progress -Activity "Downloading $Label" -Completed
    }
}

function Install-WslCore([string]$Installer) {
    $Log = Join-Path $InstallRoot ('wsl-install-' + [Guid]::NewGuid().ToString('N') + '.log')
    $Arguments = @('/i', $Installer, '/passive', '/norestart', '/L*V!', $Log)
    Write-Host "Installing WSL: a progress window will open. Detailed log: $Log" -ForegroundColor Cyan
    $Process = Start-Process msiexec.exe -ArgumentList (($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ') -PassThru
    $Timer = [Diagnostics.Stopwatch]::StartNew()
    try {
        while (-not $Process.WaitForExit(15000)) {
            Write-Host ("WSL installer: running, elapsed {0:mm\:ss}. Log: {1}" -f $Timer.Elapsed, $Log)
        }
        if ($Process.ExitCode -eq 1618) { throw "Another Windows installation is running. Wait for it to finish, then retry. Log: $Log" }
        if ($Process.ExitCode -notin @(0, 3010)) { throw "WSL core installation failed (exit $($Process.ExitCode)). Log: $Log" }
        return $Process.ExitCode
    } finally { $Process.Dispose() }
}

function Test-WslCoreVersion {
    try {
        & $Wsl --version *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        # Inbox WSL can emit a redirected native error on PowerShell 5.1.
        # That expected unsupported query must enter the official MSI fallback.
        return $false
    }
}

function Test-WslMoveVersion {
    try {
        $Text = (& $Wsl --version 2>$null | Out-String) -replace "`0", ''
        $Version = [regex]::Match($Text, '\d+\.\d+\.\d+(?:\.\d+)?')
        return $LASTEXITCODE -eq 0 -and $Version.Success -and [version]$Version.Value -ge [version]'3.0.1'
    } catch { return $false }
}

function Protect-Directory([string]$Path) {
    $Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Invoke-Checked 'icacls.exe' @($Path, '/inheritance:r', '/grant:r', "*${Sid}:(OI)(CI)F", '*S-1-5-18:(OI)(CI)F', '/T', '/Q')
}

function Test-Bundle([string]$Root) {
    $Manifest = Get-Content -Raw -LiteralPath (Join-Path $Root 'manifest.json') -Encoding UTF8 | ConvertFrom-Json
    if ($Manifest.schema_version -ne 1 -or $Manifest.source_revision -notmatch '^[0-9a-f]{40}$' -or [string]$Manifest.bot_qq -notmatch '^[1-9][0-9]{4,19}$') {
        throw 'Invalid deployment manifest.'
    }
    $Prefix = [IO.Path]::GetFullPath($Root).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    foreach ($Entry in $Manifest.files.PSObject.Properties) {
        $Target = [IO.Path]::GetFullPath((Join-Path $Root $Entry.Name))
        if (-not $Target.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase) -or $Entry.Value -notmatch '^[0-9a-f]{64}$') {
            throw 'Invalid deployment payload path or checksum.'
        }
        if ((Get-FileHash -LiteralPath $Target -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Entry.Value) {
            throw "Deployment file checksum mismatch: $($Entry.Name). Extract the original ZIP again."
        }
    }
    return $Manifest
}

function Save-State($State) {
    $Temporary = $StatePath + '.' + [Guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText($Temporary, ($State | ConvertTo-Json), $Utf8)
        if (Test-Path -LiteralPath $StatePath) { [IO.File]::Replace($Temporary, $StatePath, [NullString]::Value) }
        else { [IO.File]::Move($Temporary, $StatePath) }
    } finally {
        if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary }
    }
}

function Copy-VerifiedPayload([string]$Source, [string]$Destination, $Manifest) {
    New-Item -ItemType Directory -Path $Destination -Force | Out-Null
    # A failed first copy can resume from the verified original package. Existing
    # payload modifications fail their hashes; they are never silently replaced.
    $Names = @($Manifest.files.PSObject.Properties.Name) + @('manifest.json')
    foreach ($Name in $Names) {
        $InputFile = Join-Path $Source $Name
        $OutputFile = Join-Path $Destination $Name
        if (Test-Path -LiteralPath $OutputFile) {
            $Current = (Get-FileHash -LiteralPath $OutputFile).Hash.ToLowerInvariant()
            if ((Get-FileHash -LiteralPath $InputFile).Hash.ToLowerInvariant() -eq $Current) { continue }
            # Only an exact previously verified distribution file may upgrade.
            # An interrupted update may contain a mixture of old and new files.
            $OldHash = $null
            if ($Name -eq 'manifest.json' -and $Manifest.PSObject.Properties['previous_manifest_sha256']) {
                $OldHash = $Manifest.previous_manifest_sha256
            } elseif ($Manifest.PSObject.Properties['previous_files']) {
                $Entry = $Manifest.previous_files.PSObject.Properties[$Name]
                if ($Entry) { $OldHash = $Entry.Value }
            }
            if ($Current -ne $OldHash) { throw "Existing deployment payload changed: $Name. Nothing was replaced." }
        }
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($OutputFile)) -Force | Out-Null
        $Temporary = $OutputFile + '.' + [Guid]::NewGuid().ToString('N')
        try {
            Copy-Item -LiteralPath $InputFile -Destination $Temporary
            if (Test-Path -LiteralPath $OutputFile) { [IO.File]::Replace($Temporary, $OutputFile, [NullString]::Value) }
            else { [IO.File]::Move($Temporary, $OutputFile) }
        } finally {
            if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary }
        }
    }
}

function Get-WslStoragePath {
    $Entries = @(Get-ChildItem 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss' -ErrorAction SilentlyContinue)
    foreach ($Entry in $Entries) {
        $Value = Get-ItemProperty -LiteralPath $Entry.PSPath
        if ($Value.DistributionName -eq $Distro) {
            return [IO.Path]::GetFullPath(([string]$Value.BasePath -replace '^\\\\\?\\', '')).TrimEnd('\')
        }
    }
    return $null
}

function Get-LegacyEntries([string]$Root) {
    if ((Get-Item -LiteralPath $Root -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Legacy reparse point refused.' }
    $Queue = [Collections.Generic.Queue[string]]::new()
    $Queue.Enqueue($Root)
    while ($Queue.Count -gt 0) {
        foreach ($Entry in Get-ChildItem -LiteralPath $Queue.Dequeue() -Force) {
            if ($Entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Legacy reparse point refused.' }
            $Entry
            if ($Entry.PSIsContainer) { $Queue.Enqueue($Entry.FullName) }
        }
    }
}

function Set-WslSwapOnDataDrive([string]$ConfigPath) {
    $Exists = Test-Path -LiteralPath $ConfigPath
    if ($Exists -and ((Get-Item -LiteralPath $ConfigPath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'WSL configuration link refused; existing settings were preserved.' }
    $Original = if ($Exists) { [IO.File]::ReadAllText($ConfigPath) } else { '' }
    $Newline = if ($Original.Contains("`r`n")) { "`r`n" } else { [Environment]::NewLine }
    $Lines = [Collections.Generic.List[string]]::new()
    foreach ($Line in ($Original -split '\r?\n')) { $Lines.Add($Line) }
    $Sections = @()
    for ($Index = 0; $Index -lt $Lines.Count; $Index++) { if ($Lines[$Index] -match '^\s*\[wsl2\]\s*(?:[;#].*)?$') { $Sections += $Index } }
    if ($Sections.Count -gt 1) { throw 'Multiple wsl2 sections are ambiguous; existing configuration was preserved.' }
    if ($Sections.Count -eq 0) { $Lines.Add('[wsl2]'); $Sections = @($Lines.Count - 1) }
    $Start = $Sections[0]; $End = $Lines.Count
    for ($Index = $Start + 1; $Index -lt $Lines.Count; $Index++) { if ($Lines[$Index] -match '^\s*\[') { $End = $Index; break } }
    $Swap = @()
    for ($Index = $Start + 1; $Index -lt $End; $Index++) { if ($Lines[$Index] -match '^\s*swapFile\s*=') { $Swap += $Index } }
    if ($Swap.Count -gt 1) { throw 'Multiple swapFile settings are ambiguous; existing configuration was preserved.' }
    $Desired = 'swapFile=' + $InstallRoot.Replace('\', '\\') + '\\wsl-swap.vhdx'
    if ($Swap.Count) { $Lines[$Swap[0]] = $Desired } else { $Lines.Insert($Start + 1, $Desired) }
    $Updated = $Lines -join $Newline
    if ($Updated -eq $Original) { return $false }
    $Temporary = $ConfigPath + '.yuki.' + [Guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText($Temporary, $Updated, $Utf8)
        if ($Exists) {
            if ([IO.File]::ReadAllText($ConfigPath) -ne $Original) { throw 'WSL settings changed concurrently; nothing was replaced.' }
            [IO.File]::Replace($Temporary, $ConfigPath, [NullString]::Value)
        } else { [IO.File]::Move($Temporary, $ConfigPath) }
    } finally { if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary } }
    Write-Host 'WSL swap is configured on D:. Other WSL settings were preserved; restart Windows to apply it.' -ForegroundColor Cyan
    return $true
}

function Copy-LegacyInstallation($State) {
    $Source = [string]$State.legacy_root
    if ($Source -ne $LegacyRoot) { throw 'Legacy root is outside the original installer directory.' }
    if (-not (Test-Path -LiteralPath $Source)) { return }
    $LegacyState = Get-Content -Raw -LiteralPath (Join-Path $Source 'deployment-state.json') | ConvertFrom-Json
    if ($LegacyState.bundle_id -ne $State.bundle_id) { throw 'Legacy ownership changed; migration stopped.' }
    # WSL owns its live VHD. Move it through WSL later, never copy a mounted disk.
    $Prefix = [IO.Path]::GetFullPath($Source).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $Entries = @(Get-LegacyEntries $Source)
    foreach ($File in ($Entries | Where-Object { -not $_.PSIsContainer })) {
        if ($File.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Legacy reparse point refused.' }
        $Relative = $File.FullName.Substring($Prefix.Length)
        $WindowsRelative = $Relative.Replace('/', '\')
        if ($WindowsRelative -eq 'deployment-state.json' -or $WindowsRelative -eq '.install.lock' -or $WindowsRelative.StartsWith('wsl\', [StringComparison]::OrdinalIgnoreCase)) { continue }
        $Target = Join-Path $InstallRoot $Relative
        # Bundle upgrades have their own exact-old/new hash checks below.
        if (Test-Path -LiteralPath $Target) {
            if ($WindowsRelative.StartsWith('bundle\', [StringComparison]::OrdinalIgnoreCase)) { continue }
            if ((Get-FileHash -LiteralPath $Target).Hash -ne (Get-FileHash -LiteralPath $File.FullName).Hash) { throw "Migration destination changed: $Relative" }
        } else {
            New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($Target)) -Force | Out-Null
            $Temporary = $Target + '.' + [Guid]::NewGuid().ToString('N')
            try {
                Copy-Item -LiteralPath $File.FullName -Destination $Temporary
                if ((Get-FileHash -LiteralPath $Temporary).Hash -ne (Get-FileHash -LiteralPath $File.FullName).Hash) { throw "Migration copy failed: $Relative" }
                Move-Item -LiteralPath $Temporary -Destination $Target
            } finally { if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary } }
        }
    }
    Write-Host 'Existing installer configuration copied to D:. Original C: files preserved until WSL location is verified.' -ForegroundColor Cyan
}

function Move-OwnedWsl($State) {
    $Owner = (& $Wsl --distribution $Distro --user root --exec cat /etc/yuki-deployment-id 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $Owner -ne $State.bundle_id) { throw 'WSL ownership differs. No disk was moved.' }
    $Storage = Get-WslStoragePath
    $ExpectedStorage = Join-Path $InstallRoot 'wsl'
    if ($Storage -eq $ExpectedStorage) { return }
    if (-not $State.PSObject.Properties['legacy_root'] -or $State.legacy_root -ne $LegacyRoot -or $Storage -ne (Join-Path $LegacyRoot 'wsl')) { throw 'WSL storage is outside the owned source/destination. Migration stopped.' }
    if (Test-Path -LiteralPath $ExpectedStorage) { throw 'A previous WSL move left a destination. It was preserved; automatic overwrite refused.' }
    Invoke-Wsl 'root' @('bash', '-c', 'if systemctl cat yuki-bocchi.service >/dev/null 2>&1; then systemctl stop yuki-bocchi; fi; if command -v docker >/dev/null 2>&1; then docker stop $(docker ps -q --filter label=com.docker.compose.project=yuki-bocchi-gateway) 2>/dev/null || true; fi')
    Invoke-Checked $Wsl @('--terminate', $Distro) 'Stopping the owned WSL'
    Invoke-Checked $Wsl @('--manage', $Distro, '--move', $ExpectedStorage) 'Moving the original WSL disk to D:'
    if ((Get-WslStoragePath) -ne $ExpectedStorage) { throw 'WSL did not confirm its D: storage path. C: data was preserved.' }
}

function Remove-VerifiedLegacyCopies($State, $Manifest) {
    if (-not $State.PSObject.Properties['legacy_root'] -or -not (Test-Path -LiteralPath $State.legacy_root)) { return }
    if ((Get-WslStoragePath) -ne (Join-Path $InstallRoot 'wsl')) { throw 'C: cleanup refused: WSL is not confirmed on D:.' }
    $Source = [string]$State.legacy_root
    if ($Source -ne $LegacyRoot) { throw 'Legacy cleanup root differs from the original installer directory.' }
    $Prefix = [IO.Path]::GetFullPath($Source).TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    $Entries = @(Get-LegacyEntries $Source)
    foreach ($File in ($Entries | Where-Object { -not $_.PSIsContainer })) {
        if ($File.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        $Relative = $File.FullName.Substring($Prefix.Length)
        $WindowsRelative = $Relative.Replace('/', '\')
        if ($WindowsRelative -eq '.install.lock' -or $WindowsRelative.StartsWith('wsl\', [StringComparison]::OrdinalIgnoreCase)) { continue }
        $Hash = (Get-FileHash -LiteralPath $File.FullName).Hash.ToLowerInvariant()
        $Target = Join-Path $InstallRoot $Relative
        $Safe = (Test-Path -LiteralPath $Target) -and (Get-FileHash -LiteralPath $Target).Hash.ToLowerInvariant() -eq $Hash
        if ($WindowsRelative -eq 'deployment-state.json') { $Safe = $State.PSObject.Properties['legacy_state_sha256'] -and $Hash -eq $State.legacy_state_sha256 }
        if (-not $Safe -and $WindowsRelative.StartsWith('bundle\', [StringComparison]::OrdinalIgnoreCase)) {
            $Name = $WindowsRelative.Substring(7).Replace('\', '/')
            if ($Name -eq 'manifest.json') { $Safe = $Manifest.PSObject.Properties['previous_manifest_sha256'] -and $Hash -eq $Manifest.previous_manifest_sha256 }
            elseif ($Manifest.PSObject.Properties['previous_files']) {
                $Old = $Manifest.previous_files.PSObject.Properties[$Name]
                $New = $Manifest.files.PSObject.Properties[$Name]
                $Safe = $Old -and $New -and $Hash -eq $Old.Value -and (Test-Path -LiteralPath $Target) -and (Get-FileHash -LiteralPath $Target).Hash.ToLowerInvariant() -eq $New.Value
            }
        }
        if ($Safe) { Remove-Item -LiteralPath $File.FullName }
    }
    foreach ($Directory in ($Entries | Where-Object { $_.PSIsContainer } | Sort-Object { $_.FullName.Length } -Descending)) {
        if (-not ($Directory.Attributes -band [IO.FileAttributes]::ReparsePoint) -and @(Get-ChildItem -LiteralPath $Directory.FullName -Force).Count -eq 0) { Remove-Item -LiteralPath $Directory.FullName }
    }
    if (@(Get-ChildItem -LiteralPath $Source -Force).Count -eq 0) { Remove-Item -LiteralPath $Source }
    else { Write-Host 'C: has additional or changed files; those files were preserved.' -ForegroundColor Yellow }
}

function Register-Resume {
    $RunOnce = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce'
    if (-not (Test-Path $RunOnce)) { New-Item $RunOnce -Force | Out-Null }
    $Script = Join-Path $BundleRoot 'Deploy-Yuki.ps1'
    $Command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$Script`" -Resume"
    New-ItemProperty -Path $RunOnce -Name 'YukiBocchiSetup' -Value $Command -PropertyType String -Force | Out-Null
}

function Invoke-Wsl([string]$User, [string[]]$Arguments) {
    Invoke-Checked $Wsl (@('--distribution', $Distro, '--user', $User, '--exec') + $Arguments)
}

$InstallLock = $null
try {
    if ([Environment]::Is64BitOperatingSystem -ne $true -or $env:PROCESSOR_ARCHITECTURE -notin @('AMD64', 'x86')) {
        throw 'This package requires an x64 Windows PC.'
    }
    $Build = [Environment]::OSVersion.Version.Build
    if ($Build -lt 19041) { throw 'Windows 10 build 19041 or Windows 11 is required; hardware virtualization must be enabled.' }
    if (-not [Environment]::Is64BitProcess) { throw 'Run the package from 64-bit Windows Explorer.' }
    $Manifest = Test-Bundle $PSScriptRoot
    $BotQQ = [string]$Manifest.bot_qq
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $Principal = New-Object Security.Principal.WindowsPrincipal($Identity)
    if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host 'Windows will request administrator permission to install WSL.' -ForegroundColor Cyan
        $Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`""
        if ($Resume) { $Arguments += ' -Resume' }
        $Elevated = Start-Process -FilePath (Join-Path $PSHOME 'powershell.exe') -ArgumentList $Arguments -Verb RunAs -Wait -PassThru
        exit $Elevated.ExitCode
    }

    if (-not (Test-Path -LiteralPath 'D:\')) { throw 'D: is unavailable. Installation stopped; C: is not used as a fallback.' }
    $Drive = [IO.DriveInfo]::new('D:\')
    if ($Drive.DriveFormat -ne 'NTFS') { throw 'D: must use NTFS for private credentials and WSL disk permissions.' }
    Write-Host "[1/9] Installation and data directory: $InstallRoot" -ForegroundColor Cyan
    if (-not (Test-Path -LiteralPath $InstallRoot) -and $Drive.AvailableFreeSpace -lt 25GB) { throw 'D: needs at least 25 GB free for the first build.' }
    # Refuse overlapping old/new launchers; UAC's waiting parent uses the same path.
    $OtherSetups = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object {
        $_.ProcessId -ne $PID -and $_.CommandLine -and $_.CommandLine.Contains('Deploy-Yuki.ps1') -and -not $_.CommandLine.Contains($PSCommandPath)
    })
    if ($OtherSetups.Count -gt 0) { throw 'An earlier Yuki installer is still open. Close its window before running this package; do not interrupt an active Windows MSI installation.' }

    if (Test-Path -LiteralPath $InstallRoot) {
        if (-not (Test-Path -LiteralPath $StatePath)) { throw "Existing $InstallRoot is not owned by this installer; nothing was overwritten." }
        $State = Get-Content -Raw -LiteralPath $StatePath -Encoding UTF8 | ConvertFrom-Json
        if ($State.bundle_id -ne $Manifest.bundle_id) { throw 'A different Yuki package owns this installation. Automatic replacement is refused.' }
    } else {
        $LegacyStatePath = Join-Path $LegacyRoot 'deployment-state.json'
        if (Test-Path -LiteralPath $LegacyRoot) {
            if (-not (Test-Path -LiteralPath $LegacyStatePath)) { throw 'The C: folder has no ownership state; it was left untouched.' }
            $State = Get-Content -Raw -LiteralPath $LegacyStatePath -Encoding UTF8 | ConvertFrom-Json
            if ($State.bundle_id -ne $Manifest.bundle_id) { throw 'The C: installation belongs to another package. Nothing was overwritten.' }
            $OldManifest = Join-Path $LegacyRoot 'bundle\manifest.json'
            if (-not $Manifest.PSObject.Properties['previous_manifest_sha256'] -or -not (Test-Path -LiteralPath $OldManifest) -or (Get-FileHash -LiteralPath $OldManifest).Hash.ToLowerInvariant() -ne $Manifest.previous_manifest_sha256) {
                throw 'The C: package does not match this verified repair package.'
            }
            $State | Add-Member -NotePropertyName legacy_root -NotePropertyValue $LegacyRoot -Force
            $State | Add-Member -NotePropertyName legacy_state_sha256 -NotePropertyValue (Get-FileHash -LiteralPath $LegacyStatePath).Hash.ToLowerInvariant() -Force
        } else {
            $Admin = ''
            while ($Admin -notmatch '^[1-9][0-9]{4,19}$' -or $Admin -eq $BotQQ) {
                $Admin = (Read-Host "Enter YOUR administrator QQ number (Bot: $BotQQ)").Trim()
            }
            $State = [pscustomobject]@{ bundle_id = $Manifest.bundle_id; admin_qq = $Admin; distro_imported = $false; completed = $false }
        }
        New-Item -ItemType Directory -Path $InstallRoot | Out-Null
        Protect-Directory $InstallRoot
        Save-State $State
    }
    Protect-Directory $InstallRoot
    try { $InstallLock = [IO.File]::Open((Join-Path $InstallRoot '.install.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
    catch { throw 'Another D: Yuki installer owns this installation. Wait for it to finish.' }
    if ($State.PSObject.Properties['legacy_root']) { Copy-LegacyInstallation $State }
    Copy-VerifiedPayload $PSScriptRoot $BundleRoot $Manifest
    $Manifest = Test-Bundle $BundleRoot
    $TemporaryRoot = Join-Path $InstallRoot 'temporary'
    New-Item -ItemType Directory -Path $TemporaryRoot -Force | Out-Null
    $env:TEMP = $TemporaryRoot; $env:TMP = $TemporaryRoot
    Register-Resume
    $SwapChanged = Set-WslSwapOnDataDrive (Join-Path $env:USERPROFILE '.wslconfig')

    Write-Host '[2/9] Enabling Windows WSL features...' -ForegroundColor Cyan
    $NeedsRestart = $SwapChanged
    foreach ($Feature in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
        $Status = Get-WindowsOptionalFeature -Online -FeatureName $Feature
        if ($Status.State -ne 'Enabled') {
            $Result = Enable-WindowsOptionalFeature -Online -FeatureName $Feature -All -NoRestart
            $NeedsRestart = $NeedsRestart -or $Result.RestartNeeded -or $Status.State -eq 'EnablePending'
        }
    }
    if ($NeedsRestart) {
        Register-Resume
        Write-Host 'WSL features / D: swap settings are prepared. Restart Windows and sign in; installation will resume.' -ForegroundColor Yellow
        Write-Host 'If it does not resume, double-click Deploy.cmd again. Existing progress is preserved.'
        exit 3010
    }
    if (-not (Test-Path -LiteralPath $Wsl)) { throw 'Windows WSL component is unavailable after installation; restart Windows.' }
    # Inbox WSL on a clean PC can lack --update/--web-download and systemd support.
    # Install the digest-pinned official MSI there; preserve an existing modern WSL.
    $NeedsMove = $State.PSObject.Properties['legacy_root'] -and (Get-WslStoragePath) -and (Get-WslStoragePath) -ne (Join-Path $InstallRoot 'wsl')
    $MoveSupported = $false
    if ($NeedsMove) { $MoveSupported = Test-WslMoveVersion }
    if (-not (Test-WslCoreVersion) -or ($NeedsMove -and -not $MoveSupported)) {
        $Installer = Join-Path $InstallRoot 'wsl-core-x64.msi'
        $Expected = '28b1a0d013640a2ac95898ea705fa186e5b4ff767a1c1b49257161bc106599c6'
        Write-Host '[3/9] Downloading and installing the official WSL core...' -ForegroundColor Cyan
        Get-VerifiedDownload 'WSL core' 'https://github.com/microsoft/WSL/releases/download/3.0.1/wsl.3.0.1.0.x64.msi' $Installer $Expected
        $Result = Install-WslCore $Installer
        Remove-Item -LiteralPath $Installer
        if ($Result -eq 3010) {
            Register-Resume
            Write-Host 'Restart Windows and sign in; Yuki installation will resume.' -ForegroundColor Yellow
            exit 3010
        }
    }
    Invoke-Checked $Wsl @('--version')
    Write-Host '[4/9] Preparing the owned WSL disk on D:...' -ForegroundColor Cyan
    $Names = @(& $Wsl --list --quiet) | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ }
    if ($LASTEXITCODE -ne 0) { throw 'Cannot list WSL distributions. Restart Windows and retry.' }
    if ($Distro -in $Names) {
        $Owner = (& $Wsl --distribution $Distro --user root --exec cat /etc/yuki-deployment-id 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $Owner -ne $State.bundle_id) {
            throw 'An unrelated Yuki-Bocchi WSL distribution exists. It was not changed.'
        }
        Move-OwnedWsl $State
    } else {
        $Rootfs = Join-Path $InstallRoot 'ubuntu-rootfs.tar.gz'
        $Expected = '8251e27ffff381a4af5f41dcb94d867de3e0d9774a9241908ab34555d99315ea'
        Get-VerifiedDownload 'Ubuntu WSL base' 'https://cloud-images.ubuntu.com/wsl/releases/24.04/20240423/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz' $Rootfs $Expected
        Invoke-Checked $Wsl @('--import', $Distro, (Join-Path $InstallRoot 'wsl'), $Rootfs, '--version', '2') 'Importing WSL into D:'
        # Stamp only this newly imported distro; never adopt an unmarked existing distro.
        Invoke-Wsl 'root' @('bash', '-c', 'printf "%s\n" "$1" > /etc/yuki-deployment-id', 'bash', $State.bundle_id)
        $State.distro_imported = $true
        Save-State $State
        Remove-Item -LiteralPath $Rootfs
    }
    if ((Get-WslStoragePath) -ne (Join-Path $InstallRoot 'wsl')) { throw 'The WSL virtual disk is not on D:. Deployment was not continued.' }

    if (-not $State.completed) {
        $LinuxBundle = (& $Wsl --distribution $Distro --user root --exec wslpath -a -u $BundleRoot | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $LinuxBundle.StartsWith('/')) { throw 'Cannot resolve the bundle path inside WSL.' }
        # A retry after a late failure can find the original Bot active. Drain it
        # before restarting this distro or running any migration, never start a second writer.
        Invoke-Wsl 'root' @('bash', '-c', 'if systemctl cat yuki-bocchi.service >/dev/null 2>&1; then systemctl stop yuki-bocchi; fi; mkdir -p /opt/yuki-bootstrap; cp -a "$1"/. /opt/yuki-bootstrap/; chmod 700 /opt/yuki-bootstrap', 'bash', $LinuxBundle)
        Write-Host '[5/9] Installing Linux dependencies; each command prints its output...' -ForegroundColor Cyan
        Invoke-Wsl 'root' @('bash', '/opt/yuki-bootstrap/bootstrap.sh', 'system')
        Invoke-Checked $Wsl @('--terminate', $Distro)
        Invoke-Wsl 'root' @('bash', '-c', 'for n in $(seq 1 60); do state=$(systemctl is-system-running 2>/dev/null || true); case "$state" in running|degraded) exit 0;; esac; sleep 2; done; echo "WSL systemd failed to become ready" >&2; exit 1')
        # Enabled systemd units restart with WSL. Stop an earlier partial instance again.
        Invoke-Wsl 'root' @('bash', '-c', 'if systemctl cat yuki-bocchi.service >/dev/null 2>&1; then systemctl stop yuki-bocchi; fi')
        Write-Host '[6/9] Building WebUI, Python dependencies and native Monty...' -ForegroundColor Cyan
        Invoke-Wsl 'yuki' @('bash', '/opt/yuki-bootstrap/bootstrap.sh', 'build')
        Invoke-Wsl 'root' @('bash', '/opt/yuki-bootstrap/bootstrap.sh', 'install-worker')
        Write-Host '[7/9] Testing native isolation, the real API and database migrations...' -ForegroundColor Cyan
        Invoke-Wsl 'yuki' @('bash', '/opt/yuki-bootstrap/bootstrap.sh', 'verify', $State.admin_qq)
    }
    Write-Host '[8/9] Starting Yuki and pulling the QQ gateway image...' -ForegroundColor Cyan
    Invoke-Wsl 'root' @('bash', '/opt/yuki-bootstrap/bootstrap.sh', 'start')

    foreach ($Url in @('http://127.0.0.1:18765/livez', 'http://127.0.0.1:6099/')) {
        $Ready = $false
        for ($Attempt = 0; $Attempt -lt 45; $Attempt++) {
            try { Invoke-WebRequest -UseBasicParsing -Uri $Url -TimeoutSec 4 | Out-Null; $Ready = $true; break }
            catch { Start-Sleep -Seconds 2 }
        }
        if (-not $Ready) { throw "Windows cannot reach $Url. Check WSL localhost forwarding or a port conflict; deployment is not marked ready." }
    }
    $CredentialFile = Join-Path $InstallRoot 'management.txt'
    $Credentials = & $Wsl --distribution $Distro --user yuki --exec cat /home/yuki/app/management.txt
    if ($LASTEXITCODE -ne 0) { throw 'Unable to read the generated management credentials.' }
    [IO.File]::WriteAllText($CredentialFile, ($Credentials -join "`r`n"), $Utf8)
    $Start = Join-Path $BundleRoot 'Start-Yuki.cmd'
    $Shortcut = Join-Path ([Environment]::GetFolderPath('Startup')) 'Yuki-Bocchi.lnk'
    $Shell = New-Object -ComObject WScript.Shell
    $Link = $Shell.CreateShortcut($Shortcut)
    $Link.TargetPath = $Start
    $Link.WorkingDirectory = $BundleRoot
    $Link.WindowStyle = 7
    $Link.Save()
    # Keep this distro alive independently of systemd services. There is at most
    # one keeper, and it has no Bot/API role. WSL otherwise idles out after the CLI exits.
    Start-Process -FilePath $Start -WindowStyle Hidden
    $State.completed = $true
    Save-State $State
    Test-Bundle $BundleRoot | Out-Null
    Remove-VerifiedLegacyCopies $State $Manifest
    Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'YukiBocchiSetup' -ErrorAction SilentlyContinue
    Write-Host "[9/9] Installation passed. Data: $InstallRoot. Open management.txt for login credentials." -ForegroundColor Green
    Write-Host "In NapCat, scan the QQ login QR using Bot account $BotQQ. Then chat privately with that QQ."
    Start-Process notepad.exe -ArgumentList "`"$CredentialFile`""
    Start-Process 'http://127.0.0.1:6099/'
    Start-Process 'http://127.0.0.1:18765/'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host 'Retry Deploy.cmd to continue. No database, QQ login, or existing deployment is deleted.'
    exit 1
} finally {
    if ($InstallLock) { $InstallLock.Dispose() }
}
