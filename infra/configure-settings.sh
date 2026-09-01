#!/bin/bash
# Configure app settings for all Azure services.
# Invoked by provision-azure.ps1 (which exports SUFFIX); can also be run
# standalone from bash if the PowerShell script's settings step fails.
#
# Secrets are pushed into Key Vault and the app settings hold only
# @Microsoft.KeyVault(...) references, so plaintext secrets never sit in
# App Settings where anyone with Reader on the resource group could see
# them. Requires the vault + managed identities provisioned by
# provision-azure.ps1. A missing vault is a hard failure unless
# ALLOW_PLAINTEXT_FALLBACK=1 is set explicitly; silent plaintext downgrade
# would defeat the reason the vault exists.
#
# Exit status: nonzero if ANY step failed, so callers (provision-azure.ps1)
# can refuse to report a successful deployment over broken settings.

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

# Track failures across all steps; the exit status at the bottom reports them.
FAILED=0

# Push secrets to Key Vault; build the Key Vault reference setting values.
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
elif [ "${ALLOW_PLAINTEXT_FALLBACK:-0}" = "1" ]; then
  # Explicit opt-in only: a deliberate choice for vault-less environments,
  # never a silent downgrade.
  echo "WARNING: Key Vault '$KV' not found - writing PLAINTEXT secrets to app settings"
  echo "         (ALLOW_PLAINTEXT_FALLBACK=1 was set)."
  DB_URL_SETTING="$DB_URL"
  JWT_SETTING="$JWT"
  OAI_KEY_SETTING="$OAI_KEY"
  SEARCH_KEY_SETTING="$SEARCH_KEY"
  STORAGE_CONN_SETTING="$STORAGE_CONN"
else
  echo "ERROR: Key Vault '$KV' not found. Run provision-azure.ps1 to create the vault"
  echo "       and managed identities, or set ALLOW_PLAINTEXT_FALLBACK=1 to knowingly"
  echo "       write plaintext secrets to app settings."
  exit 1
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
  "SCM_DO_BUILD_DURING_DEPLOYMENT=true" && echo "  OK" || { echo "  FAILED"; FAILED=1; }

az webapp config set --name "$CHAT" --resource-group "$RG" --startup-file "startup.sh" --output none \
  || { echo "  FAILED to set chat startup file"; FAILED=1; }

echo "=== UI Web App ==="
az webapp config appsettings set --name "$UI" --resource-group "$RG" --output none --settings \
  "VITE_CHAT_API_URL=$CHAT_URL" \
  "VITE_UTILS_API_URL=$UTILS_URL" \
  "VITE_INGESTION_API_URL=$INGEST_URL" \
  "VITE_MSAL_CLIENT_ID=SET_AFTER_AD_APP_REGISTRATION" \
  "VITE_MSAL_TENANT_ID=SET_AFTER_AD_APP_REGISTRATION" \
  "VITE_MSAL_REDIRECT_URI=$UI_URL" && echo "  OK" || { echo "  FAILED"; FAILED=1; }

echo "=== Utils Function App ==="
az functionapp config appsettings set --name "$UTILS" --resource-group "$RG" --output none --settings \
  "DATABASE_URL=$DB_URL_SETTING" \
  "AZURE_STORAGE_CONNECTION_STRING=$STORAGE_CONN_SETTING" \
  "AZURE_TABLE_NAME=$TABLE_NAME" \
  "DEFAULT_BLOB_CONTAINER=$BLOB_CONTAINER" \
  "JWT_SECRET=$JWT_SETTING" \
  "AZURE_SEARCH_ENDPOINT=$SEARCH_EP" \
  "AZURE_SEARCH_ADMIN_KEY=$SEARCH_KEY_SETTING" && echo "  OK" || { echo "  FAILED"; FAILED=1; }

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
  "JWT_SECRET=$JWT_SETTING" && echo "  OK" || { echo "  FAILED"; FAILED=1; }

echo ""
if [ "$FAILED" -ne 0 ]; then
  echo "Done WITH FAILURES: at least one service kept missing or stale settings."
  echo "Re-run this script after fixing the errors above, and verify at: https://portal.azure.com"
  exit 1
fi
echo "Done. Verify at: https://portal.azure.com"
