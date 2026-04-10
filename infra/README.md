# Infrastructure

Azure Bicep templates for deploying rag-app-azure.

## Resources Provisioned

- Azure SQL Database
- Azure OpenAI Service
- Azure AI Search
- Azure Storage Account (Blob + Table)
- Azure App Service (Chat + UI)
- Azure Function App (Utils)
- Azure Function App (Ingestion, containerized)
- Azure VNet + Subnets

## Deployment

```bash
az deployment group create \
  --resource-group <rg-name> \
  --template-file bicep/main.bicep \
  --parameters @bicep/parameters.json
```
