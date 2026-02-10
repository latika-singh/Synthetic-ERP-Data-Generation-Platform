# =============================================================================
# Terraform Input Variable Definitions
# Synthetic-ERP-Data-Generation-Platform
# =============================================================================
#
# This file defines all configurable parameters for the platform infrastructure.
# Variables are organized into logical groups:
#   1. Core Infrastructure — cloud provider, region, environment, project naming
#   2. Networking — VPC CIDR, availability zones, NAT gateways
#   3. Kubernetes — cluster version, node sizing, autoscaling bounds
#   4. Database — MongoDB 7.0 and Redis 7.x configuration
#   5. Storage — cloud storage buckets, versioning, encryption
#   6. Security — KMS, IP allowlists, secrets management
#   7. Tags — resource tagging for cost allocation and compliance
#
# Environment-specific values are supplied via tfvars files:
#   terraform plan -var-file=environments/dev.tfvars
#   terraform plan -var-file=environments/staging.tfvars
#   terraform plan -var-file=environments/prod.tfvars
# =============================================================================

# -----------------------------------------------------------------------------
# 1. Core Infrastructure Variables
# -----------------------------------------------------------------------------

variable "cloud_provider" {
  description = "Target cloud platform for infrastructure deployment. Determines which provider-specific resources and modules are activated. Must be one of: aws, azure, or gcp."
  type        = string

  validation {
    condition     = contains(["aws", "azure", "gcp"], var.cloud_provider)
    error_message = "The cloud_provider must be one of: \"aws\", \"azure\", or \"gcp\"."
  }
}

variable "region" {
  description = "Cloud provider region for resource deployment. Defaults to us-east-1 (AWS), eastus (Azure), or us-central1 (GCP) if not specified. Override via environment-specific tfvars."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = length(var.region) > 0
    error_message = "The region must be a non-empty string representing a valid cloud provider region."
  }
}

variable "environment" {
  description = "Deployment environment identifier. Controls resource sizing, high-availability configuration, security posture, and cost optimization. Used in resource naming and tagging."
  type        = string

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "The environment must be one of: \"dev\", \"staging\", or \"prod\"."
  }
}

variable "project_name" {
  description = "Project name used as a prefix for all resource names, ensuring globally unique naming across cloud providers. Must contain only lowercase alphanumeric characters and hyphens."
  type        = string
  default     = "synthetic-erp-platform"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,62}$", var.project_name))
    error_message = "The project_name must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, and be between 3 and 63 characters long."
  }
}

# -----------------------------------------------------------------------------
# 2. Networking Variables
# -----------------------------------------------------------------------------

variable "vpc_cidr" {
  description = "CIDR block for the Virtual Private Cloud (VPC/VNet). Each environment should use a distinct CIDR to avoid conflicts when peering: dev=10.0.0.0/16, staging=10.1.0.0/16, prod=10.2.0.0/16."
  type        = string
  default     = "10.0.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "The vpc_cidr must be a valid IPv4 CIDR block (e.g., \"10.0.0.0/16\")."
  }
}

variable "availability_zones" {
  description = "List of availability zones for multi-AZ deployment. When empty, the networking module auto-discovers available AZs in the selected region. Specify explicitly for deterministic placement (e.g., [\"us-east-1a\", \"us-east-1b\", \"us-east-1c\"])."
  type        = list(string)
  default     = []

  validation {
    condition     = length(var.availability_zones) == 0 || length(var.availability_zones) >= 2
    error_message = "If specifying availability zones, at least 2 must be provided for high-availability deployments."
  }
}

variable "enable_nat_gateway" {
  description = "Enable NAT Gateway for outbound internet access from private subnets. Required for services to pull container images, access external APIs (Auth0, cloud SDKs), and download dependencies. Disable only for fully air-gapped deployments with private registry mirroring."
  type        = bool
  default     = true
}

variable "single_nat_gateway" {
  description = "Use a single NAT Gateway shared across all availability zones. Set to true for development (cost savings) and false for staging/production (fault isolation per AZ). When false, one NAT Gateway is provisioned per AZ."
  type        = bool
  default     = true
}

# -----------------------------------------------------------------------------
# 3. Kubernetes Variables
# -----------------------------------------------------------------------------

variable "cluster_version" {
  description = "Kubernetes version for the managed cluster (EKS/AKS/GKE). Must be 1.29 or higher as required by the platform specification. Controls API compatibility for Deployments, HPA, NetworkPolicies, and other Kubernetes resources."
  type        = string
  default     = "1.29"

  validation {
    condition     = can(regex("^1\\.(29|3[0-9])$", var.cluster_version))
    error_message = "The cluster_version must be Kubernetes 1.29 or higher (e.g., \"1.29\", \"1.30\")."
  }
}

