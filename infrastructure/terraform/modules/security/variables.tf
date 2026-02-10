# =============================================================================
# Terraform Security Module — Input Variable Definitions
# =============================================================================
# Defines all configurable parameters for security infrastructure provisioning
# across AWS, Azure, and GCP cloud providers. This module manages IAM roles,
# KMS encryption keys, secrets manager instances, and network ACL rules for
# the Synthetic-ERP-Data-Generation-Platform.
#
# These variables are supplied by the root infrastructure/terraform/main.tf
# module composition which passes kubernetes module outputs (cluster_name,
# cluster_oidc_issuer_url) and environment-specific values from dev.tfvars,
# staging.tfvars, and prod.tfvars.
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  security
# =============================================================================

# -----------------------------------------------------------------------------
# Cloud Provider Selection
# -----------------------------------------------------------------------------
# Determines which cloud provider's security resources are provisioned:
#   - "aws"   → AWS IAM, KMS, Secrets Manager, Network ACLs
#   - "azure" → Azure Key Vault, Managed Identities, NSG rules
#   - "gcp"   → GCP IAM, Cloud KMS, Secret Manager, Firewall rules
#
# The main.tf conditionally creates provider-specific IAM roles, encryption
# keys, secrets manager instances, and network security rules based on this
# value using the count = var.cloud_provider == "provider" ? 1 : 0 pattern.
# -----------------------------------------------------------------------------
variable "cloud_provider" {
  type        = string
  description = "Cloud provider for security resources (aws, azure, or gcp)"

  validation {
    condition     = contains(["aws", "azure", "gcp"], var.cloud_provider)
    error_message = "cloud_provider must be one of: aws, azure, gcp"
  }
}

# -----------------------------------------------------------------------------
# Deployment Environment
# -----------------------------------------------------------------------------
# Controls environment-specific behaviour such as KMS key policies, secrets
# rotation schedules, IAM role boundaries, and network ACL strictness.
# The value flows into the common naming convention:
#   ${project_name}-${environment}-{resource}
#
# Typical configurations:
#   dev     — relaxed key policies, shorter secret retention, broader ACLs
#   staging — production-like policies for pre-release validation
#   prod    — strict key policies, full rotation, restrictive ACLs,
#             SOC 2 Type II compliance enforced
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
# Used as the leading segment of every security resource name to avoid
# collisions across projects sharing the same cloud account or subscription.
# Combined with environment for resource identification:
#   ${project_name}-${environment}-kms-key
#   ${project_name}-${environment}-service-role
# -----------------------------------------------------------------------------
variable "project_name" {
  type        = string
  default     = "synthetic-erp-platform"
  description = "Project name used for resource naming prefix and security resource identification"
}

# -----------------------------------------------------------------------------
# Cloud Provider Region
# -----------------------------------------------------------------------------
# Specifies the geographic region where all security resources are deployed.
# Must be a valid region identifier for the selected cloud_provider:
#
#   AWS:   e.g., us-east-1, eu-west-1, ap-southeast-1
#   Azure: e.g., eastus, westeurope, southeastasia
#   GCP:   e.g., us-central1, europe-west1, asia-southeast1
#
# For KMS keys the region determines where the key material is stored and
# processed, which may affect data residency compliance requirements.
# GCP KMS key rings are regional resources and cannot be moved after creation.
# -----------------------------------------------------------------------------
variable "region" {
  type        = string
  description = "Cloud provider region for security resource deployment"
}

# -----------------------------------------------------------------------------
# KMS Encryption Key Creation Toggle
# -----------------------------------------------------------------------------
# Controls whether KMS encryption keys are provisioned for AES-256
# data-at-rest encryption. When enabled, the module creates:
#
#   AWS:   aws_kms_key resources with automatic key rotation
#   Azure: azurerm_key_vault_key resources in a dedicated Key Vault
#   GCP:   google_kms_crypto_key resources in a regional key ring
#
# Two keys are created: one for data encryption and one for audit log
# encryption (separate key for defense-in-depth).
#
# Required for SOC 2 Type II compliance (Constraint C-004) which mandates
# AES-256 encryption at rest for all stored data including MongoDB
# collections, Redis data, and exported datasets.
#
# Disabling this is only appropriate in development environments that use
# provider-managed default encryption.
# -----------------------------------------------------------------------------
variable "enable_kms" {
  type        = bool
  default     = true
  description = "Whether to create KMS encryption keys for AES-256 data-at-rest encryption. Required for SOC 2 Type II compliance."
}

