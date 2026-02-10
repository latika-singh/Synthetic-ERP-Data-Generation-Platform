# =============================================================================
# Synthetic-ERP-Data-Generation-Platform — Root Terraform Outputs
# =============================================================================
#
# This file exposes key resource identifiers and connection endpoints from all
# five composed infrastructure modules (networking, kubernetes, database,
# storage, security) as root-level Terraform outputs.
#
# Downstream consumers of these outputs include:
#   • CI/CD pipelines (GitHub Actions) — for kubectl/helm deployment targets
#   • Kubernetes ConfigMaps and Secrets — for injecting database and cache URIs
#   • Service configuration (.env files) — for backend microservice connectivity
#   • Monitoring dashboards — for linking infrastructure resources to metrics
#   • Terraform remote state consumers — via terraform_remote_state data source
#
# Sensitive outputs (database credentials, Redis URLs, kubeconfig) are marked
# with sensitive = true to prevent exposure in terraform plan/apply console
# output and CI/CD logs.  These values are still stored in the Terraform state
# file; ensure the state backend is encrypted (AES-256) per security
# requirement R-006.
#
# Reference: Agent Action Plan §0.3.1 — Root Configuration Files
# =============================================================================


# =============================================================================
# 1. KUBERNETES CLUSTER OUTPUTS
# =============================================================================
# Connection details for the managed Kubernetes cluster provisioned by the
# kubernetes module (EKS on AWS, AKS on Azure, GKE on GCP).  These outputs
# are essential for CI/CD pipelines, kubectl access, and Helm chart
# deployments.
# =============================================================================

# -----------------------------------------------------------------------------
# 1a. Cluster API Server Endpoint
# -----------------------------------------------------------------------------
# The HTTPS endpoint of the Kubernetes API server.  Used by:
#   • kubectl and Helm CLI tools in CI/CD pipelines
#   • The kubernetes and helm Terraform providers (configured in main.tf)
#   • ArgoCD or Flux GitOps controllers for cluster registration
#   • Monitoring systems for API server health checks
#
# Format: https://<cluster-specific-hostname>
#   AWS  — EKS endpoint (e.g., https://ABCDEF.gr7.us-east-1.eks.amazonaws.com)
#   Azure — AKS FQDN   (e.g., https://synth-erp-aks-xxxxx.hcp.eastus.azmk8s.io:443)
#   GCP  — GKE endpoint (e.g., https://34.123.45.67)
# -----------------------------------------------------------------------------
output "cluster_endpoint" {
  description = "Kubernetes cluster API server endpoint URL. Used by CI/CD pipelines, kubectl, and Helm for cluster access and resource deployment."
  value       = module.kubernetes.cluster_endpoint
}

# -----------------------------------------------------------------------------
# 1b. Cluster Name
# -----------------------------------------------------------------------------
# The name identifier of the managed Kubernetes cluster.  Used for:
#   • AWS EKS auth token retrieval (aws eks get-token --cluster-name)
#   • Azure AKS credential fetch (az aks get-credentials --name)
#   • GCP GKE credential fetch (gcloud container clusters get-credentials)
#   • Resource tagging and monitoring dashboard references
# -----------------------------------------------------------------------------
output "cluster_name" {
  description = "Name of the managed Kubernetes cluster for CLI authentication and resource identification."
  value       = module.kubernetes.cluster_name
}

# -----------------------------------------------------------------------------
# 1c. Kubeconfig
# -----------------------------------------------------------------------------
# Full kubeconfig content for authenticating to the Kubernetes cluster.
# Marked sensitive because it contains authentication credentials (tokens,
# certificates, or credential helper commands).
#
# Usage:
#   terraform output -raw kubeconfig > ~/.kube/config
#   export KUBECONFIG=$(terraform output -raw kubeconfig)
#
# In CI/CD pipelines, this value should be injected as a masked secret
# environment variable rather than written to disk.
# -----------------------------------------------------------------------------
output "kubeconfig" {
  description = "Full kubeconfig content for Kubernetes cluster access. Contains authentication credentials — handle as a secret."
  value       = module.kubernetes.kubeconfig
  sensitive   = true
}

