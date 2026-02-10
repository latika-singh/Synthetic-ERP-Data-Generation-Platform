# =============================================================================
# Terraform Kubernetes Module — Input Variable Definitions
# =============================================================================
# Defines all configurable parameters for managed Kubernetes cluster
# provisioning across AWS (EKS), Azure (AKS), and GCP (GKE). These variables
# are supplied by the root infrastructure/terraform/main.tf module composition
# which passes networking module outputs (vpc_id, subnet_ids) and
# environment-specific values from dev.tfvars, staging.tfvars, and prod.tfvars.
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  kubernetes
# =============================================================================

# -----------------------------------------------------------------------------
# Cloud Provider Selection
# -----------------------------------------------------------------------------
# Determines which managed Kubernetes service is provisioned:
#   - "aws"   → Amazon Elastic Kubernetes Service (EKS)
#   - "azure" → Azure Kubernetes Service (AKS)
#   - "gcp"   → Google Kubernetes Engine (GKE)
#
# The main.tf conditionally creates provider-specific cluster resources,
# IAM roles, node groups, and logging integrations based on this value.
# -----------------------------------------------------------------------------
variable "cloud_provider" {
  type        = string
  description = "Cloud provider for Kubernetes cluster provisioning (aws, azure, or gcp)"

  validation {
    condition     = contains(["aws", "azure", "gcp"], var.cloud_provider)
    error_message = "cloud_provider must be one of: aws, azure, gcp"
  }
}

# -----------------------------------------------------------------------------
# Deployment Environment
# -----------------------------------------------------------------------------
# Controls environment-specific behaviour such as node pool sizing, logging
# verbosity, private endpoint settings, and resource tagging. The value flows
# into the common naming convention: ${project_name}-${environment}-cluster.
#
# Typical configurations:
#   dev     — smaller nodes, fewer replicas, public endpoint
#   staging — production-like sizing, private endpoint optional
#   prod    — full-scale nodes, private endpoint, enhanced logging
# -----------------------------------------------------------------------------
variable "environment" {
  type        = string
  description = "Deployment environment (dev, staging, prod)"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod"
  }
}

# -----------------------------------------------------------------------------
# Project Name (Resource Naming Prefix)
# -----------------------------------------------------------------------------
# Used as the leading segment of every resource name to avoid collisions
# across projects sharing the same cloud account or subscription. Combined
# with environment for cluster identification:
#   ${project_name}-${environment}-cluster
# -----------------------------------------------------------------------------
variable "project_name" {
  type        = string
  default     = "synthetic-erp-platform"
  description = "Project name used for resource naming prefix and cluster identification"
}

# -----------------------------------------------------------------------------
# Kubernetes Cluster Version
# -----------------------------------------------------------------------------
# Specifies the Kubernetes version for the managed cluster control plane and
# node pools. The platform requires Kubernetes 1.29 or higher per the
# technical specification (Kubernetes 1.29+ container orchestration).
#
# The validation block ensures the version string begins with a major.minor
# pattern of 1.29 or above to prevent accidental deployment of unsupported
# older versions. Patch versions (e.g., "1.29.2") are also accepted.
#
# Note: The actual available versions depend on the cloud provider and region.
# Verify availability with:
#   AWS:   aws eks describe-addon-versions
#   Azure: az aks get-versions --location <region>
#   GCP:   gcloud container get-server-config --zone <zone>
# -----------------------------------------------------------------------------
variable "cluster_version" {
  type        = string
  default     = "1.29"
  description = "Kubernetes version for the managed cluster. Must be 1.29 or higher per platform requirements."

  validation {
    condition = can(regex("^1\\.(29|[3-9][0-9]|[0-9]{3,})", var.cluster_version))
    error_message = "cluster_version must be 1.29 or higher. Provide a version string such as \"1.29\", \"1.30\", or \"1.29.4\"."
  }
}

