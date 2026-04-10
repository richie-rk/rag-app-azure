#Requires -Version 7.0
<#
.SYNOPSIS
    Deletes all Azure resources for rag-app-azure by removing the resource group.

.DESCRIPTION
    Prompts for confirmation, then deletes the entire resource group and all
    resources within it. This action is irreversible.

.EXAMPLE
    ./teardown-azure.ps1
    ./teardown-azure.ps1 -ResourceGroup "ragapp-rg"
#>

param(
    [string]$ResourceGroup = "ragapp-rg"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════════╗" -ForegroundColor Red
Write-Host "║           rag-app-azure — Resource Group Teardown          ║" -ForegroundColor Red
Write-Host "╚══════════════════════════════════════════════════════════════╝" -ForegroundColor Red
Write-Host ""

# Verify the resource group exists
$rgExists = az group exists --name $ResourceGroup 2>$null
if ($rgExists -ne "true") {
    Write-Host "  Resource group '$ResourceGroup' does not exist. Nothing to delete." -ForegroundColor Yellow
    exit 0
}

# Show what will be deleted
Write-Host "  Resource group: $ResourceGroup" -ForegroundColor White
Write-Host ""
Write-Host "  Resources that will be PERMANENTLY DELETED:" -ForegroundColor Yellow

$resources = az resource list --resource-group $ResourceGroup --output json 2>$null | ConvertFrom-Json
if ($resources) {
    foreach ($r in $resources) {
        Write-Host "    - $($r.type.Split('/')[-1]): $($r.name)" -ForegroundColor Gray
    }
} else {
    Write-Host "    (empty resource group)" -ForegroundColor Gray
}

Write-Host ""
Write-Host "  WARNING: This will delete ALL resources above including databases," -ForegroundColor Red
Write-Host "  storage accounts, and all data within them. This cannot be undone." -ForegroundColor Red
Write-Host ""

$confirmation = Read-Host "  Type the resource group name to confirm deletion [$ResourceGroup]"
if ($confirmation -ne $ResourceGroup) {
    Write-Host ""
    Write-Host "  Confirmation did not match. Aborting." -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "  Deleting resource group '$ResourceGroup'..." -ForegroundColor Cyan
Write-Host "  (This may take 2-5 minutes)" -ForegroundColor Gray

az group delete --name $ResourceGroup --yes --no-wait 2>$null

if ($LASTEXITCODE -eq 0) {
    Write-Host ""
    Write-Host "  Deletion initiated. The resource group is being removed in the background." -ForegroundColor Green
    Write-Host "  Monitor progress: az group show --name $ResourceGroup" -ForegroundColor Gray
    Write-Host ""

    # Clean up local .env files
    $scriptDir = $PSScriptRoot
    $envPath = Join-Path (Split-Path $scriptDir -Parent) ".env"
    $uiEnvPath = Join-Path (Split-Path $scriptDir -Parent) "ui" ".env"

    if (Test-Path $envPath) {
        $cleanEnv = Read-Host "  Delete local .env file? (y/N)"
        if ($cleanEnv -eq "y") {
            Remove-Item $envPath -Force
            Write-Host "  .env deleted" -ForegroundColor Green
        }
    }
    if (Test-Path $uiEnvPath) {
        $cleanUiEnv = Read-Host "  Delete local ui/.env file? (y/N)"
        if ($cleanUiEnv -eq "y") {
            Remove-Item $uiEnvPath -Force
            Write-Host "  ui/.env deleted" -ForegroundColor Green
        }
    }
} else {
    Write-Host "  Failed to delete resource group." -ForegroundColor Red
}

Write-Host ""
