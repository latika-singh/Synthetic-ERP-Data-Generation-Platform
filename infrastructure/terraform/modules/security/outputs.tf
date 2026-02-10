# =============================================================================
# Terraform Security Module — Output Definitions
# =============================================================================
# Exposes key security resource identifiers to consuming modules and the root
# infrastructure/terraform/outputs.tf configuration.  These outputs enable
# downstream modules (storage, kubernetes) and the root composition to
# reference encryption keys, IAM roles, service accounts, and secrets manager
# resources provisioned by this security module.
#
# Outputs:
#   kms_key_ids          — KMS encryption key identifiers for AES-256 at-rest
#   kms_key_arns         — Full KMS key ARNs/URIs (sensitive)
#   iam_role_arns        — IAM role/identity identifiers per microservice
#   service_account_ids  — Unique identifiers for Kubernetes pod identity
#   secrets_manager_arns — Secrets manager resource identifiers (sensitive)
#
# Multi-Cloud Conditional Logic:
#   Every output uses conditional/ternary expressions to select the correct
#   resource reference based on var.cloud_provider.  Resources that may not
#   exist (due to count=0 or empty for_each when the provider is inactive)
#   are wrapped in try() to prevent plan-time errors.
#
# Consumed By:
#   • infrastructure/terraform/outputs.tf  — Exposes kms_key_ids, iam_role_arns
#   • storage module   — References kms_key_ids for bucket/container encryption
#   • kubernetes module — References iam_role_arns for service account bindings
#
# Security Design:
#   • kms_key_arns and secrets_manager_arns marked sensitive=true to prevent
#     accidental exposure of resource URIs in CLI output and state logs
#   • Output map keys use underscored naming convention (e.g., "api_gateway")
#     while underlying resources use hyphenated names ("api-gateway") per
#     Kubernetes naming conventions
#
# References:
#   Agent Action Plan §0.5.3 — Security Considerations
#   Agent Action Plan §0.7.2 — Constraints C-001 through C-005
#   Technical Specification §6.4 — Security Architecture
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  security
# =============================================================================


# =============================================================================
# LOCAL HELPERS
# =============================================================================
# Provides a mapping from underscored output keys (Terraform convention) to
# hyphenated resource keys (Kubernetes / cloud resource convention).  This
# ensures consumers receive clean, Terraform-idiomatic map keys while the
# module internally references resources by their cloud-native names.
# =============================================================================

locals {
  # -------------------------------------------------------------------------
  # Service name mapping: underscored output key → hyphenated resource key
  # Example: "api_gateway" → "api-gateway"
  #
  # Used by iam_role_arns and service_account_ids outputs to transform
  # for_each keys from hyphenated (cloud/K8s convention) to underscored
  # (Terraform output convention).
  # -------------------------------------------------------------------------
  service_output_keys = {
    for name in var.service_account_names : replace(name, "-", "_") => name
  }
}


# =============================================================================
# OUTPUT: kms_key_ids
# =============================================================================
# Map of KMS encryption key identifiers keyed by purpose.
#
# These short-form key IDs are used for referencing encryption keys in
# resource configurations (e.g., S3 bucket encryption, MongoDB at-rest
# encryption) where the full ARN/URI is not required.
#
# Provider-specific attributes:
#   AWS   — aws_kms_key.*.key_id      (UUID format)
#   Azure — azurerm_key_vault_key.*.id (Key Vault URI)
#   GCP   — google_kms_crypto_key.*.id (full resource path)
#
# Returns empty map when KMS is disabled (var.enable_kms = false).
# =============================================================================

output "kms_key_ids" {
  description = "Map of KMS encryption key IDs keyed by purpose (data_encryption, audit_log_encryption) for AES-256 at-rest encryption"

  value = (
    var.cloud_provider == "aws" ? {
      data_encryption      = try(aws_kms_key.data_encryption[0].key_id, "")
      audit_log_encryption = try(aws_kms_key.audit_log_encryption[0].key_id, "")
    } : var.cloud_provider == "azure" ? {
      data_encryption      = try(azurerm_key_vault_key.data_encryption[0].id, "")
      audit_log_encryption = try(azurerm_key_vault_key.audit_log_encryption[0].id, "")
    } : var.cloud_provider == "gcp" ? {
      data_encryption      = try(google_kms_crypto_key.data_encryption[0].id, "")
      audit_log_encryption = try(google_kms_crypto_key.audit_log_encryption[0].id, "")
    } : {}
  )
}


# =============================================================================
# OUTPUT: kms_key_arns
# =============================================================================
# Map of full KMS key ARNs or resource URIs for cross-account, cross-module,
# and cross-service encryption references.
#
# These fully-qualified identifiers are required when:
#   • Granting cross-account KMS access via IAM policies
#   • Referencing keys in Kubernetes Secrets Store CSI Driver configurations
#   • Configuring server-side encryption on cloud storage buckets/containers
#
# Provider-specific attributes:
#   AWS   — aws_kms_key.*.arn                              (full ARN)
#   Azure — azurerm_key_vault_key.*.resource_manager_id    (ARM resource ID)
#   GCP   — google_kms_crypto_key.*.id                     (full resource path)
#
# SENSITIVE: Marked sensitive to prevent ARN/URI exposure in CLI output,
# Terraform plan summaries, and state file logs.  Consuming modules must
# explicitly reference the output to access values.
# =============================================================================

