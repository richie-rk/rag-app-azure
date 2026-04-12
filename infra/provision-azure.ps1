#Requires -Version 5.1
<#
.SYNOPSIS
    Provisions ALL Azure resources for rag-app-azure from scratch.

.DESCRIPTION
    Single-run idempotent script that creates every Azure resource needed to run
    rag-app-azure end-to-end, configures app settings on all services, and
    generates a .env file for local development.

    Every resource uses the cheapest or free tier available.

.NOTES
    Prerequisites:
      - Azure CLI installed and logged in (az login)
      - Subscription with Azure OpenAI access approved
      - PowerShell 7+

.EXAMPLE
    ./provision-azure.ps1
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CONFIGURATION — edit these values                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

$PROJECT_PREFIX    = "ragapp"                    # Used to derive all resource names
$LOCATION          = "eastus"                    # For OpenAI + AI Search
$OPENAI_LOCATION   = "eastus"                    # May differ — check model availability
$COMPUTE_LOCATION  = "centralindia"              # For SQL, App Service, Web Apps
$FUNC_LOCATION     = "eastus2"                   # For Function Apps (Linux consumption)
$RESOURCE_GROUP    = "$PROJECT_PREFIX-rg"
$SQL_ADMIN_USER    = "ragappadmin"
$SQL_DB_NAME       = "ragappdb"

# Derived names (Azure naming constraints: lowercase, no special chars, globally unique)
# Reuse suffix from prior runs for idempotency; delete infra/.suffix to start fresh
$suffixFile = Join-Path $PSScriptRoot ".suffix"
if (Test-Path $suffixFile) {
    $UNIQUE_SUFFIX = (Get-Content $suffixFile).Trim()
} else {
    $UNIQUE_SUFFIX = (Get-Random -Minimum 1000 -Maximum 9999).ToString()
    Set-Content -Path $suffixFile -Value $UNIQUE_SUFFIX
}
$STORAGE_ACCOUNT   = "${PROJECT_PREFIX}store${UNIQUE_SUFFIX}"
$SQL_SERVER        = "${PROJECT_PREFIX}-sql-${UNIQUE_SUFFIX}"
$SEARCH_SERVICE    = "${PROJECT_PREFIX}-search-${UNIQUE_SUFFIX}"
$OPENAI_ACCOUNT    = "${PROJECT_PREFIX}-openai-${UNIQUE_SUFFIX}"
$APP_PLAN          = "${PROJECT_PREFIX}-plan"
$CHAT_APP          = "${PROJECT_PREFIX}-chat-${UNIQUE_SUFFIX}"
$UI_APP            = "${PROJECT_PREFIX}-ui-${UNIQUE_SUFFIX}"
$UTILS_FUNC        = "${PROJECT_PREFIX}-utils-${UNIQUE_SUFFIX}"
$INGEST_FUNC       = "${PROJECT_PREFIX}-ingest-${UNIQUE_SUFFIX}"

# Constants
$BLOB_CONTAINER    = "documents"
$TABLE_NAME        = "chatsessions"
$OPENAI_API_VER    = "2024-10-21"
$EMBED_DEPLOYMENT  = "text-embedding-ada-002"
$LLM_DEPLOYMENT    = "gpt-4o"
$EMBED_DIMS        = 1536
$TOP_K             = 10

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  HELPERS                                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

function Write-Status {
    param([string]$Message, [string]$Color = "White")
    switch ($Color) {
        "Green"  { Write-Host "  ✓ $Message" -ForegroundColor Green }
        "Yellow" { Write-Host "  ~ $Message" -ForegroundColor Yellow }
        "Red"    { Write-Host "  ✗ $Message" -ForegroundColor Red }
        "Cyan"   { Write-Host "  → $Message" -ForegroundColor Cyan }
        default  { Write-Host "    $Message" }
    }
}

function Write-Section {
    param([string]$Title)
    Write-Host ""
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor DarkGray
    Write-Host "  $Title" -ForegroundColor White
    Write-Host "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" -ForegroundColor DarkGray
}

function Get-ResourceExists {
    param([string]$ResourceId)
    $null = az resource show --ids $ResourceId 2>$null
    return $LASTEXITCODE -eq 0
}