# -----------------------------------------------------------------------------
# 1d. Load Balancer IP
# -----------------------------------------------------------------------------
# External IP address assigned to the cluster's ingress load balancer.  This
# is the entry point for all external HTTP(S) traffic to the platform:
#   • Web Console (React frontend) — served via NGINX Ingress path /
#   • API Gateway (Flask backend)  — served via NGINX Ingress path /api/v1/
#
# Note: This value may be empty immediately after cluster creation.  The
# external IP is allocated by the cloud provider's load balancer controller
# once the NGINX Ingress Controller Helm chart has been deployed and the
# LoadBalancer Service has been provisioned.  Subsequent terraform apply runs
# will populate this output once the IP is assigned.
#
# To configure DNS, create an A record (or CNAME for AWS ELB) pointing your
# domain to this IP address.
# -----------------------------------------------------------------------------
output "load_balancer_ip" {
  description = "External IP address of the ingress load balancer for routing HTTP(S) traffic to the Web Console and API Gateway."
  value       = module.kubernetes.load_balancer_ip
}


# =============================================================================
# 2. DATABASE OUTPUTS
# =============================================================================
# Connection details for MongoDB 7.0 (Metadata Repository) and Redis 7.x
# (session/API cache) provisioned by the database module.  Both outputs are
# marked sensitive as they may contain embedded authentication credentials.
# =============================================================================

# -----------------------------------------------------------------------------
# 2a. MongoDB Connection URI
# -----------------------------------------------------------------------------
# Full MongoDB connection string compatible with PyMongo 4.x.  Includes:
#   • Authentication credentials (username:password)
#   • Replica set configuration (for HA deployments)
#   • TLS/SSL parameters
#   • Connection pool settings
#
# Consumed by all six backend microservices via the shared MongoDB client
# (src/backend/shared/database/mongodb.py) for accessing the five core
# collections: generation_profiles, statistical_profiles, schema_definitions,
# audit_logs, and tenant_configurations.
#
# Cloud-specific format:
#   AWS  — DocumentDB:   mongodb://user:pass@docdb-cluster.region.docdb.amazonaws.com:27017/?tls=true&...
#   Azure — Cosmos DB:    mongodb://user:pass@account.mongo.cosmos.azure.com:10255/?ssl=true&...
#   GCP  — Atlas/Custom: mongodb+srv://user:pass@cluster.mongodb.net/?retryWrites=true&...
# -----------------------------------------------------------------------------
output "database_uri" {
  description = "MongoDB connection URI for PyMongo 4.x clients. Contains authentication credentials and TLS parameters. Used by all backend microservices."
  value       = module.database.mongodb_uri
  sensitive   = true
}

# -----------------------------------------------------------------------------
# 2b. Redis Connection URL
# -----------------------------------------------------------------------------
# Full Redis connection URL compatible with the redis-py 5.x client.
# Includes host, port, and (where applicable) authentication credentials.
#
# Consumed by all six backend microservices via the shared Redis client
# (src/backend/shared/database/redis_client.py) for:
#   • API response caching (≤50ms target response time)
#   • Session management (JWT refresh token storage)
#   • Generation job progress tracking (real-time updates)
#   • Rate limiter state (per-tier request counts)
#
# Cloud-specific format:
#   AWS  — ElastiCache: redis://primary-endpoint.xxxxx.use1.cache.amazonaws.com:6379
#   Azure — Redis Cache: rediss://:accesskey@hostname:6380 (TLS on port 6380)
#   GCP  — Memorystore: redis://10.0.0.x:6379
# -----------------------------------------------------------------------------
output "redis_url" {
  description = "Redis connection URL for session management and API caching. May contain access keys — handle as a secret."
  value       = module.database.redis_url
  sensitive   = true
}


