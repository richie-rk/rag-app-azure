#!/bin/bash
# Configure app settings for all Azure services.
# Invoked by provision-azure.ps1 (which exports SUFFIX); can also be run
# standalone from bash if the PowerShell script's settings step fails.
#
# Secrets are pushed into Key Vault and the app settings hold only
# @Microsoft.KeyVault(...) references, so plaintext secrets never sit in
# App Settings where anyone with Reader on the resource group could see
# them. Requires the vault + managed identities provisioned by
# provision-azure.ps1; if the vault is missing this falls back to plaintext
# settings with a loud warning so a deploy still works.

SUFFIX="${SUFFIX:-6010}"
RG="${RG:-ragapp-rg}"
CHAT="${CHAT:-ragapp-chat-$SUFFIX}"
UI="${UI:-ragapp-ui-$SUFFIX}"
UTILS="${UTILS:-ragapp-utils-$SUFFIX}"
INGEST="${INGEST:-ragapp-ingest-$SUFFIX}"
KV="${KV:-ragapp-kv-$SUFFIX}"

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

# Push secrets to Key Vault; build reference-or-plaintext setting values.
kv_ref() { echo "@Microsoft.KeyVault(SecretUri=https://${KV}.vault.azure.net/secrets/$1/)"; }

if az keyvault show --name "$KV" --resource-group "$RG" --output none 2>/dev/null; then
  echo "=== Key Vault secrets ==="
  # Abort on any failed write: continuing would apply Key Vault references
  # for secrets that were never stored, leaving every service broken at
  # startup while the script still reports success.
  az keyvault secret set --vault-name "$KV" --name "database-url" --value "$DB_URL" --output none \
    && az keyvault secret set --vault-name "$KV" --name "jwt-secret" --value "$JWT" --output none \
    && az keyvault secret set --vault-name "$KV" --name "azure-openai-api-key" --value "$OAI_KEY" --output none \
    && az keyvault secret set --vault-name "$KV" --name "azure-search-admin-key" --value "$SEARCH_KEY" --output none \
    && az keyvault secret set --vault-name "$KV" --name "azure-storage-connection-string" --value "$STORAGE_CONN" --output none \
    && echo "  OK" || { echo "  FAILED - aborting before any Key Vault references are applied"; exit 1; }

  DB_URL_SETTING=$(kv_ref database-url)
  JWT_SETTING=$(kv_ref jwt-secret)
  OAI_KEY_SETTING=$(kv_ref azure-openai-api-key)
  SEARCH_KEY_SETTING=$(kv_ref azure-search-admin-key)
  STORAGE_CONN_SETTING=$(kv_ref azure-storage-connection-string)
else
  echo "WARNING: Key Vault '$KV' not found - writing PLAINTEXT secrets to app settings."
  echo "         Run provision-azure.ps1 to create the vault and managed identities."
  DB_URL_SETTING="$DB_URL"
  JWT_SETTING="$JWT"
  OAI_KEY_SETTING="$OAI_KEY"
  SEARCH_KEY_SETTING="$SEARCH_KEY"
  STORAGE_CONN_SETTING="$STORAGE_CONN"
fi

echo "=== Chat Web App ==="
az webapp config appsettings set --name "$CHAT" --resource-group "$RG" --output none --settings \
  "DATABASE_URL=$DB_URL_SETTING" \
  "AZURE_OPENAI_ENDPOINT=$OAI_ENDPOINT" \
  "AZURE_OPENAI_API_KEY=$OAI_KEY_SETTING" \
  "AZURE_OPENAI_API_VERSION=$OAI_VER" \
  "DEFAULT_LLM_DEPLOYMENT=$LLM" \
  "EMBEDDING_DEPLOYMENT=$EMBED" \
  "EMBEDDING_DIMENSIONS=$EMBED_DIMS" \
  "AZURE_SEARCH_ENDPOINT=$SEARCH_EP" \
  "AZURE_SEARCH_ADMIN_KEY=$SEARCH_KEY_SETTING" \
  "AZURE_STORAGE_CONNECTION_STRING=$STORAGE_CONN_SETTING" \
  "DEFAULT_BLOB_CONTAINER=$BLOB_CONTAINER" \
  "AZURE_TABLE_NAME=$TABLE_NAME" \
  "DEFAULT_TOP_K=$TOP_K" \
  "JWT_SECRET=$JWT_SETTING" \
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
  "DATABASE_URL=$DB_URL_SETTING" \
  "AZURE_STORAGE_CONNECTION_STRING=$STORAGE_CONN_SETTING" \
  "AZURE_TABLE_NAME=$TABLE_NAME" \
  "DEFAULT_BLOB_CONTAINER=$BLOB_CONTAINER" \
  "JWT_SECRET=$JWT_SETTING" \
  "AZURE_SEARCH_ENDPOINT=$SEARCH_EP" \
  "AZURE_SEARCH_ADMIN_KEY=$SEARCH_KEY_SETTING" && echo "  OK" || echo "  FAILED"

echo "=== Ingestion Function App ==="
az functionapp config appsettings set --name "$INGEST" --resource-group "$RG" --output none --settings \
  "DATABASE_URL=$DB_URL_SETTING" \
  "AZURE_OPENAI_ENDPOINT=$OAI_ENDPOINT" \
  "AZURE_OPENAI_API_KEY=$OAI_KEY_SETTING" \
  "AZURE_OPENAI_API_VERSION=$OAI_VER" \
  "EMBEDDING_DEPLOYMENT=$EMBED" \
  "EMBEDDING_DIMENSIONS=$EMBED_DIMS" \
  "AZURE_SEARCH_ENDPOINT=$SEARCH_EP" \
  "AZURE_SEARCH_ADMIN_KEY=$SEARCH_KEY_SETTING" \
  "AZURE_STORAGE_CONNECTION_STRING=$STORAGE_CONN_SETTING" \
  "DEFAULT_BLOB_CONTAINER=$BLOB_CONTAINER" \
  "JWT_SECRET=$JWT_SETTING" && echo "  OK" || echo "  FAILED"

echo ""
echo "Done. Verify at: https://portal.azure.com"
