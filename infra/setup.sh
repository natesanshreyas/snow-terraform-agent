#!/usr/bin/env bash
# =============================================================================
# Snow → Terraform Agent — Full Azure Production Infrastructure Setup
# =============================================================================
# Run each section in order.  Steps that create resources can take 1-5 minutes.
# Prerequisites:
#   az login (or Cloud Shell)
#   docker available on the machine (for step 1.12)
#   Node 20+ and npm available (for servicenow-mcp-server in Docker build)
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# CONFIGURE THESE before running
# ---------------------------------------------------------------------------
SUBSCRIPTION="<your-subscription-name-or-id>"   # e.g. "My Azure Subscription"
SUFFIX="<your-unique-suffix>"                    # e.g. "contoso25" — used for storage/ACR/KV names
                                                 # must be globally unique, lowercase alphanumeric only
LOCATION="eastus2"

AOAI_ENDPOINT="https://<your-resource>.<region>.openai.azure.com/"
AOAI_DEPLOYMENT="gpt-4.1-nano"                  # your deployed model name

SERVICENOW_INSTANCE="https://devXXXXXX.service-now.com"
SERVICENOW_USERNAME="admin"
# Export credentials before running:
#   export SERVICENOW_PASSWORD="your-snow-password"
#   export GITHUB_PAT="ghp_..."

GITHUB_ORG="<your-github-org-or-username>"
GITHUB_TERRAFORM_REPO="terraform-modules-demo"  # repo that contains modules/ and examples/

PUBLISHER_EMAIL="<your-email@domain.com>"       # used by APIM
# ---------------------------------------------------------------------------

RG="snow-tf-agent-rg"
STORAGE_ACCOUNT="snowtfagent${SUFFIX}"          # 3-24 lowercase alphanumeric, globally unique
ACR_NAME="snowtfagent${SUFFIX}"
KV_NAME="snow-tf-kv-${SUFFIX}"
IMAGE="${ACR_NAME}.azurecr.io/snow-tf-agent:latest"

# Validate required exports
: "${SERVICENOW_PASSWORD:?'Export SERVICENOW_PASSWORD before running'}"
: "${GITHUB_PAT:?'Export GITHUB_PAT before running'}"

SUBSCRIPTION_ID=$(az account show --subscription "$SUBSCRIPTION" --query id -o tsv)
az account set --subscription "$SUBSCRIPTION_ID"

echo "==> Using: SUBSCRIPTION=$SUBSCRIPTION_ID  RG=$RG  LOCATION=$LOCATION  SUFFIX=$SUFFIX"

# ---------------------------------------------------------------------------
# 1.2  Resource Group
# ---------------------------------------------------------------------------
az group create --name "$RG" --location "$LOCATION" --tags app=snow-tf-agent

# ---------------------------------------------------------------------------
# 1.3  Log Analytics workspace
# ---------------------------------------------------------------------------
az monitor log-analytics workspace create \
  --resource-group "$RG" \
  --workspace-name "snow-tf-agent-logs" \
  --location "$LOCATION" \
  --sku PerGB2018 \
  --retention-time 30

LOG_WS_ID=$(az monitor log-analytics workspace show \
  --resource-group "$RG" --workspace-name "snow-tf-agent-logs" \
  --query customerId -o tsv)

LOG_WS_KEY=$(az monitor log-analytics workspace get-shared-keys \
  --resource-group "$RG" --workspace-name "snow-tf-agent-logs" \
  --query primarySharedKey -o tsv)

# ---------------------------------------------------------------------------
# 1.4  Application Insights
# ---------------------------------------------------------------------------
az monitor app-insights component create \
  --app "snow-tf-agent-ai" \
  --location "$LOCATION" \
  --resource-group "$RG" \
  --workspace "snow-tf-agent-logs" \
  --kind web

AI_CONN_STR=$(az monitor app-insights component show \
  --app "snow-tf-agent-ai" --resource-group "$RG" \
  --query connectionString -o tsv)

# ---------------------------------------------------------------------------
# 1.5  Service Bus namespace + queue
# ---------------------------------------------------------------------------
az servicebus namespace create \
  --name "snow-tf-agent-sb" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku Standard

az servicebus queue create \
  --name "provisioning-queue" \
  --namespace-name "snow-tf-agent-sb" \
  --resource-group "$RG" \
  --lock-duration "PT1H" \
  --max-delivery-count 10 \
  --default-message-time-to-live "P1D"