# =============================================================================
# 3. NETWORKING OUTPUTS
# =============================================================================
# Network infrastructure identifiers from the networking module.  These outputs
# enable downstream consumers to reference the VPC/VNet and subnet topology
# for additional resource placement, peering, or network policy configuration.
# =============================================================================

# -----------------------------------------------------------------------------
# 3a. VPC / VNet Identifier
# -----------------------------------------------------------------------------
# The unique identifier of the Virtual Private Cloud (AWS), Virtual Network
# (Azure), or VPC Network (GCP) hosting the entire platform.
#
# Used by:
#   • VPC peering configurations for connecting to customer networks
#   • Additional security group / NSG rules outside of Terraform
#   • Network monitoring and flow log dashboards
#   • Service mesh or VPN gateway attachments
#
# Cloud-specific format:
#   AWS  — VPC ID:    vpc-0abc123def456789a
#   Azure — VNet ID:  /subscriptions/.../virtualNetworks/synth-erp-vnet
#   GCP  — Network:   projects/.../global/networks/synth-erp-vpc
# -----------------------------------------------------------------------------
output "vpc_id" {
  description = "VPC/VNet/VPC Network identifier hosting the platform infrastructure. Used for peering, security group references, and monitoring."
  value       = module.networking.vpc_id
}

# -----------------------------------------------------------------------------
# 3b. Subnet Identifiers
# -----------------------------------------------------------------------------
# A structured map containing both public and private subnet identifiers from
# the networking module.  Downstream consumers can reference specific subnet
# tiers for resource placement:
#
#   • public_subnet_ids  — Used for load balancers, NAT gateways, bastion
#     hosts, and any internet-facing resources
#   • private_subnet_ids — Used for Kubernetes worker nodes, database
#     instances, Redis clusters, and all internal-only workloads
#
# Example usage in Terraform remote state consumer:
#   data.terraform_remote_state.infra.outputs.subnet_ids.private
# -----------------------------------------------------------------------------
output "subnet_ids" {
  description = "Map of subnet identifiers with 'public' and 'private' keys, each containing a list of subnet IDs for resource placement."
  value = {
    public  = module.networking.public_subnet_ids
    private = module.networking.private_subnet_ids
  }
}


# =============================================================================
# 4. STORAGE OUTPUTS
# =============================================================================
# Cloud storage bucket/container identifiers from the storage module.  These
# outputs are consumed by the Provisioning Service configuration for synthetic
# data export targets and by audit log archival configurations.
# =============================================================================

# -----------------------------------------------------------------------------
# 4a. Storage Bucket Names
# -----------------------------------------------------------------------------
# Map of cloud storage bucket/container names keyed by purpose:
#   • "synthetic_data" — Target for Provisioning Service exports (SQL, CSV,
#     JSON, Parquet files generated by the platform)
#   • "audit_logs"     — Immutable audit trail archive for SOC 2 Type II
#     compliance with 7-year retention policy
#
# Cloud-specific format:
#   AWS  — S3 bucket names:      synth-erp-prod-synthetic-data
#   Azure — Container names:     synthetic-data (within a storage account)
#   GCP  — GCS bucket names:     synth-erp-prod-synthetic-data
#
# Usage in application config:
#   export SYNTHETIC_DATA_BUCKET=$(terraform output -json storage_bucket_names | jq -r '.synthetic_data')
#   export AUDIT_LOG_BUCKET=$(terraform output -json storage_bucket_names | jq -r '.audit_logs')
# -----------------------------------------------------------------------------
output "storage_bucket_names" {
  description = "Map of cloud storage bucket/container names keyed by purpose (synthetic_data, audit_logs) for Provisioning Service export and audit trail archival."
  value       = module.storage.bucket_names
}


# =============================================================================
# 5. SECURITY OUTPUTS
# =============================================================================
# Encryption key identifiers and IAM role references from the security module.
# These outputs enable cross-module encryption references and workload
# identity configuration for Kubernetes pod-level access control.
# =============================================================================