# -----------------------------------------------------------------------------
# Secrets Manager Provisioning Toggle
# -----------------------------------------------------------------------------
# Controls whether secrets manager resources are provisioned for storing
# sensitive configuration data. When enabled, the module creates secrets for:
#
#   - MongoDB connection credentials
#   - Redis connection credentials
#   - JWT signing secret
#   - Auth0 client credentials
#   - Encryption key references
#
# Provider-specific resources created:
#   AWS:   aws_secretsmanager_secret with KMS encryption
#   Azure: azurerm_key_vault_secret in the module's Key Vault
#   GCP:   google_secret_manager_secret with automatic replication
#
# Secrets are created with placeholder JSON templates; actual values are
# injected via CI/CD pipelines or manual rotation processes. Never commit
# real credentials to Terraform state or version control.
# -----------------------------------------------------------------------------
variable "enable_secrets_manager" {
  type        = bool
  default     = true
  description = "Whether to provision secrets manager resources (AWS Secrets Manager, Azure Key Vault, GCP Secret Manager) for storing database credentials, API keys, and JWT secrets."
}

# -----------------------------------------------------------------------------
# Kubernetes Cluster Name
# -----------------------------------------------------------------------------
# The name of the managed Kubernetes cluster created by the kubernetes module.
# Used for binding cloud IAM roles to Kubernetes service accounts:
#
#   AWS:   IRSA (IAM Roles for Service Accounts) — trust policy references
#          the EKS cluster's OIDC provider ARN
#   Azure: AAD Pod Identity / Workload Identity — federated credential
#          linked to the AKS cluster identity
#   GCP:   Workload Identity — IAM binding between GCP service accounts
#          and Kubernetes service accounts in the cluster
#
# This value is passed from the root main.tf:
#   module.security.cluster_name = module.kubernetes.cluster_name
#
# An empty default allows the security module to be applied before the
# Kubernetes module during initial infrastructure bootstrapping; IAM
# bindings are completed once the cluster is available.
# -----------------------------------------------------------------------------
variable "cluster_name" {
  type        = string
  default     = ""
  description = "Name of the Kubernetes cluster from the kubernetes module. Used for IRSA (AWS), Workload Identity (GCP), and AAD pod identity (Azure) bindings."
}

# -----------------------------------------------------------------------------
# Kubernetes Cluster OIDC Issuer URL
# -----------------------------------------------------------------------------
# The OIDC (OpenID Connect) issuer URL of the Kubernetes cluster, primarily
# required for AWS IRSA (IAM Roles for Service Accounts) trust policies.
#
# For AWS EKS, the OIDC issuer URL is used to create an IAM OIDC identity
# provider which enables pods to assume IAM roles via projected service
# account tokens. The trust policy for each service role references this
# URL to restrict role assumption to specific service accounts.
#
# For Azure and GCP, workload identity federation uses different mechanisms
# but this URL can still serve as a reference for audit and documentation.
#
# This value is passed from the root main.tf:
#   module.security.cluster_oidc_issuer_url = module.kubernetes.cluster_oidc_issuer_url
#
# An empty default allows the security module to be applied before the
# Kubernetes module during initial bootstrapping.
# -----------------------------------------------------------------------------
variable "cluster_oidc_issuer_url" {
  type        = string
  default     = ""
  description = "OIDC issuer URL of the Kubernetes cluster for IAM role trust policies (AWS IRSA). Passed from the kubernetes module output."
}

# -----------------------------------------------------------------------------
# Kubernetes Service Account Names
# -----------------------------------------------------------------------------
# List of Kubernetes service account names corresponding to the platform's
# six backend microservices. For each service account name, the module
# creates a cloud-native identity with least-privilege policies:
#
#   api-gateway          → KMS Decrypt, Secrets read
#   generation-engine    → KMS Encrypt/Decrypt, Storage read/write, Secrets read
#   profiling-service    → KMS Decrypt, Secrets read (DB credentials only)
#   quality-service      → KMS Decrypt, Storage read, Secrets read
#   compliance-service   → KMS Decrypt, Secrets read, Audit log write
#   provisioning-service → KMS Encrypt/Decrypt, Storage full access, Secrets read
#
# Provider-specific identities created:
#   AWS:   IAM Roles with IRSA trust policies
#   Azure: User-assigned Managed Identities with RBAC role assignments
#   GCP:   GCP Service Accounts with IAM bindings
#
# The default list includes all six microservices defined in the platform
# architecture. Additional service accounts can be appended for sidecars,
# monitoring agents, or future services.
# -----------------------------------------------------------------------------
variable "service_account_names" {
  type        = list(string)
  default     = ["api-gateway", "generation-engine", "profiling-service", "quality-service", "compliance-service", "provisioning-service"]
  description = "List of Kubernetes service account names corresponding to the six microservices. Used to create IAM roles/managed identities/service accounts with least-privilege policies for each service."
}

