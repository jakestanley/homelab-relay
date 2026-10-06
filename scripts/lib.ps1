# Shared by up.ps1, install-service.ps1 and fetch-tools.ps1. Dot-source it;
# it does nothing on its own. Windows PowerShell 5.1 compatible.

$RepoRoot = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $RepoRoot ".env"
$DefaultPrefix = Split-Path -Leaf $RepoRoot

function Get-DotEnvValue {
    param([string]$Path, [string]$Key)
    if (-not (Test-Path $Path)) { return $null }
    foreach ($line in Get-Content $Path) {
        if ($line -match '^\s*#' -or $line -notmatch '=') { continue }
        $parts = $line -split '=', 2
        if ($parts[0].Trim() -eq $Key) { return $parts[1].Trim().Trim('"').Trim("'") }
    }
    return $null
}

function Test-IsAdmin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]::new($id)).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Test-Interactive {
    return [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
}

# Run a native command; throw, naming it, if it fails. Never pass secrets in
# $Arguments: a failure message names the arguments.
# Its output goes to the host, not the pipeline, so a function calling it
# returns only what it means to return.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "$Exe $($Arguments -join ' ') exited with $LASTEXITCODE"
    }
}

# git's stdout, with its stderr discarded. In Windows PowerShell 5.1,
# redirecting a native command's stderr while $ErrorActionPreference is
# Stop turns the first stderr line into a terminating error, so this
# relaxes it for the one call.
function Invoke-GitQuiet {
    param([string[]]$Arguments)
    $ErrorActionPreference = "Continue"
    $out = & git @Arguments 2>$null
    return $out
}

# NSSM services run with a reduced PATH, so the interpreter is resolved to a
# full path here, once, and only the venv's python.exe is used after that.
function Resolve-BootstrapPython {
    param([string]$Explicit)
    $candidates = @($Explicit, (Get-DotEnvValue $EnvFile "RELAY_PYTHON_EXE"), $env:RELAY_PYTHON_EXE)
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path $candidate)) { return $candidate }
    }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) { return $py.Source }
    throw "Python not found. Set RELAY_PYTHON_EXE in .env to the full path of python.exe (3.11 or later)."
}

# The repo-local .venv, created on first run. requirements.txt is installed
# only when it has changed, so re-running up.ps1 needs no network.
function Initialize-Venv {
    param([string]$PythonExe)
    $venv = Join-Path $RepoRoot ".venv"
    $venvPython = Join-Path $venv "Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        $bootstrap = Resolve-BootstrapPython $PythonExe
        Write-Host "Creating .venv with $bootstrap"
        if ($bootstrap -match 'py\.exe$') {
            Invoke-Native $bootstrap @("-3", "-m", "venv", $venv)
        } else {
            Invoke-Native $bootstrap @("-m", "venv", $venv)
        }
    }
    $requirements = Join-Path $RepoRoot "requirements.txt"
    $marker = Join-Path $venv "requirements.sha256"
    $want = (Get-FileHash -Algorithm SHA256 $requirements).Hash
    $have = if (Test-Path $marker) { (Get-Content $marker -Raw).Trim() } else { "" }
    if ($want -ne $have) {
        Write-Host "Installing requirements.txt into .venv"
        Invoke-Native $venvPython @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", $requirements)
        Set-Content -Path $marker -Value $want -Encoding ascii
    }
    return $venvPython
}

function Resolve-Nssm {
    $cmd = Get-Command nssm.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "nssm.exe not found on PATH. Install NSSM (choco install nssm) and retry." }
    return $cmd.Source
}
