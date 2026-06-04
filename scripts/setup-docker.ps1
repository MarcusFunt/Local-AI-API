#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "nvidia")]
    [string]$Accelerator = "auto",
    [switch]$SkipTests,
    [switch]$NoAudio
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$GatewayHealthUrl = "http://127.0.0.1:8080/health"
$OllamaHealthUrl = "http://127.0.0.1:8080/health/ollama"
$AgentZeroPort = if ([string]::IsNullOrWhiteSpace($env:AGENT_ZERO_PORT)) { 50080 } else { [int]$env:AGENT_ZERO_PORT }

function Write-Log {
    param([Parameter(Mandatory = $true)][string]$Message)

    $timestamp = Get-Date -Format "o"
    Write-Host "[$timestamp] $Message"
}

function Stop-Fail {
    param([Parameter(Mandatory = $true)][string]$Message)

    throw $Message
}

function Test-Command {
    param([Parameter(Mandatory = $true)][string]$Name)

    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-External {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        Stop-Fail "$FilePath $($Arguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Get-RepoRoot {
    return (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
}

function Ensure-EnvFile {
    param([Parameter(Mandatory = $true)][string]$Root)

    $envPath = Join-Path $Root ".env"
    $examplePath = Join-Path $Root ".env.example"
    if (-not (Test-Path -LiteralPath $envPath)) {
        Copy-Item -LiteralPath $examplePath -Destination $envPath
        Write-Log "Created .env from .env.example."
    }
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
    if (Test-Path -LiteralPath $dockerDesktopPath) {
        Write-Log "Starting Docker Desktop."
        Start-Process -FilePath $dockerDesktopPath -WindowStyle Hidden
        for ($attempt = 1; $attempt -le 90; $attempt++) {
            if (Test-DockerInfo) {
                Write-Log "Docker Desktop is ready."
                return
            }
            Start-Sleep -Seconds 2
        }
    }

    Stop-Fail "Docker is not running. Start Docker Desktop and rerun this script."
}

function Require-Docker {
    if (-not (Test-Command "docker")) {
        Stop-Fail "Docker CLI was not found. Install Docker Desktop, then rerun this script."
    }

    Start-DockerDesktopIfNeeded

    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-Fail "Docker Compose plugin was not found. Update Docker Desktop, then rerun this script."
    }
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

    return "cpu"
}

function Get-ComposeArgumentsForAccelerator {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$SelectedAccelerator
    )

    $arguments = switch ($SelectedAccelerator) {
        "nvidia" {
            @("-f", (Join-Path $Root "compose.yaml"), "-f", (Join-Path $Root "compose.gpu-nvidia.yaml"))
        }
        "cpu" {
            @("-f", (Join-Path $Root "compose.yaml"), "-f", (Join-Path $Root "compose.cpu.yaml"))
        }
        default {
            Stop-Fail "Unknown Windows accelerator profile: $SelectedAccelerator"
        }
    }
    return $arguments + @("-f", (Join-Path $Root "compose.agent-zero.yaml"))
}

function Invoke-DockerCompose {
    param(
        [Parameter(Mandatory = $true)][string[]]$ComposeArguments,
        [Parameter(Mandatory = $true)][string[]]$CommandArguments
    )

    Invoke-External -FilePath "docker" -Arguments (@("compose") + $ComposeArguments + $CommandArguments)
}

function Build-GatewayImage {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArguments)

    if ($NoAudio) {
        $env:INSTALL_AUDIO = "false"
    }

    Write-Log "Validating Docker Compose configuration."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("config")

    Write-Log "Building gateway image with container-owned Python dependencies."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("build", "gateway")

    if (-not $SkipTests) {
        Write-Log "Running tests inside the gateway image."
        Invoke-External -FilePath "docker" -Arguments @(
            "run", "--rm",
            "--entrypoint", "python",
            "--workdir", "/app",
            "local-ai-api-gateway:latest",
            "-m", "pytest", "tests", "-v"
        )
    }
}

function Start-Stack {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArguments)

    Write-Log "Starting private Ollama container."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("up", "-d", "ollama")

    Write-Log "Pulling configured Ollama models into the Docker volume."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("run", "--rm", "model-init")

    Write-Log "Starting gateway container."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("up", "-d", "gateway")

    Write-Log "Starting Agent Zero."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("up", "-d", "agent-zero")
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
    $root = Get-RepoRoot
    Set-Location $root

    Ensure-EnvFile -Root $root
    Require-Docker

    $selectedAccelerator = Get-AcceleratorProfile
    Write-Log "Selected accelerator profile: $selectedAccelerator."
    $composeArguments = @(Get-ComposeArgumentsForAccelerator -Root $root -SelectedAccelerator $selectedAccelerator)

    Build-GatewayImage -ComposeArguments $composeArguments
    Start-Stack -ComposeArguments $composeArguments

    Wait-ForUrl -Url $GatewayHealthUrl -Label "Gateway health"
    Wait-ForUrl -Url $OllamaHealthUrl -Label "Ollama health"
    Wait-ForUrl -Url "http://127.0.0.1:$AgentZeroPort" -Label "Agent Zero UI" -Attempts 90

    Write-Log "Running dev model smoke check."
    Invoke-WebRequest -Uri "http://127.0.0.1:8080/status/check" -Method Post -UseBasicParsing -TimeoutSec 120 *> $null

    Write-Log "Docker setup complete. Gateway: http://127.0.0.1:8080/ Agent Zero: http://127.0.0.1:$AgentZeroPort/"
}

try {
    Main
}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}
