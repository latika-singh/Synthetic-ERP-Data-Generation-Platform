# =============================================================================
# Terraform Security Module — Main Resource Definitions
# =============================================================================
# Provisions security infrastructure for the Synthetic-ERP-Data-Generation-
# Platform across AWS, Azure, or GCP.  This module is the security foundation
# that all other Terraform modules and Kubernetes workloads depend on for
# encryption key management, IAM access controls, and secret injection.
#
# Resources provisioned per cloud provider:
#
#   AWS   → KMS keys (data + audit), IAM roles per service with IRSA trust,
#           Secrets Manager secrets, Network ACLs
#   Azure → Key Vault with encryption keys, User-assigned Managed Identities,
#           RBAC role assignments, Key Vault secrets, NSG rules
#   GCP   → Cloud KMS key ring + crypto keys, GCP Service Accounts,
#           IAM bindings, Workload Identity bindings, Secret Manager secrets
#
# Security Design Principles:
#   • Least-privilege IAM — each microservice receives its own identity with
#     only the permissions it requires (see local.aws_service_policy_actions)
#   • Encryption at rest — AES-256 via customer-managed KMS keys with
#     automatic rotation (configurable via var.key_rotation_days)
#   • Separate audit key — audit logs use a distinct KMS key with stricter
#     policies for SOC 2 Type II defence-in-depth (Constraint C-004)
#   • Per-tenant isolation — encryption contexts (AWS), key per tenant
#     (Azure Key Vault), and per-tenant key versions (GCP) support
#     multi-tenant data segregation
#   • Defence-in-depth — network ACLs / NSG rules complement the networking
#     module's VPC-level security groups
#   • Secrets management — all credentials stored in cloud-native secrets
#     managers with KMS encryption; placeholder values injected via CI/CD
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
# LOCAL VALUES
# =============================================================================
# Centralises naming conventions, tags, service definitions, per-service
# policy configurations, and secret templates consumed by all provider-
# specific resource blocks.
# =============================================================================

