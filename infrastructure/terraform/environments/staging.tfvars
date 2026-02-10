# =============================================================================
# Staging Environment — Terraform Variable Values
# Synthetic-ERP-Data-Generation-Platform
# =============================================================================
#
# Production-mirrored staging environment at reduced scale, designed for:
#   - Pre-release validation of infrastructure and application changes
#   - Integration testing with realistic multi-AZ topology
#   - Performance benchmarking with medium-scale Kubernetes cluster
#   - Deployment rehearsal matching production network and security posture
#   - Cost-efficient staging without sacrificing HA validation
#
# Staging mirrors production topology (multi-AZ, per-AZ NAT, KMS encryption,
# secrets management) at approximately 30-50% of production resource capacity.
# This ensures deployment parity while controlling cloud spend.
#
# Usage:
#   terraform plan  -var-file=environments/staging.tfvars
#   terraform apply -var-file=environments/staging.tfvars
#
# =============================================================================

# -----------------------------------------------------------------------------
# 1. Core Infrastructure
# -----------------------------------------------------------------------------

# Staging environment identifier — controls resource sizing and configuration
environment = "staging"

# Same cloud provider as production for deployment parity
cloud_provider = "aws"

# Same region as production for network topology and latency parity
region = "us-east-1"

# Consistent project naming prefix across all provisioned resources
project_name = "synthetic-erp-platform"

# -----------------------------------------------------------------------------
# 2. Networking — Production-Like Multi-AZ Topology
# -----------------------------------------------------------------------------

# Dedicated staging CIDR block — avoids overlap with dev (10.0.0.0/16) and
# prod (10.2.0.0/16) to support VPC peering if needed
vpc_cidr = "10.1.0.0/16"

# Three availability zones matching production topology for full HA validation
availability_zones = ["us-east-1a", "us-east-1b", "us-east-1c"]

# NAT Gateway enabled for outbound connectivity to Auth0, cloud SDKs, and
# container image registries (mirrors production configuration)
enable_nat_gateway = true

# Per-AZ NAT Gateways matching production fault isolation — validates that
# AZ failure does not disrupt outbound connectivity for remaining zones
single_nat_gateway = false

# -----------------------------------------------------------------------------
# 3. Kubernetes — Medium Cluster for Staging Validation
# -----------------------------------------------------------------------------

# Kubernetes 1.29+ as required by platform specification
cluster_version = "1.29"

# Medium instances for staging: t3.large provides 2 vCPU / 8 GiB RAM per node,
# sufficient for integration testing and moderate generation workloads
node_instance_type = "t3.large"

# Minimum 2 nodes for high-availability validation across availability zones
min_nodes = 2

# Maximum 6 nodes — validates HPA autoscaling behavior at reduced ceiling
# (production allows up to 20 nodes)
max_nodes = 6

# Default 3 nodes running — one per AZ for baseline staging load distribution;
# autoscaler adjusts between min_nodes (2) and max_nodes (6)
desired_nodes = 3

# -----------------------------------------------------------------------------
# 4. Database — Medium MongoDB and Redis for Staging
# -----------------------------------------------------------------------------

# MongoDB 7.0 with WiredTiger storage engine for the Metadata Repository
# Stores: generation_profiles, statistical_profiles, schema_definitions,
#         audit_logs, tenant_configurations
mongodb_version = "7.0"

# Medium MongoDB instance for staging workloads — m5.large provides
# 2 vCPU / 8 GiB RAM for realistic query performance testing
mongodb_instance_type = "m5.large"

# 50 GB storage for staging metadata volumes — sufficient for integration
# testing, staging audit logs, and statistical profile storage
mongodb_storage_size_gb = 50

# 3-member replica set matching production topology: primary + 2 secondaries
# for failover validation and read scaling testing
mongodb_replica_count = 3

# Redis 7.x for session management, API response caching (50ms cached
# response target), and real-time generation job progress tracking
redis_version = "7.0"

# Medium Redis node for realistic performance testing — cache.m5.large
# provides sufficient memory for staging session and caching workloads
redis_node_type = "cache.m5.large"

# 1 Redis read replica for HA validation in staging (production uses 2)
redis_replica_count = 1

# -----------------------------------------------------------------------------
# 5. Storage — Versioned and Encrypted Cloud Storage
# -----------------------------------------------------------------------------

# Staging bucket naming prefix for synthetic data exports (S3/Blob/GCS)
storage_bucket_prefix = "synth-erp-staging"

# Object versioning enabled to match production data protection behavior
enable_versioning = true

# AES-256 server-side encryption enabled per security requirements and
# SOC 2 Type II compliance — matches production security posture
encryption_enabled = true

# Transition objects to infrequent access tier after 180 days in staging
# (production uses 365 days); balances cost optimization with data access needs
storage_lifecycle_days = 180

# -----------------------------------------------------------------------------
# 6. Security — Production-Like Security Posture
# -----------------------------------------------------------------------------

# KMS enabled for encryption key management matching production — validates
# per-tenant AES-256-GCM encryption integration with HashiCorp Vault
enable_kms = true

# Restrict public endpoint access to internal corporate network ranges only;
# staging does not require VPN CIDR range (production adds 172.16.0.0/12)
allowed_cidr_blocks = ["10.0.0.0/8"]

# Secrets Manager enabled for staging credentials management — validates
# the same secrets workflow used in production (database passwords, JWT keys,
# Auth0 client secrets, cloud provider credentials)
enable_secrets_manager = true

# WAF enabled for staging to validate API Gateway protection rules before
# production deployment — tests OWASP Top 10 defenses and rate limiting
enable_waf = true

# SSL/TLS certificate — empty string triggers automatic provisioning via
# AWS ACM with TLS 1.3 minimum enforcement as per security specification
ssl_certificate_arn = ""

# -----------------------------------------------------------------------------
# 7. Monitoring and Observability
# -----------------------------------------------------------------------------

# Prometheus + Grafana monitoring stack enabled for staging observability
# across all six microservices with OpenTelemetry distributed tracing
enable_monitoring = true

# 365-day log retention for staging — sufficient for pre-release audit trail
# validation without the full 7-year (2555 days) production retention cost
log_retention_days = 365

# -----------------------------------------------------------------------------
# 8. Tags — Staging Resource Tagging
# -----------------------------------------------------------------------------

# Resource tags for cost allocation, environment identification, and
# infrastructure governance in the staging environment
tags = {
  Environment = "staging"
  ManagedBy   = "terraform"
  Project     = "synthetic-erp-platform"
  CostCenter  = "staging"
}
