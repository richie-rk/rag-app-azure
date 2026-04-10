#Requires -Version 7.0
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
$LOCATION          = "eastus2"                   # Primary region
$OPENAI_LOCATION   = "eastus2"                   # May differ — check model availability
$RESOURCE_GROUP    = "$PROJECT_PREFIX-rg"
$SQL_ADMIN_USER    = "ragappadmin"
$SQL_DB_NAME       = "ragappdb"

# Derived names (Azure naming constraints: lowercase, no special chars, globally unique)
$UNIQUE_SUFFIX     = (Get-Random -Minimum 1000 -Maximum 9999).ToString()
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

# Prompt for SQL password
Write-Host ""
$sqlPasswordSecure = Read-Host -Prompt "  Enter SQL admin password (min 8 chars, mixed case + number)" -AsSecureString
$sqlPassword = [System.Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($sqlPasswordSecure)
)

if ($sqlPassword.Length -lt 8) {
    Write-Status "Password must be at least 8 characters." "Red"
    exit 1
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
Write-Host "  Location               : $LOCATION" -ForegroundColor White
Write-Host "  OpenAI location        : $OPENAI_LOCATION" -ForegroundColor White
Write-Host ""

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  1. RESOURCE GROUP                                                          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "1/10  Resource Group"

$rgExists = az group exists --name $RESOURCE_GROUP 2>$null
if ($rgExists -eq "true") {
    Write-Status "$RESOURCE_GROUP already exists" "Yellow"
} else {
    az group create --name $RESOURCE_GROUP --location $LOCATION --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$RESOURCE_GROUP created" "Green"
    } else {
        Write-Status "Failed to create resource group" "Red"
    }
}

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  2. STORAGE ACCOUNT                                                         ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "2/10  Storage Account"

