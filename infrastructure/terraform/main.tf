# =============================================================================
# Synthetic-ERP-Data-Generation-Platform — Root Terraform Configuration
# =============================================================================
#
# This file is the primary entry point for all terraform plan/apply operations.
# It composes five infrastructure modules — networking, kubernetes, database,
# storage, and security — into a cohesive, multi-cloud deployment targeting
# AWS, Azure, or GCP based on the var.cloud_provider variable.
#
# Module Dependency Graph:
#
#   networking ──┬──► kubernetes ──► security ──┬──► database
#                │                              └──► storage
#                └─────────────────────────────────► (shared VPC/subnet refs)
#
# Usage:
#   # Initialize with cloud-specific backend (see backend section below):
#   terraform init -backend-config=environments/backend-dev.hcl
#
#   # Plan/apply with environment-specific variables:
#   terraform plan  -var-file=environments/dev.tfvars
#   terraform apply -var-file=environments/dev.tfvars
#
# Air-Gapped Deployment (Constraint C-003):
#   Configure a filesystem or network mirror for providers in ~/.terraformrc
#   or via the TF_CLI_CONFIG_FILE environment variable before terraform init.
#
# Reference: Agent Action Plan §0.3.1, §0.5.1, §0.7.2
# =============================================================================


# =============================================================================
# 1. TERRAFORM BACKEND CONFIGURATION
# =============================================================================
# Remote state storage with locking for safe team collaboration and CI/CD
# pipelines.  The backend type is set to S3 (AWS default) using partial
# configuration — actual bucket, key, region, and DynamoDB table values are
# supplied via -backend-config at terraform init.
#
# Terraform does NOT support variables or expressions in backend blocks, so
# the backend must be configured externally.  Examples for each cloud provider:
#
# ── AWS (S3 + DynamoDB Locking) ──────────────────────────────────────────────
#   terraform init \
#     -backend-config="bucket=synth-erp-tfstate-${ENV}" \
#     -backend-config="key=infrastructure/terraform.tfstate" \
#     -backend-config="region=us-east-1" \
#     -backend-config="dynamodb_table=synth-erp-tfstate-lock" \
#     -backend-config="encrypt=true"
#
# ── Azure (Blob Storage + Blob Lease Locking) ────────────────────────────────
#   To use Azure Blob backend, replace the backend "s3" block below with:
#     backend "azurerm" {}
#   Then init with:
#   terraform init \
#     -backend-config="storage_account_name=syntherptfstate" \
#     -backend-config="container_name=tfstate" \
#     -backend-config="key=infrastructure/terraform.tfstate" \
#     -backend-config="resource_group_name=synth-erp-tfstate-rg"
#
# ── GCP (Cloud Storage + Built-in Locking) ───────────────────────────────────
#   To use GCS backend, replace the backend "s3" block below with:
#     backend "gcs" {}
#   Then init with:
#   terraform init \
#     -backend-config="bucket=synth-erp-tfstate-${ENV}" \
#     -backend-config="prefix=infrastructure/terraform"
# =============================================================================

terraform {
  backend "s3" {
    # -------------------------------------------------------------------------
    # All values supplied via -backend-config at terraform init.
    # The encrypt flag ensures the state file is encrypted at rest (AES-256)
    # in the S3 bucket, satisfying security requirement R-006.
    # -------------------------------------------------------------------------
    encrypt = true
  }
}


# =============================================================================
# 2. LOCAL VALUES
# =============================================================================
# Centralises naming conventions, common tags, and derived values consumed by
# every module.  Keeping these in one place guarantees consistent resource
# naming and tagging across the entire infrastructure estate.
# =============================================================================

