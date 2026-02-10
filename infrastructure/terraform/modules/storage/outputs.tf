# =============================================================================
# Terraform Storage Module — Output Definitions
# =============================================================================
# Exposes provisioned cloud storage resource identifiers and endpoints to
# consuming modules and the root infrastructure/terraform/outputs.tf
# configuration.  These outputs enable downstream infrastructure components
# and application services to reference the storage targets provisioned by
# this module.
#
# Outputs:
#   bucket_names       — Storage bucket/container names keyed by purpose
#   bucket_arns        — Full resource ARNs/IDs/self_links for IAM binding
#   storage_endpoints  — Storage endpoint URLs for application connectivity
#   storage_account_id — Azure Storage Account ID (Azure-only, conditional)
#
# Multi-Cloud Conditional Logic:
#   Every output uses conditional/ternary expressions to select the correct
#   resource reference based on var.cloud_provider.  Resources that may not
#   exist (due to count=0 when the provider is inactive) are wrapped in
#   try() to prevent plan-time errors on non-selected providers.
#
# Consumed By:
#   • infrastructure/terraform/outputs.tf — Exposes storage_bucket_names
#     (references module.storage.bucket_names)
#   • Kubernetes ConfigMaps — Provisioning Service environment configuration
#     injects bucket names and endpoint URLs for cloud storage export targets
#   • Security module IAM policies — References bucket_arns for granting
#     least-privilege storage access to service roles
#
# Storage Targets:
#   • synthetic_data — Primary export destination for generated synthetic ERP
#     data in SQL, CSV, JSON, and Parquet formats.  Used by the Provisioning
#     Service (src/backend/provisioning_service/cloud/).
#   • audit_logs — Tamper-evident compliance audit trail storage with 7-year
#     retention for SOC 2 Type II compliance (Constraint C-004).
#
# References:
#   Agent Action Plan §0.3.1 — Storage Module Outputs
#   Agent Action Plan §0.5.3 — Security Considerations (AES-256 at rest)
#   Agent Action Plan §0.7.2 — Constraint C-004 (SOC 2 Type II)
#   Technical Specification §6.3 — Integration Architecture
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  storage
# =============================================================================


# =============================================================================
# OUTPUT: bucket_names
# =============================================================================
# Map of cloud storage bucket or container names keyed by purpose.
#
# These human-readable names are used for:
#   • Provisioning Service configuration — identifying which bucket to upload
#     generated synthetic datasets to (SQL, CSV, JSON, Parquet formats)
#   • Compliance Service configuration — identifying the audit log archive
#     target for tamper-evident SOC 2 Type II audit trail storage
#   • Kubernetes ConfigMap injection — environment variables like
#     SYNTHETIC_DATA_BUCKET and AUDIT_LOG_BUCKET
#   • CI/CD pipeline outputs — displaying deployed storage resource names
#
# Provider-specific attributes:
#   AWS   — aws_s3_bucket.*.id           (S3 bucket name, globally unique)
#   Azure — azurerm_storage_container.*.name (container name within account)
#   GCP   — google_storage_bucket.*.name (GCS bucket name, globally unique)
#
# Map keys (consistent across all providers):
#   "synthetic_data" — Main export target for generated data
#   "audit_logs"     — SOC 2 Type II compliance audit archive
# =============================================================================

output "bucket_names" {
  description = "Map of cloud storage bucket/container names keyed by purpose (synthetic_data, audit_logs)"

  value = (
    var.cloud_provider == "aws" ? {
      synthetic_data = try(aws_s3_bucket.synthetic_data[0].id, "")
      audit_logs     = try(aws_s3_bucket.audit_logs[0].id, "")
    } : var.cloud_provider == "azure" ? {
      synthetic_data = try(azurerm_storage_container.synthetic_data[0].name, "")
      audit_logs     = try(azurerm_storage_container.audit_logs[0].name, "")
    } : var.cloud_provider == "gcp" ? {
      synthetic_data = try(google_storage_bucket.synthetic_data[0].name, "")
      audit_logs     = try(google_storage_bucket.audit_logs[0].name, "")
    } : {}
  )
}


# =============================================================================
# OUTPUT: bucket_arns
# =============================================================================
# Map of full resource ARNs, resource IDs, or self_links for cross-module
# references and IAM policy attachment.
#
# These fully-qualified identifiers are required when:
#   • Granting IAM roles access to specific storage resources via the
#     security module's per-service least-privilege policies
#   • Configuring S3 bucket policies or GCS IAM bindings for fine-grained
#     access control at the resource level
#   • Referencing storage targets in Terraform remote state consumers
#   • Auditing infrastructure resource inventory for SOC 2 Type II compliance
#
# Provider-specific attributes:
#   AWS   — aws_s3_bucket.*.arn                               (full ARN)
#           e.g., arn:aws:s3:::synth-erp-prod-synthetic-data
#   Azure — azurerm_storage_container.*.resource_manager_id   (ARM resource ID)
#           e.g., /subscriptions/.../containers/synthetic-data
#   GCP   — google_storage_bucket.*.self_link                 (self_link URI)
#           e.g., https://www.googleapis.com/storage/v1/b/synth-erp-prod-synthetic-data
#
# Map keys match bucket_names for consistent cross-referencing:
#   "synthetic_data" — Export target resource identifier
#   "audit_logs"     — Audit archive resource identifier
# =============================================================================

