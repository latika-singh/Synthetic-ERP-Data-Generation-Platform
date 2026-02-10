# =============================================================================
# Terraform Database Module — Input Variable Definitions
# =============================================================================
# Defines all configurable parameters for provisioning MongoDB 7.0 and
# Redis 7.x database infrastructure across AWS, Azure, and GCP cloud
# providers. This module manages:
#
#   MongoDB: AWS DocumentDB, Azure Cosmos DB (MongoDB API), GCP MongoDB
#            Atlas or self-managed instances
#   Redis:   AWS ElastiCache, Azure Cache for Redis, GCP Memorystore
#
# These variables are supplied by the root infrastructure/terraform/main.tf
# module composition which passes networking module outputs (vpc_id,
# subnet_ids, security_group_ids) and security module outputs (kms_key_id)
# along with environment-specific values from dev.tfvars, staging.tfvars,
# and prod.tfvars.
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  database
# =============================================================================

# -----------------------------------------------------------------------------
# Cloud Provider Selection
# -----------------------------------------------------------------------------
# Determines which cloud provider's database resources are provisioned:
#   - "aws"   → AWS DocumentDB (MongoDB-compatible) + ElastiCache for Redis
#   - "azure" → Azure Cosmos DB (MongoDB API) + Azure Cache for Redis
#   - "gcp"   → MongoDB Atlas (managed) or self-managed MongoDB on GCE +
#                GCP Memorystore for Redis
#
# The main.tf conditionally creates provider-specific database resources
# based on this value using the count = var.cloud_provider == "provider" ? 1 : 0
# pattern.
# -----------------------------------------------------------------------------
variable "cloud_provider" {
  type        = string
  description = "Cloud provider for database resources (aws, azure, or gcp)"

  validation {
    condition     = contains(["aws", "azure", "gcp"], var.cloud_provider)
    error_message = "cloud_provider must be one of: aws, azure, gcp"
  }
}

# -----------------------------------------------------------------------------
# Deployment Environment
# -----------------------------------------------------------------------------
# Controls environment-specific behaviour such as instance sizing, replica
# counts, backup retention policies, and encryption enforcement. The value
# flows into the common naming convention:
#   ${project_name}-${environment}-{resource}
#
# Typical configurations:
#   dev     — minimal replicas, smaller instances, short backup retention
#   staging — production-like sizing for pre-release validation
#   prod    — full HA replicas, production-grade instances, 30+ day backups,
#             AES-256 encryption at rest enforced
# -----------------------------------------------------------------------------
variable "environment" {
  type        = string
  description = "Deployment environment (dev, staging, prod)"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod"
  }
}

# -----------------------------------------------------------------------------
# Project Name (Resource Naming Prefix)
# -----------------------------------------------------------------------------
# Used as the leading segment of every database resource name to avoid
# collisions across projects sharing the same cloud account or subscription.
# Combined with environment for resource identification:
#   ${project_name}-${environment}-docdb-cluster
#   ${project_name}-${environment}-redis-cluster
# -----------------------------------------------------------------------------
variable "project_name" {
  type        = string
  default     = "synthetic-erp-platform"
  description = "Project name used for resource naming prefix and database resource identification"
}

# -----------------------------------------------------------------------------
# Cloud Provider Region
# -----------------------------------------------------------------------------
# Specifies the geographic region where all database resources are deployed.
# Must be a valid region identifier for the selected cloud_provider:
#
#   AWS:   e.g., us-east-1, eu-west-1, ap-southeast-1
#   Azure: e.g., eastus, westeurope, southeastasia
#   GCP:   e.g., us-central1, europe-west1, asia-southeast1
#
# Database instances are deployed in the same region as the Kubernetes
# cluster and networking resources to minimise latency between backend
# microservices and the data layer.
# -----------------------------------------------------------------------------
variable "region" {
  type        = string
  description = "Cloud provider region for database resource deployment"
}