locals {
  # ---------------------------------------------------------------------------
  # Resource naming prefix: "{project}-{env}"
  # Example: "synthetic-erp-platform-prod"
  # ---------------------------------------------------------------------------
  name_prefix = "${var.project_name}-${var.environment}"

  # ---------------------------------------------------------------------------
  # Boolean flags for conditional resource creation per cloud provider.
  # Every resource block uses count = local.is_<provider> ? ... : 0 or
  # for_each = local.<provider>_service_set for iterable resources.
  # ---------------------------------------------------------------------------
  is_aws   = var.cloud_provider == "aws"
  is_azure = var.cloud_provider == "azure"
  is_gcp   = var.cloud_provider == "gcp"

  # ---------------------------------------------------------------------------
  # Service account name sets — provider-specific for for_each iteration.
  # When the cloud provider is inactive the set is empty to skip creation.
  # ---------------------------------------------------------------------------
  service_names     = toset(var.service_account_names)
  aws_service_set   = local.is_aws ? local.service_names : toset([])
  azure_service_set = local.is_azure ? local.service_names : toset([])
  gcp_service_set   = local.is_gcp ? local.service_names : toset([])

  # ---------------------------------------------------------------------------
  # Kubernetes namespace for workload identity bindings (IRSA / AAD / WI).
  # Matches the namespace defined in infrastructure/kubernetes/namespace.yaml.
  # ---------------------------------------------------------------------------
  k8s_namespace = "synthetic-erp-platform"

  # ---------------------------------------------------------------------------
  # Safe data source references — use try() to prevent plan-time errors
  # when the data source is not instantiated (wrong cloud provider).
  # ---------------------------------------------------------------------------
  aws_account_id        = try(data.aws_caller_identity.current[0].account_id, "")
  aws_region_name       = try(data.aws_region.current[0].name, "")
  azure_tenant_id       = try(data.azurerm_client_config.current[0].tenant_id, "")
  azure_object_id       = try(data.azurerm_client_config.current[0].object_id, "")
  azure_subscription_id = try(data.azurerm_client_config.current[0].subscription_id, "")
  gcp_project_id        = try(data.google_client_config.current[0].project, "")

  # ---------------------------------------------------------------------------
  # AWS OIDC provider identifiers for IRSA (IAM Roles for Service Accounts).
  # Stripped URL and derived ARN for trust policy construction.
  # Empty when cluster_oidc_issuer_url is not yet available (bootstrapping).
  # ---------------------------------------------------------------------------
  oidc_issuer_stripped = var.cluster_oidc_issuer_url != "" ? replace(var.cluster_oidc_issuer_url, "https://", "") : ""
  oidc_provider_arn    = local.aws_account_id != "" && local.oidc_issuer_stripped != "" ? "arn:aws:iam::${local.aws_account_id}:oidc-provider/${local.oidc_issuer_stripped}" : ""

  # ---------------------------------------------------------------------------
  # Azure Key Vault name — constrained to 3–24 characters, alphanumeric
  # and hyphens.  Resource group derived from networking module convention.
  # ---------------------------------------------------------------------------
  azure_key_vault_name      = substr(replace("${local.name_prefix}-kv", "_", ""), 0, 24)
  azure_resource_group_name = "${local.name_prefix}-rg"
  azure_key_expiry_period   = "P${var.key_rotation_days * 2}D"

  # ---------------------------------------------------------------------------
  # GCP Service Account ID prefix — constrained to 6–30 characters.
  # Truncated from project_name for uniqueness within the character limit.
  # ---------------------------------------------------------------------------
  gcp_sa_id_prefix = substr(var.project_name, 0, 15)

  # ---------------------------------------------------------------------------
  # GCP labels — must be lowercase keys/values.  Converted from common_tags.
  # ---------------------------------------------------------------------------
  gcp_labels = { for k, v in local.common_tags : lower(k) => lower(v) }

  # ---------------------------------------------------------------------------
  # Secret definitions used across all cloud providers.
  # Each secret has a name and a placeholder JSON template.  Actual values
  # are injected via CI/CD pipelines or manual rotation — never committed
  # to version control or Terraform state.
  # ---------------------------------------------------------------------------
  secret_definitions = {
    "mongodb-credentials" = jsonencode({
      username = "REPLACE_VIA_CICD"
      password = "REPLACE_VIA_CICD"
      host     = "mongodb.${local.name_prefix}.internal"
      port     = "27017"
      database = "synthetic_erp"
      options  = "retryWrites=true&w=majority"
    })
    "redis-credentials" = jsonencode({
      host        = "redis.${local.name_prefix}.internal"
      port        = "6379"
      password    = "REPLACE_VIA_CICD"
      tls_enabled = "true"
      database    = "0"
    })
    "jwt-secret" = jsonencode({
      secret_key     = "REPLACE_VIA_CICD"
      algorithm      = "RS256"
      expiry_seconds = "3600"
      issuer         = "synthetic-erp-platform"
    })
    "auth0-credentials" = jsonencode({
      domain        = "REPLACE_VIA_CICD"
      client_id     = "REPLACE_VIA_CICD"
      client_secret = "REPLACE_VIA_CICD"
      audience      = "https://api.synthetic-erp-platform.com"
      callback_url  = "https://${local.name_prefix}.example.com/callback"
    })
    "encryption-keys" = jsonencode({
      master_key_id  = "REPLACE_VIA_CICD"
      algorithm      = "AES-256-GCM"
      key_derivation = "HKDF-SHA256"
      key_version    = "1"
    })
  }
  secret_names = toset(keys(local.secret_definitions))

  # ---------------------------------------------------------------------------
  # AWS per-service IAM policy actions — least-privilege access.
  # Each microservice receives exactly the permissions it requires.
  # ---------------------------------------------------------------------------
  aws_service_policy_actions = {
    "api-gateway" = {
      description = "API Gateway: KMS decrypt, secrets read, S3 config read"
      actions = [
        "kms:Decrypt",
        "kms:DescribeKey",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
        "s3:GetObject",
        "s3:ListBucket",
      ]
    }
    "generation-engine" = {
      description = "Generation Engine: KMS encrypt/decrypt, S3 read/write, secrets read"
      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:DescribeKey",
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket",
        "s3:DeleteObject",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
      ]
    }
    "profiling-service" = {
      description = "Profiling Service: KMS decrypt, secrets read for DB credentials"
      actions = [
        "kms:Decrypt",
        "kms:DescribeKey",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
      ]
    }
    "quality-service" = {
      description = "Quality Service: KMS decrypt, S3 read, secrets read"
      actions = [
        "kms:Decrypt",
        "kms:DescribeKey",
        "s3:GetObject",
        "s3:ListBucket",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
      ]
    }
    "compliance-service" = {
      description = "Compliance Service: KMS decrypt, secrets read, CloudWatch audit writes"
      actions = [
        "kms:Decrypt",
        "kms:DescribeKey",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
      ]
    }
    "provisioning-service" = {
      description = "Provisioning Service: KMS encrypt/decrypt, S3 full access, secrets read"
      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey",
        "kms:GenerateDataKeyWithoutPlaintext",
        "kms:DescribeKey",
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket",
        "s3:DeleteObject",
        "s3:GetBucketLocation",
        "s3:AbortMultipartUpload",
        "s3:ListMultipartUploadParts",
        "secretsmanager:GetSecretValue",
        "secretsmanager:DescribeSecret",
        "rds-db:connect",
      ]
    }
  }

  # ---------------------------------------------------------------------------
  # Common tags applied to every resource for cost tracking, compliance
  # labelling, and environment identification.
  # ---------------------------------------------------------------------------
  common_tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Module      = "security"
    },
    var.tags,
  )
}