# -----------------------------------------------------------------------------
# Allowed CIDR Blocks (Network ACL Allowlist)
# -----------------------------------------------------------------------------
# CIDR blocks permitted in network ACL rules for additional defense-in-depth
# security. These rules complement the VPC-level security groups and NSGs
# created by the networking module, providing a second layer of network
# access control.
#
# Typical usage:
#   - Corporate VPN CIDR ranges for administrative access
#   - CI/CD runner IP ranges for deployment pipelines
#   - Partner network ranges for API integration
#
# An empty list (default) means no additional NACL restrictions are applied
# beyond the networking module's security groups — suitable for environments
# where VPC-level rules are sufficient.
#
# For air-gapped deployments (Constraint C-003), restrict to internal
# network CIDRs only to prevent any external connectivity.
# -----------------------------------------------------------------------------
variable "allowed_cidr_blocks" {
  type        = list(string)
  default     = []
  description = "CIDR blocks allowed in network ACL rules for additional defense-in-depth security. Complements VPC-level security groups from the networking module."
}

# -----------------------------------------------------------------------------
# KMS Key Rotation Period (Days)
# -----------------------------------------------------------------------------
# Number of days between automatic KMS key rotations. Automatic key rotation
# creates a new version of the cryptographic key material while retaining
# previous versions for decrypting existing data.
#
# SOC 2 Type II compliance (Constraint C-004) recommends key rotation every
# 90 days or less to limit the exposure window of any potentially
# compromised key material.
#
# Provider-specific behaviour:
#   AWS:   KMS keys support automatic annual rotation natively; custom
#          rotation intervals require AWS Lambda-based rotation
#   Azure: Key Vault keys support configurable rotation policies with
#          automatic rotation at the specified interval
#   GCP:   Cloud KMS supports configurable rotation periods specified
#          in seconds (key_rotation_days * 86400)
#
# Validation enforces a minimum of 30 days (to prevent excessive key
# version accumulation) and a maximum of 365 days (to ensure at least
# annual rotation for compliance).
# -----------------------------------------------------------------------------
variable "key_rotation_days" {
  type        = number
  default     = 90
  description = "Number of days between automatic KMS key rotations. SOC 2 Type II recommends 90 days or less."

  validation {
    condition     = var.key_rotation_days >= 30 && var.key_rotation_days <= 365
    error_message = "key_rotation_days must be between 30 and 365"
  }
}

# -----------------------------------------------------------------------------
# VPC / VNet Identifier
# -----------------------------------------------------------------------------
# The identifier of the VPC (AWS), VNet (Azure), or VPC Network (GCP)
# created by the networking module. Used for associating network ACL rules
# and security configurations with the correct virtual network.
#
# This value is passed from the root main.tf:
#   module.security.vpc_id = module.networking.vpc_id
#
# An empty default allows the security module to be applied independently
# for IAM and KMS resources that do not require VPC association (e.g.,
# during initial bootstrapping or for global resources like IAM roles).
# -----------------------------------------------------------------------------
variable "vpc_id" {
  type        = string
  default     = ""
  description = "VPC/VNet identifier from the networking module for network ACL association."
}

# -----------------------------------------------------------------------------
# Subnet Identifiers
# -----------------------------------------------------------------------------
# List of subnet identifiers from the networking module used for scoping
# network security rules (NACLs, NSG rules, firewall rules) to specific
# network segments.
#
# Typically includes both public and private subnet IDs to apply
# defense-in-depth security rules:
#   - Public subnets: restrict inbound to HTTPS (443) from allowed CIDRs
#   - Private subnets: restrict inbound to service ports from VPC CIDR only
#
# This value is passed from the root main.tf:
#   module.security.subnet_ids = concat(
#     module.networking.public_subnet_ids,
#     module.networking.private_subnet_ids
#   )
#
# An empty default allows the security module to operate without subnet
# associations when only IAM, KMS, or secrets resources are needed.
# -----------------------------------------------------------------------------
variable "subnet_ids" {
  type        = list(string)
  default     = []
  description = "Subnet identifiers from the networking module for network security rule scoping."
}

# -----------------------------------------------------------------------------
# Additional Resource Tags
# -----------------------------------------------------------------------------
# Supplementary tags merged with the module's default tags (project_name,
# environment, managed_by) and applied to every security resource. Use for
# cost-centre attribution, team ownership, compliance labels, or custom
# metadata required by organizational tagging policies.
#
# Example:
#   tags = {
#     "CostCenter"  = "engineering"
#     "Owner"       = "platform-team"
#     "Compliance"  = "soc2-type-ii"
#     "DataClass"   = "confidential"
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
  description = "Additional tags to apply to all security resources for cost tracking, compliance labeling, and environment identification."
}
