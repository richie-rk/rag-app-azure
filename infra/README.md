# Infrastructure

PowerShell scripts to provision and tear down all Azure resources for rag-app-azure.

## Prerequisites

1. **Azure CLI** installed and logged in:
   ```bash
   az --version     # Requires 2.60+
   az login
   az account show  # Verify correct subscription
   ```

2. **PowerShell 7+** (cross-platform):
   ```bash
   pwsh --version   # Requires 7.0+
   ```

3. **Azure subscription** with:
   - Azure OpenAI access approved ([request here](https://aka.ms/oai/access))
   - Ability to create resources (Contributor role or higher)
   - No existing Free-tier SQL database (limit: 1 per subscription)
   - No existing Free-tier AI Search (limit: 1 per subscription)

## Quick Start

```powershell
cd infra
./provision-azure.ps1
```

The script prompts for a SQL admin password, then creates everything automatically. Takes 5-10 minutes.

## What Gets Created

| # | Resource | SKU / Tier | Azure Service |
|---|----------|-----------|---------------|
| 1 | Resource Group | N/A | Container for all resources |
| 2 | Storage Account | Standard_LRS | Blob (documents) + Table (chat sessions) |
| 3 | SQL Server | N/A | Logical server |
| 4 | SQL Database | Free (Serverless Gen5) | Application data (users, projects, audit) |
| 5 | AI Search | Free | Vector + semantic search indexes |
| 6 | Azure OpenAI | S0 (pay per use) | LLM and embedding models |
| 7 | App Service Plan | F1 (Free Linux) | Hosts web apps |
| 8 | Chat Web App | On F1 plan | FastAPI chat service |
| 9 | UI Web App | On F1 plan | React frontend |
| 10 | Utils Function App | Consumption | Project/user/session CRUD |
| 11 | Ingestion Function App | Consumption | Document ingestion pipeline |

Plus two model deployments on the OpenAI resource:
- `text-embedding-ada-002` (Standard, capacity 30)
- `gpt-4o` (GlobalStandard, capacity 30)

## Cost Breakdown

### Free Tier Limits

| Resource | Free Allowance | What Happens at Limit |
|----------|---------------|----------------------|
| **SQL Database** | 100,000 vCore-seconds/month, 32 GB storage | Auto-pauses; resume on next query |
| **AI Search** | 50 MB storage, 3 indexes, no semantic ranker | Hard limit, must upgrade to Basic |
| **App Service (F1)** | 60 CPU-minutes/day, 1 GB RAM, 1 GB storage | App stops until next day (no custom domain, no SSL, no always-on) |
| **Functions (Consumption)** | 1M executions/month + 400,000 GB-seconds | Billed per execution beyond free tier |
| **Storage (LRS)** | Pay per use (first 5 GB ~$0.10/month) | No free tier, but negligible cost |
| **Azure OpenAI (S0)** | No free tier, pay per token | ~$0.15/1M tokens embed, $2.50-$10/1M tokens chat |

### Estimated Monthly Cost

| Scenario | Cost |
|----------|------|
| **Idle** (all resources paused/stopped) | ~$0.01 (storage only) |
| **Light development** (few queries/day) | ~$1-5 |
| **Active development** (hundreds of queries) | ~$5-20 |
| **Production (Basic tiers)** | ~$80-150 |

The only resource with no free tier is Azure OpenAI. All other resources start at $0.

### Free Tier Gotchas

- **SQL auto-pause**: The database pauses after 60 minutes of inactivity. The first query after a pause takes 30-60 seconds to resume. This is normal, not a bug.
- **F1 CPU limit**: 60 CPU-minutes per day total across both web apps. A sustained load test will hit this fast. For anything beyond basic testing, upgrade to B1 ($13/month).
- **AI Search Free**: No semantic ranker on Free SKU. Hybrid search still works (vector + BM25), but you won't get the reranking quality boost. Also hard-limited to 50 MB indexed data (roughly 500-1000 short documents). Upgrade to Basic ($70/month) for semantic ranker and 2 GB storage.
- **No custom domains on F1**: The apps are accessible only at `*.azurewebsites.net`. Upgrade to B1 for custom domain + SSL.
- **Cold starts**: Both Function Apps (Consumption plan) have cold starts of 5-15 seconds after being idle. This is expected.

## Production Upgrade Path

| Resource | Free to Production | Monthly Cost |
|----------|------------------|-------------|
| SQL Database | Free to S0 (or GP Serverless) | $15-60 |
| AI Search | Free to Basic | $70 |
| App Service Plan | F1 to B1 (or P1v3) | $13-140 |
| Functions | Consumption to Premium EP1 | $150 |
| Storage | Standard_LRS to Standard_GRS | +$0.02/GB |

Upgrade commands:
```powershell
# SQL: upgrade to S0
az sql db update --name ragappdb --server <server> -g ragapp-rg --edition Standard --service-objective S0

# Search: upgrade to Basic
az search service update --name <search> -g ragapp-rg --sku basic

# App Service: upgrade to B1
az appservice plan update --name ragapp-plan -g ragapp-rg --sku B1
```

## Script Features

- **Idempotent**: Safe to run multiple times. It checks if each resource exists before creating.
- **Colored output**: Green (created), yellow (already exists), red (failed).
- **Continues on failure**: If one resource fails, the script continues to the next.
- **Auto-configures everything**: Sets app settings on all 4 services with connection strings, keys, and endpoints.
- **Generates .env**: Creates a ready-to-use `.env` file for local development.
- **Secure password handling**: SQL password prompted via `Read-Host -AsSecureString`.
- **JWT secret**: Auto-generated with cryptographically random bytes.
- **Developer IP**: Auto-detects your public IP and adds a SQL firewall rule.

## Teardown

```powershell
cd infra
./teardown-azure.ps1
```

Prompts for confirmation (type the resource group name), then deletes everything. Optionally cleans up local `.env` files.

To target a specific resource group:
```powershell
./teardown-azure.ps1 -ResourceGroup "my-custom-rg"
```

## Customization

Edit the variables at the top of `provision-azure.ps1`:

```powershell
$PROJECT_PREFIX    = "ragapp"      # Change to your project name
$LOCATION          = "eastus2"     # Your preferred Azure region
$OPENAI_LOCATION   = "eastus2"     # Region with OpenAI model availability
$SQL_ADMIN_USER    = "ragappadmin" # SQL admin username
```

All resource names are derived from `$PROJECT_PREFIX` with a random 4-digit suffix for global uniqueness.

## Manual Steps After Provisioning

The script prints these as "NEXT STEPS" after completion:

1. **Create Entra ID App Registration** for MSAL SSO
2. **Create AD security group** for access control
3. **Run database migrations** (SQLAlchemy `create_all`)
4. **Deploy each service** to its Azure resource

See the script output for exact commands.
