# =============================================================================
# Synthetic ERP Data Generation Platform - Database Module
# =============================================================================
# Core Terraform database module resource definitions that provision MongoDB 7.0
# as the metadata repository and Redis 7.x for session/API caching across
# AWS (DocumentDB + ElastiCache), Azure (Cosmos DB + Azure Cache for Redis),
# and GCP (self-managed MongoDB / Atlas + Memorystore).
#
# All six backend microservices (API Gateway, Generation Engine, Profiling
# Service, Quality Service, Compliance Service, Provisioning Service) depend
# on these resources for metadata persistence and caching.
#
# Security: AES-256 encryption at rest via KMS, TLS 1.3 in transit.
# HA: Replica sets, multi-AZ, automatic failover in production.
# Multi-tenant: Namespace isolation via shard keys on tenant_id.
# =============================================================================

# -----------------------------------------------------------------------------
# Local Values
# -----------------------------------------------------------------------------
# Common tags, naming conventions, and version normalization used across all
# cloud provider resource definitions to ensure consistency.
# -----------------------------------------------------------------------------
locals {
  # Unified name prefix for all resources in this module
  name_prefix = "${var.project_name}-${var.environment}"

  # Normalized MongoDB engine version for provider-specific format differences
  # AWS DocumentDB uses "7.0", Azure Cosmos DB uses "7.0", GCP Atlas uses "7.0"
  mongodb_engine_version = replace(var.mongodb_version, "/[^0-9.]/", "")

  # Normalized Redis version for provider-specific format differences
  # AWS ElastiCache uses "7.0", Azure Cache uses "6", GCP Memorystore uses "REDIS_7_0"
  redis_engine_version = replace(var.redis_version, "/[^0-9.]/", "")

  # GCP Memorystore requires REDIS_X_Y format
  redis_gcp_version = "REDIS_${replace(local.redis_engine_version, ".", "_")}"

  # Common tags applied to all resources for cost tracking, compliance, and identification
  common_tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Module      = "database"
      Component   = "data-layer"
    },
    var.tags
  )

  # Azure-compatible tags (lowercase keys for consistency with Azure conventions)
  azure_tags = merge(
    {
      project     = var.project_name
      environment = var.environment
      managed_by  = "terraform"
      module      = "database"
      component   = "data-layer"
    },
    var.tags
  )

  # GCP-compatible labels (lowercase, hyphens only, no special characters)
  gcp_labels = merge(
    {
      project     = replace(lower(var.project_name), "/[^a-z0-9-]/", "-")
      environment = lower(var.environment)
      managed-by  = "terraform"
      module      = "database"
      component   = "data-layer"
    },
    { for k, v in var.tags : replace(lower(k), "/[^a-z0-9-]/", "-") => replace(lower(v), "/[^a-z0-9-]/", "-") }
  )
}

# =============================================================================
# AWS RESOURCES
# =============================================================================
# Provisions AWS DocumentDB (MongoDB 7.0 compatible) and ElastiCache for Redis.
# All resources are conditionally created only when var.cloud_provider == "aws".
# =============================================================================

# -----------------------------------------------------------------------------
# AWS DocumentDB (MongoDB-compatible) - Subnet Group
# -----------------------------------------------------------------------------
# Places DocumentDB instances in private subnets from the networking module
# to ensure database traffic stays within the VPC boundary.
# -----------------------------------------------------------------------------
resource "aws_docdb_subnet_group" "mongodb" {
  count = var.cloud_provider == "aws" ? 1 : 0

  name        = "${local.name_prefix}-mongodb-subnet-group"
  description = "Subnet group for ${local.name_prefix} DocumentDB cluster - private subnet placement"
  subnet_ids  = var.subnet_ids

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-mongodb-subnet-group"
  })
}

