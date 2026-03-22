variable "resource_group_name" {
  description = "Name of the Azure resource group that will contain all resources."
  type        = string
  default     = "snow-terraform-agent-rg"
}

variable "location" {
  description = "Azure region where all resources will be deployed."
  type        = string
  default     = "eastus2"
}

variable "cluster_name" {
  description = "Name of the AKS cluster."
  type        = string
  default     = "snow-agent-aks"
}

variable "acr_name" {
  description = <<-EOT
    Name of the Azure Container Registry. Must be globally unique across all of Azure and
    contain only lowercase alphanumeric characters. Consider appending a random suffix
    (e.g. snowagentacr<random4chars>) to avoid naming conflicts.
  EOT
  type        = string
  default     = "snowagentacr"
}

variable "dns_label" {
  description = <<-EOT
    DNS label used for the nginx ingress controller's public IP.
    The resulting FQDN will be <dns_label>.<location>.cloudapp.azure.com.
    Must be globally unique per Azure region. Consider appending a short random suffix
    (e.g. snow-agent-abc123) to avoid conflicts.
  EOT
  type        = string
  default     = "snow-agent"
}

variable "node_count" {
  description = "Number of nodes in the default AKS system node pool."
  type        = number
  default     = 2
}

variable "node_vm_size" {
  description = "VM size for the AKS system node pool."
  type        = string
  default     = "Standard_D2s_v3"
}