function New-RandomSecret {
    param([int]$Length = 48)
    $bytes = [byte[]]::new($Length)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes)
}

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  PRE-FLIGHT CHECKS                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════════╗" -ForegroundColor Cyan
Write-Host "║         rag-app-azure — Azure Provisioning Script          ║" -ForegroundColor Cyan
Write-Host "╚══════════════════════════════════════════════════════════════╝" -ForegroundColor Cyan
Write-Host ""

# Verify az CLI
Write-Status "Checking Azure CLI..." "Cyan"
$azVersion = az version --output tsv 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Status "Azure CLI not found. Install from https://aka.ms/installazurecli" "Red"
    exit 1
}
Write-Status "Azure CLI found" "Green"

# Verify login
$account = az account show --output json 2>$null | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) {
    Write-Status "Not logged in. Run 'az login' first." "Red"
    exit 1
}
Write-Status "Logged in as: $($account.user.name)" "Green"
Write-Status "Subscription: $($account.name) ($($account.id))" "Green"

# Only prompt for SQL password if SQL server doesn't exist yet
$sqlPassword = $null
$sqlCheck = az sql server show --name $SQL_SERVER --resource-group $RESOURCE_GROUP --output json 2>$null
if (-not $sqlCheck) {
    Write-Host ""
    $sqlPasswordSecure = Read-Host -Prompt "  Enter SQL admin password (min 8 chars, mixed case + number)" -AsSecureString
    $sqlPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sqlPasswordSecure)
    )
    if ($sqlPassword.Length -lt 8) {
        Write-Status "Password must be at least 8 characters." "Red"
        exit 1
    }
} else {
    Write-Status "SQL Server already exists — skipping password prompt" "Green"
    # Read password from existing .env for DATABASE_URL generation
    $envFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
    if (Test-Path $envFile) {
        $dbLine = Get-Content $envFile | Where-Object { $_ -match "^DATABASE_URL=" }
        if ($dbLine -match "://[^:]+:([^@]+)@") {
            $sqlPassword = [Uri]::UnescapeDataString($Matches[1])
        }
    }
    if (-not $sqlPassword) {
        Write-Host ""
        $sqlPasswordSecure = Read-Host -Prompt "  Enter existing SQL admin password (for .env generation)" -AsSecureString
        $sqlPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sqlPasswordSecure)
        )
    }
}

# Generate JWT secret
$JWT_SECRET = New-RandomSecret

# Detect current public IP for SQL firewall
Write-Status "Detecting public IP for SQL firewall..." "Cyan"
try {
    $devIp = (Invoke-RestMethod -Uri "https://api.ipify.org" -TimeoutSec 5).Trim()
    Write-Status "Developer IP: $devIp" "Green"
} catch {
    $devIp = $null
    Write-Status "Could not detect public IP — skipping developer firewall rule" "Yellow"
}

Write-Host ""
Write-Host "  Resource naming prefix : $PROJECT_PREFIX" -ForegroundColor White
Write-Host "  Unique suffix          : $UNIQUE_SUFFIX" -ForegroundColor White
Write-Host "  Compute location       : $COMPUTE_LOCATION" -ForegroundColor White
Write-Host "  OpenAI/Search location : $OPENAI_LOCATION" -ForegroundColor White
Write-Host ""

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  1. RESOURCE GROUP                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "1/10  Resource Group"

# If RG is mid-deletion from a prior teardown, wait for it to finish
$rgState = az group show --name $RESOURCE_GROUP --query properties.provisioningState --output tsv 2>$null
if ($rgState -eq "Deleting") {
    Write-Status "$RESOURCE_GROUP is being deleted — waiting (this may take a few minutes)..." "Yellow"
    az group wait --name $RESOURCE_GROUP --deleted --timeout 600 2>$null
    Write-Status "Previous resource group deleted" "Green"
}

$rgExists = az group exists --name $RESOURCE_GROUP 2>$null
if ($rgExists -eq "true") {
    Write-Status "$RESOURCE_GROUP already exists" "Yellow"
} else {
    $rgError = az group create --name $RESOURCE_GROUP --location $COMPUTE_LOCATION --output none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$RESOURCE_GROUP created" "Green"
    } else {
        $errText = ($rgError | Out-String).Trim()
        Write-Status "Failed to create resource group — cannot continue" "Red"
        if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
        exit 1
    }
}

