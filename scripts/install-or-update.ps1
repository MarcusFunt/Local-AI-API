#Requires -Version 5.1
[CmdletBinding()]
param(
    [ValidateSet("auto", "cpu", "nvidia")]
    [string]$Accelerator = $(if ([string]::IsNullOrWhiteSpace($env:LOCAL_AI_API_ACCELERATOR)) { "auto" } else { $env:LOCAL_AI_API_ACCELERATOR }),
    [switch]$SkipRepoSync,
    [switch]$SkipTailscaleServe,
    [switch]$NoScheduledTask,
    [switch]$ScheduledRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ProjectName = "local-ai-api"
$RemoteName = if ([string]::IsNullOrWhiteSpace($env:REMOTE_NAME)) { "origin" } else { $env:REMOTE_NAME }
$RemoteBranch = if ([string]::IsNullOrWhiteSpace($env:REMOTE_BRANCH)) { "main" } else { $env:REMOTE_BRANCH }
$GatewayHealthUrl = if ([string]::IsNullOrWhiteSpace($env:GATEWAY_HEALTH_URL)) { "http://127.0.0.1:8080/health" } else { $env:GATEWAY_HEALTH_URL }
$OllamaHealthUrl = if ([string]::IsNullOrWhiteSpace($env:OLLAMA_HEALTH_URL)) { "http://127.0.0.1:8080/health/ollama" } else { $env:OLLAMA_HEALTH_URL }
$TaskName = "Local AI API Update"
$script:InstallMutex = $null
$script:InstallMutexAcquired = $false

if ($env:LOCAL_AI_API_SKIP_REPO_SYNC -eq "1") {
    $SkipRepoSync = $true
}

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

function Require-Windows {
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        if (-not $IsWindows) {
            Stop-Fail "This installer targets Windows hosts with Docker Desktop."
        }
        return
    }

    if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
        Stop-Fail "This installer targets Windows hosts with Docker Desktop."
    }
}

function Acquire-Lock {
    if ($env:LOCAL_AI_API_LOCKED -eq "1") {
        return
    }

    $script:InstallMutex = [System.Threading.Mutex]::new($false, "Local\local-ai-api-install")
    $script:InstallMutexAcquired = $script:InstallMutex.WaitOne(0)
    if (-not $script:InstallMutexAcquired) {
        Stop-Fail "Another $ProjectName install/update run is already active."
    }
    $env:LOCAL_AI_API_LOCKED = "1"
}

function Release-Lock {
    if ($script:InstallMutexAcquired -and $null -ne $script:InstallMutex) {
        $script:InstallMutex.ReleaseMutex()
        $script:InstallMutex.Dispose()
        $script:InstallMutexAcquired = $false
    }
}

function Get-RepoRoot {
    $output = @(& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -ne 0 -or $output.Count -eq 0 -or [string]::IsNullOrWhiteSpace($output[0])) {
        Stop-Fail "Run this script from inside the $ProjectName Git repository."
    }

    return (Resolve-Path $output[0]).Path
}

function Get-FileHashString {
    param([Parameter(Mandatory = $true)][string]$Path)

    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToLowerInvariant()
}

function Get-ReexecArguments {
    $arguments = @()
    if ($Accelerator -ne "auto") {
        $arguments += @("-Accelerator", $Accelerator)
    }
    if ($SkipRepoSync) {
        $arguments += "-SkipRepoSync"
    }
    if ($SkipTailscaleServe) {
        $arguments += "-SkipTailscaleServe"
    }
    if ($NoScheduledTask) {
        $arguments += "-NoScheduledTask"
    }
    if ($ScheduledRun) {
        $arguments += "-ScheduledRun"
    }

    return $arguments
}

function Sync-RepoToGitHub {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ScriptPath
    )

    if ($SkipRepoSync) {
        Write-Log "Skipping repository sync because -SkipRepoSync was provided."
        return
    }

    $beforeHash = Get-FileHashString -Path $ScriptPath

    Write-Log "Fetching $RemoteName/$RemoteBranch."
    Invoke-External -FilePath "git" -Arguments @("-C", $Root, "fetch", "--prune", $RemoteName, $RemoteBranch)

    Write-Log "Forcing tracked files to $RemoteName/$RemoteBranch."
    Invoke-External -FilePath "git" -Arguments @("-C", $Root, "reset", "--hard", "$RemoteName/$RemoteBranch")

    Write-Log "Cleaning untracked files while preserving runtime env files."
    Invoke-External -FilePath "git" -Arguments @(
        "-C", $Root,
        "clean", "-fdx",
        "-e", ".env",
        "-e", ".env.local",
        "-e", ".env.*.local",
        "-e", "compose.local.yaml",
        "-e", ".local/"
    )

    $afterHash = Get-FileHashString -Path $ScriptPath
    if ($beforeHash -ne $afterHash -and $env:LOCAL_AI_API_REEXECED -ne "1") {
        Write-Log "Installer changed during update; re-executing the updated script."
        $env:LOCAL_AI_API_REEXECED = "1"
        $reexecArguments = @(Get-ReexecArguments)
        & $ScriptPath @reexecArguments
        $exitCode = if ($null -eq $LASTEXITCODE) { 0 } else { $LASTEXITCODE }
        exit $exitCode
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

function Require-DockerDesktop {
    if (-not (Test-Command "docker")) {
        Stop-Fail "Docker CLI was not found. Install Docker Desktop for Windows, then rerun this script."
    }

    Start-DockerDesktopIfNeeded

    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        Stop-Fail "Docker Compose plugin was not found. Update Docker Desktop, then rerun this script."
    }
}

