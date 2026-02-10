# =============================================================================
# Terraform Storage Module — Input Variable Definitions
# =============================================================================
# Defines all configurable parameters for cloud storage provisioning across
# AWS S3, Azure Blob Storage, and GCP Cloud Storage. This module manages
# storage buckets for synthetic data exports and audit log retention for the
# Synthetic-ERP-Data-Generation-Platform.
#
# Two storage targets are provisioned per cloud provider:
#   - synthetic-data: Export destination for generated synthetic ERP data in
#     SQL, CSV, JSON, and Parquet formats via the Provisioning Service
#   - audit-logs: Tamper-evident audit trail storage with 7-year retention
#     for SOC 2 Type II compliance (Constraint C-004)
#
# These variables are supplied by the root infrastructure/terraform/main.tf
# module composition which passes the security module's KMS key outputs and
# environment-specific values from dev.tfvars, staging.tfvars, and prod.tfvars.
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  storage
# =============================================================================

# -----------------------------------------------------------------------------
# Cloud Provider Selection
# -----------------------------------------------------------------------------
# Determines which cloud provider's storage resources are provisioned:
#   - "aws"   → AWS S3 buckets with server-side encryption, lifecycle rules,
#                CORS, public access blocks, and bucket policies
#   - "azure" → Azure Storage Account with Blob containers, geo-redundant
#                replication, encryption, and management policies
#   - "gcp"   → GCP Cloud Storage buckets with uniform bucket-level access,
#                KMS encryption, lifecycle rules, and IAM bindings
#
# The main.tf conditionally creates provider-specific storage resources based
# on this value using the count = var.cloud_provider == "provider" ? 1 : 0
# pattern. Only one provider's resources are created per deployment.
# -----------------------------------------------------------------------------
variable "cloud_provider" {
  type        = string
  description = "Cloud provider for storage resources (aws, azure, or gcp)"

  validation {
    condition     = contains(["aws", "azure", "gcp"], var.cloud_provider)
    error_message = "cloud_provider must be one of: aws, azure, gcp"
  }
}

# -----------------------------------------------------------------------------
# Deployment Environment
# -----------------------------------------------------------------------------
# Controls environment-specific behaviour such as storage redundancy levels,
# lifecycle policy aggressiveness, CORS restrictions, and force-destroy
# settings. The value flows into the common naming convention:
#   ${bucket_name_prefix}-${environment}-{purpose}
#
# Typical configurations:
#   dev     — relaxed CORS ("*"), shorter lifecycle, force_destroy allowed,
#             locally redundant storage
#   staging — production-like settings for pre-release validation, restricted
#             CORS to staging domain
#   prod    — strict CORS (Web Console domain only), geo-redundant storage,
#             force_destroy disabled, full 7-year audit retention enforced
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
# Cloud Provider Region
# -----------------------------------------------------------------------------
# Specifies the geographic region where all storage resources are deployed.
# Must be a valid region identifier for the selected cloud_provider:
#
#   AWS:   e.g., us-east-1, eu-west-1, ap-southeast-1
#   Azure: e.g., eastus, westeurope, southeastasia
#   GCP:   e.g., us-central1, europe-west1, asia-southeast1
#
# For S3, this determines the bucket's physical location and affects latency
# for the Provisioning Service exports. For GCS, the location also
# constrains where data is stored at rest, which may affect data residency
# compliance. Azure Storage Accounts inherit the Resource Group's region but
# can configure geo-redundant replication to a paired region.
# -----------------------------------------------------------------------------
variable "region" {
  type        = string
  description = "Cloud provider region for storage bucket location/replication"
}

# -----------------------------------------------------------------------------
# Project Name (Resource Naming Prefix)
# -----------------------------------------------------------------------------
# Used as a component of resource names and tags to identify resources
# belonging to this platform. Combined with environment and purpose for
# complete resource identification:
#   ${project_name} → tag value for project identification
#   ${bucket_name_prefix}-${environment}-{purpose} → bucket naming
#
# Aligns with the naming convention established across all Terraform modules
# (networking, kubernetes, database, security) for consistent resource
# inventory and cost attribution.
# -----------------------------------------------------------------------------
variable "project_name" {
  type        = string
  default     = "synthetic-erp-platform"
  description = "Project name used for resource naming prefix and resource identification"
}

# -----------------------------------------------------------------------------
# Bucket Name Prefix
# -----------------------------------------------------------------------------
# Prefix applied to all cloud storage bucket and container names. The final
# resource names follow the pattern:
#   {prefix}-{environment}-{purpose}
#
# Examples:
#   synthetic-erp-dev-synthetic-data
#   synthetic-erp-prod-audit-logs
#
# IMPORTANT: Bucket names must be globally unique for AWS S3 and GCP Cloud
# Storage. Azure Storage Account names must be globally unique, contain only
# alphanumeric characters, and be between 3-24 characters. The module
# handles Azure naming constraints by stripping hyphens from the prefix.
#
# When deploying multiple instances of the platform (e.g., multi-tenant or
# multi-region), override this default with a unique prefix per deployment.
# -----------------------------------------------------------------------------
variable "bucket_name_prefix" {
  type        = string
  default     = "synthetic-erp"
  description = "Prefix for cloud storage bucket names. Final bucket names follow the pattern: {prefix}-{environment}-{purpose}. Must be globally unique for S3 and GCS."
}

