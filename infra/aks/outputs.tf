output "hostname" {
  description = "Fully qualified domain name of the application. Paste this into k8s/ingress.yaml as the host value, and use it as the endpoint URL in the ServiceNow business rule."
  value       = "${var.dns_label}.${var.location}.cloudapp.azure.com"
}

output "acr_login_server" {
  description = "Login server URL for the Azure Container Registry. Replace <ACR_LOGIN_SERVER> in k8s/deployment.yaml with this value before applying the manifest."
  value       = azurerm_container_registry.acr.login_server
}

output "aks_connect_command" {
  description = "Run this command to merge the AKS credentials into your local kubeconfig so that kubectl targets this cluster."
  value       = "az aks get-credentials --resource-group ${azurerm_resource_group.rg.name} --name ${azurerm_kubernetes_cluster.aks.name}"
}

output "resource_group" {
  description = "Name of the resource group containing all deployed resources."
  value       = azurerm_resource_group.rg.name
}