# -----------------------------------------------------------------------------
# Worker Node Instance Type / VM Size
# -----------------------------------------------------------------------------
# Specifies the compute instance type for Kubernetes worker nodes in the
# managed node group or node pool. The default targets AWS; override for
# other providers:
#
#   AWS:   t3.large     (2 vCPU, 8 GiB — general-purpose burstable)
#   Azure: Standard_D4s_v3  (4 vCPU, 16 GiB — general-purpose)
#   GCP:   e2-standard-4    (4 vCPU, 16 GiB — general-purpose)
#
# For the Generation Engine workloads (AI/ML with PyTorch/TensorFlow),
# consider GPU-enabled instance types (e.g., p3.2xlarge, Standard_NC6s_v3,
# n1-standard-4 with attached GPU) and separate node pools.
# -----------------------------------------------------------------------------
variable "node_instance_type" {
  type        = string
  default     = "t3.large"
  description = "Instance type/VM size for Kubernetes worker nodes. Default is AWS t3.large; use Standard_D4s_v3 for Azure or e2-standard-4 for GCP."
}

# -----------------------------------------------------------------------------
# Minimum Worker Nodes
# -----------------------------------------------------------------------------
# Lower bound for the node group/pool size. The cluster autoscaler will
# never scale below this count, ensuring baseline capacity for core platform
# services (API Gateway, Generation Engine, Quality Service, etc.).
#
# Recommended minimums:
#   dev     — 1-2 nodes
#   staging — 2-3 nodes
#   prod    — 3+ nodes (for HA across availability zones)
# -----------------------------------------------------------------------------
variable "min_nodes" {
  type        = number
  default     = 2
  description = "Minimum number of worker nodes in the node group/pool. Used as the lower bound for cluster autoscaler."

  validation {
    condition     = var.min_nodes >= 1
    error_message = "min_nodes must be at least 1 to ensure cluster availability."
  }
}

# -----------------------------------------------------------------------------
# Maximum Worker Nodes
# -----------------------------------------------------------------------------
# Upper bound for the node group/pool size. The cluster autoscaler will
# scale up to this limit when pods cannot be scheduled due to insufficient
# resources. This is the primary lever for supporting HPA horizontal
# scalability targeting 1M+ records/minute throughput.
#
# Recommended maximums:
#   dev     — 5 nodes   (cost control)
#   staging — 10 nodes  (moderate scale testing)
#   prod    — 20+ nodes (production throughput requirements)
# -----------------------------------------------------------------------------
variable "max_nodes" {
  type        = number
  default     = 10
  description = "Maximum number of worker nodes in the node group/pool. Used as the upper bound for cluster autoscaler to support HPA horizontal scalability."

  validation {
    condition     = var.max_nodes >= 1
    error_message = "max_nodes must be at least 1 to ensure cluster availability."
  }
}

# -----------------------------------------------------------------------------
# Desired / Initial Worker Nodes
# -----------------------------------------------------------------------------
# The initial number of worker nodes launched when the cluster is first
# provisioned or updated. The cluster autoscaler will adjust the count
# between min_nodes and max_nodes based on actual pod resource requests.
#
# This value should be set between min_nodes and max_nodes. If the cluster
# autoscaler is enabled, it will override this count as needed.
# -----------------------------------------------------------------------------
variable "desired_nodes" {
  type        = number
  default     = 3
  description = "Desired/initial number of worker nodes in the node group/pool."

  validation {
    condition     = var.desired_nodes >= 1
    error_message = "desired_nodes must be at least 1 to ensure cluster availability."
  }
}

# -----------------------------------------------------------------------------
# VPC / VNet / Network ID
# -----------------------------------------------------------------------------
# The identifier of the VPC (AWS), VNet (Azure), or VPC Network (GCP) created
# by the networking module. The Kubernetes cluster and its node groups are
# placed within this network to inherit security group rules, route tables,
# and NAT gateway connectivity.
#
# This value is passed from the root main.tf:
#   module.kubernetes.vpc_id = module.networking.vpc_id
# -----------------------------------------------------------------------------
variable "vpc_id" {
  type        = string
  description = "VPC/VNet/Network ID from the networking module for cluster placement. Passed from module.networking.vpc_id."
}

