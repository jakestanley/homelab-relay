<#
.SYNOPSIS
Install or update the relay's NSSM services; optionally start, stop or remove them.

.DESCRIPTION
The relay is several NSSM services, one per process: <ServiceName>-mediamtx,
<ServiceName>-out-<slot> for each output in relay.yaml, <ServiceName>-recorder
and <ServiceName>-status. -ServiceName is that prefix, and defaults to the
repo folder name.

  .\scripts\install-service.ps1             install/update, start nothing
  .\scripts\install-service.ps1 -Start      install/update, then restart all
  .\scripts\install-service.ps1 -Stop       stop all
  .\scripts\install-service.ps1 -Stop -Uninstall   stop and remove all

-Start restarts every service, as homelab-standards requires, so it
interrupts a broadcast. To apply a change during one, use up.ps1, which
restarts only what changed. Firewall rules are up.ps1's job, not this
script's. Must run elevated.
#>
param(
    [string]$ServiceName,
    [switch]$Start,
    [switch]$Stop,
    [switch]$Uninstall,
    [string]$PythonExe
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib.ps1")
if (-not $ServiceName) { $ServiceName = $DefaultPrefix }

Write-Host "== homelab-relay install-service: START"
if (-not (Test-IsAdmin)) { throw "Run from an elevated PowerShell: NSSM needs it." }
if (-not (Test-Path $EnvFile)) { throw "Missing $EnvFile. Copy .env.example to .env and fill it in." }

$py = Initialize-Venv $PythonExe
$nssm = Resolve-Nssm
$common = @("--prefix", $ServiceName, "--env", $EnvFile, "--nssm", $nssm)

if ($Uninstall) {
    Invoke-Native $py (@("-m", "relay.install", "remove") + $common)
} elseif ($Stop) {
    Invoke-Native $py (@("-m", "relay.install", "stop") + $common)
} else {
    & (Join-Path $PSScriptRoot "fetch-tools.ps1") -ServiceName $ServiceName
    Invoke-Native $py (@("-m", "relay.install", "apply", "--no-start") + $common)
    if ($Start) {
        Invoke-Native $py (@("-m", "relay.install", "restart") + $common)
    }
}

Write-Host "== homelab-relay install-service: END"
Invoke-Native $py (@("-m", "relay.install", "status") + $common)