variable "node_instance_type" {
  description = "Instance type for Kubernetes worker nodes. Sizing varies by environment: dev=t3.medium (2 vCPU/4GB), staging=t3.large (2 vCPU/8GB), prod=m5.xlarge (4 vCPU/16GB) to support generation throughput targets of 1M+ records/minute."
  type        = string
  default     = "t3.medium"

  validation {
    condition     = length(var.node_instance_type) > 0
    error_message = "The node_instance_type must be a non-empty string representing a valid cloud provider instance type."
  }
}

variable "min_nodes" {
  description = "Minimum number of Kubernetes worker nodes maintained by the Horizontal Pod Autoscaler (HPA). Ensures baseline capacity is always available: dev=1, staging=2, prod=3 (one per AZ for HA)."
  type        = number
  default     = 2

  validation {
    condition     = var.min_nodes >= 1 && var.min_nodes <= 100
    error_message = "The min_nodes must be between 1 and 100."
  }
}

variable "max_nodes" {
  description = "Maximum number of Kubernetes worker nodes the autoscaler can provision. Caps horizontal scaling cost: dev=3, staging=6, prod=20. Production max supports burst capacity for 1M+ records/minute throughput."
  type        = number
  default     = 10

  validation {
    condition     = var.max_nodes >= 1 && var.max_nodes <= 100
    error_message = "The max_nodes must be between 1 and 100."
  }
}

variable "desired_nodes" {
  description = "Initial desired number of Kubernetes worker nodes. The autoscaler adjusts from this baseline within min_nodes and max_nodes bounds: dev=2, staging=3, prod=5."
  type        = number
  default     = 3

  validation {
    condition     = var.desired_nodes >= 1 && var.desired_nodes <= 100
    error_message = "The desired_nodes must be between 1 and 100."
  }
}

# -----------------------------------------------------------------------------
# 4. Database Variables
# -----------------------------------------------------------------------------

variable "mongodb_version" {
  description = "MongoDB version for the Metadata Repository. Fixed at 7.0 per platform specification. Stores generation_profiles, statistical_profiles, schema_definitions, audit_logs, and tenant_configurations collections with WiredTiger storage engine."
  type        = string
  default     = "7.0"

  validation {
    condition     = can(regex("^7\\.0$", var.mongodb_version))
    error_message = "The mongodb_version must be \"7.0\" as required by the platform specification."
  }
}

variable "mongodb_instance_type" {
  description = "Instance type for MongoDB nodes. Sizing scales with environment: dev=t3.medium (minimal metadata storage), staging=m5.large (performance testing), prod=m5.2xlarge (production metadata workloads with 7-year audit log retention)."
  type        = string
  default     = "t3.medium"

  validation {
    condition     = length(var.mongodb_instance_type) > 0
    error_message = "The mongodb_instance_type must be a non-empty string representing a valid cloud provider instance type."
  }
}

variable "mongodb_storage_size_gb" {
  description = "Allocated storage in gigabytes for MongoDB persistent volumes. Must accommodate metadata repository growth and audit logs with 7-year retention: dev=20GB, staging=50GB, prod=500GB."
  type        = number
  default     = 100

  validation {
    condition     = var.mongodb_storage_size_gb >= 10 && var.mongodb_storage_size_gb <= 10000
    error_message = "The mongodb_storage_size_gb must be between 10 and 10000 GB."
  }
}

variable "mongodb_replica_count" {
  description = "Number of MongoDB replica set members for high availability. Minimum 1 for dev, 3 for production (primary + 2 secondaries). Enables automatic failover and read scaling."
  type        = number
  default     = 3

  validation {
    condition     = contains([1, 3, 5, 7], var.mongodb_replica_count)
    error_message = "The mongodb_replica_count must be an odd number: 1, 3, 5, or 7 for proper replica set election."
  }
}

variable "redis_version" {
  description = "Redis version for the caching layer. Fixed at 7.0 per platform specification. Provides session management, API response caching (50ms cached response target), and real-time generation job progress tracking."
  type        = string
  default     = "7.0"

  validation {
    condition     = can(regex("^7\\.[0-9]+$", var.redis_version))
    error_message = "The redis_version must be 7.x (e.g., \"7.0\", \"7.2\") as required by the platform specification."
  }
}

variable "redis_node_type" {
  description = "Instance type for Redis cache nodes. Sizing reflects caching demands: dev=cache.t3.micro (minimal), staging=cache.m5.large (performance testing), prod=cache.m5.xlarge (production session and progress caching)."
  type        = string
  default     = "cache.t3.micro"

  validation {
    condition     = length(var.redis_node_type) > 0
    error_message = "The redis_node_type must be a non-empty string representing a valid cache node type."
  }
}

variable "redis_replica_count" {
  description = "Number of Redis read replicas for high availability and read scaling. Set to 0 for dev (standalone), 1+ for staging/prod."
  type        = number
  default     = 1

  validation {
    condition     = var.redis_replica_count >= 0 && var.redis_replica_count <= 5
    error_message = "The redis_replica_count must be between 0 and 5."
  }
}