# -----------------------------------------------------------------------------
# MongoDB Engine Version
# -----------------------------------------------------------------------------
# Controls the MongoDB-compatible engine version for the deployed cluster.
# Version 7.0 is required for:
#
#   - WiredTiger storage engine (default in 7.0)
#   - Compatibility with PyMongo 4.x driver used by all backend services
#   - Queryable Encryption support for SOC 2 Type II compliance
#   - Improved aggregation pipeline performance for statistical profiling
#
# Provider-specific version mapping:
#   AWS DocumentDB:     Supports engine versions aligned with MongoDB 5.0/6.0
#                       wire protocol; DocumentDB cluster parameter group
#                       controls compatibility mode
#   Azure Cosmos DB:    MongoDB API version 7.0 wire protocol support
#   GCP Atlas/Self-Managed: Native MongoDB 7.0
#
# Validation ensures only MongoDB 7.0 or compatible patch versions are used
# to maintain consistency across all five MongoDB collections
# (generation_profiles, statistical_profiles, schema_definitions, audit_logs,
# tenant_configurations).
# -----------------------------------------------------------------------------
variable "mongodb_version" {
  type        = string
  default     = "7.0"
  description = "MongoDB engine version. Must be 7.0 for WiredTiger storage engine and compatibility with PyMongo 4.x driver."

  validation {
    condition     = can(regex("^7\\.0", var.mongodb_version))
    error_message = "mongodb_version must be 7.0 or a compatible patch version (e.g., 7.0, 7.0.12). MongoDB 7.0 is required for WiredTiger and PyMongo 4.x compatibility."
  }
}

# -----------------------------------------------------------------------------
# MongoDB Instance Type / Size
# -----------------------------------------------------------------------------
# Determines the compute and memory capacity of each MongoDB cluster node.
# Provider-specific instance type mappings:
#
#   AWS DocumentDB:  db.r6g.large (2 vCPU, 16 GiB RAM) — suitable for
#                    production workloads with up to 1M+ records/minute
#                    throughput target
#   Azure Cosmos DB: Throughput-based (RU/s); instance type is abstracted
#                    by Cosmos DB's serverless or provisioned throughput model
#   GCP Self-Managed: e2-standard-4 (4 vCPU, 16 GB RAM) for self-hosted
#                     MongoDB on Compute Engine instances
#   GCP Atlas:       M10 (general purpose) or M30 (production) tier
#
# Adjust based on workload characteristics:
#   - Statistical profiling: memory-intensive (large aggregation pipelines)
#   - Audit logging: write-intensive (append-only, high write throughput)
#   - Metadata storage: balanced read/write for generation profiles
# -----------------------------------------------------------------------------
variable "mongodb_instance_type" {
  type        = string
  default     = "db.r6g.large"
  description = "Instance type/size for MongoDB cluster nodes. AWS: db.r6g.large (DocumentDB), Azure: determined by Cosmos DB throughput, GCP: e2-standard-4 (self-managed) or M10 (Atlas)."
}

# -----------------------------------------------------------------------------
# MongoDB Instance Count (Replica Set Size)
# -----------------------------------------------------------------------------
# Number of MongoDB instances in the replica set for high availability and
# read scalability. The replica set provides:
#
#   - Automatic failover: if the primary becomes unavailable, a secondary
#     is elected as the new primary (requires majority quorum)
#   - Read distribution: secondary reads for profiling queries and reports
#   - Data durability: write concern "majority" ensures data survives
#     node failures
#
# Recommended configurations:
#   dev:     1 (single instance, no HA — cost-effective)
#   staging: 3 (minimum HA configuration with quorum)
#   prod:    3-5 (full HA with read replicas for reporting workloads)
#
# AWS DocumentDB supports 1-15 instances per cluster. Azure Cosmos DB
# manages replicas automatically. GCP Atlas uses the cluster tier to
# determine replica topology.
# -----------------------------------------------------------------------------
variable "mongodb_instance_count" {
  type        = number
  default     = 3
  description = "Number of MongoDB instances in the replica set for high availability. Minimum 3 for production environments."

  validation {
    condition     = var.mongodb_instance_count >= 1 && var.mongodb_instance_count <= 15
    error_message = "mongodb_instance_count must be between 1 and 15. Use 3+ for production high availability."
  }
}

