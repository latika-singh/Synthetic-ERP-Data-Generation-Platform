# =============================================================================
# Synthetic-ERP-Data-Generation-Platform — Database Module Outputs
# =============================================================================
#
# Exposes MongoDB 7.0 and Redis 7.x connection details to consuming modules
# and the root Terraform configuration.  These outputs are the sole interface
# through which the six backend microservices (API Gateway, Generation Engine,
# Profiling Service, Quality Service, Compliance Service, Provisioning Service)
# obtain the connection information needed to access the metadata repository
# and caching layer.
#
# Consumers:
#   • Root infrastructure/terraform/outputs.tf — re-exports database_uri and
#     redis_url for CI/CD pipelines and remote state consumers.
#   • Kubernetes ConfigMaps / Secrets — injected into Pod environment variables
#     for backend service connectivity.
#   • Monitoring dashboards — reference mongodb_instance_id and redis_endpoint
#     for resource-level metrics and health checks.
#
# Sensitive outputs (mongodb_uri, redis_url) are marked sensitive = true to
# prevent credential leakage in terraform plan/apply console output, CI/CD
# logs, and state file diffs.  The Terraform state backend must be encrypted
# (AES-256) per security requirement R-006.
#
# Cloud provider selection (var.cloud_provider) determines which resource
# attributes are referenced.  All values use conditional/ternary logic with
# try() guards for safe access when provider-specific resources have count = 0.
#
# Module:  database
# Project: Synthetic-ERP-Data-Generation-Platform
# =============================================================================