# -----------------------------------------------------------------------------
# Private Subnet IDs
# -----------------------------------------------------------------------------
# List of private subnet identifiers from the networking module where
# Kubernetes worker nodes are placed. Using private subnets ensures that
# worker nodes are not directly accessible from the public internet,
# aligning with the platform's security requirements (TLS 1.3, AES-256,
# network isolation).
#
# Multi-AZ placement is achieved by providing subnets across different
# availability zones, which the managed Kubernetes service distributes
# nodes across for high availability.
#
# This value is passed from the root main.tf:
#   module.kubernetes.subnet_ids = module.networking.private_subnet_ids
# -----------------------------------------------------------------------------
variable "subnet_ids" {
  type        = list(string)
  description = "List of private subnet IDs from the networking module for Kubernetes node placement. Passed from module.networking.private_subnet_ids."
}

# -----------------------------------------------------------------------------
# Cluster Autoscaler Toggle
# -----------------------------------------------------------------------------
# Enables or disables the Kubernetes cluster autoscaler, which automatically
# adjusts the number of worker nodes based on pending pod resource requests.
# When enabled, the autoscaler scales between min_nodes and max_nodes.
#
# The cluster autoscaler works in conjunction with the Horizontal Pod
# Autoscaler (HPA) defined in infrastructure/kubernetes/hpa.yaml:
#   - HPA scales pods horizontally based on CPU/memory metrics
#   - Cluster Autoscaler scales nodes when pods cannot be scheduled
#
# This is essential for meeting the 1M+ records/minute throughput target
# during peak generation workloads.
#
# Provider-specific implementation:
#   AWS:   Managed via IAM policy + Kubernetes Deployment
#   Azure: Built-in AKS auto-scaling (enable_auto_scaling on node pool)
#   GCP:   Built-in GKE autoscaling on node pool configuration
# -----------------------------------------------------------------------------
variable "enable_cluster_autoscaler" {
  type        = bool
  default     = true
  description = "Whether to enable the Kubernetes cluster autoscaler for automatic node scaling based on pod resource requests."
}

# -----------------------------------------------------------------------------
# Cloud Provider Region
# -----------------------------------------------------------------------------
# The geographic region for the Kubernetes cluster deployment. Must be a
# valid region identifier for the selected cloud_provider:
#
#   AWS:   e.g., us-east-1, eu-west-1, ap-southeast-1
#   Azure: e.g., eastus, westeurope, southeastasia
#   GCP:   e.g., us-central1, europe-west1, asia-southeast1
#
# The region affects latency, data residency compliance, available instance
# types, and Kubernetes version availability. For GKE, the cluster is
# created as a regional cluster (multi-zone) for high availability.
# -----------------------------------------------------------------------------
variable "region" {
  type        = string
  description = "Cloud provider region for cluster deployment. Used for provider-specific region configuration."
}

# -----------------------------------------------------------------------------
# Additional Resource Tags
# -----------------------------------------------------------------------------
# Supplementary tags merged with the module's default tags (project_name,
# environment, managed_by) and applied to every Kubernetes cluster resource.
# Use for cost-centre attribution, team ownership, compliance labels, or
# custom metadata.
#
# Example:
#   tags = {
#     "CostCenter"  = "engineering"
#     "Owner"       = "platform-team"
#     "Compliance"  = "soc2-type-ii"
#   }
#
# Note: Tag key/value constraints vary by provider:
#   AWS:   Max 50 tags, key max 128 chars, value max 256 chars
#   Azure: Max 50 tags, key max 512 chars, value max 256 chars
#   GCP:   Max 64 labels, key/value max 63 chars, lowercase + hyphens only
# -----------------------------------------------------------------------------
variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional tags to apply to all Kubernetes cluster resources for cost tracking and identification."
}