# Verify RG is usable before proceeding
$rgVerify = az group show --name $RESOURCE_GROUP --query properties.provisioningState --output tsv 2>$null
if ($rgVerify -ne "Succeeded") {
    Write-Status "Resource group not ready (state: $rgVerify) — wait and re-run" "Red"
    exit 1
}

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  2. STORAGE ACCOUNT                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "2/10  Storage Account"

$storageCheck = az storage account show --name $STORAGE_ACCOUNT --resource-group $RESOURCE_GROUP --output json 2>$null
if ($storageCheck) {
    Write-Status "$STORAGE_ACCOUNT already exists" "Yellow"
} else {
    $storeError = az storage account create `
        --name $STORAGE_ACCOUNT `
        --resource-group $RESOURCE_GROUP `
        --location $LOCATION `
        --sku Standard_LRS `
        --kind StorageV2 `
        --min-tls-version TLS1_2 `
        --output none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$STORAGE_ACCOUNT created (Standard_LRS)" "Green"
    } else {
        $errText = ($storeError | Out-String).Trim()
        Write-Status "Failed to create storage account" "Red"
        if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
    }
}

# Get connection string
$STORAGE_CONN_STR = az storage account show-connection-string `
    --name $STORAGE_ACCOUNT `
    --resource-group $RESOURCE_GROUP `
    --output tsv 2>$null

# Create blob container
Write-Status "Creating blob container '$BLOB_CONTAINER'..." "Cyan"
az storage container create `
    --name $BLOB_CONTAINER `
    --account-name $STORAGE_ACCOUNT `
    --auth-mode login `
    --output none 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Status "Blob container '$BLOB_CONTAINER' ready" "Green"
} else {
    # Try with connection string fallback
    az storage container create `
        --name $BLOB_CONTAINER `
        --connection-string $STORAGE_CONN_STR `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "Blob container '$BLOB_CONTAINER' ready" "Green"
    } else {
        Write-Status "Container may already exist or failed — continuing" "Yellow"
    }
}

# Create table
Write-Status "Creating table '$TABLE_NAME'..." "Cyan"
az storage table create `
    --name $TABLE_NAME `
    --connection-string $STORAGE_CONN_STR `
    --output none 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Status "Table '$TABLE_NAME' ready" "Green"
} else {
    Write-Status "Table may already exist or failed — continuing" "Yellow"
}

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  3. AZURE SQL                                                               ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "3/10  Azure SQL Server + Database"

$sqlCheck = az sql server show --name $SQL_SERVER --resource-group $RESOURCE_GROUP --output json 2>$null
if ($sqlCheck) {
    Write-Status "SQL Server $SQL_SERVER already exists" "Yellow"
} else {
    $sqlError = az sql server create `
        --name $SQL_SERVER `
        --resource-group $RESOURCE_GROUP `
        --location $COMPUTE_LOCATION `
        --admin-user $SQL_ADMIN_USER `
        --admin-password $sqlPassword `
        --output none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Status "SQL Server $SQL_SERVER created in $COMPUTE_LOCATION" "Green"
    } else {
        $errText = ($sqlError | Out-String).Trim()
        Write-Status "Failed to create SQL Server" "Red"
        if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
    }
}