SB_CONN_STR=$(az servicebus namespace authorization-rule keys list \
  --namespace-name "snow-tf-agent-sb" \
  --resource-group "$RG" \
  --name RootManageSharedAccessKey \
  --query primaryConnectionString -o tsv)

# ---------------------------------------------------------------------------
# 1.6  Storage Account + container
# ---------------------------------------------------------------------------
az storage account create \
  --name "$STORAGE_ACCOUNT" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku Standard_LRS \
  --kind StorageV2 \
  --allow-blob-public-access false \
  --min-tls-version TLS1_2

STORAGE_KEY=$(az storage account keys list \
  --account-name "$STORAGE_ACCOUNT" --resource-group "$RG" \
  --query "[0].value" -o tsv)

az storage container create \
  --name "runs" \
  --account-name "$STORAGE_ACCOUNT" \
  --account-key "$STORAGE_KEY"

# ---------------------------------------------------------------------------
# 1.7  Container Registry
# ---------------------------------------------------------------------------
az acr create \
  --name "$ACR_NAME" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku Basic \
  --admin-enabled false

# ---------------------------------------------------------------------------
# 1.8  User-Assigned Managed Identity
# ---------------------------------------------------------------------------
az identity create \
  --name "snow-tf-agent-identity" \
  --resource-group "$RG" \
  --location "$LOCATION"

MI_PRINCIPAL_ID=$(az identity show --name "snow-tf-agent-identity" \
  --resource-group "$RG" --query principalId -o tsv)
MI_CLIENT_ID=$(az identity show --name "snow-tf-agent-identity" \
  --resource-group "$RG" --query clientId -o tsv)
MI_RESOURCE_ID=$(az identity show --name "snow-tf-agent-identity" \
  --resource-group "$RG" --query id -o tsv)

# ---------------------------------------------------------------------------
# 1.9  Key Vault + secrets
# ---------------------------------------------------------------------------
az keyvault create \
  --name "$KV_NAME" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku standard \
  --enable-rbac-authorization true

KV_ID=$(az keyvault show --name "$KV_NAME" \
  --resource-group "$RG" --query id -o tsv)

MY_OBJ_ID=$(az ad signed-in-user show --query id -o tsv)
az role assignment create \
  --role "Key Vault Administrator" \
  --assignee "$MY_OBJ_ID" \
  --scope "$KV_ID"

# Wait for RBAC propagation
echo "Waiting 30s for Key Vault RBAC propagation..."
sleep 30

az keyvault secret set --vault-name "$KV_NAME" \
  --name "snow-password" --value "$SERVICENOW_PASSWORD"
az keyvault secret set --vault-name "$KV_NAME" \
  --name "github-pat" --value "$GITHUB_PAT"

# ---------------------------------------------------------------------------
# 1.10  RBAC role assignments for Managed Identity
# ---------------------------------------------------------------------------
az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee "$MI_PRINCIPAL_ID" \
  --scope "$KV_ID"

STORAGE_ID=$(az storage account show --name "$STORAGE_ACCOUNT" \
  --resource-group "$RG" --query id -o tsv)
az role assignment create \
  --role "Storage Blob Data Contributor" \
  --assignee "$MI_PRINCIPAL_ID" \
  --scope "$STORAGE_ID"

SB_NS_ID=$(az servicebus namespace show --name "snow-tf-agent-sb" \
  --resource-group "$RG" --query id -o tsv)
az role assignment create \
  --role "Azure Service Bus Data Owner" \
  --assignee "$MI_PRINCIPAL_ID" \
  --scope "$SB_NS_ID"

# Azure OpenAI — find the resource by matching its endpoint hostname
AOAI_HOSTNAME=$(echo "$AOAI_ENDPOINT" | sed 's|https://||' | sed 's|/.*||')
AOAI_ID=$(az cognitiveservices account list \
  --query "[?contains(properties.endpoint,'$AOAI_HOSTNAME')].id | [0]" -o tsv)
if [ -n "$AOAI_ID" ]; then
  az role assignment create \
    --role "Cognitive Services OpenAI User" \
    --assignee "$MI_PRINCIPAL_ID" \
    --scope "$AOAI_ID"
else
  echo "WARNING: Could not find Azure OpenAI resource for endpoint '$AOAI_ENDPOINT'."
  echo "         Manually assign 'Cognitive Services OpenAI User' to $MI_PRINCIPAL_ID."
fi

ACR_ID=$(az acr show --name "$ACR_NAME" \
  --resource-group "$RG" --query id -o tsv)
az role assignment create \
  --role "AcrPull" \
  --assignee "$MI_PRINCIPAL_ID" \
  --scope "$ACR_ID"

