# =============================================================================
# Terraform Storage Module — Resource Definitions (main.tf)
# =============================================================================
# Provisions cloud storage infrastructure for the Synthetic-ERP-Data-Generation-
# Platform across three supported cloud providers: AWS S3, Azure Blob Storage,
# and GCP Cloud Storage.
#
# Two storage targets are provisioned per cloud provider:
#   1. synthetic-data — Primary export destination for generated synthetic ERP
#      data in SQL, CSV, JSON, and Parquet formats. Used by the Provisioning
#      Service (src/backend/provisioning_service/) for cloud storage exports.
#   2. audit-logs — Tamper-evident compliance audit trail storage with 7-year
#      retention for SOC 2 Type II compliance (Constraint C-004).
#
# Security features implemented:
#   - AES-256 server-side encryption using KMS keys from the security module
#   - Bucket versioning for data protection and recovery
#   - Public access blocked on all storage targets
#   - SSL/TLS enforced for all storage operations
#   - CORS configuration for Web Console (React 19.x) direct uploads
#
# Cost optimization features:
#   - Automatic storage tiering (hot -> warm -> cold) based on object age
#   - Configurable lifecycle expiration for synthetic data
#   - Abort incomplete multipart uploads (AWS)
#
# Conditional resource creation:
#   All resources use count = var.cloud_provider == "provider" ? 1 : 0 to
#   ensure only the selected cloud provider's resources are provisioned.
#
# This module is consumed by the root infrastructure/terraform/main.tf and
# exposes outputs via outputs.tf for cross-module references.
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  storage
# =============================================================================

# -----------------------------------------------------------------------------
# Local Values
# -----------------------------------------------------------------------------
# Common tags, naming conventions, and derived values used across all cloud
# provider resource definitions in this module.
# -----------------------------------------------------------------------------
locals {
  # Common tags applied to every resource in this module. Merged with any
  # additional tags supplied via var.tags. User-supplied tags take precedence
  # in case of key conflicts.
  common_tags = merge(
    {
      project     = var.project_name
      environment = var.environment
      managed_by  = "terraform"
      module      = "storage"
    },
    var.tags
  )

  # Bucket naming convention: {prefix}-{environment}-{purpose}
  # Must be globally unique for AWS S3 and GCP Cloud Storage.
  synthetic_data_bucket_name = "${var.bucket_name_prefix}-${var.environment}-synthetic-data"
  audit_logs_bucket_name     = "${var.bucket_name_prefix}-${var.environment}-audit-logs"

  # Azure Storage Account names: 3-24 chars, alphanumeric only, globally unique.
  # Strip hyphens and truncate to 24 characters to comply with Azure naming
  # constraints. Example: "syntheticerpdev" from "synthetic-erp" + "dev".
  azure_storage_account_name = substr(
    replace("${var.bucket_name_prefix}${var.environment}", "-", ""),
    0,
    24
  )

  # Flag indicating whether customer-managed KMS encryption is configured.
  # When true, resources use the KMS key from var.kms_key_id (security module).
  # When false, resources use provider-managed encryption (still AES-256).
  use_kms_encryption = var.kms_key_id != ""

  # Azure Key Vault key ID URL parsing for customer-managed key configuration.
  # Decomposes the Key Vault key URL into vault name, key name, and version.
  # URL format: https://{vault-name}.vault.azure.net/keys/{key-name}/{version}
  azure_kms_url_parts  = var.cloud_provider == "azure" && local.use_kms_encryption ? split("/", var.kms_key_id) : []
  azure_key_vault_name = length(local.azure_kms_url_parts) > 2 ? element(split(".", local.azure_kms_url_parts[2]), 0) : ""
  azure_key_name       = length(local.azure_kms_url_parts) > 4 ? local.azure_kms_url_parts[4] : ""
  azure_key_version    = length(local.azure_kms_url_parts) > 5 ? local.azure_kms_url_parts[5] : ""

  # GCP project ID for constructing service account email addresses used in
  # IAM bindings. Derived from the google_project data source when deploying
  # to GCP. Service account emails follow the security module's naming
  # convention: {project_name}-{service-name}@{project_id}.iam.gserviceaccount.com
  gcp_project_id            = var.cloud_provider == "gcp" ? data.google_project.current[0].project_id : ""
  gcp_provisioning_sa_email = "serviceAccount:${var.project_name}-provisioning-service@${local.gcp_project_id}.iam.gserviceaccount.com"
  gcp_compliance_sa_email   = "serviceAccount:${var.project_name}-compliance-service@${local.gcp_project_id}.iam.gserviceaccount.com"
}

