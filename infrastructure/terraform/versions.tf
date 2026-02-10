# =============================================================================
# Synthetic-ERP-Data-Generation-Platform — Terraform Version Constraints
# =============================================================================
#
# This file enforces Terraform CLI and provider version constraints for the
# Synthetic-ERP-Data-Generation-Platform infrastructure.  Pinning versions
# guarantees reproducible plans/applies across developer workstations, CI/CD
# runners, and — critically — air-gapped environments where a local provider
# mirror (terraform provider mirror) is used instead of the public registry.
#
# Provider selection:
#   • aws       — Amazon Web Services (EKS, S3, RDS, IAM, KMS, VPC)
#   • azurerm   — Microsoft Azure (AKS, Blob Storage, CosmosDB, Key Vault)
#   • google    — Google Cloud Platform (GKE, GCS, Cloud KMS)
#   • kubernetes — In-cluster and remote Kubernetes resource management
#   • helm      — Helm chart deployments for monitoring stacks and ingress
#   • random    — Deterministic random naming to ensure resource uniqueness
#
# Version policy:
#   ~> (pessimistic constraint) allows only patch-level updates within the
#   specified minor version, preventing unintended breaking changes while
#   still receiving security and bug-fix releases.
#
# Reference:  Agent Action Plan §0.4.1 (Dependency Inventory) and §0.7.2
#             (Technical Constraints — Terraform 1.7+)
# =============================================================================

terraform {
  # ---------------------------------------------------------------------------
  # Terraform CLI version
  # ---------------------------------------------------------------------------
  # The platform requires Terraform 1.7+ for:
  #   • Removed block support for safe refactoring of infrastructure
  #   • Enhanced provider installation mirroring (air-gapped C-003 support)
  #   • Improved plan output and state management capabilities
  # ---------------------------------------------------------------------------
  required_version = ">= 1.7.0"

  # ---------------------------------------------------------------------------
  # Required provider declarations
  # ---------------------------------------------------------------------------
  # Each provider is sourced from the HashiCorp registry by default.  For
  # air-gapped deployments (Constraint C-003), configure a filesystem or
  # network mirror in the CLI configuration file (~/.terraformrc) or via the
  # TF_CLI_CONFIG_FILE environment variable.
  # ---------------------------------------------------------------------------
  required_providers {
    # -------------------------------------------------------------------------
    # AWS Provider — Amazon Web Services
    # -------------------------------------------------------------------------
    # Used for provisioning:
    #   • Amazon EKS (Elastic Kubernetes Service) clusters
    #   • Amazon S3 buckets for synthetic data export storage
    #   • Amazon RDS / DocumentDB for managed database instances
    #   • AWS IAM roles, policies, and instance profiles
    #   • AWS KMS keys for AES-256 encryption at rest
    #   • Amazon VPC networking (subnets, security groups, NAT gateways)
    # -------------------------------------------------------------------------
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }

    # -------------------------------------------------------------------------
    # Azure Provider — Microsoft Azure
    # -------------------------------------------------------------------------
    # Used for provisioning:
    #   • Azure Kubernetes Service (AKS) clusters
    #   • Azure Blob Storage for synthetic data export
    #   • Azure Cosmos DB (MongoDB API) for metadata repository
    #   • Azure Key Vault for secrets and encryption key management
    #   • Azure Virtual Network, subnets, and NSGs
    #   • Azure Managed Identity for workload identity federation
    # -------------------------------------------------------------------------
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 3.0"
    }

    # -------------------------------------------------------------------------
    # Google Cloud Provider — Google Cloud Platform
    # -------------------------------------------------------------------------
    # Used for provisioning:
    #   • Google Kubernetes Engine (GKE) clusters
    #   • Google Cloud Storage (GCS) buckets for synthetic data export
    #   • Cloud KMS for encryption key management
    #   • VPC networks, subnets, and firewall rules
    #   • IAM service accounts and workload identity
    # -------------------------------------------------------------------------
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }

    # -------------------------------------------------------------------------
    # Kubernetes Provider
    # -------------------------------------------------------------------------
    # Used for managing Kubernetes resources after cluster provisioning:
    #   • Namespace creation (synthetic-erp-platform, monitoring)
    #   • ConfigMaps and Secrets for service configuration
    #   • NetworkPolicies for multi-tenant traffic isolation
    #   • ResourceQuotas for per-tenant resource limits
    #   • ServiceAccounts for workload identity binding
    # -------------------------------------------------------------------------
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.0"
    }

    # -------------------------------------------------------------------------
    # Helm Provider
    # -------------------------------------------------------------------------
    # Used for deploying Helm charts:
    #   • NGINX Ingress Controller with TLS 1.3 termination
    #   • Prometheus + Grafana monitoring stack
    #   • OpenTelemetry Collector for distributed tracing
    #   • Cert-Manager for automated TLS certificate management
    # -------------------------------------------------------------------------
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.0"
    }

    # -------------------------------------------------------------------------
    # Random Provider
    # -------------------------------------------------------------------------
    # Used for generating unique, deterministic resource identifiers:
    #   • Unique suffixes for globally-scoped resource names (S3 buckets, etc.)
    #   • Random passwords for database bootstrap credentials
    #   • Unique pet names for non-production environment resources
    # The random provider ensures idempotent naming across plan/apply cycles.
    # -------------------------------------------------------------------------
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}
