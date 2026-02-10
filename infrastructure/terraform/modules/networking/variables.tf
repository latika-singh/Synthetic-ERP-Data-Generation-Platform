# =============================================================================
# Terraform Networking Module — Input Variable Definitions
# =============================================================================
# Defines all configurable parameters for VPC/VNet provisioning across
# AWS, Azure, and GCP cloud providers. These variables are supplied by the
# root infrastructure/terraform/main.tf module composition with
# environment-specific values from dev.tfvars, staging.tfvars, and prod.tfvars.
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  networking
# =============================================================================

# -----------------------------------------------------------------------------
# Cloud Provider Selection
# -----------------------------------------------------------------------------
# Determines which cloud provider's networking resources are provisioned.
# The main.tf conditionally creates AWS VPC, Azure VNet, or GCP VPC Network
# resources based on this value.
# -----------------------------------------------------------------------------
variable "cloud_provider" {
  type        = string
  description = "Cloud provider for networking resources (aws, azure, or gcp)"

  validation {
    condition     = contains(["aws", "azure", "gcp"], var.cloud_provider)
    error_message = "The cloud_provider must be one of: aws, azure, gcp."
  }
}

# -----------------------------------------------------------------------------
# Deployment Environment
# -----------------------------------------------------------------------------
# Controls environment-specific behaviour such as NAT gateway topology,
# flow-log retention, resource sizing, and tagging. The value flows into
# the common naming convention: ${project_name}-${environment}-{resource}.
# -----------------------------------------------------------------------------
variable "environment" {
  type        = string
  description = "Deployment environment (dev, staging, prod)"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "The environment must be one of: dev, staging, prod."
  }
}

# -----------------------------------------------------------------------------
# Project Name (Resource Naming Prefix)
# -----------------------------------------------------------------------------
# Used as the leading segment of every resource name to avoid collisions
# across projects sharing the same cloud account / subscription.
# -----------------------------------------------------------------------------
variable "project_name" {
  type        = string
  default     = "synthetic-erp-platform"
  description = "Project name used for resource naming prefix"
}

# -----------------------------------------------------------------------------
# Cloud Provider Region
# -----------------------------------------------------------------------------
# Specifies the geographic region where all networking resources are deployed.
# Must be a valid region identifier for the selected cloud_provider
# (e.g., us-east-1 for AWS, eastus for Azure, us-central1 for GCP).
# -----------------------------------------------------------------------------
variable "region" {
  type        = string
  description = "Cloud provider region for resource deployment"
}

# -----------------------------------------------------------------------------
# VPC / VNet CIDR Block
# -----------------------------------------------------------------------------
# The primary address space for the virtual network. Subnets for public
# (load balancers, ingress) and private (Kubernetes nodes, databases, Redis)
# tiers are carved from this block.
#
# The default /16 provides 65,534 usable addresses, sufficient for large-scale
# Kubernetes clusters with thousands of pods.
# -----------------------------------------------------------------------------
variable "vpc_cidr" {
  type        = string
  default     = "10.0.0.0/16"
  description = "CIDR block for the VPC/VNet"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "The vpc_cidr must be a valid CIDR notation (e.g., 10.0.0.0/16)."
  }
}

# -----------------------------------------------------------------------------
# Availability Zones
# -----------------------------------------------------------------------------
# List of availability zones (or GCP zones) for multi-AZ deployment of
# subnets, NAT gateways, and Kubernetes node groups. When left empty the
# main.tf will fall back to provider-specific defaults (typically 3 AZs
# in the chosen region).
# -----------------------------------------------------------------------------
variable "availability_zones" {
  type        = list(string)
  default     = []
  description = "List of availability zones for multi-AZ deployment. If empty, will use provider defaults."
}

# -----------------------------------------------------------------------------
# NAT Gateway — Enable / Disable
# -----------------------------------------------------------------------------
# Controls whether NAT gateways are provisioned for outbound internet access
# from private subnets. Required for Kubernetes nodes and backend services to
# pull container images, send metrics, and reach external APIs.
#
# May be disabled in air-gapped deployments (Constraint C-003) where a
# private registry mirror is used instead.
# -----------------------------------------------------------------------------
variable "enable_nat_gateway" {
  type        = bool
  default     = true
  description = "Whether to create NAT Gateway for private subnet outbound internet access"
}