# -----------------------------------------------------------------------------
# Data Sources
# -----------------------------------------------------------------------------

# AWS caller identity for constructing bucket policies with account-level
# access restrictions. Only fetched when deploying to AWS.
data "aws_caller_identity" "current" {
  count = var.cloud_provider == "aws" ? 1 : 0
}

# GCP project data for constructing service account email addresses used in
# bucket-level IAM bindings. Only fetched when deploying to GCP.
data "google_project" "current" {
  count = var.cloud_provider == "gcp" ? 1 : 0
}

# Azure Key Vault data source for customer-managed key encryption binding.
# Looks up the Key Vault resource ID from the vault name extracted from
# var.kms_key_id. Only fetched when deploying to Azure with CMK enabled.
data "azurerm_key_vault" "main" {
  count               = var.cloud_provider == "azure" && local.use_kms_encryption ? 1 : 0
  name                = local.azure_key_vault_name
  resource_group_name = var.resource_group_name
}


# =============================================================================
# AWS S3 Resources
# =============================================================================
# Provisions AWS S3 buckets with versioning, AES-256 KMS encryption, lifecycle
# tiering, CORS, public access blocking, and SSL-enforcing bucket policies.
# Created only when var.cloud_provider == "aws".
# =============================================================================

# -----------------------------------------------------------------------------
# S3 Bucket — Synthetic Data Export
# -----------------------------------------------------------------------------
# Primary storage target for Provisioning Service exports. Receives generated
# synthetic ERP data in SQL, CSV, JSON, and Parquet formats.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "synthetic_data" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket        = local.synthetic_data_bucket_name
  force_destroy = var.force_destroy

  tags = merge(local.common_tags, {
    purpose     = "synthetic-data-export"
    data_class  = "synthetic"
    description = "Synthetic ERP data export storage for the Provisioning Service"
  })
}

# -----------------------------------------------------------------------------
# S3 Bucket — Audit Logs
# -----------------------------------------------------------------------------
# Compliance audit trail storage with 7-year retention for SOC 2 Type II.
# Versioning is always enabled regardless of var.enable_versioning.
# Lifecycle rules enforce transition to archival tiers but never delete.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket" "audit_logs" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket        = local.audit_logs_bucket_name
  force_destroy = var.force_destroy

  tags = merge(local.common_tags, {
    purpose     = "audit-log-storage"
    data_class  = "compliance"
    retention   = "7-years"
    description = "Tamper-evident audit log storage for SOC 2 Type II compliance"
  })
}

# -----------------------------------------------------------------------------
# S3 Bucket Versioning — Synthetic Data
# -----------------------------------------------------------------------------
# Enables object versioning based on var.enable_versioning. Provides data
# protection against accidental overwrites and deletions of exported datasets.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_versioning" "synthetic_data" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.synthetic_data[0].id

  versioning_configuration {
    status = var.enable_versioning ? "Enabled" : "Suspended"
  }
}

# -----------------------------------------------------------------------------
# S3 Bucket Versioning — Audit Logs
# -----------------------------------------------------------------------------
# Always enabled for compliance. Audit logs must be tamper-evident per SOC 2
# Type II requirements (Constraint C-004). This overrides var.enable_versioning.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_versioning" "audit_logs" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.audit_logs[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