# Check SQL server actually exists before configuring firewall & database
$sqlServerReady = az sql server show --name $SQL_SERVER --resource-group $RESOURCE_GROUP --output json 2>$null
if ($sqlServerReady) {
    # Firewall: allow Azure services
    Write-Status "Configuring firewall rules..." "Cyan"
    az sql server firewall-rule create `
        --server $SQL_SERVER `
        --resource-group $RESOURCE_GROUP `
        --name "AllowAzureServices" `
        --start-ip-address 0.0.0.0 `
        --end-ip-address 0.0.0.0 `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "Firewall rule: Azure services allowed" "Green"
    } else {
        Write-Status "Failed to create Azure services firewall rule" "Red"
    }

    if ($devIp) {
        az sql server firewall-rule create `
            --server $SQL_SERVER `
            --resource-group $RESOURCE_GROUP `
            --name "AllowDeveloperIP" `
            --start-ip-address $devIp `
            --end-ip-address $devIp `
            --output none 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Status "Firewall rule: Developer IP $devIp allowed" "Green"
        } else {
            Write-Status "Failed to create developer IP firewall rule" "Red"
        }
    }

    # Database (free tier)
    $dbCheck = az sql db show --name $SQL_DB_NAME --server $SQL_SERVER --resource-group $RESOURCE_GROUP --output json 2>$null
    if ($dbCheck) {
        Write-Status "Database $SQL_DB_NAME already exists" "Yellow"
    } else {
        az sql db create `
            --name $SQL_DB_NAME `
            --server $SQL_SERVER `
            --resource-group $RESOURCE_GROUP `
            --edition GeneralPurpose `
            --compute-model Serverless `
            --family Gen5 `
            --capacity 1 `
            --auto-pause-delay 60 `
            --min-capacity 0.5 `
            --use-free-limit `
            --free-limit-exhaustion-behavior AutoPause `
            --output none 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Status "Database $SQL_DB_NAME created (Free tier, serverless, 60-min auto-pause)" "Green"
        } else {
            Write-Status "Free tier unavailable — trying Basic tier..." "Yellow"
            $dbError = az sql db create `
                --name $SQL_DB_NAME `
                --server $SQL_SERVER `
                --resource-group $RESOURCE_GROUP `
                --edition Basic `
                --output none 2>&1
            if ($LASTEXITCODE -eq 0) {
                Write-Status "Database $SQL_DB_NAME created (Basic tier fallback)" "Green"
            } else {
                $errText = ($dbError | Out-String).Trim()
                Write-Status "Failed to create database" "Red"
                if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
            }
        }
    }
} else {
    Write-Status "Skipping firewall rules and database — SQL Server not available" "Yellow"
}

$SQL_FQDN = "${SQL_SERVER}.database.windows.net"
$encodedPassword = [Uri]::EscapeDataString($sqlPassword)
$DATABASE_URL = "mssql+pyodbc://${SQL_ADMIN_USER}:${encodedPassword}@${SQL_FQDN}:1433/${SQL_DB_NAME}?driver=ODBC+Driver+18+for+SQL+Server&Encrypt=yes&TrustServerCertificate=no"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  4. AZURE AI SEARCH                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "4/10  Azure AI Search"

$searchCheck = az search service show --name $SEARCH_SERVICE --resource-group $RESOURCE_GROUP --output json 2>$null
if ($searchCheck) {
    Write-Status "$SEARCH_SERVICE already exists" "Yellow"
} else {
    az search service create `
        --name $SEARCH_SERVICE `
        --resource-group $RESOURCE_GROUP `
        --location $LOCATION `
        --sku free `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$SEARCH_SERVICE created (Free SKU)" "Green"
    } else {
        Write-Status "Free tier unavailable (limit: 1 per subscription) — trying Basic SKU..." "Yellow"
        $searchError = az search service create `
            --name $SEARCH_SERVICE `
            --resource-group $RESOURCE_GROUP `
            --location $LOCATION `
            --sku basic `
            --output none 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Status "$SEARCH_SERVICE created (Basic SKU)" "Green"
        } else {
            $errText = ($searchError | Out-String).Trim()
            Write-Status "Failed to create search service" "Red"
            if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
        }
    }
}

$SEARCH_ENDPOINT = "https://${SEARCH_SERVICE}.search.windows.net"
$SEARCH_ADMIN_KEY = az search admin-key show `
    --service-name $SEARCH_SERVICE `
    --resource-group $RESOURCE_GROUP `
    --query primaryKey `
    --output tsv 2>$null

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  5. AZURE OPENAI                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "5/10  Azure OpenAI"

$openaiCheck = az cognitiveservices account show --name $OPENAI_ACCOUNT --resource-group $RESOURCE_GROUP --output json 2>$null
if ($openaiCheck) {
    Write-Status "$OPENAI_ACCOUNT already exists" "Yellow"
} else {
    az cognitiveservices account create `
        --name $OPENAI_ACCOUNT `
        --resource-group $RESOURCE_GROUP `
        --location $OPENAI_LOCATION `
        --kind OpenAI `
        --sku S0 `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$OPENAI_ACCOUNT created (S0 — pay per use)" "Green"
    } else {
        Write-Status "Failed to create OpenAI resource (ensure your subscription has OpenAI access)" "Red"
    }
}

$OPENAI_ENDPOINT = az cognitiveservices account show `
    --name $OPENAI_ACCOUNT `
    --resource-group $RESOURCE_GROUP `
    --query properties.endpoint `
    --output tsv 2>$null

$OPENAI_KEY = az cognitiveservices account keys list `
    --name $OPENAI_ACCOUNT `
    --resource-group $RESOURCE_GROUP `
    --query key1 `
    --output tsv 2>$null

# Deploy embedding model
Write-Status "Deploying $EMBED_DEPLOYMENT model..." "Cyan"
$embedCheck = az cognitiveservices account deployment show `
    --name $OPENAI_ACCOUNT `
    --resource-group $RESOURCE_GROUP `
    --deployment-name $EMBED_DEPLOYMENT `
    --output json 2>$null
if ($embedCheck) {
    Write-Status "$EMBED_DEPLOYMENT deployment already exists" "Yellow"
} else {
    az cognitiveservices account deployment create `
        --name $OPENAI_ACCOUNT `
        --resource-group $RESOURCE_GROUP `
        --deployment-name $EMBED_DEPLOYMENT `
        --model-name $EMBED_DEPLOYMENT `
        --model-version "2" `
        --model-format OpenAI `
        --sku-name Standard `
        --sku-capacity 30 `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$EMBED_DEPLOYMENT deployed (Standard, capacity 30)" "Green"
    } else {
        Write-Status "Failed to deploy $EMBED_DEPLOYMENT" "Red"
    }
}