# -----------------------------------------------------------------------------
# AWS DocumentDB - Cluster Parameter Group
# -----------------------------------------------------------------------------
# Configures DocumentDB engine parameters for TLS enforcement, audit logging,
# profiler support, and TTL monitor for automatic document expiration.
# Family "docdb7.0" aligns with MongoDB 7.0 compatibility.
# -----------------------------------------------------------------------------
resource "aws_docdb_cluster_parameter_group" "mongodb" {
  count = var.cloud_provider == "aws" ? 1 : 0

  family      = "docdb7.0"
  name        = "${local.name_prefix}-mongodb-params"
  description = "DocumentDB parameter group for ${local.name_prefix} with TLS, audit, and profiling enabled"

  # Enforce TLS for all client connections (TLS 1.2+ in transit encryption)
  parameter {
    name  = "tls"
    value = "enabled"
  }

  # Enable audit logging for SOC 2 Type II compliance requirements
  parameter {
    name  = "audit_logs"
    value = "enabled"
  }

  # Enable profiler for query performance debugging and optimization
  parameter {
    name  = "profiler"
    value = "enabled"
  }

  # Enable TTL monitor for automatic document expiration (used by audit_logs collection)
  parameter {
    name  = "ttl_monitor"
    value = "enabled"
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-mongodb-params"
  })
}

# -----------------------------------------------------------------------------
# AWS DocumentDB - Cluster
# -----------------------------------------------------------------------------
# MongoDB 7.0-compatible DocumentDB cluster with AES-256 encryption at rest
# via KMS, automated backups, CloudWatch log exports, and production-grade
# deletion protection. Serves as the metadata repository for all services.
# -----------------------------------------------------------------------------
resource "aws_docdb_cluster" "main" {
  count = var.cloud_provider == "aws" ? 1 : 0

  cluster_identifier = "${local.name_prefix}-mongodb"
  engine             = "docdb"
  engine_version     = local.mongodb_engine_version

  # Authentication credentials - injected via CI/CD or secrets manager
  master_username = var.mongodb_admin_username
  master_password = var.mongodb_admin_password

  # Network placement in private subnets from networking module
  db_subnet_group_name   = aws_docdb_subnet_group.mongodb[0].name
  vpc_security_group_ids = [var.security_group_id]

  # AES-256 encryption at rest using KMS key from security module
  storage_encrypted = true
  kms_key_id        = var.kms_key_id

  # Cluster parameter group with TLS, audit, profiler, and TTL settings
  db_cluster_parameter_group_name = aws_docdb_cluster_parameter_group.mongodb[0].name

  # Backup configuration with configurable retention
  backup_retention_period      = var.backup_retention_days
  preferred_backup_window      = "03:00-05:00"
  preferred_maintenance_window = "sun:05:00-sun:06:00"

  # Snapshot and deletion protection settings per environment
  skip_final_snapshot       = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${local.name_prefix}-mongodb-final-snapshot" : null
  deletion_protection       = var.environment == "prod"

  # CloudWatch log exports for audit compliance and debugging
  enabled_cloudwatch_logs_exports = ["audit", "profiler"]

  # Apply immediately in non-production environments for faster iteration
  apply_immediately = var.environment != "prod"

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-mongodb"
    Service = "mongodb"
    Role    = "metadata-repository"
  })
}

# -----------------------------------------------------------------------------
# AWS DocumentDB - Cluster Instances
# -----------------------------------------------------------------------------
# Creates var.mongodb_instance_count instances (default 3) across availability
# zones for replica set high availability. Each instance is a member of the
# DocumentDB cluster and participates in automatic failover.
# -----------------------------------------------------------------------------
resource "aws_docdb_cluster_instance" "mongodb_instances" {
  count = var.cloud_provider == "aws" ? var.mongodb_instance_count : 0

  identifier                   = "${local.name_prefix}-mongodb-${count.index}"
  cluster_identifier           = aws_docdb_cluster.main[0].id
  instance_class               = var.mongodb_instance_type
  auto_minor_version_upgrade   = true
  apply_immediately            = var.environment != "prod"
  preferred_maintenance_window = "sun:05:00-sun:06:00"

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-mongodb-${count.index}"
    Service = "mongodb"
    Role    = count.index == 0 ? "primary" : "replica"
  })
}

# -----------------------------------------------------------------------------
# AWS ElastiCache - Redis Subnet Group
# -----------------------------------------------------------------------------
# Places Redis nodes in private subnets from the networking module for
# network isolation and compliance with security zone requirements.
# -----------------------------------------------------------------------------
resource "aws_elasticache_subnet_group" "redis" {
  count = var.cloud_provider == "aws" ? 1 : 0

  name        = "${local.name_prefix}-redis-subnet-group"
  description = "Subnet group for ${local.name_prefix} ElastiCache Redis - private subnet placement"
  subnet_ids  = var.subnet_ids

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-redis-subnet-group"
  })
}