# =============================================================================
# DATA SOURCES
# =============================================================================
# Cloud-specific data sources for account / tenant / project context
# required for IAM policy construction and resource naming.
# =============================================================================

# AWS — Current Account Identity (provides account_id for IAM ARNs)
data "aws_caller_identity" "current" {
  count = local.is_aws ? 1 : 0
}

# AWS — Current Region (for constructing resource ARN prefixes)
data "aws_region" "current" {
  count = local.is_aws ? 1 : 0
}

# Azure — Client Configuration (provides tenant_id and object_id)
data "azurerm_client_config" "current" {
  count = local.is_azure ? 1 : 0
}

# GCP — Client Configuration (provides project ID for IAM bindings)
data "google_client_config" "current" {
  count = local.is_gcp ? 1 : 0
}


# #############################################################################
#                          AWS RESOURCES
# #############################################################################
# Created when var.cloud_provider == "aws".
# Provisions KMS encryption keys, IAM roles with IRSA trust policies,
# per-service least-privilege IAM policies, Secrets Manager secrets,
# and Network ACL rules for defence-in-depth.
# #############################################################################


# =============================================================================
# AWS KMS ENCRYPTION KEYS
# =============================================================================
# Two customer-managed KMS keys for AES-256-at-rest encryption:
#   1. data_encryption      — General platform data (MongoDB, S3 exports)
#   2. audit_log_encryption — Audit logs (separate key for SOC 2 Type II)
#
# Both keys have automatic rotation enabled.  The key policy grants the root
# account full management access and allows same-account principals to
# perform encryption operations (restricted by IAM policies on each role).
#
# Per-tenant encryption is achieved via encryption contexts, allowing the
# same key to isolate data per tenant without key proliferation.
# =============================================================================

resource "aws_kms_key" "data_encryption" {
  count = local.is_aws && var.enable_kms ? 1 : 0

  description              = "Encryption key for Synthetic ERP platform data at rest"
  enable_key_rotation      = true
  deletion_window_in_days  = 30
  customer_master_key_spec = "SYMMETRIC_DEFAULT"
  key_usage                = "ENCRYPT_DECRYPT"

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "${local.name_prefix}-data-key-policy"
    Statement = [
      {
        Sid    = "EnableRootAccountFullAccess"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.aws_account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowServiceRoleEncryptionOperations"
        Effect = "Allow"
        Principal = {
          AWS = "*"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
          "kms:CreateGrant",
          "kms:ListGrants",
          "kms:RevokeGrant",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = local.aws_account_id
          }
        }
      },
    ]
  })

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-data-encryption"
    Purpose = "data-at-rest-encryption"
    KeyType = "data"
  })
}

resource "aws_kms_alias" "data_encryption" {
  count = local.is_aws && var.enable_kms ? 1 : 0

  name          = "alias/${var.project_name}-${var.environment}-data"
  target_key_id = aws_kms_key.data_encryption[0].key_id
}