# -----------------------------------------------------------------------------
# 5a. KMS Encryption Key Identifiers
# -----------------------------------------------------------------------------
# Map of KMS encryption key IDs keyed by purpose:
#   • "data_encryption"      — AES-256 key for encrypting synthetic data at
#     rest in storage buckets, database volumes, and Kubernetes persistent
#     volumes.  Satisfies security requirement R-006 and SOC 2 Type II (C-004).
#   • "audit_log_encryption" — Dedicated key for encrypting tamper-evident
#     audit logs, ensuring separation of duties between data and audit
#     encryption keys.
#
# Cloud-specific format:
#   AWS  — KMS Key ID:         xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
#   Azure — Key Vault Key URI: https://vault.vault.azure.net/keys/keyname/version
#   GCP  — CryptoKey ID:       projects/.../cryptoKeys/keyname
# -----------------------------------------------------------------------------
output "kms_key_ids" {
  description = "Map of KMS encryption key IDs keyed by purpose (data_encryption, audit_log_encryption) for AES-256 at-rest encryption across all platform resources."
  value       = module.security.kms_key_ids
}

# -----------------------------------------------------------------------------
# 5b. IAM Role ARNs / Service Account Identifiers
# -----------------------------------------------------------------------------
# Map of IAM role identifiers for all six platform microservices, keyed by
# service name:
#   • "api_gateway"          — IAM role for the API Gateway service
#   • "generation_engine"    — IAM role for the Generation Engine service
#   • "profiling_service"    — IAM role for the Profiling Service
#   • "quality_service"      — IAM role for the Quality Service
#   • "compliance_service"   — IAM role for the Compliance Service
#   • "provisioning_service" — IAM role for the Provisioning Service
#
# These roles implement least-privilege access and are bound to Kubernetes
# service accounts via:
#   AWS  — IAM Roles for Service Accounts (IRSA)
#   Azure — Azure AD Workload Identity (Managed Identity)
#   GCP  — Workload Identity Federation (Service Account)
#
# Cloud-specific format:
#   AWS  — IAM Role ARN:     arn:aws:iam::123456789012:role/synth-erp-api-gateway
#   Azure — Managed Identity: /subscriptions/.../userAssignedIdentities/...
#   GCP  — Service Account:  api-gateway@project.iam.gserviceaccount.com
# -----------------------------------------------------------------------------
output "iam_role_arns" {
  description = "Map of IAM role ARNs/managed identity IDs/service account emails keyed by service name for Kubernetes workload identity binding."
  value       = module.security.iam_role_arns
}


# =============================================================================
# 6. ENVIRONMENT METADATA
# =============================================================================
# Contextual outputs that identify the deployment environment and cloud
# provider for use in downstream automation, tagging, and conditional logic.
# =============================================================================

# -----------------------------------------------------------------------------
# 6a. Environment Name
# -----------------------------------------------------------------------------
# The deployment environment (dev, staging, prod) for conditional logic in
# CI/CD pipelines and configuration management.
# -----------------------------------------------------------------------------
output "environment" {
  description = "Deployment environment name (dev, staging, prod) for downstream conditional logic and resource tagging."
  value       = var.environment
}

# -----------------------------------------------------------------------------
# 6b. Cloud Provider
# -----------------------------------------------------------------------------
# The active cloud provider (aws, azure, gcp) for provider-specific logic in
# CI/CD pipelines and service configuration templates.
# -----------------------------------------------------------------------------
output "cloud_provider" {
  description = "Active cloud provider (aws, azure, gcp) for provider-specific downstream configuration."
  value       = var.cloud_provider
}

# -----------------------------------------------------------------------------
# 6c. Region
# -----------------------------------------------------------------------------
# The cloud region where all infrastructure resources are deployed.
# -----------------------------------------------------------------------------
output "region" {
  description = "Cloud region where all platform infrastructure resources are deployed."
  value       = var.region
}
