#!/bin/bash
# Configure app settings for all Azure services
# Run this from bash if the PowerShell script's settings step fails

RG="ragapp-rg"
CHAT="ragapp-chat-6010"
UI="ragapp-ui-6010"
UTILS="ragapp-utils-6010"
INGEST="ragapp-ingest-6010"

# Read values from .env
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$SCRIPT_DIR/../.env"

get_env() { grep "^$1=" "$ENV_FILE" | cut -d'=' -f2-; }

DB_URL=$(get_env DATABASE_URL)
OAI_ENDPOINT=$(get_env AZURE_OPENAI_ENDPOINT)
OAI_KEY=$(get_env AZURE_OPENAI_API_KEY)
OAI_VER=$(get_env AZURE_OPENAI_API_VERSION)
LLM=$(get_env DEFAULT_LLM_DEPLOYMENT)
EMBED=$(get_env EMBEDDING_DEPLOYMENT)
EMBED_DIMS=$(get_env EMBEDDING_DIMENSIONS)
SEARCH_EP=$(get_env AZURE_SEARCH_ENDPOINT)
SEARCH_KEY=$(get_env AZURE_SEARCH_ADMIN_KEY)
STORAGE_CONN=$(get_env AZURE_STORAGE_CONNECTION_STRING)
BLOB_CONTAINER=$(get_env DEFAULT_BLOB_CONTAINER)
TABLE_NAME=$(get_env AZURE_TABLE_NAME)
TOP_K=$(get_env DEFAULT_TOP_K)
JWT=$(get_env JWT_SECRET)
UI_URL="https://${UI}.azurewebsites.net"
CHAT_URL="https://${CHAT}.azurewebsites.net"
UTILS_URL="https://${UTILS}.azurewebsites.net/api"
INGEST_URL="https://${INGEST}.azurewebsites.net/api"

echo "=== Chat Web App ==="
az webapp config appsettings set --name "$CHAT" --resource-group "$RG" --output none --settings \
  "DATABASE_URL=$DB_URL" \
  "AZURE_OPENAI_ENDPOINT=$OAI_ENDPOINT" \
  "AZURE_OPENAI_API_KEY=$OAI_KEY" \
  "AZURE_OPENAI_API_VERSION=$OAI_VER" \
  "DEFAULT_LLM_DEPLOYMENT=$LLM" \
  "EMBEDDING_DEPLOYMENT=$EMBED" \
  "EMBEDDING_DIMENSIONS=$EMBED_DIMS" \
  "AZURE_SEARCH_ENDPOINT=$SEARCH_EP" \
  "AZURE_SEARCH_ADMIN_KEY=$SEARCH_KEY" \
  "AZURE_STORAGE_CONNECTION_STRING=$STORAGE_CONN" \
  "DEFAULT_BLOB_CONTAINER=$BLOB_CONTAINER" \
  "AZURE_TABLE_NAME=$TABLE_NAME" \
  "DEFAULT_TOP_K=$TOP_K" \
  "JWT_SECRET=$JWT" \
  "ALLOWED_ORIGINS=$UI_URL" \
  "SCM_DO_BUILD_DURING_DEPLOYMENT=true" && echo "  OK" || echo "  FAILED"

az webapp config set --name "$CHAT" --resource-group "$RG" --startup-file "startup.sh" --output none

echo "=== UI Web App ==="
az webapp config appsettings set --name "$UI" --resource-group "$RG" --output none --settings \
  "VITE_CHAT_API_URL=$CHAT_URL" \
  "VITE_UTILS_API_URL=$UTILS_URL" \
  "VITE_INGESTION_API_URL=$INGEST_URL" \
  "VITE_MSAL_CLIENT_ID=SET_AFTER_AD_APP_REGISTRATION" \
  "VITE_MSAL_TENANT_ID=SET_AFTER_AD_APP_REGISTRATION" \
  "VITE_MSAL_REDIRECT_URI=$UI_URL" && echo "  OK" || echo "  FAILED"

echo "=== Utils Function App ==="
az functionapp config appsettings set --name "$UTILS" --resource-group "$RG" --output none --settings \
  "DATABASE_URL=$DB_URL" \
  "AZURE_STORAGE_CONNECTION_STRING=$STORAGE_CONN" \
  "AZURE_TABLE_NAME=$TABLE_NAME" \
  "DEFAULT_BLOB_CONTAINER=$BLOB_CONTAINER" \
  "JWT_SECRET=$JWT" \
  "AZURE_SEARCH_ENDPOINT=$SEARCH_EP" \
  "AZURE_SEARCH_ADMIN_KEY=$SEARCH_KEY" && echo "  OK" || echo "  FAILED"

echo "=== Ingestion Function App ==="
az functionapp config appsettings set --name "$INGEST" --resource-group "$RG" --output none --settings \
  "DATABASE_URL=$DB_URL" \
  "AZURE_OPENAI_ENDPOINT=$OAI_ENDPOINT" \
  "AZURE_OPENAI_API_KEY=$OAI_KEY" \
  "AZURE_OPENAI_API_VERSION=$OAI_VER" \
  "EMBEDDING_DEPLOYMENT=$EMBED" \
  "EMBEDDING_DIMENSIONS=$EMBED_DIMS" \
  "AZURE_SEARCH_ENDPOINT=$SEARCH_EP" \
  "AZURE_SEARCH_ADMIN_KEY=$SEARCH_KEY" \
  "AZURE_STORAGE_CONNECTION_STRING=$STORAGE_CONN" \
  "DEFAULT_BLOB_CONTAINER=$BLOB_CONTAINER" && echo "  OK" || echo "  FAILED"

echo ""
echo "Done. Verify at: https://portal.azure.com"