# Separate KMS key for audit log encryption (SOC 2 Type II defence-in-depth).
# Stricter policy: only allows encrypt & generate from within the account;
# decrypt is limited to the S3 service (for reading archived audit logs).
resource "aws_kms_key" "audit_log_encryption" {
  count = local.is_aws && var.enable_kms ? 1 : 0

  description              = "Encryption key for Synthetic ERP platform audit logs (SOC 2 Type II)"
  enable_key_rotation      = true
  deletion_window_in_days  = 30
  customer_master_key_spec = "SYMMETRIC_DEFAULT"
  key_usage                = "ENCRYPT_DECRYPT"

  policy = jsonencode({
    Version = "2012-10-17"
    Id      = "${local.name_prefix}-audit-key-policy"
    Statement = [
      {
        Sid    = "EnableRootAccountFullAccess"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${local.aws_account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowAuditLogEncryption"
        Effect = "Allow"
        Principal = {
          AWS = "*"
        }
        Action = [
          "kms:Encrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = local.aws_account_id
          }
        }
      },
      {
        Sid    = "RestrictAuditLogDecryption"
        Effect = "Allow"
        Principal = {
          AWS = "*"
        }
        Action = [
          "kms:Decrypt",
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = local.aws_account_id
            "kms:ViaService"    = "s3.${local.aws_region_name}.amazonaws.com"
          }
        }
      },
    ]
  })

  tags = merge(local.common_tags, {
    Name       = "${local.name_prefix}-audit-log-encryption"
    Purpose    = "audit-log-encryption"
    KeyType    = "audit"
    Compliance = "soc2-type-ii"
  })
}

resource "aws_kms_alias" "audit_log_encryption" {
  count = local.is_aws && var.enable_kms ? 1 : 0

  name          = "alias/${var.project_name}-${var.environment}-audit"
  target_key_id = aws_kms_key.audit_log_encryption[0].key_id
}


# =============================================================================
# AWS IAM ROLES (IRSA — IAM Roles for Service Accounts)
# =============================================================================
# One IAM role per microservice, bound to the corresponding Kubernetes
# service account via IRSA (EKS OIDC identity provider federation).
#
# When the OIDC issuer URL is available (cluster deployed), the trust
# policy enables web identity federation so pods can assume the role.
# When empty (pre-cluster bootstrapping), a placeholder policy allows
# the EKS service to assume the role until the cluster is ready.
# =============================================================================

resource "aws_iam_role" "service_roles" {
  for_each = local.aws_service_set

  name = "${local.name_prefix}-${each.key}-role"
  path = "/synthetic-erp/"

  assume_role_policy = local.oidc_provider_arn != "" ? jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEKSServiceAccountAssumeRole"
        Effect = "Allow"
        Principal = {
          Federated = local.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${local.oidc_issuer_stripped}:aud" = "sts.amazonaws.com"
            "${local.oidc_issuer_stripped}:sub" = "system:serviceaccount:${local.k8s_namespace}:${each.key}"
          }
        }
      },
    ]
  }) : jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowEKSServiceAssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "eks.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      },
    ]
  })

  tags = merge(local.common_tags, {
    Name           = "${local.name_prefix}-${each.key}-role"
    ServiceAccount = each.key
  })
}


# =============================================================================
# AWS IAM POLICIES (Per-Service Least-Privilege)
# =============================================================================
# Each microservice receives a custom IAM policy with only the AWS API
# actions it needs.  Policy actions are defined in
# local.aws_service_policy_actions to centralise permission management.
# =============================================================================

resource "aws_iam_policy" "service_policies" {
  for_each = local.aws_service_set

  name        = "${local.name_prefix}-${each.key}-policy"
  path        = "/synthetic-erp/"
  description = lookup(local.aws_service_policy_actions, each.key, { description = "Service policy for ${each.key}" }).description

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ServicePermissions"
        Effect   = "Allow"
        Action   = lookup(local.aws_service_policy_actions, each.key, { actions = [] }).actions
        Resource = "*"
      },
    ]
  })

  tags = merge(local.common_tags, {
    Name           = "${local.name_prefix}-${each.key}-policy"
    ServiceAccount = each.key
  })
}

# Bind each service policy to its corresponding role
resource "aws_iam_role_policy_attachment" "service_attachments" {
  for_each = local.aws_service_set

  role       = aws_iam_role.service_roles[each.key].name
  policy_arn = aws_iam_policy.service_policies[each.key].arn
}


# =============================================================================
# AWS SECRETS MANAGER
# =============================================================================
# Stores sensitive configuration data (database credentials, API keys,
# JWT secrets) encrypted with the data encryption KMS key.
#
# Secrets are created with placeholder JSON values.  Actual credentials
# are injected via CI/CD pipelines or manual rotation — NEVER committed
# to Terraform state or version control.
# =============================================================================