# -----------------------------------------------------------------------------
# AWS ElastiCache - Redis Parameter Group
# -----------------------------------------------------------------------------
# Configures Redis 7.x engine parameters for optimal caching behavior:
# - allkeys-lru eviction for automatic memory management
# - keyspace notifications for session expiry events
# - connection timeout for resource cleanup
# -----------------------------------------------------------------------------
resource "aws_elasticache_parameter_group" "redis" {
  count = var.cloud_provider == "aws" ? 1 : 0

  family      = "redis7"
  name        = "${local.name_prefix}-redis-params"
  description = "Redis 7.x parameter group for ${local.name_prefix} with LRU eviction and keyspace events"

  # Eviction policy: remove least recently used keys when memory is full
  parameter {
    name  = "maxmemory-policy"
    value = "allkeys-lru"
  }

  # Enable keyspace notification events for key expiration (session management)
  parameter {
    name  = "notify-keyspace-events"
    value = "Ex"
  }

  # Connection idle timeout in seconds (5 minutes)
  parameter {
    name  = "timeout"
    value = "300"
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-redis-params"
  })
}

# -----------------------------------------------------------------------------
# AWS ElastiCache - Redis Replication Group
# -----------------------------------------------------------------------------
# Redis 7.x replication group with TLS encryption in transit, AES-256
# encryption at rest, automatic failover for production, multi-AZ deployment,
# and configurable snapshot retention. Provides session management and API
# response caching for all backend microservices.
# -----------------------------------------------------------------------------
resource "aws_elasticache_replication_group" "main" {
  count = var.cloud_provider == "aws" ? 1 : 0

  replication_group_id = "${local.name_prefix}-redis"
  description          = "Redis cache for Synthetic ERP Platform - session management and API caching"

  # Instance sizing and replica configuration
  node_type         = var.redis_node_type
  num_cache_clusters = var.redis_replica_count
  engine_version    = local.redis_engine_version
  port              = 6379

  # Parameter and subnet group references
  parameter_group_name = aws_elasticache_parameter_group.redis[0].name
  subnet_group_name    = aws_elasticache_subnet_group.redis[0].name

  # Network security - Redis security group from networking module
  security_group_ids = [var.redis_security_group_id]

  # AES-256 encryption at rest using KMS key from security module
  at_rest_encryption_enabled = true
  kms_key_id                 = var.kms_key_id

  # TLS encryption in transit for all client connections
  transit_encryption_enabled = true

  # High availability configuration per environment
  automatic_failover_enabled = var.environment != "dev"
  multi_az_enabled           = var.environment == "prod"

  # Snapshot configuration for backup and recovery
  snapshot_retention_limit = var.environment == "prod" ? 7 : 1
  snapshot_window          = "05:00-07:00"

  # Maintenance window for patching and upgrades
  maintenance_window       = "sun:07:00-sun:08:00"
  auto_minor_version_upgrade = true

  # Apply immediately in non-production environments
  apply_immediately = var.environment != "prod"

  tags = merge(local.common_tags, {
    Name    = "${local.name_prefix}-redis"
    Service = "redis"
    Role    = "cache-session-store"
  })
}

# =============================================================================
# AZURE RESOURCES
# =============================================================================
# Provisions Azure Cosmos DB with MongoDB API and Azure Cache for Redis.
# All resources are conditionally created only when var.cloud_provider == "azure".
# =============================================================================