# -----------------------------------------------------------------------------
# Local Values — Connection String Construction
# -----------------------------------------------------------------------------
# Complex connection strings are assembled in locals to keep output blocks
# concise and to centralise the multi-provider conditional logic in one place.
# Each local handles the three-way cloud provider selection plus the GCP
# sub-condition (MongoDB Atlas vs self-managed on Compute Engine).
# -----------------------------------------------------------------------------
locals {
  # ---------------------------------------------------------------------------
  # MongoDB Connection URI
  # ---------------------------------------------------------------------------
  # Constructs a PyMongo 4.x-compatible connection string with authentication
  # credentials, TLS parameters, and replica set configuration per provider.
  #
  # AWS DocumentDB:  mongodb:// with TLS, retryWrites=false (DocumentDB
  #                  does not support retryWrites), replica set rs0.
  # Azure Cosmos DB: Provider-generated connection string from the
  #                  azurerm_cosmosdb_account resource (includes port 10255,
  #                  ssl=true, and account-level authentication).
  # GCP Atlas:       SRV connection string from mongodbatlas_cluster with
  #                  automatic replica discovery via DNS SRV records.
  # GCP Self-Managed: mongodb:// with TLS, targeting the primary instance
  #                  network IP and the configured replica set name.
  # ---------------------------------------------------------------------------
  mongodb_uri_aws = try(
    join("", [
      "mongodb://",
      var.mongodb_admin_username,
      ":",
      var.mongodb_admin_password,
      "@",
      aws_docdb_cluster.main[0].endpoint,
      ":27017/synthetic_erp_metadata",
      "?tls=true",
      "&tlsAllowInvalidCertificates=false",
      "&replicaSet=rs0",
      "&readPreference=secondaryPreferred",
      "&retryWrites=false",
    ]),
    ""
  )

  mongodb_uri_azure = try(
    azurerm_cosmosdb_account.main[0].connection_strings[0],
    ""
  )

  mongodb_uri_gcp_atlas = try(
    join("", [
      mongodbatlas_cluster.main[0].connection_strings[0].standard_srv,
      length(split("?", mongodbatlas_cluster.main[0].connection_strings[0].standard_srv)) > 1 ? "&" : "?",
      "retryWrites=true&w=majority",
    ]),
    ""
  )

  mongodb_uri_gcp_self_managed = try(
    join("", [
      "mongodb://",
      var.mongodb_admin_username,
      ":",
      var.mongodb_admin_password,
      "@",
      google_compute_instance.mongodb[0].network_interface[0].network_ip,
      ":27017/synthetic_erp_metadata",
      "?tls=true",
      "&replicaSet=${local.name_prefix}-rs",
      "&readPreference=secondaryPreferred",
      "&retryWrites=true",
      "&w=majority",
    ]),
    ""
  )

  mongodb_uri_gcp = (
    var.atlas_project_id != ""
    ? local.mongodb_uri_gcp_atlas
    : local.mongodb_uri_gcp_self_managed
  )

  mongodb_uri = (
    var.cloud_provider == "aws"   ? local.mongodb_uri_aws :
    var.cloud_provider == "azure" ? local.mongodb_uri_azure :
    local.mongodb_uri_gcp
  )

  # ---------------------------------------------------------------------------
  # Redis Connection URL
  # ---------------------------------------------------------------------------
  # Constructs a redis-py 5.x-compatible connection URL per provider.
  #
  # AWS ElastiCache:       redis:// with primary endpoint address on port 6379.
  #                        TLS is enforced at the cluster level via
  #                        transit_encryption_enabled = true; the application
  #                        layer (shared/database/redis_client.py) must set
  #                        ssl=True when connecting.
  # Azure Cache for Redis: rediss:// scheme (TLS) with the primary access key
  #                        embedded in the URL, connecting on the SSL port
  #                        (default 6380).  Non-SSL port is disabled.
  # GCP Memorystore:       redis:// with the private host IP and port.  TLS
  #                        is enforced via transit_encryption_mode =
  #                        "SERVER_AUTHENTICATION"; the client must configure
  #                        SSL accordingly.
  # ---------------------------------------------------------------------------
  redis_url_aws = try(
    "redis://${aws_elasticache_replication_group.main[0].primary_endpoint_address}:6379",
    ""
  )

  redis_url_azure = try(
    "rediss://:${azurerm_redis_cache.main[0].primary_access_key}@${azurerm_redis_cache.main[0].hostname}:${azurerm_redis_cache.main[0].ssl_port}",
    ""
  )

  redis_url_gcp = try(
    "redis://${google_redis_instance.main[0].host}:${google_redis_instance.main[0].port}",
    ""
  )

  redis_url = (
    var.cloud_provider == "aws"   ? local.redis_url_aws :
    var.cloud_provider == "azure" ? local.redis_url_azure :
    local.redis_url_gcp
  )

  # ---------------------------------------------------------------------------
  # MongoDB Instance / Cluster Identifier
  # ---------------------------------------------------------------------------
  # Non-sensitive resource identifier for monitoring dashboards, alerting
  # rules, and infrastructure cross-referencing.  Returns the provider-native
  # identifier format:
  #   AWS:  DocumentDB cluster identifier (e.g., synth-erp-prod-mongodb)
  #   Azure: Cosmos DB account resource ID (Azure ARM path)
  #   GCP Atlas: Atlas cluster ID
  #   GCP Self-Managed: Compute Engine instance ID
  # ---------------------------------------------------------------------------
  mongodb_instance_id_aws = try(
    aws_docdb_cluster.main[0].cluster_identifier,
    ""
  )

  mongodb_instance_id_azure = try(
    azurerm_cosmosdb_account.main[0].id,
    ""
  )

  mongodb_instance_id_gcp = (
    var.atlas_project_id != ""
    ? try(mongodbatlas_cluster.main[0].cluster_id, "")
    : try(google_compute_instance.mongodb[0].instance_id, "")
  )

  mongodb_instance_id = (
    var.cloud_provider == "aws"   ? local.mongodb_instance_id_aws :
    var.cloud_provider == "azure" ? local.mongodb_instance_id_azure :
    local.mongodb_instance_id_gcp
  )

  # ---------------------------------------------------------------------------
  # Redis Endpoint Hostname
  # ---------------------------------------------------------------------------
  # Non-sensitive hostname for health check probes and DNS-based service
  # discovery.  Returns only the host/IP without port or protocol:
  #   AWS:  ElastiCache primary endpoint address
  #   Azure: Azure Redis Cache hostname
  #   GCP:  Memorystore instance host IP
  # ---------------------------------------------------------------------------
  redis_endpoint_aws = try(
    aws_elasticache_replication_group.main[0].primary_endpoint_address,
    ""
  )

  redis_endpoint_azure = try(
    azurerm_redis_cache.main[0].hostname,
    ""
  )

  redis_endpoint_gcp = try(
    google_redis_instance.main[0].host,
    ""
  )

  redis_endpoint = (
    var.cloud_provider == "aws"   ? local.redis_endpoint_aws :
    var.cloud_provider == "azure" ? local.redis_endpoint_azure :
    local.redis_endpoint_gcp
  )
}

# =============================================================================
# OUTPUT DEFINITIONS
# =============================================================================