locals {
  # ---------------------------------------------------------------------------
  # Resource naming prefix — used by every module for globally unique names.
  # Pattern: {project_name}-{environment}  e.g. "synthetic-erp-platform-prod"
  # ---------------------------------------------------------------------------
  name_prefix = "${var.project_name}-${var.environment}"

  # ---------------------------------------------------------------------------
  # Common resource tags applied to ALL provisioned infrastructure.
  # Merged with user-supplied var.tags for cost allocation, compliance
  # tracking, and resource organisation.
  # ---------------------------------------------------------------------------
  common_tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Platform    = "synthetic-erp-data-generation"
    },
    var.tags,
  )

  # ---------------------------------------------------------------------------
  # Environment tier flags — drive conditional HA, security, and cost settings
  # across all modules without scattering ternary logic everywhere.
  # ---------------------------------------------------------------------------
  is_production = var.environment == "prod"
  is_staging    = var.environment == "staging"
  is_dev        = var.environment == "dev"

  # ---------------------------------------------------------------------------
  # Kubernetes service account names for the six platform microservices.
  # Passed to the security module for per-service IAM role / managed identity
  # / GCP service account creation with least-privilege policies.
  # ---------------------------------------------------------------------------
  service_account_names = [
    "api-gateway",
    "generation-engine",
    "profiling-service",
    "quality-service",
    "compliance-service",
    "provisioning-service",
  ]

  # ---------------------------------------------------------------------------
  # Resolved availability zones.
  # If the caller supplies explicit AZs via var.availability_zones, those are
  # used.  Otherwise we auto-discover AZs from the selected cloud provider's
  # data source (see Section 5 below).  Azure AZs are expressed as zone
  # numbers (["1","2","3"]) rather than named zones.
  # ---------------------------------------------------------------------------
  resolved_availability_zones = length(var.availability_zones) > 0 ? var.availability_zones : (
    var.cloud_provider == "aws" ? slice(data.aws_availability_zones.available[0].names, 0, min(3, length(data.aws_availability_zones.available[0].names))) :
    var.cloud_provider == "gcp" ? slice(data.google_compute_zones.available[0].names, 0, min(3, length(data.google_compute_zones.available[0].names))) :
    var.cloud_provider == "azure" ? ["1", "2", "3"] :
    []
  )

  # ---------------------------------------------------------------------------
  # Azure resource group name — required by Azure modules when
  # var.cloud_provider == "azure".  Follows the platform naming convention.
  # ---------------------------------------------------------------------------
  azure_resource_group_name = "${local.name_prefix}-rg"

  # ---------------------------------------------------------------------------
  # Cluster autoscaler enablement — always on for staging/prod, optional dev.
  # ---------------------------------------------------------------------------
  enable_cluster_autoscaler = var.environment != "dev" ? true : true

  # ---------------------------------------------------------------------------
  # Database backup retention (days) — graduated by environment to control
  # cost while meeting SOC 2 Type II audit requirements in production.
  # ---------------------------------------------------------------------------
  backup_retention_days = (
    local.is_production ? 35 :
    local.is_staging ? 14 :
    7
  )

  # ---------------------------------------------------------------------------
  # KMS key rotation period — 90 days for SOC 2 Type II compliance.
  # ---------------------------------------------------------------------------
  key_rotation_days = 90

  # ---------------------------------------------------------------------------
  # Storage lifecycle policy — transition days before moving data to
  # infrequent-access storage tiers and expiration for cost optimisation.
  # ---------------------------------------------------------------------------
  lifecycle_transition_days = var.storage_lifecycle_days > 0 ? var.storage_lifecycle_days : 30
  lifecycle_expiration_days = (
    local.is_production ? 730 :
    local.is_staging ? 365 :
    90
  )

  # ---------------------------------------------------------------------------
  # Kubernetes authentication token — derived from cloud-specific data
  # sources after the cluster is provisioned.  Used by the kubernetes and
  # helm providers for in-cluster resource management.
  # ---------------------------------------------------------------------------
  kubernetes_host = try(module.kubernetes.cluster_endpoint, "")
  kubernetes_ca   = try(module.kubernetes.cluster_ca_certificate, "")
  kubernetes_token = (
    var.cloud_provider == "aws" ? try(data.aws_eks_cluster_auth.cluster[0].token, "") :
    ""
  )
}


# =============================================================================
# 3. PROVIDER CONFIGURATIONS
# =============================================================================
# All cloud providers are declared so that their resources can be created
# conditionally (via count = var.cloud_provider == "..." ? 1 : 0 in modules).
# Only the selected provider requires valid credentials; inactive providers
# skip credential validation to avoid authentication failures.
#
# The kubernetes and helm providers are configured from the kubernetes module
# outputs so they can manage in-cluster resources (namespaces, ConfigMaps,
# Helm charts) after the cluster is provisioned.
# =============================================================================