$storageCheck = az storage account show --name $STORAGE_ACCOUNT --resource-group $RESOURCE_GROUP --output json 2>$null
if ($storageCheck) {
    Write-Status "$STORAGE_ACCOUNT already exists" "Yellow"
} else {
    az storage account create `
        --name $STORAGE_ACCOUNT `
        --resource-group $RESOURCE_GROUP `
        --location $LOCATION `
        --sku Standard_LRS `
        --kind StorageV2 `
        --min-tls-version TLS1_2 `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$STORAGE_ACCOUNT created (Standard_LRS)" "Green"
    } else {
        Write-Status "Failed to create storage account" "Red"
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
    az sql server create `
        --name $SQL_SERVER `
        --resource-group $RESOURCE_GROUP `
        --location $LOCATION `
        --admin-user $SQL_ADMIN_USER `
        --admin-password $sqlPassword `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "SQL Server $SQL_SERVER created" "Green"
    } else {
        Write-Status "Failed to create SQL Server" "Red"
    }
}

# Firewall: allow Azure services
Write-Status "Configuring firewall rules..." "Cyan"
az sql server firewall-rule create `
    --server $SQL_SERVER `
    --resource-group $RESOURCE_GROUP `
    --name "AllowAzureServices" `
    --start-ip-address 0.0.0.0 `
    --end-ip-address 0.0.0.0 `
    --output none 2>$null
Write-Status "Firewall rule: Azure services allowed" "Green"

if ($devIp) {
    az sql server firewall-rule create `
        --server $SQL_SERVER `
        --resource-group $RESOURCE_GROUP `
        --name "AllowDeveloperIP" `
        --start-ip-address $devIp `
        --end-ip-address $devIp `
        --output none 2>$null
    Write-Status "Firewall rule: Developer IP $devIp allowed" "Green"
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
        Write-Status "Failed to create database (free tier may already be used on this subscription)" "Red"
        Write-Status "Retrying with Basic tier (£3.22/mo)..." "Yellow"
        az sql db create `
            --name $SQL_DB_NAME `
            --server $SQL_SERVER `
            --resource-group $RESOURCE_GROUP `
            --edition Basic `
            --output none 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Status "Database $SQL_DB_NAME created (Basic tier fallback)" "Green"
        } else {
            Write-Status "Failed to create database" "Red"
        }
    }
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
        Write-Status "Failed to create search service (free tier limit: 1 per subscription)" "Red"
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
    az cognitiveservices account deployment create `
        --name $OPENAI_ACCOUNT `
        --resource-group $RESOURCE_GROUP `
        --deployment-name $LLM_DEPLOYMENT `
        --model-name $LLM_DEPLOYMENT `
        --model-version "2024-08-06" `
        --model-format OpenAI `
        --sku-name GlobalStandard `
        --sku-capacity 30 `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$LLM_DEPLOYMENT deployed (GlobalStandard, capacity 30)" "Green"
    } else {
        Write-Status "Failed to deploy $LLM_DEPLOYMENT" "Red"
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
    az appservice plan create `
        --name $APP_PLAN `
        --resource-group $RESOURCE_GROUP `
        --location $LOCATION `
        --sku F1 `
        --is-linux `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$APP_PLAN created (F1 Free Linux)" "Green"
    } else {
        Write-Status "Failed to create app service plan" "Red"
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
    az webapp create `
        --name $CHAT_APP `
        --resource-group $RESOURCE_GROUP `
        --plan $APP_PLAN `
        --runtime "PYTHON:3.11" `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$CHAT_APP created (Python 3.11 on F1)" "Green"
    } else {
        Write-Status "Failed to create chat web app" "Red"
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
    az webapp create `
        --name $UI_APP `
        --resource-group $RESOURCE_GROUP `
        --plan $APP_PLAN `
        --runtime "NODE:20-lts" `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$UI_APP created (Node 20 on F1)" "Green"
    } else {
        Write-Status "Failed to create UI web app" "Red"
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
    az functionapp create `
        --name $UTILS_FUNC `
        --resource-group $RESOURCE_GROUP `
        --storage-account $STORAGE_ACCOUNT `
        --consumption-plan-location $LOCATION `
        --runtime python `
        --runtime-version 3.11 `
        --functions-version 4 `
        --os-type Linux `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$UTILS_FUNC created (Consumption plan)" "Green"
    } else {
        Write-Status "Failed to create utils function app" "Red"
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
    az functionapp create `
        --name $INGEST_FUNC `
        --resource-group $RESOURCE_GROUP `
        --storage-account $STORAGE_ACCOUNT `
        --consumption-plan-location $LOCATION `
        --runtime python `
        --runtime-version 3.11 `
        --functions-version 4 `
        --os-type Linux `
        --output none 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Status "$INGEST_FUNC created (Consumption plan)" "Green"
    } else {
        Write-Status "Failed to create ingestion function app" "Red"
    }
}

$INGEST_URL = "https://${INGEST_FUNC}.azurewebsites.net/api"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  CONFIGURE APP SETTINGS                                                     ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

Write-Section "Configuring App Settings"

# ── Chat Web App ──────────────────────────────────────────────────────────────
Write-Status "Setting Chat Web App configuration..." "Cyan"
az webapp config appsettings set `
    --name $CHAT_APP `
    --resource-group $RESOURCE_GROUP `
    --settings `
        DATABASE_URL="$DATABASE_URL" `
        AZURE_OPENAI_ENDPOINT="$OPENAI_ENDPOINT" `
        AZURE_OPENAI_API_KEY="$OPENAI_KEY" `
        AZURE_OPENAI_API_VERSION="$OPENAI_API_VER" `
        DEFAULT_LLM_DEPLOYMENT="$LLM_DEPLOYMENT" `
        EMBEDDING_DEPLOYMENT="$EMBED_DEPLOYMENT" `
        EMBEDDING_DIMENSIONS="$EMBED_DIMS" `
        AZURE_SEARCH_ENDPOINT="$SEARCH_ENDPOINT" `
        AZURE_SEARCH_ADMIN_KEY="$SEARCH_ADMIN_KEY" `
        AZURE_STORAGE_CONNECTION_STRING="$STORAGE_CONN_STR" `
        DEFAULT_BLOB_CONTAINER="$BLOB_CONTAINER" `
        AZURE_TABLE_NAME="$TABLE_NAME" `
        DEFAULT_TOP_K="$TOP_K" `
        JWT_SECRET="$JWT_SECRET" `
        ALLOWED_ORIGINS="$UI_URL" `
        SCM_DO_BUILD_DURING_DEPLOYMENT="true" `
    --output none 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Status "Chat Web App: 16 settings configured" "Green"
} else {
    Write-Status "Failed to configure Chat Web App settings" "Red"
}

# Set startup command
az webapp config set `
    --name $CHAT_APP `
    --resource-group $RESOURCE_GROUP `
    --startup-file "startup.sh" `
    --output none 2>$null

# ── UI Web App ────────────────────────────────────────────────────────────────
Write-Status "Setting UI Web App configuration..." "Cyan"
az webapp config appsettings set `
    --name $UI_APP `
    --resource-group $RESOURCE_GROUP `
    --settings `
        VITE_CHAT_API_URL="$CHAT_URL" `
        VITE_UTILS_API_URL="$UTILS_URL" `
        VITE_INGESTION_API_URL="$INGEST_URL" `
        VITE_MSAL_CLIENT_ID="<SET_AFTER_AD_APP_REGISTRATION>" `
        VITE_MSAL_TENANT_ID="<SET_AFTER_AD_APP_REGISTRATION>" `
        VITE_MSAL_REDIRECT_URI="$UI_URL" `
    --output none 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Status "UI Web App: 6 settings configured" "Green"
} else {
    Write-Status "Failed to configure UI Web App settings" "Red"
}

# ── Utils Function App ────────────────────────────────────────────────────────
Write-Status "Setting Utils Function App configuration..." "Cyan"
az functionapp config appsettings set `
    --name $UTILS_FUNC `
    --resource-group $RESOURCE_GROUP `
    --settings `
        DATABASE_URL="$DATABASE_URL" `
        AZURE_STORAGE_CONNECTION_STRING="$STORAGE_CONN_STR" `
        AZURE_TABLE_NAME="$TABLE_NAME" `
        DEFAULT_BLOB_CONTAINER="$BLOB_CONTAINER" `
        JWT_SECRET="$JWT_SECRET" `
        AZURE_SEARCH_ENDPOINT="$SEARCH_ENDPOINT" `
        AZURE_SEARCH_ADMIN_KEY="$SEARCH_ADMIN_KEY" `
    --output none 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Status "Utils Function App: 7 settings configured" "Green"
} else {
    Write-Status "Failed to configure Utils Function App settings" "Red"
}

