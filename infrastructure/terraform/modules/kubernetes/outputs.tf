# -----------------------------------------------------------------------------
# Kubernetes Module — Output Definitions
# -----------------------------------------------------------------------------
# Exposes key cluster resource identifiers and connection details to consuming
# modules and the root Terraform configuration. These outputs are consumed by:
#
#   - infrastructure/terraform/outputs.tf (root) — exposes cluster_endpoint,
#     kubeconfig, and load_balancer_ip to downstream consumers
#   - infrastructure/terraform/modules/security/main.tf — uses node_group_arn
#     and cluster_name for IAM role bindings and policy attachments
#   - CI/CD pipelines (GitHub Actions) — uses kubeconfig for kubectl/Helm
#     connectivity during deployment workflows
#   - Kubernetes and Helm Terraform providers — uses cluster_endpoint and
#     cluster_ca_certificate for authenticated API server communication
#
# All outputs use conditional ternary logic based on var.cloud_provider to
# select the appropriate resource reference for:
#   - AWS  → Amazon EKS (Elastic Kubernetes Service)
#   - Azure → Azure AKS (Azure Kubernetes Service)
#   - GCP  → Google GKE (Google Kubernetes Engine)
#
# Sensitive outputs (kubeconfig, cluster_ca_certificate) are marked with
# sensitive = true to prevent accidental exposure in CLI output and logs.
# -----------------------------------------------------------------------------

# -----------------------------------------------------------------------------
# Local Values — Kubeconfig Generation Templates
# -----------------------------------------------------------------------------
# AWS EKS and GCP GKE do not provide a single raw kubeconfig attribute like
# Azure AKS does. These locals generate standard kubeconfig YAML documents
# using each cluster's endpoint, CA certificate, and the provider-specific
# authentication mechanism:
#   - AWS EKS: Uses `aws eks get-token` exec-based credential plugin
#   - GCP GKE: Uses `gcloud container clusters get-credentials` exec plugin
#   - Azure AKS: Uses the native kube_config_raw attribute directly
#
# The try() function wraps all resource attribute references to ensure safe
# evaluation when the resource count is zero (provider mismatch scenarios).
# -----------------------------------------------------------------------------
locals {
  # AWS EKS kubeconfig — exec-based authentication via aws eks get-token
  # This kubeconfig uses the client.authentication.k8s.io/v1beta1 exec API
  # to dynamically obtain short-lived authentication tokens from AWS STS,
  # eliminating the need for static credentials in the kubeconfig file.
  aws_kubeconfig = var.cloud_provider == "aws" ? <<-KUBECONFIG
apiVersion: v1
kind: Config
preferences: {}
clusters:
- cluster:
    server: ${try(aws_eks_cluster.main[0].endpoint, "")}
    certificate-authority-data: ${try(aws_eks_cluster.main[0].certificate_authority[0].data, "")}
  name: ${try(aws_eks_cluster.main[0].name, "")}
contexts:
- context:
    cluster: ${try(aws_eks_cluster.main[0].name, "")}
    user: ${try(aws_eks_cluster.main[0].name, "")}
  name: ${try(aws_eks_cluster.main[0].name, "")}
current-context: ${try(aws_eks_cluster.main[0].name, "")}
users:
- name: ${try(aws_eks_cluster.main[0].name, "")}
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: aws
      args:
        - eks
        - get-token
        - --cluster-name
        - ${try(aws_eks_cluster.main[0].name, "")}
        - --region
        - ${var.region}
KUBECONFIG
  : ""

  # GCP GKE kubeconfig — exec-based authentication via gcloud CLI
  # This kubeconfig uses the gcloud container clusters get-credentials command
  # through the exec plugin API, leveraging GCP Application Default Credentials
  # or service account keys for authentication to the GKE API server.
  gcp_kubeconfig = var.cloud_provider == "gcp" ? <<-KUBECONFIG
apiVersion: v1
kind: Config
preferences: {}
clusters:
- cluster:
    server: https://${try(google_container_cluster.main[0].endpoint, "")}
    certificate-authority-data: ${try(google_container_cluster.main[0].master_auth[0].cluster_ca_certificate, "")}
  name: ${try(google_container_cluster.main[0].name, "")}
contexts:
- context:
    cluster: ${try(google_container_cluster.main[0].name, "")}
    user: ${try(google_container_cluster.main[0].name, "")}
  name: ${try(google_container_cluster.main[0].name, "")}
current-context: ${try(google_container_cluster.main[0].name, "")}
users:
- name: ${try(google_container_cluster.main[0].name, "")}
  user:
    exec:
      apiVersion: client.authentication.k8s.io/v1beta1
      command: gcloud
      args:
        - container
        - clusters
        - get-credentials
        - ${try(google_container_cluster.main[0].name, "")}
        - --region
        - ${var.region}
        - --project
        - ${try(google_container_cluster.main[0].project, "")}
KUBECONFIG
  : ""
}

