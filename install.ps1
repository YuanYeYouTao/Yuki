# Install or upgrade a Yuki deployment with Docker Compose, including its sandbox.
# Run it again in the same directory to upgrade; .env and data are kept.
[CmdletBinding()]
param(
    [string]$InstallDir = "",
    [string]$Version = "4.0.2",
    # $PROFILE is a PowerShell automatic variable; -Profile still works through the alias.
    [Alias("Profile")]
    [ValidateSet("", "lite", "headless", "desktop")]
    [string]$SandboxProfile = "",
    [switch]$Yes,
    [string]$Answers = "",
    [string]$Bundle = "",
    [string]$CharacterName = "",
    [string]$OwnerName = ""
)

$ErrorActionPreference = "Stop"
$Repository = "YuanYeYouTao/Yuki"
$BotImage = "ghcr.io/yuanyeyoutao/yuki"
$EnvironmentImage = "ghcr.io/yuanyeyoutao/yuki-environment"

function Fail([string]$Message) { throw $Message }

if ($Version -notmatch '^\d+\.\d+\.\d+$') { Fail "Version must use X.Y.Z." }
if (-not $Yes -and [Console]::IsInputRedirected) {
    Fail "No terminal: pass -Yes (and -Profile, -Answers for a first install)."
}
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { Fail "Docker is not installed." }
& docker compose version *> $null
if ($LASTEXITCODE -ne 0) { Fail "The Docker Compose CLI plugin is not available." }
& docker info *> $null
if ($LASTEXITCODE -ne 0) { Fail "Docker Engine is not running." }
if ((& docker info --format '{{.OSType}}').Trim() -ne 'linux') {
    Fail "Docker Desktop must be running Linux containers."
}
$Platform = switch ((& docker info --format '{{.Architecture}}').Trim()) {
    { $_ -in @('amd64', 'x86_64') } { 'linux/amd64' }
    { $_ -in @('arm64', 'aarch64') } { 'linux/arm64' }
    default { Fail "Yuki supports linux/amd64 and linux/arm64." }
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
$IsDeployment = (Test-Path -LiteralPath (Join-Path $InstallDir "docker-compose.yml")) -and
    (Test-Path -LiteralPath (Join-Path $InstallDir ".env.example"))
if (-not $IsDeployment -and (Get-ChildItem -LiteralPath $InstallDir -Force | Select-Object -First 1)) {
    Fail "Installation directory is not empty and is not a Yuki deployment."
}
if ($Answers) { $Answers = [System.IO.Path]::GetFullPath($Answers) }
if ($Bundle) { $Bundle = [System.IO.Path]::GetFullPath($Bundle) }

$Temporary = Join-Path ([System.IO.Path]::GetTempPath()) ("yuki-install-" + [guid]::NewGuid())
[System.IO.Directory]::CreateDirectory($Temporary) | Out-Null
try {
    # Release files replace the previous ones; .env, data and docker-compose.override.yml stay.
    $ArchiveName = "yuki-$Version-deploy.zip"
    $Archive = Join-Path $Temporary $ArchiveName
    if ($Bundle) {
        Copy-Item -LiteralPath $Bundle -Destination $Archive
    } else {
        $Base = "https://github.com/$Repository/releases/download/v$Version"
        $Checksums = Join-Path $Temporary "SHA256SUMS"
        Invoke-WebRequest -Uri "$Base/$ArchiveName" -OutFile $Archive
        Invoke-WebRequest -Uri "$Base/SHA256SUMS" -OutFile $Checksums
        $Line = Get-Content -LiteralPath $Checksums | Where-Object {
            $_ -match "^[0-9a-fA-F]{64}\s+$([regex]::Escape($ArchiveName))$"
        } | Select-Object -First 1
        if (-not $Line) { Fail "Release checksum does not list $ArchiveName." }
        $Expected = ($Line -split '\s+')[0].ToLowerInvariant()
        $Actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($Expected -ne $Actual) { Fail "Release archive checksum mismatch." }
    }
    Expand-Archive -LiteralPath $Archive -DestinationPath $Temporary
    $Source = Join-Path $Temporary "yuki-$Version-deploy"
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { Fail "Release archive layout is invalid." }
    Get-ChildItem -LiteralPath $Source -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination $InstallDir -Recurse -Force
    }
} finally {
    Remove-Item -LiteralPath $Temporary -Recurse -Force -ErrorAction SilentlyContinue
}