# -----------------------------------------------------------------------------
# MongoDB Storage Allocation (GB)
# -----------------------------------------------------------------------------
# Storage capacity in gigabytes allocated for MongoDB data, indexes, and
# the WiredTiger cache. This setting controls:
#
#   AWS DocumentDB:     EBS volume size per cluster instance
#   Azure Cosmos DB:    Auto-scales storage; this value sets the initial
#                       provisioned throughput baseline
#   GCP Self-Managed:   Persistent Disk size per Compute Engine instance
#   GCP Atlas:          Included in the cluster tier; this value is advisory
#
# Storage sizing considerations for the five MongoDB collections:
#   - generation_profiles:   ~1 KB per profile × volume of generation jobs
#   - statistical_profiles:  ~10 KB per profile with distribution data
#   - schema_definitions:    ~5 KB per schema with table/column metadata
#   - audit_logs:            Grows continuously; 7-year retention requirement
#   - tenant_configurations: ~2 KB per tenant — minimal storage impact
#
# The default of 100 GB supports approximately 10M audit log entries plus
# operational data for several hundred generation profiles. Production
# environments with high job throughput should increase to 500+ GB.
# -----------------------------------------------------------------------------
variable "mongodb_storage_size_gb" {
  type        = number
  default     = 100
  description = "Storage allocation in GB for MongoDB data. Applies to self-managed instances (GCP) and DocumentDB (AWS). Cosmos DB auto-scales."

  validation {
    condition     = var.mongodb_storage_size_gb >= 20 && var.mongodb_storage_size_gb <= 16384
    error_message = "mongodb_storage_size_gb must be between 20 and 16384 GB."
  }
}

# -----------------------------------------------------------------------------
# MongoDB Admin Username
# -----------------------------------------------------------------------------
# Master/admin username for MongoDB cluster authentication. This account
# has full administrative privileges and is used for:
#
#   - Initial cluster setup and collection creation
#   - Index management for the five core collections
#   - User/role provisioning for application service accounts
#
# Application services (API Gateway, Generation Engine, etc.) should use
# dedicated service accounts with least-privilege permissions rather than
# the admin account. The admin account credentials should be rotated
# regularly and stored in the cloud provider's secrets manager.
#
# Not marked as sensitive since the username is not a secret by itself;
# however, it should still not be committed to version control in plaintext.
# -----------------------------------------------------------------------------
variable "mongodb_admin_username" {
  type        = string
  default     = "erpadmin"
  description = "Master/admin username for MongoDB cluster authentication"
  sensitive   = false
}

# -----------------------------------------------------------------------------
# MongoDB Admin Password
# -----------------------------------------------------------------------------
# Master/admin password for MongoDB cluster authentication. This is a
# sensitive value that must be:
#
#   - Injected via CI/CD pipeline environment variables
#   - Stored in the cloud provider's secrets manager (AWS Secrets Manager,
#     Azure Key Vault, or GCP Secret Manager) after initial provisioning
#   - Rotated according to SOC 2 Type II compliance requirements
#   - Never committed to version control or Terraform state in plaintext
#
# The password is used during initial cluster provisioning and should meet
# the following complexity requirements:
#   - Minimum 16 characters
#   - Mix of uppercase, lowercase, digits, and special characters
#   - No dictionary words or common patterns
#
# After provisioning, application services retrieve credentials from the
# secrets manager rather than referencing this variable directly.
# -----------------------------------------------------------------------------
variable "mongodb_admin_password" {
  type        = string
  description = "Master/admin password for MongoDB cluster authentication. Should be injected via CI/CD or secrets manager."
  sensitive   = true
}