# -----------------------------------------------------------------------------
# NAT Gateway — Single vs. Multi-AZ
# -----------------------------------------------------------------------------
# When true a single shared NAT gateway is created in one AZ, reducing cost
# — ideal for dev/staging. When false, one NAT gateway per AZ is provisioned
# for high availability — recommended for prod to eliminate single-AZ failure.
#
# Only relevant when enable_nat_gateway is true.
# -----------------------------------------------------------------------------
variable "single_nat_gateway" {
  type        = bool
  default     = true
  description = "Whether to use a single shared NAT Gateway (cost-effective for dev) or one per AZ (high availability for prod)"
}

# -----------------------------------------------------------------------------
# Public Subnet CIDR Blocks
# -----------------------------------------------------------------------------
# CIDR blocks for public subnets hosting load balancers, NGINX ingress
# controllers, and bastion hosts. One subnet is created per availability zone.
#
# The defaults carve three /24 subnets (254 addresses each) from the upper
# portion of the 10.0.0.0/16 VPC CIDR space.
# -----------------------------------------------------------------------------
variable "public_subnet_cidrs" {
  type        = list(string)
  default     = ["10.0.1.0/24", "10.0.2.0/24", "10.0.3.0/24"]
  description = "CIDR blocks for public subnets (load balancers, ingress controllers). One per availability zone."
}

# -----------------------------------------------------------------------------
# Private Subnet CIDR Blocks
# -----------------------------------------------------------------------------
# CIDR blocks for private subnets hosting Kubernetes worker nodes, MongoDB
# StatefulSets, Redis clusters, and all six backend microservices. One subnet
# is created per availability zone.
#
# The defaults carve three /24 subnets well-separated from the public range
# to simplify security group rules and network ACLs.
# -----------------------------------------------------------------------------
variable "private_subnet_cidrs" {
  type        = list(string)
  default     = ["10.0.10.0/24", "10.0.11.0/24", "10.0.12.0/24"]
  description = "CIDR blocks for private subnets (Kubernetes nodes, databases, Redis). One per availability zone."
}

# -----------------------------------------------------------------------------
# Allowed CIDR Blocks (IP Allowlist)
# -----------------------------------------------------------------------------
# CIDR blocks permitted to reach public-facing resources such as the API
# Gateway HTTPS endpoint (port 443). An empty list means no additional
# external access restriction is applied beyond the default security group /
# NSG / firewall rules — suitable for public SaaS deployments.
#
# For enterprise or air-gapped deployments, restrict this to corporate
# network CIDR ranges to enforce least-privilege access (Security R-006).
# -----------------------------------------------------------------------------
variable "allowed_cidr_blocks" {
  type        = list(string)
  default     = []
  description = "CIDR blocks allowed to access public-facing resources (API Gateway). Empty means no external access restriction beyond security group rules."
}

# -----------------------------------------------------------------------------
# VPC Flow Logs — Enable / Disable
# -----------------------------------------------------------------------------
# Enables VPC / VNet flow logs for network traffic auditing and anomaly
# detection. Required for SOC 2 Type II compliance (Constraint C-004) which
# mandates comprehensive, tamper-evident audit trails.
#
# Flow logs are directed to:
#   AWS  → CloudWatch Log Group
#   Azure → Network Watcher Flow Log (Storage Account)
#   GCP  → Stackdriver / Cloud Logging (subnet-level)
# -----------------------------------------------------------------------------
variable "enable_flow_logs" {
  type        = bool
  default     = true
  description = "Whether to enable VPC flow logs for audit and compliance (SOC 2 Type II)"
}

# -----------------------------------------------------------------------------
# VPC Flow Log Retention (Days)
# -----------------------------------------------------------------------------
# Number of days flow log records are retained. Minimum of 30 days enforced
# to satisfy audit requirements. The default of 90 days aligns with typical
# SOC 2 Type II audit windows while controlling storage costs.
#
# For 7-year audit log retention (as required by the platform's audit_logs
# collection), archival to cold storage should be handled outside Terraform
# via lifecycle policies.
# -----------------------------------------------------------------------------
variable "flow_log_retention_days" {
  type        = number
  default     = 90
  description = "Number of days to retain VPC flow logs"

  validation {
    condition     = var.flow_log_retention_days >= 30
    error_message = "Flow log retention must be at least 30 days to satisfy audit and compliance requirements."
  }
}

# -----------------------------------------------------------------------------
# Additional Resource Tags
# -----------------------------------------------------------------------------
# Supplementary tags merged with the module's default tags (project_name,
# environment, managed_by) and applied to every networking resource. Use for
# cost-centre attribution, team ownership, or compliance labels.
#
# Example:
#   tags = {
#     "CostCenter" = "engineering"
#     "Owner"      = "platform-team"
#   }
# -----------------------------------------------------------------------------
variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional tags to apply to all networking resources for cost tracking and identification"
}