Set-Location -LiteralPath $InstallDir
# An environment created by the retired host Manager is moved once, explicitly.
$Labels = & docker inspect --format '{{index .Config.Labels "io.yuki.sandbox"}}/{{index .Config.Labels "io.yuki.sandbox.project"}}' yuki-environment 2>$null
if ($LASTEXITCODE -eq 0 -and "$Labels".Trim() -eq "persistent-v1/") {
    Fail "This Docker host runs an environment of the old host Manager. The release files are now in $InstallDir; on the Docker host run deploy/sandbox/migrate-host-manager.sh --deployment-root <this deployment>, then run this installer again."
}

$EnvPath = Join-Path $InstallDir ".env"
function Get-EnvValue([string]$Name) {
    if (-not (Test-Path -LiteralPath $EnvPath)) { return "" }
    $Match = Get-Content -LiteralPath $EnvPath | Where-Object { $_ -match "^$Name=" } | Select-Object -Last 1
    if ($Match) { return $Match.Substring($Name.Length + 1) } else { return "" }
}
function Set-EnvValue([string]$Name, [string]$Value) {
    $Lines = @()
    if (Test-Path -LiteralPath $EnvPath) { $Lines = @(Get-Content -LiteralPath $EnvPath) }
    if ($Lines | Where-Object { $_ -match "^$Name=" }) {
        $Lines = $Lines | ForEach-Object { if ($_ -match "^$Name=") { "$Name=$Value" } else { $_ } }
    } else {
        $Lines += "$Name=$Value"
    }
    [System.IO.File]::WriteAllLines($EnvPath, [string[]]$Lines)
}
# Version tags are immutable: an image already present is the release image.
function Pull-Image([string]$Image) {
    & docker image inspect $Image *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Pulling $Image" -ForegroundColor Cyan
        & docker pull --quiet --platform $Platform $Image | Out-Null
        if ($LASTEXITCODE -ne 0) { Fail "Unable to pull $Image." }
    }
}

$Image = "${BotImage}:$Version"
Pull-Image $Image
$First = -not (Test-Path -LiteralPath $EnvPath)
if ($First) {
    if (-not $CharacterName -and -not $Yes) { $CharacterName = Read-Host "Character name [Yuki]" }
    if (-not $CharacterName) { $CharacterName = "Yuki" }
    if (-not $OwnerName -and -not $Yes) { $OwnerName = Read-Host "Your name, as Yuki will know you [$env:USERNAME]" }
    if (-not $OwnerName) { $OwnerName = if ($env:USERNAME) { $env:USERNAME } else { "me" } }
    if ($Yes) {
        if (-not $Answers -or -not (Test-Path -LiteralPath $Answers)) { Fail "A first install with -Yes needs -Answers FILE." }
        Get-Content -Raw -LiteralPath $Answers | & docker run --rm -i --platform $Platform --entrypoint yuki `
            --volume "${InstallDir}:/deploy" --workdir /deploy $Image setup desktop-configure --deployment-root /deploy | Out-Null
    } else {
        & docker run --rm -it --platform $Platform --entrypoint yuki `
            --volume "${InstallDir}:/deploy" --workdir /deploy $Image setup --deployment-root /deploy
    }
    if ($LASTEXITCODE -ne 0) { Fail "Setup did not complete." }
    if ($env:OS -eq "Windows_NT") {
        # Secrets stay readable by this user only.
        $Identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        foreach ($Target in @(@(".env", "${Identity}:(F)", $false), @(".yuki\backups", "${Identity}:(OI)(CI)F", $true), @("character", "${Identity}:(OI)(CI)F", $true))) {
            $Path = Join-Path $InstallDir $Target[0]
            if (Test-Path -LiteralPath $Path) {
                $Arguments = @($Path, "/inheritance:r", "/grant:r", $Target[1])
                if ($Target[2]) { $Arguments += @("/T", "/C") }
                & icacls @Arguments *> $null
                if ($LASTEXITCODE -ne 0) { Fail "Unable to restrict configuration ACLs." }
            }
        }
    }
}
Set-EnvValue "YUKI_VERSION" $Version
if ($CharacterName) { Set-EnvValue "YUKI_CHARACTER_NAME" $CharacterName }
# The owner this install creates, kept until it exists, so a rerun after a failure finishes it.
if ($First) { Set-EnvValue "YUKI_OWNER_NAME" $OwnerName }