# -----------------------------------------------------------------------------
# Redis Engine Version
# -----------------------------------------------------------------------------
# Controls the Redis engine version for the deployed cache cluster.
# Version 7.x is required for:
#
#   - Redis Functions (server-side scripting) for atomic progress updates
#   - Enhanced ACL support for multi-tenant session isolation
#   - Client-side caching notifications for reduced cache invalidation latency
#   - Improved memory efficiency for large hash objects (statistical profiles)
#
# Provider-specific version handling:
#   AWS ElastiCache:      Supports Redis 7.0 and 7.1 engine versions
#   Azure Cache for Redis: Supports Redis 6.x on Basic/Standard, 7.x on
#                          Premium tier with VNet injection
#   GCP Memorystore:      Supports Redis 7.0 and 7.2 engine versions
#
# The platform uses Redis for:
#   - API response caching (50ms cached vs 100ms standard response times)
#   - Session management and JWT token caching
#   - Real-time generation job progress tracking via pub/sub
#   - Rate limiter counters (60/300/1000 req/min tiered limits)
#   - Distributed locks for batch processing coordination
# -----------------------------------------------------------------------------
variable "redis_version" {
  type        = string
  default     = "7.0"
  description = "Redis engine version. Must be 7.x for Redis 7 feature compatibility (functions, ACLs)."

  validation {
    condition     = can(regex("^7", var.redis_version))
    error_message = "redis_version must start with 7 (e.g., 7.0, 7.1, 7.2). Redis 7.x is required for functions and ACL support."
  }
}

# -----------------------------------------------------------------------------
# Redis Node Type / Size
# -----------------------------------------------------------------------------
# Determines the compute and memory capacity of each Redis cache node.
# Provider-specific node type mappings:
#
#   AWS ElastiCache:       cache.r6g.large (2 vCPU, 13.07 GiB RAM) —
#                          memory-optimised for caching workloads
#   Azure Cache for Redis: Determined by SKU (C = Basic/Standard,
#                          P = Premium) and capacity parameter
#   GCP Memorystore:       Determined by memory_size_gb parameter;
#                          node type is abstracted
#
# Memory sizing considerations for platform caching:
#   - Session cache: ~2 KB per active user session
#   - API response cache: ~5 KB per cached endpoint response
#   - Progress tracking: ~1 KB per active generation job
#   - Rate limiter counters: ~100 bytes per client per window
#   - Distributed locks: ~200 bytes per active lock
# -----------------------------------------------------------------------------
variable "redis_node_type" {
  type        = string
  default     = "cache.r6g.large"
  description = "Node type/size for Redis cache instances. AWS: cache.r6g.large (ElastiCache), Azure: determined by SKU, GCP: determined by memory_size_gb."
}

# -----------------------------------------------------------------------------
# Redis Replica Count
# -----------------------------------------------------------------------------
# Number of Redis read replicas for high availability and read scalability.
# Replicas provide:
#
#   - Automatic failover: if the primary becomes unavailable, a replica
#     is promoted (ElastiCache Multi-AZ, Azure geo-replication, Memorystore HA)
#   - Read distribution: replica reads for cache-heavy workloads
#   - Data durability: asynchronous replication protects against node loss
#
# Recommended configurations:
#   dev:     0 (single node, no HA — cost-effective for development)
#   staging: 1 (basic HA for pre-release validation)
#   prod:    2+ (full HA with automatic failover across AZs)
#
# AWS ElastiCache supports 0-5 replicas per shard. Azure Cache for Redis
# Premium supports up to 3 replicas. GCP Memorystore Standard tier includes
# automatic HA with 1 replica.
# -----------------------------------------------------------------------------
variable "redis_replica_count" {
  type        = number
  default     = 2
  description = "Number of Redis read replicas for high availability. Set to 0 for development, 2+ for production."

  validation {
    condition     = var.redis_replica_count >= 0 && var.redis_replica_count <= 5
    error_message = "redis_replica_count must be between 0 and 5."
  }
}