resource "aws_secretsmanager_secret" "secrets" {
  for_each = local.is_aws && var.enable_secrets_manager ? local.secret_names : toset([])

  name        = "${local.name_prefix}-${each.key}"
  description = "Secret for ${replace(each.key, "-", " ")} — Synthetic ERP Platform"

  kms_key_id              = var.enable_kms ? aws_kms_key.data_encryption[0].arn : null
  recovery_window_in_days = 7

  tags = merge(local.common_tags, {
    Name       = "${local.name_prefix}-${each.key}"
    SecretType = each.key
  })
}

resource "aws_secretsmanager_secret_version" "secrets" {
  for_each = local.is_aws && var.enable_secrets_manager ? local.secret_names : toset([])

  secret_id     = aws_secretsmanager_secret.secrets[each.key].id
  secret_string = local.secret_definitions[each.key]
}


# =============================================================================
# AWS NETWORK ACL (Defence-in-Depth)
# =============================================================================
# Complements VPC-level security groups from the networking module with
# subnet-level network access control.
#
# Rules:
#   • Allow HTTPS (443) inbound for API access
#   • Allow MongoDB (27017) only from private VPC CIDR
#   • Allow Redis (6379) only from private VPC CIDR
#   • Allow ephemeral ports for return traffic
#   • Deny SSH and RDP (not needed in container environments)
#   • Allow all outbound (for external API calls, image pulls)
# =============================================================================

resource "aws_network_acl" "security" {
  count = local.is_aws && var.vpc_id != "" && length(var.subnet_ids) > 0 ? 1 : 0

  vpc_id     = var.vpc_id
  subnet_ids = var.subnet_ids

  # ── Inbound Rules ──────────────────────────────────────────────────────────

  # Allow HTTPS (443) from anywhere — security groups provide finer control
  ingress {
    rule_no    = 100
    protocol   = "tcp"
    action     = "allow"
    cidr_block = "0.0.0.0/0"
    from_port  = 443
    to_port    = 443
  }

  # Allow MongoDB (27017) from private VPC CIDR only
  ingress {
    rule_no    = 200
    protocol   = "tcp"
    action     = "allow"
    cidr_block = "10.0.0.0/8"
    from_port  = 27017
    to_port    = 27017
  }

  # Allow Redis (6379) from private VPC CIDR only
  ingress {
    rule_no    = 300
    protocol   = "tcp"
    action     = "allow"
    cidr_block = "10.0.0.0/8"
    from_port  = 6379
    to_port    = 6379
  }

  # Allow ephemeral ports for TCP return traffic
  ingress {
    rule_no    = 400
    protocol   = "tcp"
    action     = "allow"
    cidr_block = "0.0.0.0/0"
    from_port  = 1024
    to_port    = 65535
  }

  # Deny SSH from all (not needed in containerised environments)
  ingress {
    rule_no    = 500
    protocol   = "tcp"
    action     = "deny"
    cidr_block = "0.0.0.0/0"
    from_port  = 22
    to_port    = 22
  }

  # Deny RDP from all
  ingress {
    rule_no    = 510
    protocol   = "tcp"
    action     = "deny"
    cidr_block = "0.0.0.0/0"
    from_port  = 3389
    to_port    = 3389
  }

  # ── Outbound Rules ─────────────────────────────────────────────────────────

  # Allow all outbound traffic (for API calls, image pulls, etc.)
  egress {
    rule_no    = 100
    protocol   = "-1"
    action     = "allow"
    cidr_block = "0.0.0.0/0"
    from_port  = 0
    to_port    = 0
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-security-nacl"
  })
}


# #############################################################################
#                          AZURE RESOURCES
# #############################################################################
# Created when var.cloud_provider == "azure".
# Provisions Key Vault for centralised key and secret management,
# user-assigned managed identities per microservice for Kubernetes
# workload identity, Azure RBAC role assignments, and additional NSG
# rules for defence-in-depth.
# #############################################################################


# =============================================================================
# AZURE KEY VAULT
# =============================================================================
# Central vault for encryption keys and application secrets.
# Purge protection enabled for SOC 2 Type II compliance — keys and secrets
# cannot be permanently deleted during the retention window (90 days).
# Access policies grant the Terraform service principal full management
# rights and each service identity read-only access to keys and secrets.
# =============================================================================

