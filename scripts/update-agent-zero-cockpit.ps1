#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$AgentZeroImageTag = $(if ([string]::IsNullOrWhiteSpace($env:AGENT_ZERO_IMAGE_TAG)) { "latest" } else { $env:AGENT_ZERO_IMAGE_TAG })
)

$ErrorActionPreference = "Stop"
$candidate = "local-ai-api-agent-zero-cockpit:candidate"
$stable = "local-ai-api-agent-zero-cockpit:latest"
$reportDir = Join-Path $RepoRoot ".local"
$reportPath = Join-Path $reportDir "agent-zero-candidate.json"

function Write-CandidateReport {
    param([string]$Status, [string]$Message)
    New-Item -ItemType Directory -Path $reportDir -Force | Out-Null
    [ordered]@{
        status = $Status
        message = $Message
        image_tag = $AgentZeroImageTag
        checked_at = (Get-Date).ToUniversalTime().ToString("o")
    } | ConvertTo-Json -Compress | Set-Content -LiteralPath $reportPath -Encoding UTF8
}

try {
    & docker pull "agent0ai/agent-zero:$AgentZeroImageTag"
    if ($LASTEXITCODE -ne 0) { throw "Could not pull the configured Agent Zero image tag." }
    & docker build --build-arg "AGENT_ZERO_IMAGE_TAG=$AgentZeroImageTag" -f (Join-Path $RepoRoot "Dockerfile.agent-zero-cockpit") -t $candidate $RepoRoot
    if ($LASTEXITCODE -ne 0) { throw "Candidate image build failed." }
    & docker run --rm --entrypoint /bin/sh $candidate -c "test -f /a0/plugins/local_ai_api_cockpit/plugin.yaml && test -f /a0/plugins/local_ai_api_cockpit/api/status.py && test -f /a0/plugins/local_ai_api_cockpit/webui/cockpit.js && python -m compileall -q /a0/plugins/local_ai_api_cockpit"
    if ($LASTEXITCODE -ne 0) { throw "Candidate overlay smoke test failed." }
    & docker tag $candidate $stable
    if ($LASTEXITCODE -ne 0) { throw "Candidate image promotion failed." }
    Write-CandidateReport -Status "passed" -Message "Candidate overlay smoke test passed and was promoted locally."
}
catch {
    Write-CandidateReport -Status "failed" -Message $_.Exception.Message
    Write-Error $_.Exception.Message
    exit 1
}