# -----------------------------------------------------------------------------
# S3 Server-Side Encryption — Synthetic Data
# -----------------------------------------------------------------------------
# AES-256 encryption using KMS key from the security module when provided.
# Falls back to SSE-S3 (AES-256 with Amazon-managed keys) when var.kms_key_id
# is empty. Both options provide AES-256 encryption at rest as required.
# Bucket key is enabled with KMS to reduce KMS API call costs.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_server_side_encryption_configuration" "synthetic_data" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.synthetic_data[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = local.use_kms_encryption ? "aws:kms" : "AES256"
      kms_master_key_id = local.use_kms_encryption ? var.kms_key_id : null
    }
    bucket_key_enabled = local.use_kms_encryption
  }
}

# -----------------------------------------------------------------------------
# S3 Server-Side Encryption — Audit Logs
# -----------------------------------------------------------------------------
# AES-256 encryption for audit log data. Uses the same KMS key strategy as
# the synthetic data bucket for consistent encryption key management.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_server_side_encryption_configuration" "audit_logs" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.audit_logs[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = local.use_kms_encryption ? "aws:kms" : "AES256"
      kms_master_key_id = local.use_kms_encryption ? var.kms_key_id : null
    }
    bucket_key_enabled = local.use_kms_encryption
  }
}

# -----------------------------------------------------------------------------
# S3 Lifecycle Configuration — Synthetic Data
# -----------------------------------------------------------------------------
# Cost-optimized storage tiering for generated synthetic datasets:
#   - STANDARD -> STANDARD_IA after var.lifecycle_transition_days (default 30d)
#   - STANDARD_IA -> GLACIER after var.lifecycle_transition_days * 3 (90d)
#   - Expired and deleted after var.lifecycle_expiration_days (default 365d)
#   - Incomplete multipart uploads aborted after 7 days
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_lifecycle_configuration" "synthetic_data" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.synthetic_data[0].id

  # Tiering rule: progressively move data to cheaper storage tiers
  rule {
    id     = "synthetic-data-tiering"
    status = "Enabled"

    transition {
      days          = var.lifecycle_transition_days
      storage_class = "STANDARD_IA"
    }

    transition {
      days          = var.lifecycle_transition_days * 3
      storage_class = "GLACIER"
    }

    expiration {
      days = var.lifecycle_expiration_days
    }
  }

  # Cleanup rule: abort incomplete multipart uploads after 7 days to prevent
  # accumulation of partial uploads from failed Provisioning Service exports.
  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# -----------------------------------------------------------------------------
# S3 Lifecycle Configuration — Audit Logs
# -----------------------------------------------------------------------------
# 7-year retention lifecycle for SOC 2 Type II compliance (Constraint C-004):
#   - STANDARD -> GLACIER_IR after 90 days (instant retrieval for recent audits)
#   - GLACIER_IR -> DEEP_ARCHIVE after 365 days (lowest cost archival)
#   - No expiration rule: 7-year retention enforced by organizational policy.
#     Audit logs are never automatically deleted.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_lifecycle_configuration" "audit_logs" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.audit_logs[0].id

  rule {
    id     = "audit-log-archival"
    status = "Enabled"

    transition {
      days          = 90
      storage_class = "GLACIER_IR"
    }

    transition {
      days          = 365
      storage_class = "DEEP_ARCHIVE"
    }

    # No expiration block: audit logs are retained for 7+ years per SOC 2
    # Type II compliance requirements. Deletion is managed by organizational
    # policy and manual review after the retention period expires.
  }
}

# -----------------------------------------------------------------------------
# S3 CORS Configuration — Synthetic Data
# -----------------------------------------------------------------------------
# Enables Cross-Origin Resource Sharing for the Web Console (React 19.x
# frontend) to perform direct uploads via signed URLs and download exported
# datasets from the browser. Audit log bucket does not require CORS as it
# is accessed only by backend services.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_cors_configuration" "synthetic_data" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.synthetic_data[0].id

  cors_rule {
    allowed_origins = var.cors_allowed_origins
    allowed_methods = ["GET", "PUT", "POST"]
    allowed_headers = ["*"]
    expose_headers  = ["ETag"]
    max_age_seconds = 3600
  }
}

