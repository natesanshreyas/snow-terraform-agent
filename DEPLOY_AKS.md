# AKS Deployment Guide

## Prerequisites
- Azure CLI logged in (`az login`)
- Terraform >= 1.5
- Docker
- kubectl

---

## Step 1 — Provision infrastructure

```bash
cd infra/aks
terraform init
terraform apply
```

Note the outputs — you'll use them in the next steps:
```
hostname        = snow-agent.eastus2.cloudapp.azure.com
acr_login_server = snowagentacr.azurecr.io
aks_connect_command = az aks get-credentials ...
```

## Step 2 — Connect kubectl

```bash
az aks get-credentials --resource-group snow-terraform-agent-rg --name snow-agent-aks
```

## Step 3 — Build and push the image

```bash
az acr login --name <acr_login_server>

docker build -t <acr_login_server>/snow-terraform-agent:latest .
docker push <acr_login_server>/snow-terraform-agent:latest
```

## Step 4 — Fill in secrets

```bash
cp k8s/secret.yaml.example k8s/secret.yaml
# Edit k8s/secret.yaml — fill in:
#   AZURE_CLIENT_SECRET     (service principal secret)
#   SERVICENOW_PASSWORD
#   GITHUB_PERSONAL_ACCESS_TOKEN
#   AZURE_OPENAI_API_KEY    (only if AZURE_OPENAI_USE_AZURE_AD=false)
```

## Step 5 — Update the image in deployment.yaml

In `k8s/deployment.yaml`, replace `<ACR_LOGIN_SERVER>` with the `acr_login_server` Terraform output.

## Step 6 — Update the ingress hostname

In `k8s/ingress.yaml`, replace `<DNS_LABEL>.eastus2.cloudapp.azure.com` with the `hostname` Terraform output.

## Step 7 — Deploy

```bash
kubectl apply -f k8s/
```

Verify:
```bash
kubectl get pods
kubectl get ingress
```

## Step 8 — Set up ServiceNow

In your ServiceNow instance:

1. **System Web Services → Outbound → REST Messages → New**
   - Name: `ProvisioningAgent`
   - Endpoint: `http://<hostname>/api/provision`
   - HTTP Method: POST
   - Headers: `Content-Type: application/json`
   - Body: `{"ticket_id": "${ticket_id}"}`

2. **System Definition → Business Rules → New**
   - Table: `sc_req_item`
   - When: After Update
   - Condition: `current.approval == 'approved' && previous.approval != 'approved'`
   - Script:
     ```javascript
     var rm = new sn_ws.RESTMessageV2('ProvisioningAgent', 'trigger');
     rm.setStringParameterNoEscape('ticket_id', current.number);
     rm.execute();
     ```

---

## Updating the app

```bash
docker build -t <acr_login_server>/snow-terraform-agent:latest .
docker push <acr_login_server>/snow-terraform-agent:latest
kubectl rollout restart deployment/snow-terraform-agent
```

## Teardown

```bash
kubectl delete -f k8s/
cd infra/aks && terraform destroy
```