# ── Ingestion Function App ────────────────────────────────────────────────────
Write-Status "Setting Ingestion Function App configuration..." "Cyan"
az functionapp config appsettings set `
    --name $INGEST_FUNC `
    --resource-group $RESOURCE_GROUP `
    --settings `
        DATABASE_URL="$DATABASE_URL" `
        AZURE_OPENAI_ENDPOINT="$OPENAI_ENDPOINT" `
        AZURE_OPENAI_API_KEY="$OPENAI_KEY" `
        AZURE_OPENAI_API_VERSION="$OPENAI_API_VER" `
        EMBEDDING_DEPLOYMENT="$EMBED_DEPLOYMENT" `
        EMBEDDING_DIMENSIONS="$EMBED_DIMS" `
        AZURE_SEARCH_ENDPOINT="$SEARCH_ENDPOINT" `
        AZURE_SEARCH_ADMIN_KEY="$SEARCH_ADMIN_KEY" `
        AZURE_STORAGE_CONNECTION_STRING="$STORAGE_CONN_STR" `
        DEFAULT_BLOB_CONTAINER="$BLOB_CONTAINER" `
    --output none 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Status "Ingestion Function App: 10 settings configured" "Green"
} else {
    Write-Status "Failed to configure Ingestion Function App settings" "Red"
}

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
MSAL_CLIENT_ID=<SET_AFTER_AD_APP_REGISTRATION>
MSAL_TENANT_ID=<SET_AFTER_AD_APP_REGISTRATION>
ALLOWED_AD_GROUPS=

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
VITE_MSAL_CLIENT_ID=<SET_AFTER_AD_APP_REGISTRATION>
VITE_MSAL_TENANT_ID=<SET_AFTER_AD_APP_REGISTRATION>
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

$summary = @"

  RESOURCE SUMMARY
  ════════════════════════════════════════════════════════════════

  Resource Group       $RESOURCE_GROUP
  Location             $LOCATION

  ┌──────────────────────────────────────────────────────────────┐
  │ Resource             │ Name / URL                │ Cost/mo   │
  ├──────────────────────┼───────────────────────────┼───────────┤
  │ Storage Account      │ $STORAGE_ACCOUNT          │ ~$0.01    │
  │ SQL Server           │ $SQL_FQDN                 │ Free*     │
  │ SQL Database         │ $SQL_DB_NAME              │ Free*     │
  │ AI Search            │ $SEARCH_ENDPOINT          │ Free      │
  │ Azure OpenAI         │ $OPENAI_ENDPOINT          │ Pay/use   │
  │   └─ Embedding       │ $EMBED_DEPLOYMENT         │ ~$0.10/1M │
  │   └─ Chat LLM        │ $LLM_DEPLOYMENT           │ ~$2.50/1M │
  │ App Service Plan     │ $APP_PLAN (F1)            │ Free      │
  │ Chat Web App         │ $CHAT_URL                 │ Free      │
  │ UI Web App           │ $UI_URL                   │ Free      │
  │ Utils Function App   │ $UTILS_URL                │ Free**    │
  │ Ingestion Func App   │ $INGEST_URL               │ Free**    │
  └──────────────────────┴───────────────────────────┴───────────┘

  * SQL Free tier: 100K vCore-seconds/mo, 32GB storage, auto-pause after 60min
  ** Consumption plan: 1M executions + 400K GB-s free per month

  Estimated monthly cost (idle):  ~$0.01  (storage only)
  Estimated monthly cost (light): ~$1-5   (OpenAI usage dependent)

"@

Write-Host $summary

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