# -----------------------------------------------------------------------------
# Redis Memory Size (GB) — GCP Memorystore
# -----------------------------------------------------------------------------
# Memory allocation in gigabytes for GCP Memorystore for Redis instances.
# This parameter is the primary sizing control for Memorystore, which does
# not use traditional node types like ElastiCache.
#
# For AWS ElastiCache and Azure Cache for Redis, memory is determined by
# the node_type and SKU respectively; this variable serves as an advisory
# reference for those providers.
#
# Memorystore memory tiers:
#   1 GB   — development/testing (up to ~500K cached keys)
#   4 GB   — standard workloads (default, ~2M cached keys)
#   16 GB  — high-throughput workloads with large datasets
#   64+ GB — enterprise-scale caching with millions of active sessions
#
# The platform's Redis usage (sessions, API cache, progress tracking, rate
# limiting) typically requires 2-8 GB for production workloads depending
# on concurrent user count and active generation job volume.
# -----------------------------------------------------------------------------
variable "redis_memory_size_gb" {
  type        = number
  default     = 4
  description = "Memory allocation in GB for Redis Memorystore (GCP). ElastiCache and Azure Cache sizing is determined by node_type/SKU."

  validation {
    condition     = var.redis_memory_size_gb >= 1 && var.redis_memory_size_gb <= 300
    error_message = "redis_memory_size_gb must be between 1 and 300 GB."
  }
}

# -----------------------------------------------------------------------------
# Azure Redis Cache Capacity
# -----------------------------------------------------------------------------
# The size parameter for Azure Cache for Redis, controlling the cache
# instance capacity. The acceptable values depend on the selected SKU
# family (redis_family variable):
#
#   Family "C" (Basic/Standard):
#     0 = 250 MB, 1 = 1 GB, 2 = 2.5 GB, 3 = 6 GB, 4 = 13 GB, 5 = 26 GB, 6 = 53 GB
#
#   Family "P" (Premium — supports VNet injection and clustering):
#     1 = 6 GB, 2 = 13 GB, 3 = 26 GB, 4 = 53 GB, 5 = 120 GB
#
# Premium family ("P") is recommended for production deployments because
# it supports:
#   - VNet injection for network isolation (aligns with multi-tenant design)
#   - Redis clustering for horizontal scaling
#   - Data persistence (RDB/AOF) for durability
#   - Geo-replication for disaster recovery
#
# The default capacity of 2 (2.5 GB with Standard, 13 GB with Premium)
# is suitable for staging environments. Production should use capacity 3+
# with Premium family for the platform's caching requirements.
# -----------------------------------------------------------------------------
variable "redis_capacity" {
  type        = number
  default     = 2
  description = "Azure Redis Cache capacity (size of the Redis cache). The acceptable values depend on the SKU family."

  validation {
    condition     = var.redis_capacity >= 0 && var.redis_capacity <= 6
    error_message = "redis_capacity must be between 0 and 6. See Azure documentation for capacity-to-memory mapping per SKU family."
  }
}

# -----------------------------------------------------------------------------
# Azure Redis Cache SKU Family
# -----------------------------------------------------------------------------
# The SKU family for Azure Cache for Redis, controlling the feature set
# and performance characteristics:
#
#   "C" (Basic/Standard):
#     - Shared infrastructure (Basic) or replicated (Standard)
#     - No VNet injection — accessed via public endpoint with firewall rules
#     - Suitable for development and low-traffic staging environments
#
#   "P" (Premium):
#     - Dedicated infrastructure with guaranteed performance
#     - VNet injection for private network access (recommended for production)
#     - Redis clustering support for horizontal scaling
#     - Data persistence (RDB snapshots, AOF) for durability
#     - Geo-replication for disaster recovery
#     - Zone redundancy for high availability
#
# Production deployments of the Synthetic-ERP-Data-Generation-Platform
# should use Premium ("P") family for VNet isolation and clustering
# capabilities aligned with the multi-tenant security architecture.
# -----------------------------------------------------------------------------
variable "redis_family" {
  type        = string
  default     = "C"
  description = "Azure Redis Cache family. C = Basic/Standard, P = Premium (supports VNet injection and clustering)."

  validation {
    condition     = contains(["C", "P"], var.redis_family)
    error_message = "redis_family must be one of: C (Basic/Standard), P (Premium)."
  }
}