# -----------------------------------------------------------------------------
# 1. MongoDB Connection URI (Sensitive)
# -----------------------------------------------------------------------------
# Full MongoDB connection string compatible with PyMongo 4.x.  Contains
# embedded authentication credentials and TLS parameters.  Consumed by all
# six backend microservices through the shared MongoDB client
# (src/backend/shared/database/mongodb.py) for accessing the five core
# metadata collections:
#
#   • generation_profiles   — generation job profiles and configurations
#   • statistical_profiles  — schema profiling results from Profiling Service
#   • schema_definitions    — discovered ERP table/column metadata
#   • audit_logs            — tamper-evident audit trail (SOC 2 Type II)
#   • tenant_configurations — per-tenant resource quotas and feature flags
#
# Cloud-specific formats:
#   AWS:   mongodb://user:pass@cluster.docdb.amazonaws.com:27017/...?tls=true
#   Azure: mongodb://account:key@account.mongo.cosmos.azure.com:10255/...
#   GCP:   mongodb+srv://user:pass@cluster.mongodb.net/... (Atlas)
#          mongodb://user:pass@10.x.x.x:27017/... (self-managed)
#
# IMPORTANT: This output is marked sensitive to prevent credentials from
# appearing in terraform plan/apply output.  The value IS stored in the
# Terraform state file — ensure the backend is encrypted (AES-256).
# -----------------------------------------------------------------------------
output "mongodb_uri" {
  description = "MongoDB connection URI for PyMongo 4.x clients. Contains authentication credentials and TLS parameters. Used by all six backend microservices for metadata repository access."
  value       = local.mongodb_uri
  sensitive   = true
}

# -----------------------------------------------------------------------------
# 2. Redis Connection URL (Sensitive)
# -----------------------------------------------------------------------------
# Full Redis connection URL compatible with redis-py 5.x.  May contain
# access keys (Azure) or operate over TLS-enforced channels.  Consumed by
# all six backend microservices through the shared Redis client
# (src/backend/shared/database/redis_client.py) for:
#
#   • API response caching — achieving ≤50ms target response times
#   • Session management   — JWT refresh token and user session storage
#   • Job progress tracking — real-time generation progress via pub/sub
#   • Rate limiter state   — per-tier request counters (60/300/1000 rpm)
#   • Distributed locks    — batch processing coordination
#
# Cloud-specific formats:
#   AWS:   redis://primary-endpoint.cache.amazonaws.com:6379
#   Azure: rediss://:accesskey@hostname:6380 (TLS via rediss:// scheme)
#   GCP:   redis://10.x.x.x:6379 (Memorystore private IP)
#
# Note on TLS:
#   All three providers have TLS enabled at the infrastructure level.
#   For AWS and GCP, the redis:// scheme is used and the application layer
#   must set ssl=True.  For Azure, the rediss:// scheme signals TLS natively.
#
# IMPORTANT: This output is marked sensitive because the Azure connection URL
# embeds the primary access key.  AWS and GCP URLs do not embed secrets but
# are still marked sensitive for uniform handling.
# -----------------------------------------------------------------------------
output "redis_url" {
  description = "Redis connection URL for session management and API response caching. Used by all backend services via the shared Redis client."
  value       = local.redis_url
  sensitive   = true
}

# -----------------------------------------------------------------------------
# 3. MongoDB Instance / Cluster Identifier (Non-Sensitive)
# -----------------------------------------------------------------------------
# Provider-native identifier for the MongoDB cluster or instance.  This is a
# non-sensitive resource ID used for:
#
#   • Monitoring dashboards (Prometheus, Grafana, CloudWatch, Azure Monitor)
#   • Alerting rules (cluster health, replication lag, storage utilisation)
#   • Infrastructure cross-referencing (linking Terraform resources to K8s)
#   • Cost allocation reports (tagging and resource grouping)
#
# Cloud-specific formats:
#   AWS:   DocumentDB cluster identifier (e.g., "synth-erp-prod-mongodb")
#   Azure: Cosmos DB account ARM resource ID
#          (/subscriptions/.../cosmosdbAccounts/synth-erp-prod-mongodb)
#   GCP:   Atlas cluster ID (hexadecimal) or Compute Engine instance ID
# -----------------------------------------------------------------------------
output "mongodb_instance_id" {
  description = "MongoDB cluster/instance identifier for monitoring dashboards, alerting rules, and infrastructure cross-referencing."
  value       = local.mongodb_instance_id
  sensitive   = false
}

# -----------------------------------------------------------------------------
# 4. Redis Endpoint Hostname (Non-Sensitive)
# -----------------------------------------------------------------------------
# The hostname (or private IP) of the Redis cache endpoint without protocol
# or port.  This non-sensitive value is used for:
#
#   • Health check probes (Kubernetes liveness/readiness against Redis)
#   • DNS-based service discovery (resolving the cache endpoint)
#   • Network connectivity validation (ping/telnet from within the VPC)
#   • Monitoring agent configuration (Prometheus Redis exporter target)
#
# Cloud-specific formats:
#   AWS:   ElastiCache primary endpoint FQDN
#          (e.g., synth-erp-prod-redis.xxxxx.use1.cache.amazonaws.com)
#   Azure: Azure Redis Cache hostname
#          (e.g., synth-erp-prod-redis.redis.cache.windows.net)
#   GCP:   Memorystore private IP address
#          (e.g., 10.0.100.2)
# -----------------------------------------------------------------------------
output "redis_endpoint" {
  description = "Redis endpoint hostname for health check probes and DNS-based service discovery."
  value       = local.redis_endpoint
  sensitive   = false
}