function Invoke-Docker {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)

    Invoke-External -FilePath "docker" -Arguments $Arguments
}

function Invoke-DockerCompose {
    param(
        [Parameter(Mandatory = $true)][string[]]$ComposeArguments,
        [Parameter(Mandatory = $true)][string[]]$CommandArguments
    )

    Invoke-Docker -Arguments (@("compose") + $ComposeArguments + $CommandArguments)
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
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$SelectedAccelerator
    )

    switch ($SelectedAccelerator) {
        "nvidia" {
            return @("-f", (Join-Path $Root "compose.yaml"), "-f", (Join-Path $Root "compose.gpu-nvidia.yaml"))
        }
        "cpu" {
            return @("-f", (Join-Path $Root "compose.yaml"), "-f", (Join-Path $Root "compose.cpu.yaml"))
        }
        default {
            Stop-Fail "Unknown Windows accelerator profile: $SelectedAccelerator"
        }
    }
}

function Build-And-TestGatewayImage {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArguments)

    Write-Log "Validating Docker Compose configuration."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("config")

    Write-Log "Building gateway image."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("build", "gateway")

    Write-Log "Running tests inside the freshly built gateway image."
    Invoke-Docker -Arguments @(
        "run", "--rm",
        "--entrypoint", "python",
        "--workdir", "/app",
        "local-ai-api-gateway:latest",
        "-m", "pytest", "tests", "-v"
    )
}

function Start-Stack {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArguments)

    Write-Log "Starting Ollama."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("up", "-d", "ollama")

    Write-Log "Pulling required Ollama models."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("run", "--rm", "model-init")

    Write-Log "Starting gateway."
    Invoke-DockerCompose -ComposeArguments $ComposeArguments -CommandArguments @("up", "-d", "gateway")
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

function Get-TailscaleCommand {
    $command = Get-Command "tailscale" -ErrorAction SilentlyContinue
    if ($null -ne $command) {
        return $command.Source
    }

    $tailscalePath = Join-Path $env:ProgramFiles "Tailscale\tailscale.exe"
    if (Test-Path -LiteralPath $tailscalePath) {
        return $tailscalePath
    }

    return $null
}

function Configure-TailscaleServe {
    if ($SkipTailscaleServe) {
        Write-Log "Skipping Tailscale Serve configuration because -SkipTailscaleServe was provided."
        return
    }

    $tailscale = Get-TailscaleCommand
    if ($null -eq $tailscale) {
        Write-Log "Tailscale CLI is not installed; skipping Tailscale Serve configuration."
        return
    }

    & $tailscale status *> $null
    if ($LASTEXITCODE -ne 0) {
        if ($ScheduledRun) {
            Write-Log "Tailscale is not authenticated; skipping serve configuration during scheduled run."
            return
        }

        Write-Log "Tailscale is not authenticated; running tailscale up."
        & $tailscale up
        if ($LASTEXITCODE -ne 0) {
            Write-Log "tailscale up failed; skipping Tailscale Serve configuration."
            return
        }
    }

    Write-Log "Configuring Tailscale Serve for http://127.0.0.1:8080."
    & $tailscale serve --bg http://127.0.0.1:8080
    if ($LASTEXITCODE -ne 0) {
        Write-Log "Tailscale Serve command failed; leaving current serve config unchanged."
    }
}

function Install-ScheduledTask {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string]$ScriptPath
    )

    if ($NoScheduledTask) {
        Write-Log "Skipping scheduled task installation because -NoScheduledTask was provided."
        return
    }

    if ($ScheduledRun) {
        Write-Log "Running from scheduled task; skipping scheduled task installation."
        return
    }

    $taskArguments = "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -ScheduledRun"
    if ($Accelerator -ne "auto") {
        $taskArguments += " -Accelerator $Accelerator"
    }
    if ($SkipTailscaleServe) {
        $taskArguments += " -SkipTailscaleServe"
    }

    Write-Log "Installing user scheduled update task."
    $action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $taskArguments -WorkingDirectory $Root
    $triggers = @(
        (New-ScheduledTaskTrigger -AtLogOn)
        (New-ScheduledTaskTrigger -Daily -At 3am)
    )
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $triggers `
        -Settings $settings `
        -Description "Update and restart Local AI API Docker stack" `
        -Force | Out-Null
}

function Main {
    Require-Windows
    Acquire-Lock

    if (-not (Test-Command "git")) {
        Stop-Fail "Git was not found in PATH."
    }

    $root = Get-RepoRoot
    Set-Location $root
    $scriptPath = Join-Path $root "scripts\install-or-update.ps1"

    Sync-RepoToGitHub -Root $root -ScriptPath $scriptPath
    Require-DockerDesktop

    $selectedAccelerator = Get-AcceleratorProfile
    Write-Log "Selected accelerator profile: $selectedAccelerator."
    $composeArguments = @(Get-ComposeArgumentsForAccelerator -Root $root -SelectedAccelerator $selectedAccelerator)

    Build-And-TestGatewayImage -ComposeArguments $composeArguments
    Start-Stack -ComposeArguments $composeArguments

    Wait-ForUrl -Url $GatewayHealthUrl -Label "Gateway health"
    Wait-ForUrl -Url $OllamaHealthUrl -Label "Ollama health"

    Configure-TailscaleServe
    Install-ScheduledTask -Root $root -ScriptPath $scriptPath

    Write-Log "$ProjectName install/update complete."
}

try {
    Main
}
catch {
    Write-Log "ERROR: $($_.Exception.Message)"
    exit 1
}
finally {
    Release-Lock
}