if (-not $SandboxProfile -and -not (Get-EnvValue "YUKI_SANDBOX_PROFILE")) {
    $Memory = [int64](& docker info --format '{{.MemTotal}}') / 1MB
    $SandboxProfile = if ($Memory -lt 2048) { "lite" } elseif ($Memory -lt 4096) { "headless" } else { "desktop" }
    if (-not $Yes) {
        Write-Host ("Docker reports {0:N0} MiB of memory. Sandbox profile:" -f $Memory)
        Write-Host "  lite      terminal and files only (about 512 MiB)"
        Write-Host "  headless  adds a headless browser (about 1 GiB)"
        Write-Host "  desktop   adds a desktop you can watch and take over (about 3 GiB)"
        $Answer = Read-Host "Profile [$SandboxProfile]"
        if ($Answer) { $SandboxProfile = $Answer }
        if ($SandboxProfile -notin @("lite", "headless", "desktop")) { Fail "Unknown profile: $SandboxProfile" }
    }
}
if ($SandboxProfile) { Set-EnvValue "YUKI_SANDBOX_PROFILE" $SandboxProfile }
$SandboxProfile = Get-EnvValue "YUKI_SANDBOX_PROFILE"
# Chosen once and kept: a later install never silently moves the sandbox off gVisor.
if (-not (Get-EnvValue "YUKI_SANDBOX_RUNTIME")) {
    $Runtimes = & docker info --format '{{json .Runtimes}}'
    Set-EnvValue "YUKI_SANDBOX_RUNTIME" ($(if ($Runtimes -match '"runsc".*--overlay2=none') { "runsc" } else { "runc" }))
}

