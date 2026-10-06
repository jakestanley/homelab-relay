<#
.SYNOPSIS
Canonical entrypoint: bring the relay on this host to the state .env and
relay.yaml describe.

.DESCRIPTION
Safe to re-run against a running relay, including during a broadcast: it
restarts only the services whose configuration changed (changing one
platform's key restarts that output alone), and starts any that are
stopped. It never replaces a binary under a running relay.

In order: preflight on the sibling standards repos (warn; never pull),
.env present, .venv ready, configuration valid, pinned tools present,
firewall rules for the RTMP and status ports, then the NSSM services.
Must run elevated: installing services and firewall rules needs it.
Non-interactive (Ansible): never prompts, refuses where it would have asked.
#>
param(
    [string]$ServiceName,
    [string]$PythonExe
)

$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "lib.ps1")
if (-not $ServiceName) { $ServiceName = $DefaultPrefix }

Write-Host "== homelab-relay up: START"

# Preflight (homelab-standards AGENTS.md): sibling repos should be clean and
# on their default branch. Warn and ask; never pull or reset. A deploy with
# no siblings (an Ansible clone) only warns: nothing reads them at runtime.
$preflightFailed = $false
foreach ($sibling in @("homelab-standards", "homelab-infra")) {
    $dir = Join-Path (Split-Path -Parent $RepoRoot) $sibling
    if (-not (Test-Path (Join-Path $dir ".git"))) {
        Write-Warning "$sibling not found at $dir; skipping its preflight check."
        continue
    }
    if (Invoke-GitQuiet @("-C", $dir, "status", "--porcelain")) {
        Write-Warning "$sibling has uncommitted changes."
        $preflightFailed = $true
    }
    $default = Invoke-GitQuiet @("-C", $dir, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if (-not $default) { $default = "origin/main" }
    $branch = Invoke-GitQuiet @("-C", $dir, "rev-parse", "--abbrev-ref", "HEAD")
    if ($branch -ne ($default -replace '^origin/', '')) {
        Write-Warning "$sibling is on '$branch', not '$default'."
        $preflightFailed = $true
    } elseif ((Invoke-GitQuiet @("-C", $dir, "rev-parse", "HEAD")) -ne (Invoke-GitQuiet @("-C", $dir, "rev-parse", $default))) {
        Write-Warning "$sibling is not at $default (as last fetched)."
        $preflightFailed = $true
    }
}
if ($preflightFailed) {
    if (-not (Test-Interactive)) {
        throw "Preflight checks failed and this session is non-interactive; refusing by default."
    }
    $answer = Read-Host "Preflight checks failed. Continue anyway? [y/N]"
    if ($answer -notmatch '^(y|yes)$') { exit 1 }
}

if (-not (Test-Path $EnvFile)) {
    throw "Missing $EnvFile. Copy .env.example to .env and fill it in."
}

$py = Initialize-Venv $PythonExe

# Vendored agent docs (imported/, not committed). Best effort.
$sync = Join-Path (Split-Path -Parent $RepoRoot) "homelab-standards\scripts\sync_imports.py"
if (Test-Path $sync) {
    & $py $sync $RepoRoot
    if ($LASTEXITCODE -ne 0) { Write-Warning "syncing imported/ failed; continuing." }
}

# Every configuration problem at once, before anything is touched. Prints
# which outputs are enabled and what each is sent; never a key.
& $py -m relay.config $EnvFile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

& (Join-Path $PSScriptRoot "fetch-tools.ps1") -ServiceName $ServiceName

$ports = (& $py -m relay.install ports --env $EnvFile) | ConvertFrom-Json
$rules = @(
    @{ Name = "$ServiceName RTMP ingest (TCP $($ports.rtmp))"; Port = $ports.rtmp },
    @{ Name = "$ServiceName status page (TCP $($ports.http))"; Port = $ports.http }
)

if (-not (Test-IsAdmin)) {
    Write-Warning "Not elevated. Installing services and firewall rules needs an elevated PowerShell."
    foreach ($rule in $rules) {
        Write-Host "Run elevated: New-NetFirewallRule -DisplayName `"$($rule.Name)`" -Direction Inbound -Action Allow -Protocol TCP -LocalPort $($rule.Port) -Profile Private"
    }
    throw "Re-run scripts\up.ps1 from an elevated PowerShell."
}

# Inbound, TCP, the configured port, Private profile only (homelab-standards).
# An existing rule is left alone.
foreach ($rule in $rules) {
    if (-not (Get-NetFirewallRule -DisplayName $rule.Name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $rule.Name -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $rule.Port -Profile Private | Out-Null
        Write-Host "firewall: added '$($rule.Name)'"
    }
}

$nssm = Resolve-Nssm
& $py -m relay.install apply --prefix $ServiceName --env $EnvFile --nssm $nssm
$code = $LASTEXITCODE

Write-Host "== homelab-relay up: END"
exit $code