# -----------------------------------------------------------------------------
# 5. Storage Variables
# -----------------------------------------------------------------------------

variable "storage_bucket_prefix" {
  description = "Naming prefix for cloud storage buckets (S3/Azure Blob/GCS) used for synthetic data export. Combined with environment and region for globally unique names: synth-erp-dev, synth-erp-staging, synth-erp-prod."
  type        = string
  default     = "synth-erp"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,36}$", var.storage_bucket_prefix))
    error_message = "The storage_bucket_prefix must start with a lowercase letter, contain only lowercase alphanumeric characters and hyphens, and be between 3 and 37 characters long."
  }
}

variable "enable_versioning" {
  description = "Enable object versioning on cloud storage buckets. Provides data protection through version history for exported synthetic datasets. Recommended true for staging/production, optional false for development cost savings."
  type        = bool
  default     = true
}

variable "encryption_enabled" {
  description = "Enable server-side AES-256 encryption on cloud storage buckets. Required for SOC 2 Type II compliance and aligned with the platform's encryption-at-rest requirement. Must be true for all environments in production deployments."
  type        = bool
  default     = true
}

variable "storage_lifecycle_days" {
  description = "Number of days before transitioning stored objects to infrequent access storage class. Reduces long-term storage costs for older synthetic dataset exports. Set to 0 to disable lifecycle policies."
  type        = number
  default     = 90

  validation {
    condition     = var.storage_lifecycle_days >= 0 && var.storage_lifecycle_days <= 3650
    error_message = "The storage_lifecycle_days must be between 0 (disabled) and 3650 days (10 years)."
  }
}

# -----------------------------------------------------------------------------
# 6. Security Variables
# -----------------------------------------------------------------------------

variable "enable_kms" {
  description = "Enable cloud-managed Key Management Service (KMS) for encryption key management. Provides per-tenant AES-256-GCM encryption keys integrated with HashiCorp Vault. Required for production SOC 2 Type II compliance. May be disabled in development for cost savings."
  type        = bool
  default     = true
}

variable "allowed_cidr_blocks" {
  description = "List of CIDR blocks permitted to access the platform's public endpoints (API Gateway, Web Console). Controls network-level access: dev=[\"0.0.0.0/0\"] (open), staging=[\"10.0.0.0/8\"] (internal), prod=[\"10.0.0.0/8\", \"172.16.0.0/12\"] (corporate/VPN only)."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for cidr in var.allowed_cidr_blocks : can(cidrhost(cidr, 0))])
    error_message = "All entries in allowed_cidr_blocks must be valid IPv4 CIDR blocks (e.g., \"10.0.0.0/8\", \"172.16.0.0/12\")."
  }
}

variable "enable_secrets_manager" {
  description = "Enable cloud-native secrets manager (AWS Secrets Manager / Azure Key Vault / GCP Secret Manager) for storing sensitive configuration: database credentials, JWT signing keys, Auth0 client secrets, and API keys. Required for production; may use environment variables directly in development."
  type        = bool
  default     = true
}

variable "enable_waf" {
  description = "Enable Web Application Firewall (WAF) for the API Gateway ingress. Provides protection against OWASP Top 10 threats, rate limiting enforcement, and geo-blocking capabilities. Recommended for staging and production environments."
  type        = bool
  default     = false
}

variable "ssl_certificate_arn" {
  description = "ARN or identifier of the SSL/TLS certificate for HTTPS endpoints. When empty, a certificate is automatically provisioned via ACM (AWS), App Service Certificates (Azure), or Google-managed certificates (GCP). Enforces TLS 1.3 minimum as required by the security specification."
  type        = string
  default     = ""
}

# -----------------------------------------------------------------------------
# 7. Monitoring and Observability Variables
# -----------------------------------------------------------------------------

variable "enable_monitoring" {
  description = "Enable Prometheus and Grafana monitoring stack deployment. Provides metrics collection, alerting, and dashboards for all six microservices. Integrates with OpenTelemetry distributed tracing."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "Number of days to retain application and audit logs. Must support 7-year retention for SOC 2 Type II audit compliance in production (2555 days). Development environments may use shorter retention."
  type        = number
  default     = 90

  validation {
    condition     = var.log_retention_days >= 1 && var.log_retention_days <= 2555
    error_message = "The log_retention_days must be between 1 and 2555 (7 years for SOC 2 Type II compliance)."
  }
}

# -----------------------------------------------------------------------------
# 8. Tags Variable
# -----------------------------------------------------------------------------

variable "tags" {
  description = "Additional resource tags applied to all provisioned infrastructure. Merged with default tags (Project, Environment, ManagedBy) for cost allocation, compliance tracking, and resource organization. Include CostCenter, DataClassification, and Compliance tags for production."
  type        = map(string)
  default     = {}

  validation {
    condition     = length(var.tags) <= 50
    error_message = "The tags map must contain 50 or fewer key-value pairs to comply with cloud provider tag limits."
  }
}