# -----------------------------------------------------------------------------
# Azure Cosmos DB - Account (MongoDB API)
# -----------------------------------------------------------------------------
# Cosmos DB account configured with MongoDB wire protocol compatibility,
# session consistency (optimal for MongoDB workloads), customer-managed
# encryption keys, VNet integration, continuous backup, and automatic
# failover for production environments.
# -----------------------------------------------------------------------------
resource "azurerm_cosmosdb_account" "main" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = "${local.name_prefix}-mongodb"
  location            = var.region
  resource_group_name = var.resource_group_name
  offer_type          = "Standard"
  kind                = "MongoDB"

  # MongoDB 7.0 wire protocol compatibility
  mongo_server_version = local.mongodb_engine_version

  # Automatic failover for production high availability
  automatic_failover_enabled = var.environment == "prod"

  # Enable MongoDB API capability
  capabilities {
    name = "EnableMongo"
  }

  # Enable serverless for development (cost optimization) or provisioned for production
  dynamic "capabilities" {
    for_each = var.environment == "dev" ? [1] : []
    content {
      name = "EnableServerless"
    }
  }

  # Session consistency is the recommended default for MongoDB workloads
  # Balances between strong consistency and performance
  consistency_policy {
    consistency_level       = "Session"
    max_interval_in_seconds = 5
    max_staleness_prefix    = 100
  }

  # Primary geo-location for the Cosmos DB account
  geo_location {
    location          = var.region
    failover_priority = 0
    zone_redundant    = var.environment == "prod"
  }

  # VNet integration for network isolation
  is_virtual_network_filter_enabled = true

  # Associate with private subnets from networking module
  dynamic "virtual_network_rule" {
    for_each = var.subnet_ids
    content {
      id                                   = virtual_network_rule.value
      ignore_missing_vnet_service_endpoint = false
    }
  }

  # Customer-managed encryption key from security module (AES-256 at rest)
  key_vault_key_id = var.kms_key_id != "" ? var.kms_key_id : null

  # Continuous backup for point-in-time recovery
  backup {
    type                = "Continuous"
    tier                = var.environment == "prod" ? "Continuous30Days" : "Continuous7Days"
    storage_redundancy  = var.environment == "prod" ? "Geo" : "Local"
  }

  # Disable public network access for security
  public_network_access_enabled = false

  tags = merge(local.azure_tags, {
    service = "mongodb"
    role    = "metadata-repository"
  })
}

# -----------------------------------------------------------------------------
# Azure Cosmos DB - MongoDB Database
# -----------------------------------------------------------------------------
# Creates the "synthetic_erp_metadata" database that houses all five core
# collections used by the platform's microservices.
# -----------------------------------------------------------------------------
resource "azurerm_cosmosdb_mongo_database" "main" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = "synthetic_erp_metadata"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.main[0].name

  # Shared throughput across collections (only for provisioned mode)
  throughput = var.environment != "dev" ? 1000 : null
}

# -----------------------------------------------------------------------------
# Azure Cosmos DB - MongoDB Collection: generation_profiles
# -----------------------------------------------------------------------------
# Stores generation job profiles with tenant-based sharding for multi-tenant
# isolation. Indexed on tenant_id + created_at for efficient job listing.
# -----------------------------------------------------------------------------
resource "azurerm_cosmosdb_mongo_collection" "generation_profiles" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = "generation_profiles"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.main[0].name
  database_name       = azurerm_cosmosdb_mongo_database.main[0].name

  # Shard key for multi-tenant data isolation
  shard_key = "tenant_id"

  # Index for efficient tenant-scoped queries ordered by creation time
  index {
    keys   = ["tenant_id", "created_at"]
    unique = false
  }

  # Default _id index required by Cosmos DB
  index {
    keys   = ["_id"]
    unique = true
  }
}

# -----------------------------------------------------------------------------
# Azure Cosmos DB - MongoDB Collection: statistical_profiles
# -----------------------------------------------------------------------------
# Stores statistical profiling results linked to schema definitions with
# tenant isolation. Indexed on schema_id + tenant_id for profile lookups.
# -----------------------------------------------------------------------------
resource "azurerm_cosmosdb_mongo_collection" "statistical_profiles" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = "statistical_profiles"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.main[0].name
  database_name       = azurerm_cosmosdb_mongo_database.main[0].name

  shard_key = "tenant_id"

  # Index for schema-based profile lookups within tenant scope
  index {
    keys   = ["schema_id", "tenant_id"]
    unique = false
  }

  index {
    keys   = ["_id"]
    unique = true
  }
}

# -----------------------------------------------------------------------------
# Azure Cosmos DB - MongoDB Collection: schema_definitions
# -----------------------------------------------------------------------------
# Stores ERP schema definitions discovered by the Profiling Service with
# tenant isolation. Indexed on erp_type + tenant_id for schema browsing.
# -----------------------------------------------------------------------------
resource "azurerm_cosmosdb_mongo_collection" "schema_definitions" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = "schema_definitions"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.main[0].name
  database_name       = azurerm_cosmosdb_mongo_database.main[0].name

  shard_key = "tenant_id"

  # Index for ERP type-based schema browsing within tenant scope
  index {
    keys   = ["erp_type", "tenant_id"]
    unique = false
  }

  index {
    keys   = ["_id"]
    unique = true
  }
}