# Deploy LLM model
Write-Status "Deploying $LLM_DEPLOYMENT model..." "Cyan"
$llmCheck = az cognitiveservices account deployment show `
    --name $OPENAI_ACCOUNT `
    --resource-group $RESOURCE_GROUP `
    --deployment-name $LLM_DEPLOYMENT `
    --output json 2>$null
if ($llmCheck) {
    Write-Status "$LLM_DEPLOYMENT deployment already exists" "Yellow"
} else {
    $llmError = az cognitiveservices account deployment create `
        --name $OPENAI_ACCOUNT `
        --resource-group $RESOURCE_GROUP `
        --deployment-name $LLM_DEPLOYMENT `
        --model-name $LLM_DEPLOYMENT `
        --model-version "2024-11-20" `
        --model-format OpenAI `
        --sku-name GlobalStandard `
        --sku-capacity 30 `
        --output none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$LLM_DEPLOYMENT deployed (GlobalStandard, capacity 30)" "Green"
    } else {
        Write-Status "GlobalStandard SKU unavailable — trying Standard..." "Yellow"
        $llmError = az cognitiveservices account deployment create `
            --name $OPENAI_ACCOUNT `
            --resource-group $RESOURCE_GROUP `
            --deployment-name $LLM_DEPLOYMENT `
            --model-name $LLM_DEPLOYMENT `
            --model-version "2024-11-20" `
            --model-format OpenAI `
            --sku-name Standard `
            --sku-capacity 30 `
            --output none 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Status "$LLM_DEPLOYMENT deployed (Standard, capacity 30)" "Green"
        } else {
            $errText = ($llmError | Out-String).Trim()
            Write-Status "Failed to deploy $LLM_DEPLOYMENT" "Red"
            if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
        }
    }
}

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  6. APP SERVICE PLAN (Free F1)                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "6/10  App Service Plan"