# -----------------------------------------------------------------------------
# VPC / VNet Identifier
# -----------------------------------------------------------------------------
# The identifier of the VPC (AWS), VNet (Azure), or VPC Network (GCP)
# created by the networking module. Used for:
#
#   AWS DocumentDB:     Placing the cluster in the VPC and associating
#                       the DB subnet group
#   AWS ElastiCache:    Placing the replication group in the VPC and
#                       associating the cache subnet group
#   Azure Cosmos DB:    VNet service endpoint or private endpoint association
#   Azure Redis:        VNet injection (Premium SKU) or private endpoint
#   GCP Self-Managed:   Compute instances placed in the VPC network
#   GCP Memorystore:    Authorized VPC network for private service access
#
# This value is passed from the root main.tf:
#   module.database.vpc_id = module.networking.vpc_id
# -----------------------------------------------------------------------------
variable "vpc_id" {
  type        = string
  description = "VPC/VNet identifier from the networking module for database network placement and security group association."
}

# -----------------------------------------------------------------------------
# Subnet Identifiers (Private Subnets)
# -----------------------------------------------------------------------------
# List of private subnet identifiers from the networking module for database
# instance placement across availability zones. Databases are deployed in
# private subnets to ensure they are not directly accessible from the
# public internet.
#
# Multi-AZ placement:
#   AWS DocumentDB:  Instances distributed across subnets via DB subnet group
#   AWS ElastiCache: Nodes distributed across subnets via cache subnet group
#   Azure Cosmos DB: Private endpoint placed in the designated subnet
#   Azure Redis:     VNet-injected into the specified subnet (Premium)
#   GCP MongoDB:     Compute instances placed across zones in the subnet
#   GCP Memorystore: Connected via the VPC for private access
#
# This value is passed from the root main.tf:
#   module.database.subnet_ids = module.networking.private_subnet_ids
# -----------------------------------------------------------------------------
variable "subnet_ids" {
  type        = list(string)
  description = "List of private subnet identifiers from the networking module for database instance placement across availability zones."
}

# -----------------------------------------------------------------------------
# MongoDB Security Group / NSG Identifier
# -----------------------------------------------------------------------------
# Security group (AWS), Network Security Group (Azure), or firewall rule
# tag (GCP) controlling inbound and outbound traffic to MongoDB instances.
# The referenced security group should enforce:
#
#   - Inbound: Allow TCP port 27017 from backend service subnets only
#   - Outbound: Allow responses to backend services
#   - Deny: All other inbound traffic (no public access)
#
# This is typically the "database-sg" security group created by the
# networking module, which restricts MongoDB access to the backend
# services security group (backend-services-sg).
#
# This value is passed from the root main.tf:
#   module.database.security_group_id = module.networking.security_group_ids["database"]
#
# An empty default allows the database module to be applied without
# explicit security group association during initial bootstrapping or
# when the networking module manages security groups separately.
# -----------------------------------------------------------------------------
variable "security_group_id" {
  type        = string
  default     = ""
  description = "Security group/NSG ID for MongoDB access control. Allows inbound traffic on port 27017 from backend services only."
}