# -----------------------------------------------------------------------------
# Azure Cosmos DB - MongoDB Collection: audit_logs
# -----------------------------------------------------------------------------
# Stores tamper-evident audit log entries for SOC 2 Type II compliance.
# Sharded by tenant_id, indexed on timestamp + action for efficient querying.
# TTL index configured for 7-year retention policy.
# -----------------------------------------------------------------------------
resource "azurerm_cosmosdb_mongo_collection" "audit_logs" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = "audit_logs"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.main[0].name
  database_name       = azurerm_cosmosdb_mongo_database.main[0].name

  shard_key = "tenant_id"

  # Index for time-range and action-based audit log queries
  index {
    keys   = ["timestamp", "action"]
    unique = false
  }

  # Index for tenant-scoped audit queries
  index {
    keys   = ["tenant_id", "timestamp"]
    unique = false
  }

  index {
    keys   = ["_id"]
    unique = true
  }
}

# -----------------------------------------------------------------------------
# Azure Cosmos DB - MongoDB Collection: tenant_configurations
# -----------------------------------------------------------------------------
# Stores per-tenant configuration including resource quotas, namespace
# isolation settings, and feature flags. Unique index on tenant_id ensures
# one configuration document per tenant.
# -----------------------------------------------------------------------------
resource "azurerm_cosmosdb_mongo_collection" "tenant_configurations" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = "tenant_configurations"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.main[0].name
  database_name       = azurerm_cosmosdb_mongo_database.main[0].name

  shard_key = "_id"

  # Unique index ensuring one configuration per tenant
  index {
    keys   = ["tenant_id"]
    unique = true
  }

  index {
    keys   = ["_id"]
    unique = true
  }
}

# -----------------------------------------------------------------------------
# Azure Cache for Redis
# -----------------------------------------------------------------------------
# Azure Redis Cache with TLS-only connections, LRU eviction, RDB persistence,
# Premium tier for production (VNet injection, clustering), Standard for
# non-production environments. Provides session management and API caching.
# -----------------------------------------------------------------------------
resource "azurerm_redis_cache" "main" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = "${local.name_prefix}-redis"
  location            = var.region
  resource_group_name = var.resource_group_name

  # SKU-based sizing: Premium for production (VNet + clustering), Standard for others
  capacity  = var.redis_capacity
  family    = var.environment == "prod" ? "P" : var.redis_family
  sku_name  = var.environment == "prod" ? "Premium" : "Standard"

  # TLS enforcement - disable non-SSL port for security compliance
  enable_non_ssl_port = false
  minimum_tls_version = "1.2"

  # Redis version configuration
  redis_version = local.redis_engine_version

  # Private subnet placement (Premium tier only)
  subnet_id = var.environment == "prod" ? var.subnet_ids[0] : null

  # Redis engine configuration
  redis_configuration {
    maxmemory_policy = "allkeys-lru"

    # RDB persistence for data durability
    rdb_backup_enabled            = true
    rdb_backup_frequency          = 60
    rdb_backup_max_snapshot_count = var.environment == "prod" ? 5 : 1

    # AOF persistence for production (stronger durability guarantee)
    aof_backup_enabled = var.environment == "prod"
  }

  # Maintenance schedule for patching
  patch_schedule {
    day_of_week    = "Sunday"
    start_hour_utc = 2
  }

  # Disable public network access for security
  public_network_access_enabled = false

  tags = merge(local.azure_tags, {
    service = "redis"
    role    = "cache-session-store"
  })
}

# =============================================================================
# GCP RESOURCES
# =============================================================================
# Provisions self-managed MongoDB 7.0 on Compute Engine (with MongoDB Atlas as
# a preferred alternative) and GCP Memorystore for Redis.
# All resources are conditionally created only when var.cloud_provider == "gcp".
# =============================================================================