# ---------------------------------------------------------------------------
# 1.11  ACA Environment
# ---------------------------------------------------------------------------
az containerapp env create \
  --name "snow-tf-agent-env" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --logs-workspace-id "$LOG_WS_ID" \
  --logs-workspace-key "$LOG_WS_KEY"

# ---------------------------------------------------------------------------
# Phase 3 — Azure AI Foundry Hub + Project (optional, for eval logging)
# ---------------------------------------------------------------------------
# Skip this section if your subscription policy blocks ML workspaces with
# publicNetworkAccess:Enabled — eval scores will still appear in App Insights.
# ---------------------------------------------------------------------------
az extension add --name ml --yes 2>/dev/null || true

az ml workspace create \
  --kind hub \
  --resource-group "$RG" \
  --name "snow-tf-agent-hub" \
  --location "$LOCATION" \
  --storage-account "$STORAGE_ACCOUNT" \
  --application-insights "snow-tf-agent-ai"

HUB_ID="/subscriptions/${SUBSCRIPTION_ID}/resourceGroups/${RG}/providers/Microsoft.MachineLearningServices/workspaces/snow-tf-agent-hub"

az ml workspace create \
  --kind project \
  --resource-group "$RG" \
  --name "snow-tf-agent-evals" \
  --hub-id "$HUB_ID"

FOUNDRY_HOST=$(az ml workspace show \
  --name "snow-tf-agent-evals" --resource-group "$RG" \
  --query "discovery_url" -o tsv | sed 's|https://||' | sed 's|/api/2.0/workspaces.*||')

# Connection string format: <host>;<subscription_id>;<resource_group>;<project_name>
FOUNDRY_CONN_STR="${FOUNDRY_HOST};${SUBSCRIPTION_ID};${RG};snow-tf-agent-evals"
echo "Foundry connection string: $FOUNDRY_CONN_STR"

# Grant MI the AzureML Data Scientist role on the Foundry project
FOUNDRY_ID=$(az ml workspace show \
  --name "snow-tf-agent-evals" --resource-group "$RG" --query id -o tsv)
az role assignment create \
  --role "AzureML Data Scientist" \
  --assignee "$MI_PRINCIPAL_ID" \
  --scope "$FOUNDRY_ID"

# ---------------------------------------------------------------------------
# 1.12  Build & push Docker image
# ---------------------------------------------------------------------------
echo ""
echo "==> Building and pushing Docker image..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."
az acr login --name "$ACR_NAME"
docker build -t "$IMAGE" .
docker push "$IMAGE"
cd -

# ---------------------------------------------------------------------------
# 1.13  ACA API app
# ---------------------------------------------------------------------------
az containerapp create \
  --name "snow-tf-agent-api" \
  --resource-group "$RG" \
  --environment "snow-tf-agent-env" \
  --image "$IMAGE" \
  --registry-server "${ACR_NAME}.azurecr.io" \
  --registry-identity "$MI_RESOURCE_ID" \
  --user-assigned "$MI_RESOURCE_ID" \
  --target-port 8000 \
  --ingress external \
  --min-replicas 1 --max-replicas 3 \
  --cpu 0.5 --memory 1.0Gi \
  --secrets \
    "snow-password=keyvaultref:https://${KV_NAME}.vault.azure.net/secrets/snow-password,identityref:${MI_RESOURCE_ID}" \
    "github-pat=keyvaultref:https://${KV_NAME}.vault.azure.net/secrets/github-pat,identityref:${MI_RESOURCE_ID}" \
  --env-vars \
    "AZURE_CLIENT_ID=$MI_CLIENT_ID" \
    "AZURE_OPENAI_ENDPOINT=$AOAI_ENDPOINT" \
    "AZURE_OPENAI_DEPLOYMENT_NAME=$AOAI_DEPLOYMENT" \
    "AZURE_OPENAI_API_VERSION=2024-10-21" \
    "AZURE_OPENAI_USE_AZURE_AD=true" \
    "AZURE_SERVICE_BUS_HOSTNAME=snow-tf-agent-sb.servicebus.windows.net" \
    "AZURE_SERVICE_BUS_QUEUE_NAME=provisioning-queue" \
    "AZURE_STORAGE_ACCOUNT_NAME=$STORAGE_ACCOUNT" \
    "AZURE_STORAGE_CONTAINER_NAME=runs" \
    "SERVICENOW_INSTANCE_URL=$SERVICENOW_INSTANCE" \
    "SERVICENOW_USERNAME=$SERVICENOW_USERNAME" \
    "GITHUB_ORG=$GITHUB_ORG" \
    "GITHUB_TERRAFORM_REPO=$GITHUB_TERRAFORM_REPO" \
    "AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID" \
    "SNOW_MCP_COMMAND=servicenow-mcp-server" \
    "GITHUB_MCP_COMMAND=npx @modelcontextprotocol/server-github" \
    "APPLICATIONINSIGHTS_CONNECTION_STRING=$AI_CONN_STR" \
    "SERVICENOW_PASSWORD=secretref:snow-password" \
    "GITHUB_PERSONAL_ACCESS_TOKEN=secretref:github-pat"