output "kms_key_arns" {
  description = "Map of full KMS key ARNs/resource URIs for cross-account or cross-module encryption references"
  sensitive   = true

  value = (
    var.cloud_provider == "aws" ? {
      data_encryption      = try(aws_kms_key.data_encryption[0].arn, "")
      audit_log_encryption = try(aws_kms_key.audit_log_encryption[0].arn, "")
    } : var.cloud_provider == "azure" ? {
      data_encryption      = try(azurerm_key_vault_key.data_encryption[0].resource_manager_id, "")
      audit_log_encryption = try(azurerm_key_vault_key.audit_log_encryption[0].resource_manager_id, "")
    } : var.cloud_provider == "gcp" ? {
      data_encryption      = try(google_kms_crypto_key.data_encryption[0].id, "")
      audit_log_encryption = try(google_kms_crypto_key.audit_log_encryption[0].id, "")
    } : {}
  )
}


# =============================================================================
# OUTPUT: iam_role_arns
# =============================================================================
# Map of IAM role identifiers for all six microservices, keyed by
# underscored service name.
#
# These identifiers are consumed by the Kubernetes module to bind cloud
# IAM roles/identities to Kubernetes service accounts, enabling pods to
# authenticate against cloud APIs using workload identity:
#
#   AWS   — IRSA (IAM Roles for Service Accounts) via aws_iam_role.*.arn
#   Azure — Workload Identity via azurerm_user_assigned_identity.*.id
#   GCP   — Workload Identity via google_service_account.*.email
#
# Map keys (consistent across all providers):
#   api_gateway, generation_engine, profiling_service,
#   quality_service, compliance_service, provisioning_service
#
# Each role is configured with least-privilege permissions specific to
# its microservice's operational requirements (see main.tf for policy
# definitions).
# =============================================================================

output "iam_role_arns" {
  description = "Map of IAM role ARNs/managed identity IDs/service account emails keyed by service name for workload identity binding"

  value = (
    var.cloud_provider == "aws" ? {
      for out_key, svc_name in local.service_output_keys :
      out_key => try(aws_iam_role.service_roles[svc_name].arn, "")
    } : var.cloud_provider == "azure" ? {
      for out_key, svc_name in local.service_output_keys :
      out_key => try(azurerm_user_assigned_identity.service_identities[svc_name].id, "")
    } : var.cloud_provider == "gcp" ? {
      for out_key, svc_name in local.service_output_keys :
      out_key => try(google_service_account.service_accounts[svc_name].email, "")
    } : {}
  )
}


# =============================================================================
# OUTPUT: service_account_ids
# =============================================================================
# Map of service account unique identifiers for Kubernetes pod identity
# binding.  These are the internal, immutable identifiers (distinct from
# the ARN/email in iam_role_arns) used for:
#
#   • Validating pod identity tokens during authentication
#   • Correlating cloud audit logs back to specific service accounts
#   • Configuring fine-grained Kubernetes RBAC trust relationships
#
# Provider-specific attributes:
#   AWS   — aws_iam_role.*.unique_id                              (AROA...)
#   Azure — azurerm_user_assigned_identity.*.principal_id         (GUID)
#   GCP   — google_service_account.*.unique_id                    (numeric)
#
# Map keys match iam_role_arns for consistent cross-referencing:
#   api_gateway, generation_engine, profiling_service,
#   quality_service, compliance_service, provisioning_service
# =============================================================================

output "service_account_ids" {
  description = "Map of service account unique identifiers for Kubernetes pod identity binding"

  value = (
    var.cloud_provider == "aws" ? {
      for out_key, svc_name in local.service_output_keys :
      out_key => try(aws_iam_role.service_roles[svc_name].unique_id, "")
    } : var.cloud_provider == "azure" ? {
      for out_key, svc_name in local.service_output_keys :
      out_key => try(azurerm_user_assigned_identity.service_identities[svc_name].principal_id, "")
    } : var.cloud_provider == "gcp" ? {
      for out_key, svc_name in local.service_output_keys :
      out_key => try(google_service_account.service_accounts[svc_name].unique_id, "")
    } : {}
  )
}


# =============================================================================
# OUTPUT: secrets_manager_arns
# =============================================================================
# Map of secrets manager resource identifiers keyed by secret purpose.
#
# These identifiers enable application workloads and Kubernetes Secrets Store
# CSI Driver to retrieve secrets at runtime for:
#
#   mongodb_credentials — MongoDB 7.0 connection credentials
#   redis_credentials   — Redis 7.x connection credentials
#   jwt_secret          — JWT RS256 signing/verification keys
#   auth0_credentials   — Auth0 OAuth 2.0 client credentials
#   encryption_keys     — AES-256-GCM data encryption key material
#
# Provider-specific attributes:
#   AWS   — aws_secretsmanager_secret.secrets[*].arn  (full ARN)
#   Azure — azurerm_key_vault_secret.secrets[*].id    (Key Vault URI)
#   GCP   — google_secret_manager_secret.secrets[*].id (full resource path)
#
# SENSITIVE: Marked sensitive to prevent secrets manager URIs from appearing
# in CLI output, plan summaries, or state file logs.  These URIs can be
# used to retrieve secret values if IAM permissions allow.
#
# Returns empty map when secrets manager is disabled
# (var.enable_secrets_manager = false).
# =============================================================================

output "secrets_manager_arns" {
  description = "Map of secrets manager ARNs/IDs keyed by secret purpose for application secret injection"
  sensitive   = true

  value = (
    var.cloud_provider == "aws" ? {
      for name, secret in aws_secretsmanager_secret.secrets :
      replace(name, "-", "_") => secret.arn
    } : var.cloud_provider == "azure" ? {
      for name, secret in azurerm_key_vault_secret.secrets :
      replace(name, "-", "_") => secret.id
    } : var.cloud_provider == "gcp" ? {
      for name, secret in google_secret_manager_secret.secrets :
      replace(name, "-", "_") => secret.id
    } : {}
  )
}