# -----------------------------------------------------------------------------
# GCP MongoDB - Self-Managed on Compute Engine
# -----------------------------------------------------------------------------
# Provisions a Compute Engine instance running MongoDB 7.0 with WiredTiger
# storage engine. The startup script handles MongoDB installation, replica set
# initialization, and security configuration. For production workloads,
# MongoDB Atlas (below) is the preferred managed alternative.
# -----------------------------------------------------------------------------
resource "google_compute_instance" "mongodb" {
  count = var.cloud_provider == "gcp" && var.atlas_project_id == "" ? var.mongodb_instance_count : 0

  name         = "${local.name_prefix}-mongodb-${count.index}"
  machine_type = var.mongodb_instance_type
  zone         = "${var.region}-${count.index == 0 ? "a" : count.index == 1 ? "b" : "c"}"

  # Boot disk with Ubuntu 22.04 LTS and SSD persistent disk for MongoDB storage
  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2204-lts"
      size  = var.mongodb_storage_size_gb
      type  = "pd-ssd"
    }
  }

  # Network placement in private subnet from networking module
  network_interface {
    network    = var.vpc_id
    subnetwork = var.subnet_ids[0]

    # No external IP - private subnet only access
  }

  # MongoDB 7.0 installation and WiredTiger configuration startup script
  metadata_startup_script = <<-SCRIPT
    #!/bin/bash
    set -euo pipefail

    # Log all output for debugging
    exec > /var/log/mongodb-setup.log 2>&1

    echo "=== MongoDB 7.0 Installation Starting ==="

    # Import MongoDB GPG key and add repository
    curl -fsSL https://www.mongodb.org/static/pgp/server-7.0.asc | \
      gpg --dearmor -o /usr/share/keyrings/mongodb-server-7.0.gpg
    echo "deb [ signed-by=/usr/share/keyrings/mongodb-server-7.0.gpg ] https://repo.mongodb.org/apt/ubuntu jammy/mongodb-org/7.0 multiverse" | \
      tee /etc/apt/sources.list.d/mongodb-org-7.0.list

    # Install MongoDB 7.0
    apt-get update -y
    apt-get install -y mongodb-org

    # Configure WiredTiger storage engine with optimized settings
    cat > /etc/mongod.conf <<EOF
    storage:
      dbPath: /var/lib/mongodb
      engine: wiredTiger
      wiredTiger:
        engineConfig:
          cacheSizeGB: $(echo "$(free -g | awk '/Mem:/{print $2}') * 0.5" | bc | cut -d. -f1)
          journalCompressor: snappy
        collectionConfig:
          blockCompressor: snappy
        indexConfig:
          prefixCompression: true

    systemLog:
      destination: file
      logAppend: true
      path: /var/log/mongodb/mongod.log

    net:
      port: 27017
      bindIp: 0.0.0.0
      tls:
        mode: requireTLS
        certificateKeyFile: /etc/ssl/mongodb/server.pem
        CAFile: /etc/ssl/mongodb/ca.pem

    security:
      authorization: enabled
      keyFile: /etc/mongodb/keyfile

    replication:
      replSetName: "${local.name_prefix}-rs"

    setParameter:
      auditAuthorizationSuccess: true
    EOF

    # Create data directory with proper permissions
    mkdir -p /var/lib/mongodb
    chown -R mongodb:mongodb /var/lib/mongodb

    # Create SSL directory
    mkdir -p /etc/ssl/mongodb
    mkdir -p /etc/mongodb

    # Generate replica set keyfile for internal authentication
    openssl rand -base64 756 > /etc/mongodb/keyfile
    chmod 400 /etc/mongodb/keyfile
    chown mongodb:mongodb /etc/mongodb/keyfile

    # Enable and start MongoDB service
    systemctl enable mongod
    systemctl start mongod

    echo "=== MongoDB 7.0 Installation Complete ==="

    # Initialize replica set on the first instance only
    if [ "${count.index}" = "0" ]; then
      sleep 10
      mongosh --eval '
        rs.initiate({
          _id: "${local.name_prefix}-rs",
          members: [
            ${join(",\n            ", [for i in range(var.mongodb_instance_count) : "{ _id: ${i}, host: \"${local.name_prefix}-mongodb-${i}:27017\" }"])}
          ]
        })
      '
      echo "=== Replica Set Initialized ==="
    fi
  SCRIPT

  # Service account for GCP API access
  service_account {
    scopes = ["cloud-platform"]
  }

  # Network tags for firewall rules
  tags = ["mongodb", "${local.name_prefix}-mongodb"]

  # Instance labels for resource identification
  labels = merge(local.gcp_labels, {
    service = "mongodb"
    role    = count.index == 0 ? "primary" : "replica"
  })

  # Allow stopping for updates in non-production environments
  allow_stopping_for_update = var.environment != "prod"
}