ACA_API_FQDN=$(az containerapp show \
  --name "snow-tf-agent-api" --resource-group "$RG" \
  --query "properties.configuration.ingress.fqdn" -o tsv)

echo "ACA API FQDN: $ACA_API_FQDN"

# ---------------------------------------------------------------------------
# 1.14  ACA Worker app
# ---------------------------------------------------------------------------
az containerapp create \
  --name "snow-tf-agent-worker" \
  --resource-group "$RG" \
  --environment "snow-tf-agent-env" \
  --image "$IMAGE" \
  --registry-server "${ACR_NAME}.azurecr.io" \
  --registry-identity "$MI_RESOURCE_ID" \
  --user-assigned "$MI_RESOURCE_ID" \
  --ingress disabled \
  --min-replicas 1 --max-replicas 5 \
  --cpu 1.0 --memory 2.0Gi \
  --command "python" \
  --args "-m,src.asb_consumer" \
  --secrets \
    "snow-password=keyvaultref:https://${KV_NAME}.vault.azure.net/secrets/snow-password,identityref:${MI_RESOURCE_ID}" \
    "github-pat=keyvaultref:https://${KV_NAME}.vault.azure.net/secrets/github-pat,identityref:${MI_RESOURCE_ID}" \
    "asb-connection-string=$SB_CONN_STR" \
  --env-vars \
    "AZURE_CLIENT_ID=$MI_CLIENT_ID" \
    "AZURE_OPENAI_ENDPOINT=$AOAI_ENDPOINT" \
    "AZURE_OPENAI_DEPLOYMENT_NAME=$AOAI_DEPLOYMENT" \
    "AZURE_OPENAI_API_VERSION=2024-10-21" \
    "AZURE_OPENAI_USE_AZURE_AD=true" \
    "AZURE_SERVICE_BUS_HOSTNAME=snow-tf-agent-sb.servicebus.windows.net" \
    "AZURE_SERVICE_BUS_QUEUE_NAME=provisioning-queue" \
    "AZURE_STORAGE_ACCOUNT_NAME=$STORAGE_ACCOUNT" \
    "AZURE_STORAGE_CONTAINER_NAME=runs" \
    "SERVICENOW_INSTANCE_URL=$SERVICENOW_INSTANCE" \
    "SERVICENOW_USERNAME=$SERVICENOW_USERNAME" \
    "GITHUB_ORG=$GITHUB_ORG" \
    "GITHUB_TERRAFORM_REPO=$GITHUB_TERRAFORM_REPO" \
    "AZURE_SUBSCRIPTION_ID=$SUBSCRIPTION_ID" \
    "SNOW_MCP_COMMAND=servicenow-mcp-server" \
    "GITHUB_MCP_COMMAND=npx @modelcontextprotocol/server-github" \
    "APPLICATIONINSIGHTS_CONNECTION_STRING=$AI_CONN_STR" \
    "SERVICENOW_PASSWORD=secretref:snow-password" \
    "GITHUB_PERSONAL_ACCESS_TOKEN=secretref:github-pat"

# KEDA ASB scaler — triggers worker scale-out when messages arrive
az containerapp update \
  --name "snow-tf-agent-worker" \
  --resource-group "$RG" \
  --scale-rule-name "asb-queue-scaler" \
  --scale-rule-type "azure-servicebus" \
  --scale-rule-auth "connection=asb-connection-string" \
  --scale-rule-metadata \
    "queueName=provisioning-queue" \
    "namespace=snow-tf-agent-sb" \
    "messageCount=1"

# ---------------------------------------------------------------------------
# 1.15  APIM (Consumption tier)
# ---------------------------------------------------------------------------

