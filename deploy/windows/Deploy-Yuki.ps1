[CmdletBinding()]
param([switch]$Resume)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$Distro = 'Yuki-Bocchi'
$InstallRoot = Join-Path $env:LOCALAPPDATA 'Yuki-Bocchi'
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

function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    $Info = New-Object System.Diagnostics.ProcessStartInfo
    $Info.FileName = $Executable
    $Info.Arguments = (@($Arguments | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $Info.UseShellExecute = $false
    $Process = [Diagnostics.Process]::Start($Info)
    try {
        $Process.WaitForExit()
        if ($Process.ExitCode -ne 0) { throw "Command failed: $Executable (exit $($Process.ExitCode))." }
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

function Protect-Directory([string]$Path) {
    $Sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    Invoke-Checked 'icacls.exe' @($Path, '/inheritance:r', '/grant:r', "*${Sid}:(OI)(CI)F", '*S-1-5-18:(OI)(CI)F', '/T', '/Q')
}

function Test-Bundle([string]$Root) {
    $Manifest = Get-Content -Raw -LiteralPath (Join-Path $Root 'manifest.json') -Encoding UTF8 | ConvertFrom-Json
    if ($Manifest.schema_version -ne 1 -or $Manifest.source_revision -notmatch '^[0-9a-f]{40}$' -or [string]$Manifest.bot_qq -notmatch '^[1-9][0-9]{4,19}$') {
        throw 'Invalid deployment manifest.'
    }
    $Prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
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
        Move-Item -LiteralPath $Temporary -Destination $StatePath -Force
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
            if ((Get-FileHash -LiteralPath $InputFile).Hash -ne (Get-FileHash -LiteralPath $OutputFile).Hash) {
                throw "Existing deployment payload changed: $Name."
            }
            continue
        }
        New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($OutputFile)) -Force | Out-Null
        $Temporary = $OutputFile + '.' + [Guid]::NewGuid().ToString('N')
        try {
            Copy-Item -LiteralPath $InputFile -Destination $Temporary
            Move-Item -LiteralPath $Temporary -Destination $OutputFile
        } finally {
            if (Test-Path -LiteralPath $Temporary) { Remove-Item -LiteralPath $Temporary }
        }
    }
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

    if (Test-Path -LiteralPath $InstallRoot) {
        if (-not (Test-Path -LiteralPath $StatePath)) { throw "Existing $InstallRoot is not owned by this installer; nothing was overwritten." }
        $State = Get-Content -Raw -LiteralPath $StatePath -Encoding UTF8 | ConvertFrom-Json
        if ($State.bundle_id -ne $Manifest.bundle_id) { throw 'A different Yuki package owns this installation. Automatic replacement is refused.' }
        Copy-VerifiedPayload $PSScriptRoot $BundleRoot $Manifest
        $Manifest = Test-Bundle $BundleRoot
    } else {
        $Admin = ''
        while ($Admin -notmatch '^[1-9][0-9]{4,19}$' -or $Admin -eq $BotQQ) {
            $Admin = (Read-Host "Enter YOUR administrator QQ number (Bot: $BotQQ)").Trim()
        }
        New-Item -ItemType Directory -Path $InstallRoot | Out-Null
        Protect-Directory $InstallRoot
        $State = [pscustomobject]@{ bundle_id = $Manifest.bundle_id; admin_qq = $Admin; distro_imported = $false; completed = $false }
        Save-State $State
        Copy-VerifiedPayload $PSScriptRoot $BundleRoot $Manifest
        $Manifest = Test-Bundle $BundleRoot
    }
    Protect-Directory $InstallRoot

    $NeedsRestart = $false
    foreach ($Feature in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
        $Status = Get-WindowsOptionalFeature -Online -FeatureName $Feature
        if ($Status.State -ne 'Enabled') {
            $Result = Enable-WindowsOptionalFeature -Online -FeatureName $Feature -All -NoRestart
            $NeedsRestart = $NeedsRestart -or $Result.RestartNeeded -or $Status.State -eq 'EnablePending'
        }
    }
    if ($NeedsRestart) {
        Register-Resume
        Write-Host 'WSL features are enabled. Restart Windows and sign in; installation will resume.' -ForegroundColor Yellow
        Write-Host 'If it does not resume, double-click Deploy.cmd again. Existing progress is preserved.'
        exit 3010
    }
    if (-not (Test-Path -LiteralPath $Wsl)) { throw 'Windows WSL component is unavailable after installation; restart Windows.' }
    # Inbox WSL on a clean PC can lack --update/--web-download and systemd support.
    # Install the digest-pinned official MSI there; preserve an existing modern WSL.
    if (-not (Test-WslCoreVersion)) {
        $Installer = Join-Path $InstallRoot 'wsl-core-x64.msi'
        $Expected = '28b1a0d013640a2ac95898ea705fa186e5b4ff767a1c1b49257161bc106599c6'
        Write-Host 'Installing the official WSL core and Linux kernel...' -ForegroundColor Cyan
        if (-not (Test-Path -LiteralPath $Installer) -or (Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected) {
            Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/microsoft/WSL/releases/download/3.0.1/wsl.3.0.1.0.x64.msi' -OutFile $Installer
        }
        if ((Get-FileHash -LiteralPath $Installer -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected) {
            throw 'WSL core checksum mismatch; installation refused.'
        }
        $Result = Start-Process msiexec.exe -ArgumentList "/i `"$Installer`" /qn /norestart" -Wait -PassThru
        if ($Result.ExitCode -notin @(0, 3010)) { throw "WSL core installation failed (exit $($Result.ExitCode))." }
        Remove-Item -LiteralPath $Installer
        if ($Result.ExitCode -eq 3010) {
            Register-Resume
            Write-Host 'Restart Windows and sign in; Yuki installation will resume.' -ForegroundColor Yellow
            exit 3010
        }
    }
    Invoke-Checked $Wsl @('--version')
    $Names = @(& $Wsl --list --quiet) | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ }
    if ($LASTEXITCODE -ne 0) { throw 'Cannot list WSL distributions. Restart Windows and retry.' }
    if ($Distro -in $Names) {
        $Owner = (& $Wsl --distribution $Distro --user root --exec cat /etc/yuki-deployment-id 2>$null | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or $Owner -ne $State.bundle_id) {
            throw 'An unrelated Yuki-Bocchi WSL distribution exists. It was not changed.'
        }
    } else {
        $Rootfs = Join-Path $InstallRoot 'ubuntu-rootfs.tar.gz'
        $Expected = '8251e27ffff381a4af5f41dcb94d867de3e0d9774a9241908ab34555d99315ea'
        if (-not (Test-Path -LiteralPath $Rootfs) -or (Get-FileHash -LiteralPath $Rootfs -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected) {
            Write-Host 'Downloading the checksum-pinned Ubuntu WSL base...' -ForegroundColor Cyan
            Invoke-WebRequest -UseBasicParsing -Uri 'https://cloud-images.ubuntu.com/wsl/releases/24.04/20240423/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz' -OutFile $Rootfs
        }
        if ((Get-FileHash -LiteralPath $Rootfs -Algorithm SHA256).Hash.ToLowerInvariant() -ne $Expected) {
            throw 'Ubuntu base checksum mismatch; import refused.'
        }
        Invoke-Checked $Wsl @('--import', $Distro, (Join-Path $InstallRoot 'wsl'), $Rootfs, '--version', '2')
        # Stamp only this newly imported distro; never adopt an unmarked existing distro.
        Invoke-Wsl 'root' @('bash', '-c', 'printf "%s\n" "$1" > /etc/yuki-deployment-id', 'bash', $State.bundle_id)
        $State.distro_imported = $true
        Save-State $State
        Remove-Item -LiteralPath $Rootfs
    }

    if (-not $State.completed) {
        $LinuxBundle = (& $Wsl --distribution $Distro --user root --exec wslpath -a -u $BundleRoot | Out-String).Trim()
        if ($LASTEXITCODE -ne 0 -or -not $LinuxBundle.StartsWith('/')) { throw 'Cannot resolve the bundle path inside WSL.' }
        # A retry after a late failure can find the original Bot active. Drain it
        # before restarting this distro or running any migration, never start a second writer.
        Invoke-Wsl 'root' @('bash', '-c', 'if systemctl cat yuki-bocchi.service >/dev/null 2>&1; then systemctl stop yuki-bocchi; fi; mkdir -p /opt/yuki-bootstrap; cp -a "$1"/. /opt/yuki-bootstrap/; chmod 700 /opt/yuki-bootstrap', 'bash', $LinuxBundle)
        Invoke-Wsl 'root' @('bash', '/opt/yuki-bootstrap/bootstrap.sh', 'system')
        Invoke-Checked $Wsl @('--terminate', $Distro)
        Invoke-Wsl 'root' @('bash', '-c', 'for n in $(seq 1 60); do state=$(systemctl is-system-running 2>/dev/null || true); case "$state" in running|degraded) exit 0;; esac; sleep 2; done; echo "WSL systemd failed to become ready" >&2; exit 1')
        # Enabled systemd units restart with WSL. Stop an earlier partial instance again.
        Invoke-Wsl 'root' @('bash', '-c', 'if systemctl cat yuki-bocchi.service >/dev/null 2>&1; then systemctl stop yuki-bocchi; fi')
        Write-Host 'Building the fixed Yuki version, WebUI and native Monty. The first build can take a while.' -ForegroundColor Cyan
        Invoke-Wsl 'yuki' @('bash', '/opt/yuki-bootstrap/bootstrap.sh', 'build')
        Invoke-Wsl 'root' @('bash', '/opt/yuki-bootstrap/bootstrap.sh', 'install-worker')
        Write-Host 'Testing real Monty isolation, the real API and database migrations...' -ForegroundColor Cyan
        Invoke-Wsl 'yuki' @('bash', '/opt/yuki-bootstrap/bootstrap.sh', 'verify', $State.admin_qq)
    }
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
    Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\RunOnce' -Name 'YukiBocchiSetup' -ErrorAction SilentlyContinue
    Write-Host 'Installation passed. Open management.txt for both login credentials.' -ForegroundColor Green
    Write-Host "In NapCat, scan the QQ login QR using Bot account $BotQQ. Then chat privately with that QQ."
    Start-Process notepad.exe -ArgumentList "`"$CredentialFile`""
    Start-Process 'http://127.0.0.1:6099/'
    Start-Process 'http://127.0.0.1:18765/'
} catch {
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host 'Retry Deploy.cmd to continue. No database, QQ login, or existing deployment is deleted.'
    exit 1
}