$planCheck = az appservice plan show --name $APP_PLAN --resource-group $RESOURCE_GROUP --output json 2>$null
if ($planCheck) {
    Write-Status "$APP_PLAN already exists" "Yellow"
} else {
    # Try F1 (free) first, then B1 as fallback
    $planError = az appservice plan create `
        --name $APP_PLAN `
        --resource-group $RESOURCE_GROUP `
        --location $COMPUTE_LOCATION `
        --sku F1 `
        --is-linux `
        --output none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$APP_PLAN created (F1 Free Linux in $COMPUTE_LOCATION)" "Green"
    } else {
        Write-Status "F1 unavailable — trying B1..." "Yellow"
        $planError = az appservice plan create `
            --name $APP_PLAN `
            --resource-group $RESOURCE_GROUP `
            --location $COMPUTE_LOCATION `
            --sku B1 `
            --is-linux `
            --output none 2>&1
        if ($LASTEXITCODE -eq 0) {
            Write-Status "$APP_PLAN created (B1 Linux in $COMPUTE_LOCATION)" "Green"
        } else {
            $errText = ($planError | Out-String).Trim()
            Write-Status "Failed to create app service plan" "Red"
            if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
        }
    }
}

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  7. CHAT WEB APP                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "7/10  Chat Web App (FastAPI)"

$chatCheck = az webapp show --name $CHAT_APP --resource-group $RESOURCE_GROUP --output json 2>$null
if ($chatCheck) {
    Write-Status "$CHAT_APP already exists" "Yellow"
} else {
    $appError = az webapp create `
        --name $CHAT_APP `
        --resource-group $RESOURCE_GROUP `
        --plan $APP_PLAN `
        --runtime "PYTHON:3.11" `
        --output none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$CHAT_APP created (Python 3.11)" "Green"
    } else {
        $errText = ($appError | Out-String).Trim()
        Write-Status "Failed to create chat web app" "Red"
        if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
    }
}

$CHAT_URL = "https://${CHAT_APP}.azurewebsites.net"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  8. UI WEB APP                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "8/10  UI Web App (React)"

$uiCheck = az webapp show --name $UI_APP --resource-group $RESOURCE_GROUP --output json 2>$null
if ($uiCheck) {
    Write-Status "$UI_APP already exists" "Yellow"
} else {
    $appError = az webapp create `
        --name $UI_APP `
        --resource-group $RESOURCE_GROUP `
        --plan $APP_PLAN `
        --runtime "NODE:20-lts" `
        --output none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$UI_APP created (Node 20)" "Green"
    } else {
        $errText = ($appError | Out-String).Trim()
        Write-Status "Failed to create UI web app" "Red"
        if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
    }
}

$UI_URL = "https://${UI_APP}.azurewebsites.net"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  9. UTILS FUNCTION APP (Consumption)                                        ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "9/10  Utils Function App"

$utilsCheck = az functionapp show --name $UTILS_FUNC --resource-group $RESOURCE_GROUP --output json 2>$null
if ($utilsCheck) {
    Write-Status "$UTILS_FUNC already exists" "Yellow"
} else {
    $funcError = az functionapp create `
        --name $UTILS_FUNC `
        --resource-group $RESOURCE_GROUP `
        --storage-account $STORAGE_ACCOUNT `
        --consumption-plan-location $FUNC_LOCATION `
        --runtime python `
        --runtime-version 3.11 `
        --functions-version 4 `
        --os-type Linux `
        --output none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$UTILS_FUNC created (Consumption plan in $COMPUTE_LOCATION)" "Green"
    } else {
        $errText = ($funcError | Out-String).Trim()
        Write-Status "Failed to create utils function app" "Red"
        if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
    }
}

$UTILS_URL = "https://${UTILS_FUNC}.azurewebsites.net/api"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  10. INGESTION FUNCTION APP (Consumption)                                   ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "10/10  Ingestion Function App"

$ingestCheck = az functionapp show --name $INGEST_FUNC --resource-group $RESOURCE_GROUP --output json 2>$null
if ($ingestCheck) {
    Write-Status "$INGEST_FUNC already exists" "Yellow"
} else {
    $funcError = az functionapp create `
        --name $INGEST_FUNC `
        --resource-group $RESOURCE_GROUP `
        --storage-account $STORAGE_ACCOUNT `
        --consumption-plan-location $FUNC_LOCATION `
        --runtime python `
        --runtime-version 3.11 `
        --functions-version 4 `
        --os-type Linux `
        --output none 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$INGEST_FUNC created (Consumption plan in $COMPUTE_LOCATION)" "Green"
    } else {
        $errText = ($funcError | Out-String).Trim()
        Write-Status "Failed to create ingestion function app" "Red"
        if ($errText) { Write-Host "    $errText" -ForegroundColor DarkRed }
    }
}