# Write policy XML first
cat > /tmp/apim-policy.xml <<'XML'
<policies>
  <inbound>
    <base />
    <rate-limit-by-key calls="30" renewal-period="60"
      counter-key="@(context.Subscription.Id)"
      increment-condition="@(context.Response.StatusCode != 429)" />
    <set-backend-service base-url="{{aca-backend-url}}" />
    <set-header name="Ocp-Apim-Subscription-Key" exists-action="delete" />
    <set-header name="X-Forwarded-Via" exists-action="override">
      <value>snow-tf-agent-apim</value>
    </set-header>
  </inbound>
  <backend><base /></backend>
  <outbound>
    <set-header name="x-ms-request-id" exists-action="delete" />
    <base />
  </outbound>
  <on-error><base /></on-error>
</policies>
XML

az apim create \
  --name "snow-tf-agent-apim" \
  --resource-group "$RG" \
  --location "$LOCATION" \
  --sku-name Consumption \
  --publisher-email "$PUBLISHER_EMAIL" \
  --publisher-name "SnowTFAgent" \
  --no-wait

echo "Waiting for APIM to be created (this takes ~5 minutes)..."
az apim wait --created --name "snow-tf-agent-apim" --resource-group "$RG"

az apim nv create \
  --service-name "snow-tf-agent-apim" --resource-group "$RG" \
  --named-value-id "aca-backend-url" \
  --display-name "aca-backend-url" \
  --value "https://$ACA_API_FQDN" \
  --secret false

az apim api create \
  --service-name "snow-tf-agent-apim" --resource-group "$RG" \
  --api-id "snow-tf-agent-api" \
  --display-name "Snow Terraform Provisioning API" \
  --path "v1" \
  --protocols https \
  --subscription-required true \
  --service-url "https://$ACA_API_FQDN"

# Operations
az apim api operation create \
  --service-name "snow-tf-agent-apim" --resource-group "$RG" \
  --api-id "snow-tf-agent-api" \
  --operation-id "post-provision" \
  --display-name "Submit Provisioning Request" \
  --method POST \
  --url-template "/api/provision"

az apim api operation create \
  --service-name "snow-tf-agent-apim" --resource-group "$RG" \
  --api-id "snow-tf-agent-api" \
  --operation-id "get-provision-status" \
  --display-name "Get Provisioning Status" \
  --method GET \
  --url-template "/api/provision/{run_id}/status"

az apim api operation create \
  --service-name "snow-tf-agent-apim" --resource-group "$RG" \
  --api-id "snow-tf-agent-api" \
  --operation-id "get-health" \
  --display-name "Health Check" \
  --method GET \
  --url-template "/health"

az apim product create \
  --service-name "snow-tf-agent-apim" --resource-group "$RG" \
  --product-id "snow-tf-agent-product" \
  --display-name "Snow TF Agent" \
  --subscription-required true \
  --approval-required false \
  --state published

az apim product api add \
  --service-name "snow-tf-agent-apim" --resource-group "$RG" \
  --product-id "snow-tf-agent-product" \
  --api-id "snow-tf-agent-api"

az apim api policy create \
  --service-name "snow-tf-agent-apim" --resource-group "$RG" \
  --api-id "snow-tf-agent-api" \
  --value @/tmp/apim-policy.xml \
  --format xml

# ---------------------------------------------------------------------------
# Done — print verification commands
# ---------------------------------------------------------------------------
APIM_GW=$(az apim show --name snow-tf-agent-apim -g "$RG" --query gatewayUrl -o tsv)
APIM_KEY=$(az apim subscription list --service-name snow-tf-agent-apim -g "$RG" \
  --query "[0].primaryKey" -o tsv)

echo ""
echo "============================================================"
echo "  Setup complete!"
echo "============================================================"
echo ""
echo "APIM Gateway : $APIM_GW"
echo "APIM Key     : $APIM_KEY"
echo ""
echo "Verification commands:"
echo ""
echo "# Health check"
echo "curl -H 'Ocp-Apim-Subscription-Key: $APIM_KEY' '$APIM_GW/v1/health'"
echo ""
echo "# Submit provisioning request"
echo "RESP=\$(curl -s -X POST -H 'Content-Type: application/json' \\"
echo "  -H 'Ocp-Apim-Subscription-Key: $APIM_KEY' \\"
echo "  -d '{\"ticket_id\":\"RITM0001234\"}' '$APIM_GW/v1/api/provision')"
echo "echo \$RESP"
echo "RUN_ID=\$(echo \$RESP | python3 -c \"import sys,json; print(json.load(sys.stdin)['run_id'])\")"
echo ""
echo "# Poll status (worker picks up within ~30s of message enqueue)"
echo "curl -H 'Ocp-Apim-Subscription-Key: $APIM_KEY' \\"
echo "  '$APIM_GW/v1/api/provision/\$RUN_ID/status'"