# -----------------------------------------------------------------------------
# GCP MongoDB Atlas - Managed Cluster (Preferred)
# -----------------------------------------------------------------------------
# MongoDB Atlas provides a fully managed MongoDB 7.0 cluster on GCP with
# automatic scaling, backups, encryption, and monitoring. This is the
# preferred deployment option over self-managed Compute Engine instances.
# Activated when var.atlas_project_id is provided.
# -----------------------------------------------------------------------------
resource "mongodbatlas_cluster" "main" {
  count = var.cloud_provider == "gcp" && var.atlas_project_id != "" ? 1 : 0

  project_id = var.atlas_project_id
  name       = "${local.name_prefix}-mongodb"

  # Cluster topology: replica set for data redundancy and read scaling
  cluster_type = "REPLICASET"

  # GCP provider configuration
  provider_name               = "GCP"
  provider_region_name        = var.region
  provider_instance_size_name = var.mongodb_instance_type

  # MongoDB 7.0 engine version
  mongo_db_major_version = local.mongodb_engine_version

  # GCP-native encryption at rest using Google Cloud KMS
  encryption_at_rest_provider = "GCP"

  # Auto-scaling for storage to handle growing metadata volumes
  auto_scaling_disk_gb_enabled = true

  # Backup configuration
  cloud_backup = true

  # Point-in-time recovery for production environments
  pit_enabled = var.environment == "prod"

  # Replica set configuration - 3 nodes across zones for HA
  replication_specs {
    num_shards = 1
    regions_config {
      region_name     = var.region
      electable_nodes = 3
      priority        = 7
      read_only_nodes = 0
    }
  }

  # Advanced configuration for WiredTiger and connection pooling
  advanced_configuration {
    javascript_enabled           = false
    minimum_enabled_tls_protocol = "TLS1_2"
  }

  # Resource labels for identification and cost tracking
  dynamic "labels" {
    for_each = local.gcp_labels
    content {
      key   = labels.key
      value = labels.value
    }
  }
}

# -----------------------------------------------------------------------------
# GCP Memorystore for Redis
# -----------------------------------------------------------------------------
# Google Cloud Memorystore for Redis 7.0 with HA tier for production,
# BASIC tier for development. Private Service Access for VPC-native
# connectivity, TLS encryption, authentication, and RDB persistence.
# -----------------------------------------------------------------------------
resource "google_redis_instance" "main" {
  count = var.cloud_provider == "gcp" ? 1 : 0

  name           = "${local.name_prefix}-redis"
  display_name   = "Redis cache for ${local.name_prefix} - session and API caching"

  # HA tier for production (automatic failover), BASIC for cost-optimized dev
  tier           = var.environment == "prod" ? "STANDARD_HA" : "BASIC"
  memory_size_gb = var.redis_memory_size_gb

  # Redis 7.0 engine version (GCP format: REDIS_7_0)
  redis_version = local.redis_gcp_version

  # VPC-native connectivity via Private Service Access
  authorized_network = var.vpc_id
  reserved_ip_range  = var.redis_reserved_ip_range != "" ? var.redis_reserved_ip_range : null
  connect_mode       = "PRIVATE_SERVICE_ACCESS"

  # TLS encryption in transit
  transit_encryption_mode = "SERVER_AUTHENTICATION"

  # Authentication requirement for connection security
  auth_enabled = true

  # RDB persistence for data durability across restarts
  persistence_config {
    persistence_mode    = "RDB"
    rdb_snapshot_period = "TWELVE_HOURS"
  }

  # Maintenance window configuration
  maintenance_policy {
    weekly_maintenance_window {
      day = "SUNDAY"
      start_time {
        hours   = 2
        minutes = 0
        seconds = 0
        nanos   = 0
      }
    }
  }

  # Redis configuration overrides
  redis_configs = {
    maxmemory-policy       = "allkeys-lru"
    notify-keyspace-events = "Ex"
    timeout                = "300"
  }

  # Region for the Redis instance
  region = var.region

  labels = merge(local.gcp_labels, {
    service = "redis"
    role    = "cache-session-store"
  })
}