# -----------------------------------------------------------------------------
# Output: cluster_endpoint
# -----------------------------------------------------------------------------
# The Kubernetes API server endpoint URL used by kubectl, Helm, CI/CD pipelines,
# and the Kubernetes/Helm Terraform providers to communicate with the cluster.
#
# Provider-specific resource paths:
#   - AWS EKS:   aws_eks_cluster.main[0].endpoint (returns full https:// URL)
#   - Azure AKS: azurerm_kubernetes_cluster.main[0].kube_config[0].host
#   - GCP GKE:   google_container_cluster.main[0].endpoint (IP only, prefixed
#                 with "https://" for standard URL format)
# -----------------------------------------------------------------------------
output "cluster_endpoint" {
  description = "The endpoint URL of the managed Kubernetes cluster API server"
  value = (
    var.cloud_provider == "aws" ? try(aws_eks_cluster.main[0].endpoint, "") :
    var.cloud_provider == "azure" ? try(azurerm_kubernetes_cluster.main[0].kube_config[0].host, "") :
    var.cloud_provider == "gcp" ? "https://${try(google_container_cluster.main[0].endpoint, "")}" :
    ""
  )
}

# -----------------------------------------------------------------------------
# Output: cluster_name
# -----------------------------------------------------------------------------
# The cluster identifier used for resource tagging, Kubernetes provider
# configuration, IAM role bindings, and monitoring/logging references.
# Follows the naming convention: ${var.project_name}-${var.environment}-cluster
#
# Provider-specific resource paths:
#   - AWS EKS:   aws_eks_cluster.main[0].name
#   - Azure AKS: azurerm_kubernetes_cluster.main[0].name
#   - GCP GKE:   google_container_cluster.main[0].name
# -----------------------------------------------------------------------------
output "cluster_name" {
  description = "The name of the managed Kubernetes cluster"
  value = (
    var.cloud_provider == "aws" ? try(aws_eks_cluster.main[0].name, "") :
    var.cloud_provider == "azure" ? try(azurerm_kubernetes_cluster.main[0].name, "") :
    var.cloud_provider == "gcp" ? try(google_container_cluster.main[0].name, "") :
    ""
  )
}

# -----------------------------------------------------------------------------
# Output: cluster_ca_certificate
# -----------------------------------------------------------------------------
# The base64-encoded CA certificate for TLS verification of the Kubernetes API
# server. Required by the Kubernetes and Helm Terraform providers, kubectl, and
# any client that needs to verify the cluster's TLS certificate chain.
#
# Marked sensitive to prevent accidental exposure in terraform plan/apply output
# and state file logging. Consumers must handle this value securely.
#
# Provider-specific resource paths:
#   - AWS EKS:   aws_eks_cluster.main[0].certificate_authority[0].data
#   - Azure AKS: azurerm_kubernetes_cluster.main[0].kube_config[0].cluster_ca_certificate
#   - GCP GKE:   google_container_cluster.main[0].master_auth[0].cluster_ca_certificate
# -----------------------------------------------------------------------------
output "cluster_ca_certificate" {
  description = "Base64-encoded CA certificate for the Kubernetes cluster"
  sensitive   = true
  value = (
    var.cloud_provider == "aws" ? try(aws_eks_cluster.main[0].certificate_authority[0].data, "") :
    var.cloud_provider == "azure" ? try(azurerm_kubernetes_cluster.main[0].kube_config[0].cluster_ca_certificate, "") :
    var.cloud_provider == "gcp" ? try(google_container_cluster.main[0].master_auth[0].cluster_ca_certificate, "") :
    ""
  )
}

