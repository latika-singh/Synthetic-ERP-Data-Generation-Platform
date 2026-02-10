# =============================================================================
# Development Environment — Terraform Variable Values
# Synthetic-ERP-Data-Generation-Platform
# =============================================================================
#
# Purpose: Supplies minimal resource configurations optimized for cost
# efficiency during development. Uses the same architecture and modules as
# staging/production but with reduced instance sizes, replica counts, and
# optional feature toggles to keep cloud spend low.
#
# Usage:
#   terraform plan  -var-file=environments/dev.tfvars
#   terraform apply -var-file=environments/dev.tfvars
#
# Cost-Saving Measures in This Environment:
#   - Single NAT gateway shared across AZs (vs. per-AZ in prod)
#   - Two availability zones instead of three
#   - Smallest viable instance types (t3.medium nodes, cache.t3.micro Redis)
#   - Single MongoDB replica (no HA replica set)
#   - No Redis replicas (standalone mode)
#   - KMS and Secrets Manager disabled (environment variables used directly)
#   - WAF disabled
#   - Shortened log and object lifecycle retention
#   - S3 versioning disabled
# =============================================================================

# -----------------------------------------------------------------------------
# 1. Core Infrastructure
# -----------------------------------------------------------------------------

# Default to AWS for the development environment.
cloud_provider = "aws"

# US East (N. Virginia) — lowest-cost region with broadest service availability.
region = "us-east-1"

# Development environment identifier. Used in resource naming, tagging, and
# conditional logic within Terraform modules (e.g., replica counts, HA toggles).
environment = "dev"

# Project naming prefix applied to all provisioned resources for easy
# identification and cost-allocation grouping.
project_name = "synthetic-erp-platform"

# -----------------------------------------------------------------------------
# 2. Networking
# -----------------------------------------------------------------------------

# Standard /16 VPC providing 65,536 private IPs — more than sufficient for dev.
# Uses 10.0.x.x range; staging uses 10.1.x.x, prod uses 10.2.x.x to avoid
# conflicts when VPC peering is configured between environments.
vpc_cidr = "10.0.0.0/16"

# Two availability zones only. This is the minimum required for managed
# Kubernetes (EKS requires ≥2 AZs) while keeping costs low by avoiding a
# third NAT gateway and set of subnets.
availability_zones = ["us-east-1a", "us-east-1b"]

# NAT Gateway is required for private-subnet services to reach the internet
# (pulling container images, Auth0 token validation, cloud SDK calls).
enable_nat_gateway = true

# Share a single NAT Gateway across both AZs. Production uses per-AZ NAT
# gateways for fault isolation, but a single gateway is acceptable in dev
# and saves approximately $32/month per eliminated gateway.
single_nat_gateway = true

# -----------------------------------------------------------------------------
# 3. Kubernetes Cluster
# -----------------------------------------------------------------------------

# Kubernetes 1.29 as mandated by the platform specification. Supports all
# required API resources: Deployments, StatefulSets, HPA v2, NetworkPolicies,
# and Ingress with IngressClass.
cluster_version = "1.29"

# t3.medium (2 vCPU / 4 GiB RAM) — smallest practical instance for running
# the six microservice pods plus system workloads (CoreDNS, kube-proxy,
# ingress controller). Burstable instances keep baseline costs minimal.
node_instance_type = "t3.medium"

# Minimum 1 node to allow the cluster to scale down during idle periods
# (nights, weekends) while maintaining at least one schedulable node.
min_nodes = 1

# Cap at 3 nodes to prevent runaway autoscaling costs. Sufficient for
# running all six services plus MongoDB and Redis pods in development.
max_nodes = 3

# Start with 2 nodes as the steady-state. Provides enough capacity for
# all service deployments without triggering immediate scale-up events.
desired_nodes = 2

# -----------------------------------------------------------------------------
# 4. Database
# -----------------------------------------------------------------------------

# MongoDB 7.0 as specified in the platform requirements. Stores the five
# core collections: generation_profiles, statistical_profiles,
# schema_definitions, audit_logs, and tenant_configurations.
mongodb_version = "7.0"

# t3.medium (2 vCPU / 4 GiB RAM) — adequate for development metadata
# workloads. Profiling and generation jobs produce modest write volumes
# during development testing.
mongodb_instance_type = "t3.medium"