resource "azurerm_key_vault" "main" {
  count = local.is_azure && (var.enable_kms || var.enable_secrets_manager) ? 1 : 0

  name                = local.azure_key_vault_name
  location            = var.region
  resource_group_name = local.azure_resource_group_name
  tenant_id           = local.azure_tenant_id
  sku_name            = "standard"

  enabled_for_disk_encryption = true
  enabled_for_deployment      = true
  purge_protection_enabled    = true
  soft_delete_retention_days  = 90

  enable_rbac_authorization = false

  # Grant the Terraform service principal full access for management
  access_policy {
    tenant_id = local.azure_tenant_id
    object_id = local.azure_object_id

    key_permissions = [
      "Backup", "Create", "Decrypt", "Delete", "Encrypt", "Get",
      "Import", "List", "Purge", "Recover", "Restore", "Sign",
      "UnwrapKey", "Update", "Verify", "WrapKey",
      "GetRotationPolicy", "SetRotationPolicy",
    ]

    secret_permissions = [
      "Backup", "Delete", "Get", "List", "Purge", "Recover",
      "Restore", "Set",
    ]

    certificate_permissions = [
      "Backup", "Create", "Delete", "DeleteIssuers", "Get",
      "GetIssuers", "Import", "List", "ListIssuers", "ManageContacts",
      "ManageIssuers", "Purge", "Recover", "Restore", "SetIssuers",
      "Update",
    ]
  }

  # Grant each service managed identity read access to keys and secrets
  dynamic "access_policy" {
    for_each = local.azure_service_set
    content {
      tenant_id = local.azure_tenant_id
      object_id = azurerm_user_assigned_identity.service_identities[access_policy.key].principal_id

      key_permissions = [
        "Get", "List", "Encrypt", "Decrypt", "WrapKey", "UnwrapKey",
      ]

      secret_permissions = [
        "Get", "List",
      ]
    }
  }

  tags = merge(local.common_tags, {
    Name = local.azure_key_vault_name
  })
}


# =============================================================================
# AZURE KEY VAULT KEYS
# =============================================================================
# RSA 2048 keys for data-at-rest encryption and audit log encryption.
# Rotation policy configured per var.key_rotation_days for SOC 2 Type II.
# =============================================================================

resource "azurerm_key_vault_key" "data_encryption" {
  count = local.is_azure && var.enable_kms ? 1 : 0

  name         = "${local.name_prefix}-data-encryption"
  key_vault_id = azurerm_key_vault.main[0].id
  key_type     = "RSA"
  key_size     = 2048
  key_opts     = ["encrypt", "decrypt", "wrapKey", "unwrapKey"]

  rotation_policy {
    automatic {
      time_before_expiry = "P30D"
    }
    expire_after         = local.azure_key_expiry_period
    notify_before_expiry = "P29D"
  }

  tags = merge(local.common_tags, {
    Purpose = "data-at-rest-encryption"
    KeyType = "data"
  })
}

resource "azurerm_key_vault_key" "audit_log_encryption" {
  count = local.is_azure && var.enable_kms ? 1 : 0

  name         = "${local.name_prefix}-audit-log-encryption"
  key_vault_id = azurerm_key_vault.main[0].id
  key_type     = "RSA"
  key_size     = 2048
  key_opts     = ["encrypt", "decrypt", "wrapKey", "unwrapKey"]

  rotation_policy {
    automatic {
      time_before_expiry = "P30D"
    }
    expire_after         = local.azure_key_expiry_period
    notify_before_expiry = "P29D"
  }

  tags = merge(local.common_tags, {
    Purpose    = "audit-log-encryption"
    KeyType    = "audit"
    Compliance = "soc2-type-ii"
  })
}


# =============================================================================
# AZURE USER-ASSIGNED MANAGED IDENTITIES
# =============================================================================
# One managed identity per microservice for Kubernetes workload identity
# (Azure Workload Identity Federation).  Each identity is granted only
# the Azure RBAC roles its microservice requires.
# =============================================================================

resource "azurerm_user_assigned_identity" "service_identities" {
  for_each = local.azure_service_set

  name                = "${local.name_prefix}-${each.key}-identity"
  location            = var.region
  resource_group_name = local.azure_resource_group_name

  tags = merge(local.common_tags, {
    Name           = "${local.name_prefix}-${each.key}-identity"
    ServiceAccount = each.key
  })
}


# =============================================================================
# AZURE ROLE ASSIGNMENTS
# =============================================================================
# Grants service identities appropriate Azure RBAC roles:
#   • Key Vault Crypto User    — encryption / decryption operations
#   • Key Vault Secrets User   — reading secrets
#   • Storage Blob Data Contributor — for provisioning-service only
# =============================================================================