output "bucket_arns" {
  description = "Map of full bucket ARNs/resource IDs/self_links for IAM policy attachment and cross-module references"

  value = (
    var.cloud_provider == "aws" ? {
      synthetic_data = try(aws_s3_bucket.synthetic_data[0].arn, "")
      audit_logs     = try(aws_s3_bucket.audit_logs[0].arn, "")
    } : var.cloud_provider == "azure" ? {
      synthetic_data = try(azurerm_storage_container.synthetic_data[0].resource_manager_id, "")
      audit_logs     = try(azurerm_storage_container.audit_logs[0].resource_manager_id, "")
    } : var.cloud_provider == "gcp" ? {
      synthetic_data = try(google_storage_bucket.synthetic_data[0].self_link, "")
      audit_logs     = try(google_storage_bucket.audit_logs[0].self_link, "")
    } : {}
  )
}


# =============================================================================
# OUTPUT: storage_endpoints
# =============================================================================
# Map of storage endpoint URLs for application-level connectivity.
#
# These protocol-prefixed URLs are consumed by the Provisioning Service
# (src/backend/provisioning_service/cloud/) to construct storage client
# connections for exporting generated synthetic data:
#
#   • s3_provider.py   — Uses "s3://<bucket>" with boto3 S3 client
#   • azure_blob_provider.py — Uses Azure primary blob endpoint with
#     azure-storage-blob SDK
#   • gcs_provider.py  — Uses "gs://<bucket>" with google-cloud-storage SDK
#
# The Provisioning Service reads these endpoints from Kubernetes ConfigMap
# environment variables (STORAGE_ENDPOINT_SYNTHETIC_DATA, STORAGE_ENDPOINT_AUDIT_LOGS)
# to determine the target storage location for each export operation.
#
# Provider-specific formats:
#   AWS   — "s3://<bucket-name>"
#           e.g., s3://synth-erp-prod-synthetic-data
#   Azure — Primary blob endpoint URL (shared by both containers)
#           e.g., https://syntheticerpprod.blob.core.windows.net/
#   GCP   — "gs://<bucket-name>"
#           e.g., gs://synth-erp-prod-synthetic-data
#
# Map keys match bucket_names for consistent cross-referencing:
#   "synthetic_data" — Export target endpoint
#   "audit_logs"     — Audit archive endpoint
# =============================================================================

output "storage_endpoints" {
  description = "Map of storage endpoint URLs for application-level connectivity (Provisioning Service export targets)"

  value = (
    var.cloud_provider == "aws" ? {
      synthetic_data = try("s3://${aws_s3_bucket.synthetic_data[0].id}", "")
      audit_logs     = try("s3://${aws_s3_bucket.audit_logs[0].id}", "")
    } : var.cloud_provider == "azure" ? {
      synthetic_data = try(azurerm_storage_account.main[0].primary_blob_endpoint, "")
      audit_logs     = try(azurerm_storage_account.main[0].primary_blob_endpoint, "")
    } : var.cloud_provider == "gcp" ? {
      synthetic_data = try("gs://${google_storage_bucket.synthetic_data[0].name}", "")
      audit_logs     = try("gs://${google_storage_bucket.audit_logs[0].name}", "")
    } : {}
  )
}


# =============================================================================
# OUTPUT: storage_account_id
# =============================================================================
# Azure Storage Account resource ID, returned only when deploying to Azure.
# Returns an empty string for AWS and GCP deployments.
#
# This output is specific to Azure because Azure uses a hierarchical model
# where a single Storage Account contains multiple Blob Containers, unlike
# AWS S3 and GCP Cloud Storage where each bucket is an independent resource.
#
# The Storage Account ID is required for:
#   • Azure RBAC role assignments — granting service managed identities
#     "Storage Blob Data Contributor" or "Storage Blob Data Reader" roles
#     scoped to the storage account level
#   • Customer-managed key encryption — binding Key Vault encryption keys
#     to the storage account via azurerm_storage_account_customer_managed_key
#   • Azure Policy assignments — applying compliance policies at the
#     storage account scope
#   • Diagnostic settings — configuring storage account metrics and log
#     forwarding to Azure Monitor or Log Analytics Workspace
#
# Format: Azure Resource Manager (ARM) resource ID
#   e.g., /subscriptions/xxxx/resourceGroups/rg/providers/Microsoft.Storage/storageAccounts/syntheticerpprod
# =============================================================================

output "storage_account_id" {
  description = "Azure Storage Account ID for role assignment and access policy references"

  value = (
    var.cloud_provider == "azure"
    ? try(azurerm_storage_account.main[0].id, "")
    : ""
  )
}