# -----------------------------------------------------------------------------
# Bucket Versioning Toggle
# -----------------------------------------------------------------------------
# Controls whether object versioning is enabled on the synthetic data storage
# bucket. When enabled, every overwrite or delete creates a new version of
# the object, providing:
#
#   - Data protection against accidental overwrites or deletions
#   - Ability to recover previous versions of exported datasets
#   - Audit trail of data modifications
#
# Recommended true for production environments to prevent data loss.
#
# NOTE: The audit log bucket always has versioning enabled regardless of this
# setting to ensure tamper-evident compliance logging for SOC 2 Type II
# (Constraint C-004). This variable only affects the synthetic data bucket.
#
# Provider-specific implementation:
#   AWS:   aws_s3_bucket_versioning resource with "Enabled" status
#   Azure: blob_properties.versioning_enabled on the Storage Account
#   GCP:   versioning.enabled on the google_storage_bucket resource
# -----------------------------------------------------------------------------
variable "enable_versioning" {
  type        = bool
  default     = true
  description = "Whether to enable bucket versioning for data protection and recovery. Recommended true for production environments. Audit log buckets always have versioning enabled regardless of this setting."
}

# -----------------------------------------------------------------------------
# KMS Encryption Key Identifier
# -----------------------------------------------------------------------------
# The KMS encryption key identifier from the security module used for AES-256
# server-side encryption of all stored objects. This ensures compliance with
# the platform's encryption-at-rest requirement (AES-256) mandated by SOC 2
# Type II (Constraint C-004).
#
# Provider-specific key formats:
#   AWS:   KMS key ARN
#          e.g., arn:aws:kms:us-east-1:123456789:key/mrk-abc123
#   Azure: Key Vault key ID
#          e.g., https://myvault.vault.azure.net/keys/mykey/version
#   GCP:   Cloud KMS crypto key resource name
#          e.g., projects/my-project/locations/us/keyRings/ring/cryptoKeys/key
#
# This value is typically passed from the security module output:
#   module.storage.kms_key_id = module.security.kms_key_id
#
# When empty (default), the module falls back to provider-managed encryption
# keys (SSE-S3 for AWS, Microsoft-managed for Azure, Google-managed for GCP).
# Provider-managed keys still provide AES-256 encryption but without
# customer-controlled key lifecycle management.
# -----------------------------------------------------------------------------
variable "kms_key_id" {
  type        = string
  default     = ""
  description = "KMS encryption key identifier from the security module for AES-256 server-side encryption. For AWS: KMS key ARN. For Azure: Key Vault key ID. For GCP: Cloud KMS crypto key resource name. When empty, uses provider-managed encryption keys."
}

# -----------------------------------------------------------------------------
# Lifecycle Transition Days
# -----------------------------------------------------------------------------
# Number of days after object creation before transitioning synthetic data
# objects to a cost-optimized infrequent access storage tier:
#
#   AWS:   STANDARD → STANDARD_IA (after N days)
#          → GLACIER (after N × 3 days)
#   Azure: Hot → Cool (after N days)
#          → Archive (after N × 3 days)
#   GCP:   STANDARD → NEARLINE (after N days)
#          → COLDLINE (after N × 3 days)
#
# This tiering strategy optimises storage costs for synthetic datasets that
# are generated, delivered to the target system, and then rarely accessed.
# Fresh exports remain in the hot/standard tier for immediate access.
#
# NOTE: This setting applies only to the synthetic data bucket. Audit log
# buckets use a fixed 90-day transition to infrequent access and 365-day
# transition to archive storage for 7-year SOC 2 retention compliance.
#
# Default of 30 days balances cost savings with accessibility for recent
# exports that may need re-download or verification.
# -----------------------------------------------------------------------------
variable "lifecycle_transition_days" {
  type        = number
  default     = 30
  description = "Number of days before transitioning synthetic data objects to infrequent access storage tier (S3 Standard-IA, Azure Cool, GCS Nearline) for cost optimization."

  validation {
    condition     = var.lifecycle_transition_days >= 1 && var.lifecycle_transition_days <= 365
    error_message = "lifecycle_transition_days must be between 1 and 365"
  }
}