# -----------------------------------------------------------------------------
# Redis Security Group / NSG Identifier
# -----------------------------------------------------------------------------
# Security group (AWS), Network Security Group (Azure), or firewall rule
# tag (GCP) controlling inbound and outbound traffic to Redis instances.
# The referenced security group should enforce:
#
#   - Inbound: Allow TCP port 6379 from backend service subnets only
#   - Outbound: Allow responses to backend services
#   - Deny: All other inbound traffic (no public access)
#
# This is typically the "redis-sg" security group created by the
# networking module, which restricts Redis access to the backend
# services security group (backend-services-sg).
#
# This value is passed from the root main.tf:
#   module.database.redis_security_group_id = module.networking.security_group_ids["redis"]
#
# An empty default allows the database module to be applied without
# explicit security group association during initial bootstrapping or
# when the networking module manages security groups separately.
# -----------------------------------------------------------------------------
variable "redis_security_group_id" {
  type        = string
  default     = ""
  description = "Security group/NSG ID for Redis access control. Allows inbound traffic on port 6379 from backend services only."
}

# -----------------------------------------------------------------------------
# KMS Encryption Key Identifier
# -----------------------------------------------------------------------------
# The KMS key ID (AWS), Key Vault key URI (Azure), or Cloud KMS crypto key
# ID (GCP) from the security module used for AES-256 encryption at rest.
# When provided, all database storage is encrypted with this customer-managed
# key instead of the provider's default encryption:
#
#   AWS DocumentDB:     kms_key_id parameter on aws_docdb_cluster for
#                       storage encryption
#   AWS ElastiCache:    kms_key_id parameter for at-rest encryption on
#                       the replication group and snapshots
#   Azure Cosmos DB:    key_vault_key_id for customer-managed key encryption
#   Azure Redis:        Not directly supported; uses Azure-managed encryption
#                       (Premium tier uses disk encryption with platform keys)
#   GCP Self-Managed:   CMEK for persistent disk encryption
#   GCP Memorystore:    customer_managed_key for at-rest encryption
#
# This value is passed from the root main.tf:
#   module.database.kms_key_id = module.security.kms_key_ids["data_encryption"]
#
# Required for SOC 2 Type II compliance (Constraint C-004) which mandates
# AES-256 encryption at rest for all stored data. An empty default allows
# provider-managed encryption for development environments.
# -----------------------------------------------------------------------------
variable "kms_key_id" {
  type        = string
  default     = ""
  description = "KMS key ID/ARN from the security module for AES-256 encryption at rest. Used to encrypt MongoDB storage and Redis snapshots."
}

# -----------------------------------------------------------------------------
# Backup Retention Period (Days)
# -----------------------------------------------------------------------------
# Number of days to retain automated database backups and snapshots.
# Automated backups provide point-in-time recovery for both MongoDB and
# Redis data stores.
#
# Provider-specific backup behaviour:
#   AWS DocumentDB:     Continuous backup with retention window; supports
#                       point-in-time restore within the retention period
#   AWS ElastiCache:    Daily automatic snapshots with configurable retention
#   Azure Cosmos DB:    Continuous or periodic backups; retention configured
#                       via backup policy
#   Azure Redis:        RDB snapshots (Premium tier) or AOF persistence
#   GCP Atlas:          Continuous backup with configurable retention
#   GCP Memorystore:    Manual and scheduled RDB exports
#
# Recommended configurations:
#   dev:     1-7 days (cost-effective, sufficient for development)
#   staging: 7-14 days (enough for pre-release testing cycles)
#   prod:    30+ days (aligns with SOC 2 audit requirements)
#
# Note: The platform's audit_logs MongoDB collection requires 7-year
# retention; this is handled at the application level via archival
# policies, not by database-level backup retention.
# -----------------------------------------------------------------------------
variable "backup_retention_days" {
  type        = number
  default     = 7
  description = "Number of days to retain automated database backups. Production should use 30+, development can use 1-7."

  validation {
    condition     = var.backup_retention_days >= 1 && var.backup_retention_days <= 365
    error_message = "backup_retention_days must be between 1 and 365 days."
  }
}

