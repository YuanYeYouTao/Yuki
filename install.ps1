# Configuration-only bootstrap. Service lifecycle and database migration are manual.
[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [string]$Version = "3.8.3"
)

$ErrorActionPreference = "Stop"
$Repository = "YuanYeYouTao/Yuki-QQbot"
$BotImage = "ghcr.io/yuanyeyoutao/yuki-qqbot"

function Fail([string]$Message) {
    throw $Message
}

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Fail "Version must use X.Y.Z."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Fail "Docker is not installed."
}
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "The Docker Compose CLI plugin is not available."
}
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Fail "Docker Engine is not running."
}
$Architecture = (& docker info --format '{{.Architecture}}').Trim()
$DockerOs = (& docker info --format '{{.OSType}}').Trim()
if ($DockerOs -ne 'linux') {
    Fail "Docker Desktop must be running Linux containers."
}
if ($Architecture -notin @('amd64', 'x86_64')) {
    Fail "Yuki $Version officially supports linux/amd64; Docker reports $Architecture."
}

if (-not $InstallDir) {
    if ((Test-Path -LiteralPath "docker-compose.yml") -and (Test-Path -LiteralPath ".env.example")) {
        $InstallDir = (Get-Location).Path
    } else {
        $InstallDir = Join-Path (Get-Location).Path "yuki"
    }
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir)
[System.IO.Directory]::CreateDirectory($InstallDir) | Out-Null
$WriteProbe = Join-Path $InstallDir (".yuki-write-test-" + [guid]::NewGuid())
try {
    [System.IO.File]::WriteAllText($WriteProbe, "write-test")
} catch {
    Fail "Installation directory is not writable."
} finally {
    if (Test-Path -LiteralPath $WriteProbe) {
        Remove-Item -LiteralPath $WriteProbe -Force
    }
}
$ComposePath = Join-Path $InstallDir "docker-compose.yml"
$EnvTemplatePath = Join-Path $InstallDir ".env.example"
$Existing = (Test-Path -LiteralPath $ComposePath) -and (Test-Path -LiteralPath $EnvTemplatePath)
if (-not $Existing -and (Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1)) {
    Fail "Installation directory is not empty and is not a Yuki deployment."
}

$TemporaryRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$Temporary = Join-Path $TemporaryRoot ("yuki-setup-" + [guid]::NewGuid())
[System.IO.Directory]::CreateDirectory($Temporary) | Out-Null
try {
    if (-not $Existing) {
        $Base = "https://github.com/$Repository/releases/download/v$Version"
        $ArchiveName = "yuki-$Version-deploy.zip"
        $Archive = Join-Path $Temporary $ArchiveName
        $Checksums = Join-Path $Temporary "SHA256SUMS"
        Invoke-WebRequest -Uri "$Base/$ArchiveName" -OutFile $Archive
        Invoke-WebRequest -Uri "$Base/SHA256SUMS" -OutFile $Checksums
        $ChecksumLine = Get-Content -LiteralPath $Checksums | Where-Object {
            $_ -match "^[0-9a-fA-F]{64}\s+$([regex]::Escape($ArchiveName))$"
        } | Select-Object -First 1
        if (-not $ChecksumLine) {
            Fail "Release checksum does not list $ArchiveName."
        }
        $Expected = ($ChecksumLine -split '\s+')[0].ToLowerInvariant()
        $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Expected -ne $Actual) {
            Fail "Release archive checksum mismatch."
        }
        Expand-Archive -LiteralPath $Archive -DestinationPath $Temporary
        $Source = Join-Path $Temporary "yuki-$Version-deploy"
        if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
            Fail "Release archive layout is invalid."
        }
        if (-not $Existing) {
            Copy-Item -Path (Join-Path $Source '*') -Destination $InstallDir -Recurse -Force
            Get-ChildItem -LiteralPath $Source -Force | Where-Object Name -Like '.*' | ForEach-Object {
                Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force
            }
        }

    }

    $Image = "${BotImage}:$Version"
    Write-Host "Pulling $Image" -ForegroundColor Cyan
    & docker pull $Image
    if ($LASTEXITCODE -ne 0) { Fail "Unable to pull $Image." }

    & docker run --rm -it `
        --entrypoint qq-ai-bot-cli `
        --volume "${InstallDir}:/deploy" `
        --workdir /deploy `
        $Image setup --deployment-root /deploy
    if ($LASTEXITCODE -ne 0) { Fail "Guided setup did not complete." }

    if ($env:OS -eq "Windows_NT") {
        $Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $PrivateTargets = @(
            @{
                Path = (Join-Path $InstallDir ".env")
                Permission = "${Identity}:(F)"
                Recurse = $false
            },
            @{
                Path = (Join-Path $InstallDir ".yuki\backups")
                Permission = "${Identity}:(OI)(CI)F"
                Recurse = $true
            }
        )
        foreach ($Target in $PrivateTargets) {
            if (Test-Path -LiteralPath $Target.Path) {
                $AclArguments = @($Target.Path, "/inheritance:r", "/grant:r", $Target.Permission)
                if ($Target.Recurse) { $AclArguments += @("/T", "/C") }
                & icacls @AclArguments *> $null
                if ($LASTEXITCODE -ne 0) { Fail "Unable to restrict configuration ACLs." }
            }
        }

    }
    Write-Host "Configuration saved. No services were stopped or started and no database was upgraded." -ForegroundColor Green
    Write-Host "Review $InstallDir/Yuki-$Version-Upgrade.md before starting or upgrading the deployment."
    Write-Host "Upgrade guide: https://github.com/$Repository/blob/v$Version/docs/upgrade-$Version.md"
} finally {
    $ResolvedTemporary = [System.IO.Path]::GetFullPath($Temporary)
    if ([System.IO.Path]::GetDirectoryName($ResolvedTemporary).TrimEnd([System.IO.Path]::DirectorySeparatorChar) -ne $TemporaryRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar)) {
        Fail "Unexpected temporary directory; cleanup refused."
    }
    if (Test-Path -LiteralPath $ResolvedTemporary) {
        Remove-Item -LiteralPath $ResolvedTemporary -Recurse -Force
    }
}