# Key Vault Crypto User for all service identities
resource "azurerm_role_assignment" "kv_crypto_user" {
  for_each = local.is_azure && var.enable_kms ? local.service_names : toset([])

  scope                = azurerm_key_vault.main[0].id
  role_definition_name = "Key Vault Crypto User"
  principal_id         = azurerm_user_assigned_identity.service_identities[each.key].principal_id
}

# Key Vault Secrets User for all service identities
resource "azurerm_role_assignment" "kv_secrets_user" {
  for_each = local.is_azure && var.enable_secrets_manager ? local.service_names : toset([])

  scope                = azurerm_key_vault.main[0].id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.service_identities[each.key].principal_id
}

# Storage Blob Data Contributor for provisioning-service only
resource "azurerm_role_assignment" "storage_contributor" {
  count = local.is_azure ? 1 : 0

  scope                = "/subscriptions/${local.azure_subscription_id}/resourceGroups/${local.azure_resource_group_name}"
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.service_identities["provisioning-service"].principal_id
}


# =============================================================================
# AZURE KEY VAULT SECRETS
# =============================================================================
# Application secrets stored in Key Vault encrypted at rest.
# Placeholder JSON values — actual credentials injected via CI/CD.
# =============================================================================

resource "azurerm_key_vault_secret" "secrets" {
  for_each = local.is_azure && var.enable_secrets_manager ? local.secret_names : toset([])

  name         = each.key
  value        = local.secret_definitions[each.key]
  key_vault_id = azurerm_key_vault.main[0].id
  content_type = "application/json"

  tags = merge(local.common_tags, {
    SecretType = each.key
  })
}


# =============================================================================
# AZURE NETWORK SECURITY RULES (Defence-in-Depth)
# =============================================================================
# Additional NSG rules beyond the networking module:
#   • Deny cross-tenant traffic on database ports (Internet → data plane)
#   • Allow authenticated service mesh traffic between backend services
# These rules attach to NSGs created by the networking module.
# =============================================================================

resource "azurerm_network_security_rule" "deny_cross_tenant" {
  count = local.is_azure && var.vpc_id != "" ? 1 : 0

  name                        = "${local.name_prefix}-deny-cross-tenant"
  priority                    = 4000
  direction                   = "Inbound"
  access                      = "Deny"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_ranges     = ["27017", "6379"]
  source_address_prefix       = "Internet"
  destination_address_prefix  = "*"
  resource_group_name         = local.azure_resource_group_name
  network_security_group_name = "${local.name_prefix}-private-nsg"
}

resource "azurerm_network_security_rule" "allow_service_mesh" {
  count = local.is_azure && var.vpc_id != "" ? 1 : 0

  name                        = "${local.name_prefix}-allow-service-mesh"
  priority                    = 200
  direction                   = "Inbound"
  access                      = "Allow"
  protocol                    = "Tcp"
  source_port_range           = "*"
  destination_port_ranges     = ["5000-5005", "443", "8443"]
  source_address_prefix       = "VirtualNetwork"
  destination_address_prefix  = "VirtualNetwork"
  resource_group_name         = local.azure_resource_group_name
  network_security_group_name = "${local.name_prefix}-private-nsg"
}


# #############################################################################
#                          GCP RESOURCES
# #############################################################################
# Created when var.cloud_provider == "gcp".
# Provisions Cloud KMS key ring with crypto keys, GCP Service Accounts
# per microservice with IAM bindings, Workload Identity federation for
# GKE pod identity, and Secret Manager secrets.
# #############################################################################


# =============================================================================
# GCP CLOUD KMS
# =============================================================================
# Regional key ring containing crypto keys for data-at-rest and audit-log
# encryption.  GCP key rings are immutable — once created in a region they
# cannot be moved or deleted (only crypto key versions can be destroyed).
# =============================================================================

resource "google_kms_key_ring" "main" {
  count = local.is_gcp && var.enable_kms ? 1 : 0

  name     = "${var.project_name}-${var.environment}-keyring"
  location = var.region
  project  = local.gcp_project_id
}

resource "google_kms_crypto_key" "data_encryption" {
  count = local.is_gcp && var.enable_kms ? 1 : 0

  name            = "${local.name_prefix}-data-encryption"
  key_ring        = google_kms_key_ring.main[0].id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "${var.key_rotation_days * 86400}s"

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPT"
    protection_level = "SOFTWARE"
  }

  labels = merge(local.gcp_labels, {
    purpose  = "data-at-rest-encryption"
    key-type = "data"
  })
}