# -----------------------------------------------------------------------------
# Azure Resource Group Name
# -----------------------------------------------------------------------------
# The name of the Azure resource group where database resources are created.
# Azure requires all resources to belong to a resource group for lifecycle
# management, RBAC, and cost tracking.
#
# This value is passed from the root main.tf when cloud_provider is "azure":
#   module.database.resource_group_name = azurerm_resource_group.main.name
#
# An empty default allows the database module to be used with AWS or GCP
# where resource groups do not apply. When cloud_provider is "azure" and
# this value is empty, the module will create its own resource group using
# the naming convention: ${project_name}-${environment}-database-rg
# -----------------------------------------------------------------------------
variable "resource_group_name" {
  type        = string
  default     = ""
  description = "Azure resource group name for database resources. Required when cloud_provider is azure."
}

# -----------------------------------------------------------------------------
# MongoDB Atlas Project ID — GCP
# -----------------------------------------------------------------------------
# The MongoDB Atlas project ID for managed MongoDB clusters when deploying
# on GCP. MongoDB Atlas provides a fully managed MongoDB service that
# integrates natively with GCP networking and security:
#
#   - Private endpoints via VPC peering or Private Service Connect
#   - Customer-managed encryption keys via GCP Cloud KMS
#   - Automated backups with point-in-time recovery
#   - Built-in monitoring and performance advisor
#
# When this value is provided and cloud_provider is "gcp", the module
# provisions a MongoDB Atlas cluster instead of a self-managed MongoDB
# instance on GCE. This is the recommended approach for production GCP
# deployments due to reduced operational overhead.
#
# An empty default means the module will provision self-managed MongoDB
# on GCP Compute Engine instances (or skip Atlas provisioning for
# non-GCP providers).
# -----------------------------------------------------------------------------
variable "atlas_project_id" {
  type        = string
  default     = ""
  description = "MongoDB Atlas project ID for managed MongoDB clusters on GCP. Required when using Atlas instead of self-managed."
}

# -----------------------------------------------------------------------------
# GCP Memorystore Reserved IP Range
# -----------------------------------------------------------------------------
# Reserved IP address range for GCP Memorystore for Redis private service
# access. Memorystore requires a pre-allocated IP range within the VPC
# for private connectivity between the cache instance and Kubernetes pods.
#
# Format: CIDR notation (e.g., 10.0.100.0/29)
#
# The IP range must:
#   - Be within the VPC CIDR block but not overlap with existing subnets
#   - Be at least /29 (8 IPs) for a single Redis instance
#   - Be larger for HA configurations with read replicas
#
# If left empty, GCP will automatically allocate an IP range from the VPC
# address space. Specifying a range is recommended for production to ensure
# predictable networking and avoid conflicts with future subnet expansions.
#
# This value is only used when cloud_provider is "gcp".
# -----------------------------------------------------------------------------
variable "redis_reserved_ip_range" {
  type        = string
  default     = ""
  description = "Reserved IP range for GCP Memorystore Redis private service access. Format: CIDR notation (e.g., 10.0.100.0/29)."
}

# -----------------------------------------------------------------------------
# Additional Resource Tags
# -----------------------------------------------------------------------------
# Supplementary tags merged with the module's default tags (project_name,
# environment, managed_by) and applied to every database resource. Use for
# cost-centre attribution, team ownership, compliance labels, or custom
# metadata required by organisational tagging policies.
#
# Example:
#   tags = {
#     "CostCenter"  = "engineering"
#     "Owner"       = "platform-team"
#     "DataClass"   = "confidential"
#     "Compliance"  = "soc2-type-ii"
#     "BackupPolicy"= "daily"
#   }
#
# Note: Tag key/value constraints vary by provider:
#   AWS:   Max 50 tags, key max 128 chars, value max 256 chars
#   Azure: Max 50 tags, key max 512 chars, value max 256 chars
#   GCP:   Max 64 labels, key/value max 63 chars, lowercase + hyphens only
# -----------------------------------------------------------------------------
variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional tags to apply to all database resources for cost tracking, compliance labeling, and environment identification."
}