# -----------------------------------------------------------------------------
# AWS Provider — Amazon Web Services
# -----------------------------------------------------------------------------
# Provides EKS, S3, DocumentDB, ElastiCache, IAM, KMS, and VPC resources.
# Credentials sourced from standard AWS credential chain:
#   AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY env vars, shared credentials
#   file (~/.aws/credentials), or IAM instance profile / IRSA.
# -----------------------------------------------------------------------------
provider "aws" {
  region = var.region

  # When AWS is not the selected provider, skip credential validation and
  # metadata checks to prevent authentication errors during terraform init.
  skip_credentials_validation = var.cloud_provider != "aws"
  skip_requesting_account_id  = var.cloud_provider != "aws"
  skip_metadata_api_check     = var.cloud_provider != "aws"

  default_tags {
    tags = local.common_tags
  }
}

# -----------------------------------------------------------------------------
# Azure Provider — Microsoft Azure
# -----------------------------------------------------------------------------
# Provides AKS, Blob Storage, Cosmos DB (MongoDB API), Key Vault, VNet, and
# Managed Identity resources.  Credentials sourced from:
#   ARM_SUBSCRIPTION_ID, ARM_TENANT_ID, ARM_CLIENT_ID, ARM_CLIENT_SECRET
#   environment variables, or Azure CLI authentication (az login).
# -----------------------------------------------------------------------------
provider "azurerm" {
  features {
    key_vault {
      # Allow purging soft-deleted Key Vault resources in non-production envs
      # for easier teardown/recreation cycles.
      purge_soft_delete_on_destroy = !local.is_production
    }
    resource_group {
      # Prevent accidental destruction of resource groups containing active
      # resources in production environments.
      prevent_deletion_if_contains_resources = local.is_production
    }
  }

  # When Azure is not the selected provider, skip provider feature
  # registration to avoid ARM API calls without credentials.
  skip_provider_registration = var.cloud_provider != "azure"
}

# -----------------------------------------------------------------------------
# Google Cloud Provider — Google Cloud Platform
# -----------------------------------------------------------------------------
# Provides GKE, Cloud Storage, Cloud KMS, VPC, and IAM resources.
# Credentials sourced from:
#   GOOGLE_APPLICATION_CREDENTIALS env var pointing to a service account
#   JSON key file, or gcloud application-default credentials (gcloud auth).
#   Project ID from GOOGLE_PROJECT env var or gcloud config.
# -----------------------------------------------------------------------------
provider "google" {
  region = var.region
}

# -----------------------------------------------------------------------------
# Kubernetes Provider
# -----------------------------------------------------------------------------
# Configured from the kubernetes module outputs after cluster creation.
# Manages Kubernetes-native resources: Namespaces, ConfigMaps, Secrets,
# NetworkPolicies, ResourceQuotas, and ServiceAccounts.
#
# Authentication strategy per cloud provider:
#   AWS  — EKS cluster auth token via data.aws_eks_cluster_auth
#   Azure — Client certificate from AKS kubeconfig output
#   GCP  — Access token from google_client_config data source
#
# On first apply (cluster not yet created), try() returns empty strings
# and the provider is effectively a no-op until the cluster exists.
# -----------------------------------------------------------------------------
provider "kubernetes" {
  host                   = local.kubernetes_host
  cluster_ca_certificate = local.kubernetes_ca != "" ? base64decode(local.kubernetes_ca) : ""

  # AWS EKS token-based authentication
  token = var.cloud_provider == "aws" ? local.kubernetes_token : (
    var.cloud_provider == "gcp" ? try(data.google_client_config.current[0].access_token, "") :
    ""
  )

  # Azure AKS certificate-based authentication
  client_certificate = var.cloud_provider == "azure" ? try(
    base64decode(module.kubernetes.kubeconfig), ""
  ) : null
}

# -----------------------------------------------------------------------------
# Helm Provider
# -----------------------------------------------------------------------------
# Mirrors the kubernetes provider configuration for deploying Helm charts:
#   • NGINX Ingress Controller with TLS 1.3 termination
#   • Prometheus + Grafana monitoring stack
#   • OpenTelemetry Collector for distributed tracing
#   • Cert-Manager for automated TLS certificate management
# -----------------------------------------------------------------------------
provider "helm" {
  kubernetes {
    host                   = local.kubernetes_host
    cluster_ca_certificate = local.kubernetes_ca != "" ? base64decode(local.kubernetes_ca) : ""

    token = var.cloud_provider == "aws" ? local.kubernetes_token : (
      var.cloud_provider == "gcp" ? try(data.google_client_config.current[0].access_token, "") :
      ""
    )

    client_certificate = var.cloud_provider == "azure" ? try(
      base64decode(module.kubernetes.kubeconfig), ""
    ) : null
  }
}