$INGEST_URL = "https://${INGEST_FUNC}.azurewebsites.net/api"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CONFIGURE APP SETTINGS                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "Configuring App Settings"

# Delegate to bash script — PowerShell mangles special chars (&, =, ;) in az CLI args
Write-Status "Configuring all app settings via bash..." "Cyan"
$configScript = Join-Path $PSScriptRoot "configure-settings.sh"
bash $configScript 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Status "All app settings configured" "Green"
} else {
    Write-Status "Some settings may have failed — check Azure Portal" "Yellow"
}

# Set startup command for chat app
az webapp config set --name $CHAT_APP --resource-group $RESOURCE_GROUP --startup-file "startup.sh" --output none 2>$null

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  GENERATE .env FILE                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "Generating .env for Local Development"

$scriptDir = $PSScriptRoot
$envPath = Join-Path (Split-Path $scriptDir -Parent) ".env"

$envContent = @"
# rag-app-azure — Auto-generated by provision-azure.ps1
# Generated: $(Get-Date -Format "yyyy-MM-dd HH:mm:ss")
# Resource Group: $RESOURCE_GROUP

# ── Database ──────────────────────────────────────────────────────────────────
DATABASE_URL=$DATABASE_URL

# ── Azure OpenAI ──────────────────────────────────────────────────────────────
AZURE_OPENAI_ENDPOINT=$OPENAI_ENDPOINT
AZURE_OPENAI_API_KEY=$OPENAI_KEY
AZURE_OPENAI_API_VERSION=$OPENAI_API_VER
DEFAULT_LLM_DEPLOYMENT=$LLM_DEPLOYMENT
EMBEDDING_DEPLOYMENT=$EMBED_DEPLOYMENT
EMBEDDING_DIMENSIONS=$EMBED_DIMS

# ── Azure AI Search ───────────────────────────────────────────────────────────
AZURE_SEARCH_ENDPOINT=$SEARCH_ENDPOINT
AZURE_SEARCH_ADMIN_KEY=$SEARCH_ADMIN_KEY

# ── Azure Storage ─────────────────────────────────────────────────────────────
AZURE_STORAGE_CONNECTION_STRING=$STORAGE_CONN_STR
DEFAULT_BLOB_CONTAINER=$BLOB_CONTAINER

# ── Azure Table Storage ──────────────────────────────────────────────────────
AZURE_TABLE_NAME=$TABLE_NAME

# ── Authentication ────────────────────────────────────────────────────────────
JWT_SECRET=$JWT_SECRET
MAGIC_LINK_BASE_URL=$UI_URL/auth/verify
MSAL_CLIENT_ID=SET_AFTER_AD_APP_REGISTRATION
MSAL_TENANT_ID=SET_AFTER_AD_APP_REGISTRATION
ALLOWED_AD_GROUPS=[]

# ── Search Defaults ───────────────────────────────────────────────────────────
DEFAULT_TOP_K=$TOP_K

# ── CORS ──────────────────────────────────────────────────────────────────────
ALLOWED_ORIGINS=http://localhost:5173,$UI_URL
"@

Set-Content -Path $envPath -Value $envContent -Encoding UTF8
Write-Status ".env written to $envPath" "Green"

# Also generate ui/.env
$uiEnvPath = Join-Path (Split-Path $scriptDir -Parent) "ui" ".env"
$uiEnvContent = @"
# UI environment — Auto-generated by provision-azure.ps1
VITE_CHAT_API_URL=http://localhost:8000
VITE_UTILS_API_URL=http://localhost:7071/api
VITE_MSAL_CLIENT_ID=SET_AFTER_AD_APP_REGISTRATION
VITE_MSAL_TENANT_ID=SET_AFTER_AD_APP_REGISTRATION
VITE_MSAL_REDIRECT_URI=http://localhost:5173
"@

Set-Content -Path $uiEnvPath -Value $uiEnvContent -Encoding UTF8
Write-Status "ui/.env written to $uiEnvPath" "Green"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  SUMMARY                                                                    ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Host ""
Write-Host "╔══════════════════════════════════════════════════════════════╗" -ForegroundColor Green
Write-Host "║                  Provisioning Complete                      ║" -ForegroundColor Green
Write-Host "╚══════════════════════════════════════════════════════════════╝" -ForegroundColor Green
Write-Host ""