# -----------------------------------------------------------------------------
# S3 Public Access Block — Synthetic Data
# -----------------------------------------------------------------------------
# Blocks all public access to the synthetic data bucket. All access must be
# authenticated via IAM policies or pre-signed URLs. This is a critical
# security control for preventing unauthorized data exposure.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_public_access_block" "synthetic_data" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.synthetic_data[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------------
# S3 Public Access Block — Audit Logs
# -----------------------------------------------------------------------------
# Blocks all public access to the audit logs bucket. Audit data must never
# be publicly accessible for security and compliance reasons.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_public_access_block" "audit_logs" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.audit_logs[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# -----------------------------------------------------------------------------
# S3 Bucket Policy — Synthetic Data
# -----------------------------------------------------------------------------
# Enforces security policies on the synthetic data bucket:
#   1. Deny all non-HTTPS (non-SSL) requests to ensure TLS in transit
#   2. Restrict access to the owning AWS account to prevent cross-account
#      access unless explicitly configured
#
# Fine-grained per-service IAM role access is managed by the security module's
# IAM policies attached to each service role, following the AWS best practice
# of granting access via IAM policies rather than bucket policies.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_policy" "synthetic_data" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.synthetic_data[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "SyntheticDataBucketPolicy"
    Statement = [
      {
        Sid       = "DenyNonHTTPS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.synthetic_data[0].arn,
          "${aws_s3_bucket.synthetic_data[0].arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      {
        Sid       = "RestrictToAccount"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.synthetic_data[0].arn,
          "${aws_s3_bucket.synthetic_data[0].arn}/*"
        ]
        Condition = {
          StringNotEquals = {
            "aws:PrincipalAccount" = data.aws_caller_identity.current[0].account_id
          }
        }
      }
    ]
  })

  # Ensure public access block is applied before the bucket policy to prevent
  # transient public access windows during infrastructure provisioning.
  depends_on = [aws_s3_bucket_public_access_block.synthetic_data]
}

