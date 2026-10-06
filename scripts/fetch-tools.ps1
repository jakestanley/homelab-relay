<#
.SYNOPSIS
Fetch the pinned MediaMTX and ffmpeg builds into tools\, verified by SHA-256.

.DESCRIPTION
The relay runs the binaries pinned here, not whatever is on PATH, so the
versions that passed test/acceptance.py are the versions that run on the
night. Changing a version is an edit to this file: bump it, run the
acceptance checks, record the result in TESTING.md.

A tool already present at its pinned version is left alone, so this is
cheap to re-run and needs no network then. It refuses to replace a binary
while any relay service is running: stop the relay first (never during a
broadcast). Called by up.ps1 and install-service.ps1.
#>
param(
    [string]$ServiceName
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib.ps1")
if (-not $ServiceName) { $ServiceName = $DefaultPrefix }

# Checksums are the release assets' published SHA-256 digests, re-checked by
# download on 2026-10-06.
$Pins = @(
    @{
        Name    = "mediamtx"
        Version = "v1.21.1"
        Url     = "https://github.com/bluenviron/mediamtx/releases/download/v1.21.1/mediamtx_v1.21.1_windows_amd64.zip"
        Sha256  = "faa97974861eb75a68b5aa326c78e7e7a6f670b5ef191bace78e715130381f23"
        Inner   = $null
        Check   = "mediamtx.exe"
    },
    @{
        # Gyan's essentials build: NVENC, CUDA decode and scale_cuda, GnuTLS
        # for RTMPS.
        Name    = "ffmpeg"
        Version = "9.0.2-essentials"
        Url     = "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip"
        Sha256  = "60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba"
        Inner   = "ffmpeg-9.0.2-essentials_build"
        Check   = "bin\ffmpeg.exe"
    }
)

$ProgressPreference = "SilentlyContinue"   # PS 5.1 downloads crawl with the progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$toolsDir = Join-Path $RepoRoot "tools"
New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

foreach ($pin in $Pins) {
    $dest = Join-Path $toolsDir $pin.Name
    $marker = Join-Path $dest "VERSION"
    $want = "$($pin.Version) $($pin.Sha256)"
    if ((Test-Path $marker) -and ((Get-Content $marker -Raw).Trim() -eq $want) -and (Test-Path (Join-Path $dest $pin.Check))) {
        Write-Host "tools: $($pin.Name) $($pin.Version) present"
        continue
    }

    $running = @(Get-Service -Name "$ServiceName-*" -ErrorAction SilentlyContinue | Where-Object { $_.Status -ne "Stopped" })
    if ($running.Count -gt 0) {
        throw "tools: $($pin.Name) needs replacing, but relay services are running ($($running.Name -join ', ')). Stop the relay first (scripts\install-service.ps1 -Stop), never during a broadcast."
    }

    Write-Host "tools: fetching $($pin.Name) $($pin.Version)"
    $zip = Join-Path $env:TEMP ("homelab-relay-" + $pin.Name + "-" + $pin.Version + ".zip")
    Invoke-WebRequest -UseBasicParsing -Uri $pin.Url -OutFile $zip
    $got = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    if ($got -ne $pin.Sha256) {
        Remove-Item $zip -Force
        throw "tools: $($pin.Name) checksum mismatch: expected $($pin.Sha256), got $got. Not installed."
    }

    $staging = "$dest.new"
    if (Test-Path $staging) { Remove-Item $staging -Recurse -Force }
    Expand-Archive -Path $zip -DestinationPath $staging
    Remove-Item $zip -Force
    if ($pin.Inner) {
        $inner = Join-Path $staging $pin.Inner
        Get-ChildItem $inner | Move-Item -Destination $staging
        Remove-Item $inner -Recurse -Force
    }
    if (-not (Test-Path (Join-Path $staging $pin.Check))) {
        throw "tools: $($pin.Name) archive did not contain $($pin.Check)"
    }
    if (Test-Path $dest) { Remove-Item $dest -Recurse -Force }
    Move-Item $staging $dest
    Set-Content -Path $marker -Value $want -Encoding ascii
    Write-Host "tools: $($pin.Name) $($pin.Version) installed"
}