Write-Host ""
Write-Host "  RESOURCE SUMMARY" -ForegroundColor White
Write-Host "  ================================================================" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Resource Group       : $RESOURCE_GROUP"
Write-Host "  Location             : $LOCATION"
Write-Host ""
Write-Host "  Storage Account      : $STORAGE_ACCOUNT"
Write-Host "  SQL Server           : $SQL_FQDN"
Write-Host "  SQL Database         : $SQL_DB_NAME"
Write-Host "  AI Search            : $SEARCH_ENDPOINT"
Write-Host "  Azure OpenAI         : $OPENAI_ENDPOINT"
Write-Host "    Embedding model    : $EMBED_DEPLOYMENT"
Write-Host "    Chat LLM           : $LLM_DEPLOYMENT"
Write-Host "  App Service Plan     : $APP_PLAN"
Write-Host "  Chat Web App         : $CHAT_URL"
Write-Host "  UI Web App           : $UI_URL"
Write-Host "  Utils Function App   : $UTILS_URL"
Write-Host "  Ingestion Func App   : $INGEST_URL"
Write-Host ""
Write-Host "  Estimated cost: ~`$0 idle, ~`$1-5/mo light use (OpenAI pay-per-token)" -ForegroundColor Gray

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  NEXT STEPS                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Host "  NEXT STEPS" -ForegroundColor Cyan
Write-Host "  ════════════════════════════════════════════════════════════════" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  1. Create Azure AD App Registration for MSAL SSO:" -ForegroundColor White
Write-Host "     az ad app create --display-name `"rag-app-azure`" \" -ForegroundColor Gray
Write-Host "       --web-redirect-uris `"$UI_URL`" `"http://localhost:5173`" \" -ForegroundColor Gray
Write-Host "       --enable-id-token-issuance true" -ForegroundColor Gray
Write-Host "     Then update VITE_MSAL_CLIENT_ID and VITE_MSAL_TENANT_ID" -ForegroundColor Gray
Write-Host "     in both .env and the UI Web App settings." -ForegroundColor Gray
Write-Host ""
Write-Host "  2. Create AD security group for access control:" -ForegroundColor White
Write-Host "     az ad group create --display-name `"rag-app-users`" \" -ForegroundColor Gray
Write-Host "       --mail-nickname `"rag-app-users`"" -ForegroundColor Gray
Write-Host "     Add the group's object ID to ALLOWED_AD_GROUPS in .env." -ForegroundColor Gray
Write-Host ""
Write-Host "  3. Run database migrations:" -ForegroundColor White
Write-Host "     cd services/chat && pip install -r requirements.txt" -ForegroundColor Gray
Write-Host "     python -c `"from services.shared.models import Base; \" -ForegroundColor Gray
Write-Host "       from services.shared.database import get_engine; \" -ForegroundColor Gray
Write-Host "       Base.metadata.create_all(get_engine())`"" -ForegroundColor Gray
Write-Host ""
Write-Host "  4. Deploy services:" -ForegroundColor White
Write-Host "     # Chat service" -ForegroundColor Gray
Write-Host "     cd services/chat && az webapp up --name $CHAT_APP" -ForegroundColor Gray
Write-Host ""
Write-Host "     # Utils service" -ForegroundColor Gray
Write-Host "     cd services/utils && func azure functionapp publish $UTILS_FUNC" -ForegroundColor Gray
Write-Host ""
Write-Host "     # Ingestion service" -ForegroundColor Gray
Write-Host "     cd services/ingestion && func azure functionapp publish $INGEST_FUNC" -ForegroundColor Gray
Write-Host ""
Write-Host "     # UI" -ForegroundColor Gray
Write-Host "     cd ui && npm run build && az webapp up --name $UI_APP" -ForegroundColor Gray
Write-Host ""
Write-Host "  5. For local development, start services with:" -ForegroundColor White
Write-Host "     cd services/chat && uvicorn main:app --reload --port 8000" -ForegroundColor Gray
Write-Host "     cd services/utils && func start" -ForegroundColor Gray
Write-Host "     cd ui && npm run dev" -ForegroundColor Gray
Write-Host ""