foreach ($Volume in @("yuki-sandbox-state", "yuki-sandbox-home", "yuki-sandbox-runtime", "yuki-sandbox-code", "yuki-media-cache")) {
    & docker volume inspect $Volume *> $null
    if ($LASTEXITCODE -ne 0) { & docker volume create $Volume | Out-Null }
}
$Environment = Get-EnvValue "YUKI_ENVIRONMENT_IMAGE"
if (-not $Environment) { $Environment = "${EnvironmentImage}:$Version" }
if ($SandboxProfile -ne "lite") { $Environment = "$Environment-desktop" }
Pull-Image $Environment
if (-not $First) {
    # Telegram/Feishu credentials left in .env (or inlined by Compose) move once into
    # webui-config before the new Bot starts; start.sh then hands it to the runtime UID.
    & docker compose run --rm --no-deps -T --user 0:0 --entrypoint yuki `
        --volume "${InstallDir}:/deploy" bot setup platforms-migrate --deployment-root /deploy | Out-Null
    if ($LASTEXITCODE -ne 0) { Fail "Platform credential migration failed." }
}
& docker compose up -d --pull missing --remove-orphans
if ($LASTEXITCODE -ne 0) { Fail "docker compose up failed." }

Write-Host -NoNewline "Waiting for Yuki and its sandbox"
$Uid = Get-EnvValue "YUKI_RUNTIME_UID"; if (-not $Uid) { $Uid = "10001" }
$Gid = Get-EnvValue "YUKI_RUNTIME_GID"; if (-not $Gid) { $Gid = "10001" }
for ($Attempt = 0; ; $Attempt++) {
    # Ready means both: the Bot answers (pairing uses its API) and it reaches the sandbox.
    & docker compose exec -T bot python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/livez', timeout=3)" *> $null
    if ($LASTEXITCODE -eq 0) {
        & docker compose exec -T --user "${Uid}:${Gid}" bot yuki setup environment-check *> $null
        if ($LASTEXITCODE -eq 0) { break }
    }
    if ($Attempt -ge 120) {
        Write-Host ""
        & docker compose logs --tail 40 manager bot
        Fail "The sandbox did not become ready; the logs above show why."
    }
    Write-Host -NoNewline "."
    Start-Sleep -Seconds 5
}
Write-Host ""
Write-Host "Yuki $Version is running in $InstallDir (sandbox profile: $SandboxProfile, runtime: $(Get-EnvValue 'YUKI_SANDBOX_RUNTIME'))." -ForegroundColor Green
# The owner this install named gets their Person, administrator, and a code for Yuki Desktop,
# also when a first install stopped before here. One recorded request ID names every attempt,
# so a rerun after a lost reply returns the same Person instead of creating another.
# The same steps Yuki Desktop takes for a local deployment (yuki setup desktop-pair).
if ((Get-EnvValue "YUKI_OWNER_NAME") -and -not (Get-EnvValue "YUKI_OWNER_PERSON_ID")) {
    if (-not (Get-EnvValue "YUKI_OWNER_REQUEST_ID")) { Set-EnvValue "YUKI_OWNER_REQUEST_ID" ([guid]::NewGuid().ToString()) }
    $PairScript = @'
python -c "import json, os, uuid; request = uuid.UUID(os.environ['YUKI_OWNER_REQUEST_ID']); print(json.dumps({'display_name': os.environ['YUKI_OWNER_NAME'], **{key: str(uuid.uuid5(request, key)) for key in ('person_request_id', 'superuser_request_id', 'pair_request_id')}}))" | yuki setup desktop-pair --deployment-root /app | python -c "import json, sys; result = json.load(sys.stdin); print(result['person_id'], result.get('code') or '')"
'@
    $Paired = & docker compose exec -T -e "YUKI_OWNER_NAME=$(Get-EnvValue 'YUKI_OWNER_NAME')" `
        -e "YUKI_OWNER_REQUEST_ID=$(Get-EnvValue 'YUKI_OWNER_REQUEST_ID')" bot sh -ec $PairScript
    if ($LASTEXITCODE -eq 0 -and $Paired) {
        $Person, $Code = "$Paired".Trim() -split ' ', 2
        Set-EnvValue "YUKI_OWNER_PERSON_ID" $Person
        $Port = Get-EnvValue "YUKI_PORT"; if (-not $Port) { $Port = "8080" }
        Write-Host ""
        Write-Host "Connect Yuki Desktop (https://github.com/Institute-of-Agent-Life/Yuki-Desktop):"
        Write-Host "  1. Open Yuki Desktop and choose 連接已有 Yuki (Connect an existing Yuki)."
        Write-Host "  2. Address: http://127.0.0.1:$Port on this computer. From another computer, reach"
        Write-Host "     this port through Tailscale or HTTPS (docs/operations/tailscale-connection.zh-CN.md)."
        Write-Host "  3. Binding code: $Code  (one-time, valid for 24 hours; a new one: deploy/pair.sh on the Docker host)"
    } else {
        Write-Host "No pairing code was created; pair Yuki Desktop by hand with docs/operations/person-admin-control-plane-upgrade.md."
    }
}
Write-Host "Release notes: https://github.com/$Repository/blob/v$Version/docs/releases/v$Version.md"
