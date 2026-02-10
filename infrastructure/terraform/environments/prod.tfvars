# =============================================================================
# Production Environment — Terraform Variable Values
# Synthetic-ERP-Data-Generation-Platform
# =============================================================================
#
# Full high-availability production configuration designed to support:
#   - 1M+ records/minute throughput via HPA-scaled Kubernetes cluster
#   - SOC 2 Type II compliance with KMS encryption and secrets management
#   - Multi-AZ fault tolerance across three availability zones
#   - 7-year audit log retention for regulatory compliance
#   - AES-256 encryption at rest, TLS 1.3 in transit
#   - Restricted network access limited to corporate and VPN CIDR ranges
#   - Air-gapped deployment capability via private registry mirroring
#
# Usage:
#   terraform plan  -var-file=environments/prod.tfvars
#   terraform apply -var-file=environments/prod.tfvars
#
# =============================================================================

# -----------------------------------------------------------------------------
# 1. Core Infrastructure
# -----------------------------------------------------------------------------

# Production environment identifier — controls resource sizing, HA, and security
environment = "prod"

# Primary cloud provider for production workloads
cloud_provider = "aws"

# Production region — US East for low-latency enterprise access
region = "us-east-1"

# Consistent project naming prefix across all provisioned resources
project_name = "synthetic-erp-platform"

# -----------------------------------------------------------------------------
# 2. Networking — Full High Availability
# -----------------------------------------------------------------------------

# Dedicated production CIDR block (dev=10.0.0.0/16, staging=10.1.0.0/16)
vpc_cidr = "10.2.0.0/16"

# Three availability zones for full high availability and fault isolation
availability_zones = ["us-east-1a", "us-east-1b", "us-east-1c"]

# NAT Gateway enabled for outbound connectivity to Auth0, cloud SDKs, and
# container image registries (disable only for fully air-gapped deployments)
enable_nat_gateway = true

# Per-AZ NAT Gateways for production fault isolation — ensures that an AZ
# failure does not disrupt outbound connectivity for remaining zones
single_nat_gateway = false

# -----------------------------------------------------------------------------
# 3. Kubernetes — Large Cluster for 1M+ Records/Minute Throughput
# -----------------------------------------------------------------------------

# Kubernetes 1.29+ as required by platform specification
cluster_version = "1.29"

# Production-grade compute: m5.xlarge provides 4 vCPU / 16 GiB RAM per node,
# sufficient for Generation Engine batch processing and concurrent service loads
node_instance_type = "m5.xlarge"

# Minimum 3 nodes — one per AZ for cross-zone high availability baseline
min_nodes = 3

# Maximum 20 nodes — HPA scaling ceiling to support burst generation workloads
# targeting 1M+ records/minute throughput across Generation Engine replicas
max_nodes = 20

# Default 5 nodes running for baseline production load — autoscaler adjusts
# between min_nodes (3) and max_nodes (20) based on CPU and queue depth
desired_nodes = 5

# -----------------------------------------------------------------------------
# 4. Database — Production-Grade MongoDB and Redis
# -----------------------------------------------------------------------------

# MongoDB 7.0 with WiredTiger storage engine for the Metadata Repository
# Stores: generation_profiles, statistical_profiles, schema_definitions,
#         audit_logs (7-year retention), tenant_configurations
mongodb_version = "7.0"

# Large MongoDB instance for production metadata workloads — m5.2xlarge
# provides 8 vCPU / 32 GiB RAM for concurrent profiling and generation queries
mongodb_instance_type = "m5.2xlarge"

# 500 GB storage for production data volumes accommodating:
#   - Metadata repository growth from schema discovery across ERP systems
#   - Audit logs with mandatory 7-year retention (SOC 2 Type II)
#   - Statistical profiles and generation job history
mongodb_storage_size_gb = 500

# 3-member replica set: primary + 2 secondaries for automatic failover,
# read scaling, and data redundancy across availability zones
mongodb_replica_count = 3

# Redis 7.x for session management, API response caching (50ms cached
# response target), and real-time generation job progress tracking
redis_version = "7.0"

# Production Redis node — cache.m5.xlarge provides sufficient memory for
# session state, progress tracking, and API response caching across services
redis_node_type = "cache.m5.xlarge"

# 2 Redis read replicas for HA failover and read distribution in production
redis_replica_count = 2

# -----------------------------------------------------------------------------
# 5. Storage — Versioned and Encrypted Cloud Storage
# -----------------------------------------------------------------------------

# Production bucket naming prefix for synthetic data exports (S3/Blob/GCS)
storage_bucket_prefix = "synth-erp-prod"

# Object versioning enabled for data protection — provides rollback
# capability for exported datasets and compliance artifact preservation
enable_versioning = true

# AES-256 server-side encryption mandatory for production per security
# requirements and SOC 2 Type II compliance
encryption_enabled = true

# Transition objects to infrequent access tier after 365 days to optimize
# long-term storage costs while maintaining accessibility for audit retrieval
storage_lifecycle_days = 365

# -----------------------------------------------------------------------------
# 6. Security — Full SOC 2 Type II Compliance
# -----------------------------------------------------------------------------

# KMS enabled for per-tenant AES-256-GCM encryption key management,
# integrated with HashiCorp Vault for key rotation and access policies
enable_kms = true

# Strictly restrict public endpoint access to internal corporate networks
# and VPN ranges only — no public internet access to production services
#   10.0.0.0/8      — Internal corporate network (RFC 1918 Class A)
#   172.16.0.0/12   — VPN and management network (RFC 1918 Class B)
allowed_cidr_blocks = ["10.0.0.0/8", "172.16.0.0/12"]

# Secrets Manager enabled for production credentials management:
#   - Database connection strings and passwords
#   - JWT RS256 signing keys for Auth0 integration
#   - Auth0 client secrets and API keys
#   - Cloud provider service account credentials
#   - Encryption key material references
enable_secrets_manager = true

# WAF enabled for production API Gateway protection — defends against
# OWASP Top 10 threats, enforces additional rate limiting, and provides
# geo-blocking capabilities for compliance requirements
enable_waf = true

# SSL/TLS certificate — empty string triggers automatic provisioning via
# AWS ACM with TLS 1.3 minimum enforcement as per security specification
ssl_certificate_arn = ""

# -----------------------------------------------------------------------------
# 7. Monitoring and Observability
# -----------------------------------------------------------------------------

# Prometheus + Grafana monitoring stack enabled for production observability
# across all six microservices with OpenTelemetry distributed tracing
enable_monitoring = true

# 7-year log retention (2555 days) for SOC 2 Type II audit compliance —
# covers audit_logs, application logs, and security event logs
log_retention_days = 2555

# -----------------------------------------------------------------------------
# 8. Tags — Production Resource Tagging
# -----------------------------------------------------------------------------

# Comprehensive resource tags for cost allocation, compliance tracking,
# and infrastructure governance in production
tags = {
  Environment        = "production"
  ManagedBy          = "terraform"
  Project            = "synthetic-erp-platform"
  CostCenter         = "production"
  Compliance         = "soc2-type-ii"
  DataClassification = "confidential"
}