# 20 GB persistent volume — sufficient for development metadata, test
# schemas, and short-term audit logs. Production allocates 500 GB for
# 7-year audit retention; dev does not require long-term storage.
mongodb_storage_size_gb = 20

# Single MongoDB instance (no replica set) for development. Eliminates
# the cost of two secondary replicas while accepting the trade-off of
# no automatic failover. Production uses 3 replicas for HA.
mongodb_replica_count = 1

# Redis 7.0 as specified. Provides session caching, API response caching
# (50ms target), and real-time job progress tracking via pub/sub.
redis_version = "7.0"

# cache.t3.micro (2 vCPU / 0.5 GiB) — smallest ElastiCache node type.
# Sufficient for development caching loads with minimal session counts
# and infrequent generation job progress updates.
redis_node_type = "cache.t3.micro"

# No Redis read replicas in development. Standalone mode eliminates
# replication costs. Production uses 1-2 replicas for read scaling and HA.
redis_replica_count = 0

# -----------------------------------------------------------------------------
# 5. Storage
# -----------------------------------------------------------------------------

# Dev-specific bucket prefix. Combined with region and account ID by the
# storage module to produce globally unique bucket names like:
# synth-erp-dev-us-east-1-<account_id>
storage_bucket_prefix = "synth-erp-dev"

# Disable S3 versioning in development. Versioning adds storage cost for
# every overwritten object; in dev, synthetic dataset exports are ephemeral
# and do not require version history. Enabled in staging and production.
enable_versioning = false

# Keep server-side AES-256 encryption enabled even in development. This
# aligns with the platform's security-by-default principle and ensures
# that encryption-dependent code paths are exercised during development.
encryption_enabled = true

# Transition objects to Infrequent Access after 30 days in dev (vs. 90 in
# staging, 365 in prod). Reduces storage costs for forgotten test exports.
storage_lifecycle_days = 30

# -----------------------------------------------------------------------------
# 6. Security
# -----------------------------------------------------------------------------

# Disable KMS-managed encryption keys in development to avoid the $1/month
# per key cost. Resources use default AWS-managed encryption (SSE-S3) which
# is free. Production enables customer-managed KMS keys for per-tenant
# AES-256-GCM encryption with key rotation.
enable_kms = false

# Open access in development — allows engineers to connect from any IP.
# Staging restricts to internal networks (10.0.0.0/8), production locks
# to corporate VPN/office CIDRs only.
allowed_cidr_blocks = ["0.0.0.0/0"]

# Disable cloud-native secrets manager in development. Services read
# secrets directly from environment variables (via .env files or
# docker-compose). Production uses AWS Secrets Manager with automatic
# rotation for database credentials, JWT keys, and Auth0 client secrets.
enable_secrets_manager = false

# Disable Web Application Firewall in development. WAF adds cost and
# latency that is unnecessary when the only traffic is from developers.
# Enabled in staging for testing and production for OWASP Top 10 protection.
enable_waf = false

# No pre-provisioned SSL certificate in development. The Kubernetes ingress
# controller uses self-signed certificates or cert-manager with Let's Encrypt
# staging issuer. Production uses ACM-managed certificates.
ssl_certificate_arn = ""

# -----------------------------------------------------------------------------
# 7. Monitoring and Observability
# -----------------------------------------------------------------------------

# Enable Prometheus and Grafana monitoring even in development. Allows
# engineers to observe service metrics, test alerting rules, and validate
# OpenTelemetry tracing integration before staging/production deployment.
enable_monitoring = true

# Retain logs for 30 days in development. Sufficient for debugging recent
# issues without accumulating long-term storage costs. Production retains
# logs for 2555 days (7 years) to satisfy SOC 2 Type II audit requirements.
log_retention_days = 30

# -----------------------------------------------------------------------------
# 8. Tags
# -----------------------------------------------------------------------------

# Resource tags for cost allocation, compliance tracking, and operational
# identification. All provisioned resources receive these tags plus any
# module-level defaults (Project, Environment, ManagedBy).
tags = {
  Environment        = "development"
  ManagedBy          = "terraform"
  Project            = "synthetic-erp-platform"
  CostCenter         = "development"
  DataClassification = "non-production"
  AutoShutdown       = "true"
}