# =============================================================================
# 4. DATA SOURCES
# =============================================================================
# Cloud-specific data sources for discovering availability zones, current
# account/project context, and EKS cluster authentication tokens.  Each is
# conditionally created only when the corresponding cloud provider is active.
# =============================================================================

# -----------------------------------------------------------------------------
# AWS — Availability Zone Discovery
# -----------------------------------------------------------------------------
# Returns the list of available AZs in the selected region (e.g.,
# ["us-east-1a", "us-east-1b", "us-east-1c"]).  Used by
# local.resolved_availability_zones when var.availability_zones is empty.
# -----------------------------------------------------------------------------
data "aws_availability_zones" "available" {
  count = var.cloud_provider == "aws" ? 1 : 0

  state = "available"

  # Exclude Local Zones and Wavelength Zones which have limited service
  # support and are not suitable for EKS/DocumentDB/ElastiCache placement.
  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"]
  }
}

# -----------------------------------------------------------------------------
# AWS — Current Caller Identity
# -----------------------------------------------------------------------------
# Provides the AWS account ID for constructing IAM ARNs and resource policies.
# -----------------------------------------------------------------------------
data "aws_caller_identity" "current" {
  count = var.cloud_provider == "aws" ? 1 : 0
}

# -----------------------------------------------------------------------------
# AWS — Current Region
# -----------------------------------------------------------------------------
# Confirms the active AWS region for resource placement validation.
# -----------------------------------------------------------------------------
data "aws_region" "current" {
  count = var.cloud_provider == "aws" ? 1 : 0
}

# -----------------------------------------------------------------------------
# AWS — EKS Cluster Authentication Token
# -----------------------------------------------------------------------------
# Retrieves a short-lived authentication token for the EKS cluster.  Used by
# the kubernetes and helm providers for API server authentication.  Only
# available after the EKS cluster has been successfully provisioned.
# -----------------------------------------------------------------------------
data "aws_eks_cluster_auth" "cluster" {
  count = var.cloud_provider == "aws" ? 1 : 0
  name  = try(module.kubernetes.cluster_name, "${local.name_prefix}-cluster")
}

# -----------------------------------------------------------------------------
# GCP — Availability Zone Discovery
# -----------------------------------------------------------------------------
# Returns the list of available compute zones in the selected region (e.g.,
# ["us-central1-a", "us-central1-b", "us-central1-c"]).  Used by
# local.resolved_availability_zones when var.availability_zones is empty.
# -----------------------------------------------------------------------------
data "google_compute_zones" "available" {
  count  = var.cloud_provider == "gcp" ? 1 : 0
  region = var.region
  status = "UP"
}

# -----------------------------------------------------------------------------
# GCP — Client Configuration
# -----------------------------------------------------------------------------
# Provides the current GCP project, region, and access token.  The access
# token is used by the kubernetes/helm providers for GKE authentication.
# -----------------------------------------------------------------------------
data "google_client_config" "current" {
  count = var.cloud_provider == "gcp" ? 1 : 0
}


# =============================================================================
# 5. MODULE COMPOSITIONS
# =============================================================================
# Each module encapsulates a logical infrastructure tier.  Outputs from
# upstream modules are wired as inputs to downstream modules, establishing
# the dependency graph described at the top of this file.
#
# Module execution order (enforced via depends_on and implicit references):
#   1. networking  — VPC, subnets, security groups, NAT gateways
#   2. kubernetes  — Managed K8s cluster (EKS / AKS / GKE)
#   3. security    — KMS keys, IAM roles, secrets manager
#   4. database    — MongoDB 7.0, Redis 7.x
#   5. storage     — Cloud storage buckets (S3 / Blob / GCS)
# =============================================================================

# -----------------------------------------------------------------------------
# 5a. NETWORKING MODULE
# -----------------------------------------------------------------------------
# Provisions the foundational network infrastructure:
#   • VPC / VNet / VPC Network with configurable CIDR block
#   • Public subnets for load balancers and ingress controllers
#   • Private subnets for Kubernetes nodes, databases, and Redis
#   • NAT Gateways for outbound internet access from private subnets
#   • Security groups / NSGs / firewall rules per service tier
#   • VPC flow logs for SOC 2 Type II audit compliance
#
# Outputs consumed by: kubernetes, database, storage, security modules
# -----------------------------------------------------------------------------
module "networking" {
  source = "./modules/networking"