# -----------------------------------------------------------------------------
# S3 Bucket Policy — Audit Logs
# -----------------------------------------------------------------------------
# Enforces SSL-only access and account-level restrictions on audit logs.
# Same security posture as the synthetic data bucket policy. Audit logs
# require the strictest access controls for SOC 2 Type II compliance.
# -----------------------------------------------------------------------------
resource "aws_s3_bucket_policy" "audit_logs" {
  count = var.cloud_provider == "aws" ? 1 : 0

  bucket = aws_s3_bucket.audit_logs[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "AuditLogBucketPolicy"
    Statement = [
      {
        Sid       = "DenyNonHTTPS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.audit_logs[0].arn,
          "${aws_s3_bucket.audit_logs[0].arn}/*"
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      },
      {
        Sid       = "RestrictToAccount"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.audit_logs[0].arn,
          "${aws_s3_bucket.audit_logs[0].arn}/*"
        ]
        Condition = {
          StringNotEquals = {
            "aws:PrincipalAccount" = data.aws_caller_identity.current[0].account_id
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.audit_logs]
}


# =============================================================================
# Azure Blob Storage Resources
# =============================================================================
# Provisions an Azure Storage Account with Blob containers for synthetic data
# export and audit log storage. Includes geo-redundant replication (GRS) for
# production, HTTPS-only enforcement, TLS 1.2 minimum, customer-managed key
# encryption via Key Vault, blob versioning, soft delete with 30-day retention,
# CORS for Web Console integration, and network rules defaulting to Deny.
# Created only when var.cloud_provider == "azure".
# =============================================================================

# -----------------------------------------------------------------------------
# Azure Storage Account
# -----------------------------------------------------------------------------
# Central storage account hosting both synthetic-data and audit-logs
# containers. Azure uses a single account with multiple containers, unlike
# AWS and GCP which use separate buckets per purpose.
#
# Replication strategy:
#   - Production: GRS (geo-redundant) for disaster recovery across regions
#   - Dev/Staging: LRS (locally redundant) for cost optimization
# -----------------------------------------------------------------------------
resource "azurerm_storage_account" "main" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                     = local.azure_storage_account_name
  resource_group_name      = var.resource_group_name
  location                 = var.region
  account_tier             = "Standard"
  account_replication_type = var.environment == "prod" ? "GRS" : "LRS"

  # Enforce HTTPS-only and TLS 1.2 minimum for all storage operations.
  # Ensures compliance with the platform's TLS encryption-in-transit requirement.
  enable_https_traffic_only = true
  min_tls_version           = "TLS1_2"

  # SystemAssigned managed identity for Key Vault customer-managed key access.
  # The security module grants this identity Key Vault Crypto User permissions
  # for encryption operations on the storage account's data.
  identity {
    type = "SystemAssigned"
  }

  # Blob service properties: versioning, soft delete, and CORS configuration
  blob_properties {
    versioning_enabled = var.enable_versioning

    # Soft delete: retain deleted blobs for 30 days allowing recovery of
    # accidentally deleted synthetic datasets or audit log entries.
    delete_retention_policy {
      days = 30
    }

    # Container soft delete: retain deleted containers for 30 days to
    # protect against accidental container removal.
    container_delete_retention_policy {
      days = 30
    }

    # CORS rules for Web Console (React 19.x) direct uploads and downloads.
    # Allows the frontend to interact with storage via signed URLs.
    cors_rule {
      allowed_origins    = var.cors_allowed_origins
      allowed_methods    = ["GET", "PUT", "POST"]
      allowed_headers    = ["*"]
      exposed_headers    = ["ETag"]
      max_age_in_seconds = 3600
    }
  }

  # Network access rules: deny by default, allow Azure-trusted services.
  # Platform services access storage through Azure private endpoints or
  # trusted Azure service connections.
  network_rules {
    default_action = "Deny"
    bypass         = ["AzureServices"]
  }

  tags = merge(local.common_tags, {
    purpose = "synthetic-erp-storage"
  })
}

# -----------------------------------------------------------------------------
# Azure Customer-Managed Key Encryption
# -----------------------------------------------------------------------------
# Configures customer-managed key (CMK) encryption for the storage account
# using a Key Vault key from the security module. Created as a separate
# resource for clean lifecycle management and to leverage the storage
# account's SystemAssigned identity for Key Vault access.
#
# Prerequisites (managed by the security module):
#   - Key Vault with purge protection enabled
#   - Encryption key created in Key Vault
#   - Storage account's SystemAssigned identity granted Key Vault Crypto
#     User role for Wrap/Unwrap key operations
#
# When var.kms_key_id is empty, the storage account uses Microsoft-managed
# encryption keys (still AES-256, but without customer key lifecycle control).
# -----------------------------------------------------------------------------
resource "azurerm_storage_account_customer_managed_key" "main" {
  count = var.cloud_provider == "azure" && local.use_kms_encryption ? 1 : 0

  storage_account_id = azurerm_storage_account.main[0].id
  key_vault_id       = data.azurerm_key_vault.main[0].id
  key_name           = local.azure_key_name
  key_version        = local.azure_key_version
}

# -----------------------------------------------------------------------------
# Azure Blob Container — Synthetic Data
# -----------------------------------------------------------------------------
# Private container for Provisioning Service synthetic data exports.
# Inherits encryption, versioning, and network rules from the parent
# storage account.
# -----------------------------------------------------------------------------
resource "azurerm_storage_container" "synthetic_data" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                  = "synthetic-data"
  storage_account_name  = azurerm_storage_account.main[0].name
  container_access_type = "private"
}

# -----------------------------------------------------------------------------
# Azure Blob Container — Audit Logs
# -----------------------------------------------------------------------------
# Private container for compliance audit trail storage. Access restricted
# to backend services only; no public or anonymous access permitted.
# -----------------------------------------------------------------------------
resource "azurerm_storage_container" "audit_logs" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                  = "audit-logs"
  storage_account_name  = azurerm_storage_account.main[0].name
  container_access_type = "private"
}