# -----------------------------------------------------------------------------
# Output: kubeconfig
# -----------------------------------------------------------------------------
# Full kubeconfig content for accessing the Kubernetes cluster. This output
# provides a complete, ready-to-use kubeconfig document that can be written
# to ~/.kube/config or passed to kubectl via the KUBECONFIG environment variable.
#
# Authentication mechanisms vary by provider:
#   - AWS EKS:   exec-based plugin using `aws eks get-token` for short-lived
#                STS tokens (no static credentials stored)
#   - Azure AKS: Native kube_config_raw with Azure AD-integrated credentials
#   - GCP GKE:   exec-based plugin using `gcloud` CLI for OAuth2 token refresh
#
# Marked sensitive to prevent credential exposure in terraform output and logs.
# CI/CD pipelines should use this output with appropriate secret masking.
# -----------------------------------------------------------------------------
output "kubeconfig" {
  description = "Full kubeconfig content for accessing the Kubernetes cluster"
  sensitive   = true
  value = (
    var.cloud_provider == "aws" ? local.aws_kubeconfig :
    var.cloud_provider == "azure" ? try(azurerm_kubernetes_cluster.main[0].kube_config_raw, "") :
    var.cloud_provider == "gcp" ? local.gcp_kubeconfig :
    ""
  )
}

# -----------------------------------------------------------------------------
# Output: load_balancer_ip
# -----------------------------------------------------------------------------
# External IP address for the cluster's ingress load balancer. This IP is used
# for DNS record creation and external traffic routing to the API Gateway and
# Web Console services.
#
# IMPORTANT: For AWS and GCP, the ingress controller external IP/hostname is
# determined post-deployment when the Kubernetes Ingress Controller (e.g., NGINX
# Ingress or cloud-native ALB/NLB) provisions a cloud load balancer. This output
# returns an empty string for those providers and must be queried from the
# Kubernetes Service resource after ingress controller deployment.
#
# For Azure AKS, the cluster's outbound IP is available from the network profile,
# but the actual ingress IP is also determined post-deployment. The outbound IP
# is provided as a reference for network security group rules.
#
# Provider-specific behavior:
#   - AWS EKS:   "" (ingress controller provisions ALB/NLB post-deployment)
#   - Azure AKS: Attempts to retrieve effective outbound IPs from the load
#                 balancer profile; falls back to "" if unavailable
#   - GCP GKE:   "" (ingress controller provisions Cloud Load Balancer post-deploy)
# -----------------------------------------------------------------------------
output "load_balancer_ip" {
  description = "External IP address for the cluster load balancer (populated after ingress controller deployment)"
  value = (
    var.cloud_provider == "aws" ? "" :
    var.cloud_provider == "azure" ? try(
      tolist(azurerm_kubernetes_cluster.main[0].network_profile[0].load_balancer_profile[0].effective_outbound_ips)[0],
      ""
    ) :
    var.cloud_provider == "gcp" ? "" :
    ""
  )
}

# -----------------------------------------------------------------------------
# Output: node_group_arn
# -----------------------------------------------------------------------------
# The ARN (AWS) or resource ID (Azure/GCP) of the managed node group/pool.
# Used for IAM policy attachments, monitoring target configuration, cost
# allocation tagging, and autoscaler integration.
#
# The security module references this output to attach node-level IAM roles
# and policies. Monitoring configurations use it to scope metrics collection
# to specific node groups.
#
# Provider-specific resource paths:
#   - AWS EKS:   aws_eks_node_group.main[0].arn (full ARN for IAM references)
#   - Azure AKS: Constructed Azure Resource Manager ID for the default agent
#                 pool: {cluster_id}/agentPools/{pool_name}
#   - GCP GKE:   google_container_node_pool.main[0].id (full resource path)
# -----------------------------------------------------------------------------
output "node_group_arn" {
  description = "The ARN/ID of the managed node group for IAM and monitoring references"
  value = (
    var.cloud_provider == "aws" ? try(aws_eks_node_group.main[0].arn, "") :
    var.cloud_provider == "azure" ? try(
      "${azurerm_kubernetes_cluster.main[0].id}/agentPools/${azurerm_kubernetes_cluster.main[0].default_node_pool[0].name}",
      ""
    ) :
    var.cloud_provider == "gcp" ? try(google_container_node_pool.main[0].id, "") :
    ""
  )
}