  cloud_provider      = var.cloud_provider
  environment         = var.environment
  project_name        = var.project_name
  region              = var.region
  vpc_cidr            = var.vpc_cidr
  availability_zones  = local.resolved_availability_zones
  enable_nat_gateway  = var.enable_nat_gateway
  single_nat_gateway  = var.single_nat_gateway
  allowed_cidr_blocks = var.allowed_cidr_blocks
  enable_flow_logs    = true
  flow_log_retention_days = var.log_retention_days
  tags                = local.common_tags
}

# -----------------------------------------------------------------------------
# 5b. KUBERNETES MODULE
# -----------------------------------------------------------------------------
# Provisions a managed Kubernetes cluster sized per environment:
#   • AWS  → Amazon EKS with managed node groups
#   • Azure → Azure Kubernetes Service (AKS) with system node pools
#   • GCP  → Google Kubernetes Engine (GKE) with node pools
#
# Cluster autoscaling is enabled (min_nodes → max_nodes) to support
# Horizontal Pod Autoscaler (HPA) for the Generation Engine, targeting
# 1M+ records/minute throughput in production.
#
# Network policies (Calico) are enabled for multi-tenant traffic isolation.
#
# Inputs from: networking (vpc_id, private_subnet_ids)
# Outputs consumed by: security (cluster_name), providers (endpoint, CA cert)
# -----------------------------------------------------------------------------
module "kubernetes" {
  source = "./modules/kubernetes"

  cloud_provider            = var.cloud_provider
  environment               = var.environment
  project_name              = var.project_name
  region                    = var.region
  cluster_version           = var.cluster_version
  node_instance_type        = var.node_instance_type
  min_nodes                 = var.min_nodes
  max_nodes                 = var.max_nodes
  desired_nodes             = var.desired_nodes
  vpc_id                    = module.networking.vpc_id
  subnet_ids                = module.networking.private_subnet_ids
  enable_cluster_autoscaler = local.enable_cluster_autoscaler
  tags                      = local.common_tags

  depends_on = [module.networking]
}

# -----------------------------------------------------------------------------
# 5c. SECURITY MODULE
# -----------------------------------------------------------------------------
# Provisions security infrastructure for SOC 2 Type II compliance:
#   • KMS encryption keys (data + audit) for AES-256-at-rest encryption
#   • Per-service IAM roles / managed identities / GCP service accounts
#     with least-privilege policies (one per microservice)
#   • Secrets manager entries for database credentials, JWT keys,
#     Auth0 secrets, and encryption key material
#   • Network ACLs / NSG rules for defence-in-depth
#   • Kubernetes workload identity bindings (IRSA / AAD / WI)
#
# Inputs from: kubernetes (cluster_name), networking (vpc_id, subnet_ids)
# Outputs consumed by: database (kms_key_id), storage (kms_key_id)
# -----------------------------------------------------------------------------
module "security" {
  source = "./modules/security"

  cloud_provider         = var.cloud_provider
  environment            = var.environment
  project_name           = var.project_name
  region                 = var.region
  enable_kms             = var.enable_kms
  enable_secrets_manager = var.enable_secrets_manager
  cluster_name           = module.kubernetes.cluster_name
  service_account_names  = local.service_account_names
  allowed_cidr_blocks    = var.allowed_cidr_blocks
  key_rotation_days      = local.key_rotation_days
  vpc_id                 = module.networking.vpc_id
  subnet_ids             = module.networking.private_subnet_ids
  tags                   = local.common_tags

  depends_on = [module.kubernetes]
}