# -----------------------------------------------------------------------------
# Azure Storage Management Policy — Lifecycle Rules
# -----------------------------------------------------------------------------
# Implements lifecycle management for both synthetic data and audit log
# containers with distinct tiering and retention strategies:
#
# Synthetic Data:
#   - Hot -> Cool tier after var.lifecycle_transition_days (default 30d)
#   - Cool -> Archive tier after var.lifecycle_transition_days * 3 (90d)
#   - Delete after var.lifecycle_expiration_days (default 365d)
#
# Audit Logs (SOC 2 Type II compliance):
#   - Hot -> Cool tier after 90 days
#   - Cool -> Archive tier after 365 days
#   - No deletion: 7-year retention enforced by organizational policy
# -----------------------------------------------------------------------------
resource "azurerm_storage_management_policy" "lifecycle" {
  count = var.cloud_provider == "azure" ? 1 : 0

  storage_account_id = azurerm_storage_account.main[0].id

  # Synthetic data lifecycle: configurable tiering and expiration
  rule {
    name    = "synthetic-data-lifecycle"
    enabled = true

    filters {
      prefix_match = ["synthetic-data/"]
      blob_types   = ["blockBlob"]
    }

    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than    = var.lifecycle_transition_days
        tier_to_archive_after_days_since_modification_greater_than = var.lifecycle_transition_days * 3
        delete_after_days_since_modification_greater_than          = var.lifecycle_expiration_days
      }

      snapshot {
        delete_after_days_since_creation_greater_than = var.lifecycle_expiration_days
      }
    }
  }

  # Audit log lifecycle: fixed tiering with no deletion (7-year retention)
  rule {
    name    = "audit-logs-lifecycle"
    enabled = true

    filters {
      prefix_match = ["audit-logs/"]
      blob_types   = ["blockBlob"]
    }

    actions {
      base_blob {
        tier_to_cool_after_days_since_modification_greater_than    = 90
        tier_to_archive_after_days_since_modification_greater_than = 365
        # No delete action: audit logs retained for 7+ years per SOC 2
        # Type II compliance. Deletion managed by organizational policy.
      }
    }
  }
}


# =============================================================================
# GCP Cloud Storage Resources
# =============================================================================
# Provisions GCP Cloud Storage buckets with uniform bucket-level access,
# Cloud KMS encryption, lifecycle rules for tiering, CORS for Web Console
# integration, and IAM bindings for service-level access control.
# Created only when var.cloud_provider == "gcp".
# =============================================================================