# -----------------------------------------------------------------------------
# Lifecycle Expiration Days
# -----------------------------------------------------------------------------
# Number of days after object creation before synthetic data objects are
# automatically expired and deleted. This prevents unbounded storage growth
# from accumulated generated datasets and enforces data hygiene.
#
# IMPORTANT: This expiration policy does NOT apply to audit log buckets.
# Audit logs follow a mandatory 7-year (2,555 day) retention period for
# SOC 2 Type II compliance (Constraint C-004). Audit log lifecycle is
# hardcoded in main.tf with no automatic deletion.
#
# Typical configurations:
#   dev     — 90 days (aggressive cleanup of test data)
#   staging — 180 days (moderate retention for validation)
#   prod    — 365 days (one year retention for generated datasets)
#
# Users can re-generate synthetic datasets at any time, so expired data
# can always be recreated from the stored generation profiles.
#
# Validation enforces a minimum of 30 days (to prevent premature data loss)
# and a maximum of 3,650 days (~10 years) for exceptional retention needs.
# -----------------------------------------------------------------------------
variable "lifecycle_expiration_days" {
  type        = number
  default     = 365
  description = "Number of days before synthetic data objects are expired/deleted. Does not apply to audit log buckets which follow 7-year retention for SOC 2 Type II compliance."

  validation {
    condition     = var.lifecycle_expiration_days >= 30 && var.lifecycle_expiration_days <= 3650
    error_message = "lifecycle_expiration_days must be between 30 and 3650"
  }
}

# -----------------------------------------------------------------------------
# CORS Allowed Origins
# -----------------------------------------------------------------------------
# List of allowed HTTP origins for Cross-Origin Resource Sharing (CORS)
# configuration on storage buckets. CORS rules are required for the Web
# Console (React 19.x frontend) to perform direct uploads or signed-URL
# downloads from cloud storage.
#
# CORS rules applied:
#   - Allowed methods: GET, PUT, POST
#   - Allowed headers: * (all)
#   - Exposed headers: ETag (for multi-part upload verification)
#   - Max age: 3600 seconds (1 hour preflight cache)
#
# Recommended configurations:
#   dev     — ["*"] (unrestricted for local development)
#   staging — ["https://staging.synthetic-erp.example.com"]
#   prod    — ["https://synthetic-erp.example.com"] (Web Console domain only)
#
# WARNING: Using wildcard ["*"] in production environments is a security
# risk. Always restrict to the exact Web Console domain in staging and
# production deployments to prevent unauthorized cross-origin access.
# -----------------------------------------------------------------------------
variable "cors_allowed_origins" {
  type        = list(string)
  default     = ["*"]
  description = "List of allowed origins for CORS configuration on storage buckets. In production, restrict to the Web Console domain URL. Wildcards allowed in development only."
}

# -----------------------------------------------------------------------------
# Force Destroy Toggle
# -----------------------------------------------------------------------------
# Controls whether cloud storage buckets can be deleted by Terraform even
# when they contain objects. This is a safety mechanism to prevent
# accidental data loss during infrastructure teardown.
#
# Provider-specific behaviour:
#   AWS:   Sets force_destroy on aws_s3_bucket resources
#   Azure: Affects whether Terraform will empty containers before deletion
#   GCP:   Sets force_destroy on google_storage_bucket resources
#
# Recommended configurations:
#   dev     — true (allow clean teardown of development environments)
#   staging — false (protect staging data during infrastructure changes)
#   prod    — false (NEVER enable in production; prevents catastrophic
#             data loss from accidental terraform destroy)
#
# Even when force_destroy is true, Terraform will still show the resources
# to be destroyed in the plan output, providing a review opportunity
# before execution.
# -----------------------------------------------------------------------------
variable "force_destroy" {
  type        = bool
  default     = false
  description = "Whether to allow bucket deletion even when non-empty. Should be false in production to prevent accidental data loss. Set true only in dev/test environments."
}

# -----------------------------------------------------------------------------
# Azure Resource Group Name
# -----------------------------------------------------------------------------
# The name of the Azure Resource Group where the Storage Account and
# associated containers will be created. Required only when cloud_provider
# is set to "azure".
#
# Azure requires all resources to belong to a Resource Group for lifecycle
# management, access control, and billing. This value is typically passed
# from the networking module output:
#   module.storage.resource_group_name = module.networking.resource_group_name
#
# For AWS and GCP deployments, this variable is unused (resources are
# organized by region/project rather than resource groups). The empty
# default allows non-Azure deployments to omit this variable entirely.
#
# If cloud_provider is "azure" and this value is empty, the module's
# azurerm_storage_account resource will fail at plan time with a missing
# required argument error.
# -----------------------------------------------------------------------------
variable "resource_group_name" {
  type        = string
  default     = ""
  description = "Azure Resource Group name for storage account creation. Required when cloud_provider is 'azure'. Passed from the networking module."
}

# -----------------------------------------------------------------------------
# Additional Resource Tags
# -----------------------------------------------------------------------------
# Supplementary tags merged with the module's default tags (project_name,
# environment, managed_by) and applied to every storage resource. Use for
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
#
# The module automatically adds the following default tags to every resource:
#   project    = var.project_name
#   environment = var.environment
#   managed_by = "terraform"
#   module     = "storage"
# These are merged with any tags provided here, with user-supplied tags
# taking precedence in case of key conflicts.
# -----------------------------------------------------------------------------
variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional tags to apply to all storage resources for cost tracking, compliance labeling, and environment identification."
}