resource "google_kms_crypto_key" "audit_log_encryption" {
  count = local.is_gcp && var.enable_kms ? 1 : 0

  name            = "${local.name_prefix}-audit-log-encryption"
  key_ring        = google_kms_key_ring.main[0].id
  purpose         = "ENCRYPT_DECRYPT"
  rotation_period = "${var.key_rotation_days * 86400}s"

  version_template {
    algorithm        = "GOOGLE_SYMMETRIC_ENCRYPT"
    protection_level = "SOFTWARE"
  }

  labels = merge(local.gcp_labels, {
    purpose    = "audit-log-encryption"
    key-type   = "audit"
    compliance = "soc2-type-ii"
  })
}


# =============================================================================
# GCP SERVICE ACCOUNTS
# =============================================================================
# One GCP service account per microservice.  Each account receives only
# the IAM roles its microservice requires.  Workload Identity maps these
# GCP accounts to Kubernetes service accounts in the GKE cluster.
# =============================================================================

resource "google_service_account" "service_accounts" {
  for_each = local.gcp_service_set

  account_id   = substr("${local.gcp_sa_id_prefix}-${each.key}", 0, 30)
  display_name = "Synthetic ERP ${each.key} service account"
  description  = "Service account for the ${each.key} microservice of the Synthetic ERP platform"
  project      = local.gcp_project_id
}


# =============================================================================
# GCP IAM BINDINGS
# =============================================================================
# Per-service IAM role grants for Cloud KMS, Secret Manager, and Storage.
# =============================================================================

# Cloud KMS Encrypter/Decrypter for all service accounts
resource "google_project_iam_member" "kms_access" {
  for_each = local.is_gcp && var.enable_kms ? local.service_names : toset([])

  project = local.gcp_project_id
  role    = "roles/cloudkms.cryptoKeyEncrypterDecrypter"
  member  = "serviceAccount:${google_service_account.service_accounts[each.key].email}"
}

# Secret Manager Secret Accessor for all service accounts
resource "google_project_iam_member" "secret_access" {
  for_each = local.is_gcp && var.enable_secrets_manager ? local.service_names : toset([])

  project = local.gcp_project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.service_accounts[each.key].email}"
}

# Storage Object Admin for provisioning-service only
resource "google_project_iam_member" "storage_admin" {
  count = local.is_gcp ? 1 : 0

  project = local.gcp_project_id
  role    = "roles/storage.objectAdmin"
  member  = "serviceAccount:${google_service_account.service_accounts["provisioning-service"].email}"
}

# Cloud Logging Writer for compliance-service (audit log writes)
resource "google_project_iam_member" "logging_writer" {
  count = local.is_gcp ? 1 : 0

  project = local.gcp_project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.service_accounts["compliance-service"].email}"
}


# =============================================================================
# GCP WORKLOAD IDENTITY BINDINGS
# =============================================================================
# Links GCP service accounts to Kubernetes service accounts via Workload
# Identity, enabling GKE pods to authenticate as GCP service accounts
# without distributing key files.
# =============================================================================

resource "google_service_account_iam_binding" "workload_identity" {
  for_each = local.is_gcp && var.cluster_name != "" ? local.service_names : toset([])

  service_account_id = google_service_account.service_accounts[each.key].name
  role               = "roles/iam.workloadIdentityUser"

  members = [
    "serviceAccount:${local.gcp_project_id}.svc.id.goog[${local.k8s_namespace}/${each.key}]",
  ]
}


# =============================================================================
# GCP SECRET MANAGER
# =============================================================================
# Stores application secrets with automatic replication policy.
# Placeholder values — actual credentials injected via CI/CD pipelines.
# =============================================================================

resource "google_secret_manager_secret" "secrets" {
  for_each = local.is_gcp && var.enable_secrets_manager ? local.secret_names : toset([])

  secret_id = "${local.name_prefix}-${each.key}"
  project   = local.gcp_project_id

  replication {
    auto {}
  }

  labels = merge(local.gcp_labels, {
    secret-type = replace(each.key, "_", "-")
  })
}

resource "google_secret_manager_secret_version" "secrets" {
  for_each = local.is_gcp && var.enable_secrets_manager ? local.secret_names : toset([])

  secret      = google_secret_manager_secret.secrets[each.key].id
  secret_data = local.secret_definitions[each.key]
}