# -----------------------------------------------------------------------------
# GCS Bucket — Synthetic Data Export
# -----------------------------------------------------------------------------
# Primary storage target for Provisioning Service exports. Configured with
# STANDARD storage class, uniform bucket-level access (disabling per-object
# ACLs for simpler IAM-based access control), KMS encryption, lifecycle
# tiering from STANDARD -> NEARLINE -> COLDLINE, and CORS rules.
# -----------------------------------------------------------------------------
resource "google_storage_bucket" "synthetic_data" {
  count = var.cloud_provider == "gcp" ? 1 : 0

  name                        = local.synthetic_data_bucket_name
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = var.force_destroy

  # Object versioning for data protection against accidental overwrites.
  # Controlled by var.enable_versioning for the synthetic data bucket.
  versioning {
    enabled = var.enable_versioning
  }

  # Cloud KMS encryption for AES-256 at-rest encryption using a key from
  # the security module. When var.kms_key_id is empty, Google-managed
  # encryption keys are used (still AES-256).
  dynamic "encryption" {
    for_each = local.use_kms_encryption ? [1] : []
    content {
      default_kms_key_name = var.kms_key_id
    }
  }

  # Lifecycle rules for cost-optimized storage tiering:
  #   STANDARD -> NEARLINE after var.lifecycle_transition_days
  lifecycle_rule {
    condition {
      age = var.lifecycle_transition_days
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  #   NEARLINE -> COLDLINE after var.lifecycle_transition_days * 3
  lifecycle_rule {
    condition {
      age = var.lifecycle_transition_days * 3
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  #   Delete after var.lifecycle_expiration_days
  lifecycle_rule {
    condition {
      age = var.lifecycle_expiration_days
    }
    action {
      type = "Delete"
    }
  }

  # Abort incomplete multipart (resumable) uploads after 7 days
  lifecycle_rule {
    condition {
      age = 7
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  # CORS configuration for Web Console (React 19.x) direct uploads and
  # downloads via signed URLs.
  cors {
    origin          = var.cors_allowed_origins
    method          = ["GET", "PUT", "POST"]
    response_header = ["Content-Type", "ETag"]
    max_age_seconds = 3600
  }

  labels = merge(
    { for k, v in local.common_tags : lower(replace(k, "/[^a-z0-9_-]/", "_")) => lower(replace(v, "/[^a-z0-9_-]/", "_")) },
    {
      purpose    = "synthetic-data-export"
      data_class = "synthetic"
    }
  )
}

# -----------------------------------------------------------------------------
# GCS Bucket — Audit Logs
# -----------------------------------------------------------------------------
# Compliance audit trail storage with 7-year retention. Versioning is always
# enabled regardless of var.enable_versioning. Lifecycle rules transition
# data to cheaper tiers but never delete. Uniform bucket-level access
# ensures consistent IAM-based access control.
# -----------------------------------------------------------------------------
resource "google_storage_bucket" "audit_logs" {
  count = var.cloud_provider == "gcp" ? 1 : 0

  name                        = local.audit_logs_bucket_name
  location                    = var.region
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  force_destroy               = var.force_destroy

  # Versioning always enabled for audit logs to ensure tamper-evident
  # compliance logging for SOC 2 Type II (Constraint C-004).
  versioning {
    enabled = true
  }

  # Cloud KMS encryption with the security module's encryption key
  dynamic "encryption" {
    for_each = local.use_kms_encryption ? [1] : []
    content {
      default_kms_key_name = var.kms_key_id
    }
  }

  # Lifecycle rules for 7-year retention compliance:
  #   STANDARD -> NEARLINE after 90 days
  lifecycle_rule {
    condition {
      age = 90
    }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }

  #   NEARLINE -> COLDLINE after 365 days
  lifecycle_rule {
    condition {
      age = 365
    }
    action {
      type          = "SetStorageClass"
      storage_class = "COLDLINE"
    }
  }

  # No Delete lifecycle rule: audit logs are retained for 7+ years per
  # SOC 2 Type II compliance. Deletion managed by organizational policy.

  labels = merge(
    { for k, v in local.common_tags : lower(replace(k, "/[^a-z0-9_-]/", "_")) => lower(replace(v, "/[^a-z0-9_-]/", "_")) },
    {
      purpose    = "audit-log-storage"
      data_class = "compliance"
      retention  = "7-years"
    }
  )
}

# -----------------------------------------------------------------------------
# GCS IAM Binding — Provisioning Service (Synthetic Data)
# -----------------------------------------------------------------------------
# Grants the Provisioning Service's GCP service account objectAdmin access
# to the synthetic data bucket. This allows the Provisioning Service to
# write (upload) generated synthetic datasets and manage object lifecycle.
# The service account is created by the security module following the naming
# convention: {project_name}-provisioning-service@{project}.iam.gserviceaccount.com
# -----------------------------------------------------------------------------
resource "google_storage_bucket_iam_member" "synthetic_data_admin" {
  count = var.cloud_provider == "gcp" ? 1 : 0

  bucket = google_storage_bucket.synthetic_data[0].name
  role   = "roles/storage.objectAdmin"
  member = local.gcp_provisioning_sa_email
}

# -----------------------------------------------------------------------------
# GCS IAM Binding — Compliance Service (Audit Logs)
# -----------------------------------------------------------------------------
# Grants the Compliance Service's GCP service account objectViewer access
# to the audit logs bucket. This allows the Compliance Service to read
# audit log entries for compliance verification and reporting, but not
# modify or delete them (ensuring tamper-evidence).
# The service account is created by the security module following the naming
# convention: {project_name}-compliance-service@{project}.iam.gserviceaccount.com
# -----------------------------------------------------------------------------
resource "google_storage_bucket_iam_member" "audit_logs_viewer" {
  count = var.cloud_provider == "gcp" ? 1 : 0

  bucket = google_storage_bucket.audit_logs[0].name
  role   = "roles/storage.objectViewer"
  member = local.gcp_compliance_sa_email
}