# -----------------------------------------------------------------------------
# 5d. DATABASE MODULE
# -----------------------------------------------------------------------------
# Provisions the platform's persistent data stores:
#
#   MongoDB 7.0 (Metadata Repository)
#   ─────────────────────────────────
#   • AWS  → Amazon DocumentDB (MongoDB-compatible)
#   • Azure → Cosmos DB with MongoDB API
#   • GCP  → Self-managed MongoDB on Compute Engine or MongoDB Atlas
#   Stores five core collections: generation_profiles, statistical_profiles,
#   schema_definitions, audit_logs, tenant_configurations.
#
#   Redis 7.x (Cache Layer)
#   ───────────────────────
#   • AWS  → Amazon ElastiCache for Redis
#   • Azure → Azure Cache for Redis
#   • GCP  → Cloud Memorystore for Redis
#   Provides session management, API response caching (≤50ms target),
#   and real-time generation job progress tracking.
#
# Both stores are placed in private subnets with security-group-restricted
# access (MongoDB 27017, Redis 6379) and encrypted at rest via KMS.
#
# Inputs from: networking (vpc_id, subnet_ids, security_group_ids),
#              security (kms_key_ids)
# Outputs consumed by: root outputs (mongodb_uri, redis_url)
# -----------------------------------------------------------------------------
module "database" {
  source = "./modules/database"

  cloud_provider          = var.cloud_provider
  environment             = var.environment
  project_name            = var.project_name
  region                  = var.region

  # MongoDB 7.0 configuration
  mongodb_version         = var.mongodb_version
  mongodb_instance_type   = var.mongodb_instance_type
  mongodb_instance_count  = var.mongodb_replica_count
  mongodb_storage_size_gb = var.mongodb_storage_size_gb
  mongodb_admin_username  = "erpadmin"
  mongodb_admin_password  = "changeme-via-secrets-manager"

  # Redis 7.x configuration
  redis_version           = var.redis_version
  redis_node_type         = var.redis_node_type
  redis_replica_count     = var.redis_replica_count

  # Network placement — private subnets from networking module
  vpc_id                  = module.networking.vpc_id
  subnet_ids              = module.networking.private_subnet_ids

  # Security group access control — from networking module's service-tier SGs
  security_group_id       = try(module.networking.security_group_ids["database"], "")
  redis_security_group_id = try(module.networking.security_group_ids["redis"], "")

  # Encryption at rest — KMS key from security module
  kms_key_id              = var.enable_kms ? try(module.security.kms_key_ids["data_encryption"], "") : ""

  # Backup policy — graduated by environment
  backup_retention_days   = local.backup_retention_days

  # Azure-specific
  resource_group_name     = var.cloud_provider == "azure" ? local.azure_resource_group_name : ""

  tags = local.common_tags

  depends_on = [module.networking, module.security]
}

# -----------------------------------------------------------------------------
# 5e. STORAGE MODULE
# -----------------------------------------------------------------------------
# Provisions cloud storage for synthetic data export and audit log archival:
#   • AWS  → Amazon S3 buckets
#   • Azure → Azure Blob Storage containers
#   • GCP  → Google Cloud Storage buckets
#
# Two storage targets are created per environment:
#   1. synthetic-data — Export target for the Provisioning Service
#      (SQL, CSV, JSON, Parquet formats).  Lifecycle transitions to
#      infrequent-access tiers for cost optimisation.
#   2. audit-logs — Immutable audit trail storage with 7-year retention
#      for SOC 2 Type II compliance.  Versioning always enabled.
#
# All buckets enforce:
#   • AES-256 server-side encryption via KMS key from security module
#   • Public access blocked
#   • TLS-only access policies
#   • CORS rules for Web Console direct upload support
#
# Inputs from: security (kms_key_ids)
# Outputs consumed by: root outputs (storage_bucket_names)
# -----------------------------------------------------------------------------
module "storage" {
  source = "./modules/storage"

  cloud_provider          = var.cloud_provider
  environment             = var.environment
  project_name            = var.project_name
  region                  = var.region
  bucket_name_prefix      = var.storage_bucket_prefix
  enable_versioning       = var.enable_versioning

  # Encryption at rest — KMS key from security module
  kms_key_id              = var.enable_kms ? try(module.security.kms_key_ids["data_encryption"], "") : ""

  # Lifecycle policies for cost optimisation
  lifecycle_transition_days = local.lifecycle_transition_days
  lifecycle_expiration_days = local.lifecycle_expiration_days

  # Allow Web Console origin for direct uploads in non-production
  cors_allowed_origins    = local.is_production ? [] : ["*"]

  # Allow non-empty bucket deletion in dev for rapid iteration
  force_destroy           = local.is_dev

  # Azure-specific
  resource_group_name     = var.cloud_provider == "azure" ? local.azure_resource_group_name : ""

  tags = local.common_tags

  depends_on = [module.security]
}
