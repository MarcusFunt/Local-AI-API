#Requires -Version 5.1
<#
.SYNOPSIS
    Start the already-installed Local AI API Docker stack and open the status page.
.DESCRIPTION
    A lightweight "run" launcher: it does not sync the repository, rebuild the
    image, or run the test suite. It starts Ollama, the gateway, and Agent Zero
    with the appropriate accelerator compose override, waits for health, and
    opens the browser. Use scripts/install-or-update.ps1 (or Install.cmd) to
    install, update, or change configuration.
#>
[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "nvidia")]
    [string]$Accelerator = $(if ([string]::IsNullOrWhiteSpace($env:LOCAL_AI_API_ACCELERATOR)) { "auto" } else { $env:LOCAL_AI_API_ACCELERATOR }),
    [switch]$NoBrowser
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$GatewayUrl = "http://127.0.0.1:8080/"
$GatewayHealthUrl = "http://127.0.0.1:8080/health"
$OllamaHealthUrl = "http://127.0.0.1:8080/health/ollama"
$AgentZeroPort = if ([string]::IsNullOrWhiteSpace($env:AGENT_ZERO_PORT)) { 50080 } else { [int]$env:AGENT_ZERO_PORT }

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host "[$(Get-Date -Format 'o')] $Message"
}

function Stop-Fail {
    param([Parameter(Mandatory = $true)][string]$Message)
    throw $Message
}

function Test-Command {
    param([Parameter(Mandatory = $true)][string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-DockerInfo {
    & docker info *> $null
    return $LASTEXITCODE -eq 0
}

function Start-DockerDesktopIfNeeded {
    if (Test-DockerInfo) {
        return
    }

    $dockerDesktopPath = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path -LiteralPath $dockerDesktopPath)) {
        Stop-Fail "Docker Desktop is not running, and Docker Desktop was not found at $dockerDesktopPath."
    }

    Write-Log "Starting Docker Desktop."
    Start-Process -FilePath $dockerDesktopPath -WindowStyle Hidden

    for ($attempt = 1; $attempt -le 90; $attempt++) {
        if (Test-DockerInfo) {
            Write-Log "Docker Desktop is ready."
            return
        }
        Start-Sleep -Seconds 2
    }

    Stop-Fail "Docker Desktop did not become ready."
}

function Test-NvidiaDockerWorks {
    if (-not (Test-Command "nvidia-smi")) {
        return $false
    }

    & docker run --rm --gpus all hello-world *> $null
    return $LASTEXITCODE -eq 0
}

function Get-AcceleratorProfile {
    if ($Accelerator -ne "auto") {
        return $Accelerator
    }

    if (Test-NvidiaDockerWorks) {
        return "nvidia"
    }

    if (Test-Command "nvidia-smi") {
        Write-Log "NVIDIA GPU detected, but Docker GPU access failed; falling back to CPU."
    }

    return "cpu"
}

function Get-ComposeArgumentsForAccelerator {
    param([Parameter(Mandatory = $true)][string]$SelectedAccelerator)

    switch ($SelectedAccelerator) {
        "nvidia" {
            $arguments = @("-f", (Join-Path $RepoRoot "compose.yaml"), "-f", (Join-Path $RepoRoot "compose.gpu-nvidia.yaml"))
        }
        "cpu" {
            $arguments = @("-f", (Join-Path $RepoRoot "compose.yaml"), "-f", (Join-Path $RepoRoot "compose.cpu.yaml"))
        }
        default {
            Stop-Fail "Unknown Windows accelerator profile: $SelectedAccelerator"
        }
    }

    $arguments += @("-f", (Join-Path $RepoRoot "compose.agent-zero.yaml"))
    return $arguments
}

function Wait-ForUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][string]$Label,
        [int]$Attempts = 60
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 5 *> $null
            Write-Log "$Label is healthy."
            return
        }
        catch {
            Start-Sleep -Seconds 2
        }
    }

    Stop-Fail "$Label did not become healthy at $Url."
}

function Main {
    if (-not (Test-Command "docker")) {
        Stop-Fail "Docker CLI was not found. Install Docker Desktop, or run Install.cmd first."
    }

    Set-Location $RepoRoot

    $envPath = Join-Path $RepoRoot ".env"
    if (-not (Test-Path -LiteralPath $envPath)) {
        Copy-Item -LiteralPath (Join-Path $RepoRoot ".env.example") -Destination $envPath
        Write-Log "Created .env from .env.example."
    }

    Start-DockerDesktopIfNeeded

    $selectedAccelerator = Get-AcceleratorProfile
    Write-Log "Selected accelerator profile: $selectedAccelerator."
    $composeArguments = @(Get-ComposeArgumentsForAccelerator -SelectedAccelerator $selectedAccelerator)

    Write-Log "Starting the Local AI API stack (the first run pulls models and can take a while)."
    & docker compose @composeArguments up -d
    if ($LASTEXITCODE -ne 0) {
        Stop-Fail "docker compose up failed."
    }

    Wait-ForUrl -Url $GatewayHealthUrl -Label "Gateway health"
    Wait-ForUrl -Url $OllamaHealthUrl -Label "Ollama health"

    Write-Log "Stack is up."
    Write-Log "Gateway:    $GatewayUrl"
    Write-Log "Agent Zero: http://127.0.0.1:$AgentZeroPort/"

    if (-not $NoBrowser) {
        Start-Process $GatewayUrl
    }
}

try {
    Main
}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}
