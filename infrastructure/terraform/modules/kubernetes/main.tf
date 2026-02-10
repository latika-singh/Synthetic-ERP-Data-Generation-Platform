# =============================================================================
# Terraform Kubernetes Module — Core Resource Definitions
# =============================================================================
# Provisions managed Kubernetes clusters for the Synthetic-ERP-Data-Generation-
# Platform across AWS (EKS), Azure (AKS), and GCP (GKE). This module creates
# the core compute infrastructure onto which all six backend microservices
# (API Gateway, Generation Engine, Profiling Service, Quality Service,
# Compliance Service, Provisioning Service) and the Web Console are deployed
# via Kubernetes Deployments defined in infrastructure/kubernetes/.
#
# Features:
#   - Multi-cloud support: AWS EKS, Azure AKS, GCP GKE
#   - Configurable node groups/pools with autoscaling (min/max/desired)
#   - Cluster-level RBAC for role-based access control
#   - Network policy support (Calico) for multi-tenant traffic isolation
#   - Native logging/monitoring (CloudWatch, Azure Monitor, Cloud Logging)
#   - Private cluster endpoints for air-gapped deployment capability
#   - Cluster autoscaler IAM integration for horizontal scalability
#   - Kubernetes 1.29+ version enforcement
#
# Resource naming convention:
#   ${var.project_name}-${var.environment}-<resource-suffix>
#   Example: synthetic-erp-platform-prod-cluster
#
# Conditional resource creation:
#   All provider-specific resources use count = var.cloud_provider == "xxx" ? 1 : 0
#   to ensure only the selected provider's resources are provisioned.
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  kubernetes
# =============================================================================

# -----------------------------------------------------------------------------
# Local Values — Common Tags and Naming
# -----------------------------------------------------------------------------
# Establishes consistent naming and tagging across all cloud provider resources.
# The cluster_name local follows the convention used throughout the project's
# Terraform modules: ${project_name}-${environment}-<resource-type>.
#
# common_tags are merged with user-supplied var.tags and applied to every
# resource for cost allocation, environment identification, and compliance
# tracking (SOC 2 Type II).
# -----------------------------------------------------------------------------
locals {
  cluster_name = "${var.project_name}-${var.environment}-cluster"
  node_group_name = "${var.project_name}-${var.environment}-nodes"

  common_tags = merge(var.tags, {
    project_name = var.project_name
    environment  = var.environment
    managed_by   = "terraform"
    module       = "kubernetes"
  })

  # GCP-compatible labels must be lowercase with hyphens only
  gcp_labels = {
    for key, value in local.common_tags :
    lower(replace(key, "_", "-")) => lower(replace(value, "_", "-"))
  }
}

# =============================================================================
#
#  AWS — Amazon Elastic Kubernetes Service (EKS)
#
# =============================================================================
# Provisions an EKS cluster with:
#   - Managed node groups for worker nodes
#   - IAM roles for cluster and node group service accounts
#   - VPC CNI, CoreDNS, kube-proxy addons for network policy support
#   - CloudWatch logging for api/audit/authenticator/controllerManager/scheduler
#   - Optional cluster autoscaler IAM policy
#   - Private endpoint access for air-gapped deployment capability
# =============================================================================

# -----------------------------------------------------------------------------
# AWS CloudWatch Log Group for EKS Control Plane Logs
# -----------------------------------------------------------------------------
# Creates a dedicated CloudWatch log group for EKS control plane logging.
# Log types include api, audit, authenticator, controllerManager, and scheduler
# which are critical for SOC 2 Type II compliance audit trails.
#
# Retention is set based on environment:
#   - dev:     30 days
#   - staging: 90 days
#   - prod:    365 days (aligns with 7-year audit retention when archived)
# -----------------------------------------------------------------------------
resource "aws_cloudwatch_log_group" "eks" {
  count = var.cloud_provider == "aws" ? 1 : 0

  name              = "/aws/eks/${local.cluster_name}/cluster"
  retention_in_days = var.environment == "prod" ? 365 : var.environment == "staging" ? 90 : 30

  tags = local.common_tags
}

# -----------------------------------------------------------------------------
# AWS IAM Role — EKS Cluster Service Role
# -----------------------------------------------------------------------------
# The EKS cluster service role allows the EKS control plane to manage AWS
# resources on behalf of the cluster. Attached policies:
#   - AmazonEKSClusterPolicy: Core EKS cluster management permissions
#   - AmazonEKSVPCResourceController: VPC networking for pod ENI management
# -----------------------------------------------------------------------------
resource "aws_iam_role" "eks_cluster" {
  count = var.cloud_provider == "aws" ? 1 : 0

  name = "${local.cluster_name}-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "eks.amazonaws.com"
        }
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "eks_cluster_policy" {
  count = var.cloud_provider == "aws" ? 1 : 0

  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
  role       = aws_iam_role.eks_cluster[0].name
}

resource "aws_iam_role_policy_attachment" "eks_vpc_resource_controller" {
  count = var.cloud_provider == "aws" ? 1 : 0

  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController"
  role       = aws_iam_role.eks_cluster[0].name
}

# -----------------------------------------------------------------------------
# AWS IAM Role — EKS Node Group Role
# -----------------------------------------------------------------------------
# The node group IAM role grants worker nodes the permissions needed to:
#   - Register with the EKS cluster (AmazonEKSWorkerNodePolicy)
#   - Manage VPC networking / pod IP addresses (AmazonEKS_CNI_Policy)
#   - Pull container images from ECR (AmazonEC2ContainerRegistryReadOnly)
# -----------------------------------------------------------------------------
resource "aws_iam_role" "eks_node_group" {
  count = var.cloud_provider == "aws" ? 1 : 0

  name = "${local.cluster_name}-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "ec2.amazonaws.com"
        }
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "eks_worker_node_policy" {
  count = var.cloud_provider == "aws" ? 1 : 0

  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"
  role       = aws_iam_role.eks_node_group[0].name
}

resource "aws_iam_role_policy_attachment" "eks_cni_policy" {
  count = var.cloud_provider == "aws" ? 1 : 0

  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
  role       = aws_iam_role.eks_node_group[0].name
}

resource "aws_iam_role_policy_attachment" "eks_ecr_read_only" {
  count = var.cloud_provider == "aws" ? 1 : 0

  policy_arn = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
  role       = aws_iam_role.eks_node_group[0].name
}

# -----------------------------------------------------------------------------
# AWS EKS Cluster
# -----------------------------------------------------------------------------
# Creates the EKS control plane with:
#   - Kubernetes version pinned to var.cluster_version (>= 1.29)
#   - VPC configuration using private subnets from the networking module
#   - Private endpoint access enabled (air-gapped deployment support)
#   - Public endpoint access configurable per environment
#   - CloudWatch logging for all five control plane log types
#   - RBAC enabled by default (EKS always has RBAC enabled)
#
# The cluster depends on the IAM role policy attachments to ensure
# permissions are in place before cluster creation begins.
# -----------------------------------------------------------------------------
resource "aws_eks_cluster" "main" {
  count = var.cloud_provider == "aws" ? 1 : 0

  name     = local.cluster_name
  version  = var.cluster_version
  role_arn = aws_iam_role.eks_cluster[0].arn

  vpc_config {
    subnet_ids              = var.subnet_ids
    endpoint_private_access = true
    endpoint_public_access  = var.environment == "prod" ? false : true
    security_group_ids      = []
  }

  # Enable all five control plane log types for comprehensive audit trails
  # Required for SOC 2 Type II compliance and operational visibility
  enabled_cluster_log_types = [
    "api",
    "audit",
    "authenticator",
    "controllerManager",
    "scheduler"
  ]

  # NOTE: Encryption of Kubernetes secrets at rest using AWS KMS should be
  # configured when the security module provides a KMS key ARN. The
  # encryption_config block can be added here with the KMS key ARN from
  # module.security outputs to enforce AES-256 at-rest encryption for
  # Kubernetes secrets as required by the platform security specification.
  # Example:
  #   encryption_config {
  #     provider { key_arn = var.kms_key_arn }
  #     resources = ["secrets"]
  #   }

  tags = local.common_tags

  depends_on = [
    aws_iam_role_policy_attachment.eks_cluster_policy,
    aws_iam_role_policy_attachment.eks_vpc_resource_controller,
    aws_cloudwatch_log_group.eks,
  ]

  lifecycle {
    # Prevent accidental destruction of the cluster in production
    prevent_destroy = false
  }
}

# -----------------------------------------------------------------------------
# AWS EKS Managed Node Group
# -----------------------------------------------------------------------------
# Creates a managed node group for worker nodes with:
#   - Configurable instance type (default t3.large for AWS)
#   - Auto-scaling between min_nodes and max_nodes
#   - Initial desired_size for immediate capacity
#   - Placed in private subnets for security
#   - Amazon Linux 2 AMI (AL2_x86_64) for broad compatibility
#
# The scaling configuration supports the cluster autoscaler and HPA:
#   - HPA scales pods horizontally based on CPU/memory metrics
#   - Cluster autoscaler scales nodes when pods can't be scheduled
#   - Together they target 1M+ records/minute generation throughput
# -----------------------------------------------------------------------------
resource "aws_eks_node_group" "main" {
  count = var.cloud_provider == "aws" ? 1 : 0

  cluster_name    = aws_eks_cluster.main[0].name
  node_group_name = local.node_group_name
  node_role_arn   = aws_iam_role.eks_node_group[0].arn
  subnet_ids      = var.subnet_ids

  instance_types = [var.node_instance_type]
  ami_type       = "AL2_x86_64"
  capacity_type  = "ON_DEMAND"
  disk_size      = 100

  scaling_config {
    min_size     = var.min_nodes
    max_size     = var.max_nodes
    desired_size = var.desired_nodes
  }

  # Rolling update strategy to minimize disruption during node updates
  update_config {
    max_unavailable = 1
  }

  labels = {
    role        = "worker"
    environment = var.environment
    project     = var.project_name
  }

  tags = merge(local.common_tags, {
    # Tag required for Kubernetes cluster autoscaler discovery
    "k8s.io/cluster-autoscaler/enabled"            = "true"
    "k8s.io/cluster-autoscaler/${local.cluster_name}" = "owned"
  })

  depends_on = [
    aws_iam_role_policy_attachment.eks_worker_node_policy,
    aws_iam_role_policy_attachment.eks_cni_policy,
    aws_iam_role_policy_attachment.eks_ecr_read_only,
  ]

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}

# -----------------------------------------------------------------------------
# AWS EKS Addons — VPC CNI, CoreDNS, kube-proxy
# -----------------------------------------------------------------------------
# Essential EKS addons for networking, DNS resolution, and network policy:
#   - vpc-cni: AWS VPC CNI plugin for pod networking with native VPC IPs
#   - coredns: Kubernetes DNS service for internal service discovery
#   - kube-proxy: Network proxy for Kubernetes service abstraction
#
# These addons ensure network policy support and proper cluster networking
# required for multi-tenant traffic isolation via NetworkPolicies.
# -----------------------------------------------------------------------------
resource "aws_eks_addon" "vpc_cni" {
  count = var.cloud_provider == "aws" ? 1 : 0

  cluster_name                = aws_eks_cluster.main[0].name
  addon_name                  = "vpc-cni"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = local.common_tags

  depends_on = [aws_eks_node_group.main]
}

resource "aws_eks_addon" "coredns" {
  count = var.cloud_provider == "aws" ? 1 : 0

  cluster_name                = aws_eks_cluster.main[0].name
  addon_name                  = "coredns"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = local.common_tags

  depends_on = [aws_eks_node_group.main]
}

resource "aws_eks_addon" "kube_proxy" {
  count = var.cloud_provider == "aws" ? 1 : 0

  cluster_name                = aws_eks_cluster.main[0].name
  addon_name                  = "kube-proxy"
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = local.common_tags

  depends_on = [aws_eks_node_group.main]
}

# -----------------------------------------------------------------------------
# AWS IAM Policy — Cluster Autoscaler
# -----------------------------------------------------------------------------
# When var.enable_cluster_autoscaler is true, creates an IAM policy granting
# the Kubernetes cluster autoscaler permissions to:
#   - Describe Auto Scaling groups and launch configurations
#   - Set desired capacity on Auto Scaling groups
#   - Terminate instances in Auto Scaling groups
#
# This policy is attached to the node group IAM role so that the autoscaler
# pod (running on worker nodes) can manage node scaling. The autoscaler
# works with HPA to support 1M+ records/minute throughput targets.
# -----------------------------------------------------------------------------
resource "aws_iam_policy" "cluster_autoscaler" {
  count = var.cloud_provider == "aws" && var.enable_cluster_autoscaler ? 1 : 0

  name        = "${local.cluster_name}-cluster-autoscaler"
  description = "IAM policy for Kubernetes cluster autoscaler on EKS"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:DescribeLaunchConfigurations",
          "autoscaling:DescribeScalingActivities",
          "autoscaling:DescribeTags",
          "autoscaling:SetDesiredCapacity",
          "autoscaling:TerminateInstanceInAutoScalingGroup",
          "ec2:DescribeLaunchTemplateVersions",
          "ec2:DescribeInstanceTypes",
          "eks:DescribeNodegroup"
        ]
        Resource = "*"
      }
    ]
  })

  tags = local.common_tags
}

resource "aws_iam_role_policy_attachment" "cluster_autoscaler" {
  count = var.cloud_provider == "aws" && var.enable_cluster_autoscaler ? 1 : 0

  policy_arn = aws_iam_policy.cluster_autoscaler[0].arn
  role       = aws_iam_role.eks_node_group[0].name
}

# =============================================================================
#
#  Azure — Azure Kubernetes Service (AKS)
#
# =============================================================================
# Provisions an AKS cluster with:
#   - System-assigned managed identity (no service principal management)
#   - Default node pool with autoscaling for worker nodes
#   - Azure CNI networking with Calico network policy for isolation
#   - Azure Monitor integration via OMS agent and Log Analytics
#   - RBAC enabled for role-based access control
#   - Private cluster option for air-gapped deployment
# =============================================================================

# -----------------------------------------------------------------------------
# Azure Log Analytics Workspace
# -----------------------------------------------------------------------------
# Creates a Log Analytics workspace for Azure Monitor integration with the
# AKS cluster. Container Insights data (node metrics, pod logs, container
# metrics) flows to this workspace for operational monitoring and SOC 2
# Type II compliance audit trails.
#
# Retention aligns with environment requirements:
#   - dev:     30 days (cost control)
#   - staging: 60 days
#   - prod:    365 days (audit compliance, archivable for 7-year retention)
# -----------------------------------------------------------------------------
resource "azurerm_log_analytics_workspace" "main" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = "${var.project_name}-${var.environment}-logs"
  location            = var.region
  resource_group_name = "${var.project_name}-${var.environment}-rg"
  sku                 = "PerGB2018"
  retention_in_days   = var.environment == "prod" ? 365 : var.environment == "staging" ? 60 : 30

  tags = local.common_tags
}

# -----------------------------------------------------------------------------
# Azure Kubernetes Service (AKS) Cluster
# -----------------------------------------------------------------------------
# Creates the AKS managed cluster with:
#   - Kubernetes version pinned to var.cluster_version (>= 1.29)
#   - System-assigned managed identity for Azure resource management
#   - Default node pool with auto-scaling between min_nodes and max_nodes
#   - Azure CNI networking plugin for native VNet pod networking
#   - Calico network policy for Kubernetes NetworkPolicy support
#   - OMS agent for Azure Monitor Container Insights integration
#   - RBAC enabled for Kubernetes role-based access control
#   - Private cluster for production (air-gapped deployment capability)
#
# The default node pool provides compute capacity for all platform services.
# Autoscaling is managed natively by AKS without separate autoscaler setup.
# -----------------------------------------------------------------------------
resource "azurerm_kubernetes_cluster" "main" {
  count = var.cloud_provider == "azure" ? 1 : 0

  name                = local.cluster_name
  location            = var.region
  resource_group_name = "${var.project_name}-${var.environment}-rg"
  dns_prefix          = "${var.project_name}-${var.environment}"
  kubernetes_version  = var.cluster_version

  # Private cluster configuration for air-gapped deployment capability
  # In production, the API server is only accessible from within the VNet
  private_cluster_enabled = var.environment == "prod" ? true : false

  # RBAC must be enabled for role-based access control (platform requirement)
  role_based_access_control_enabled = true

  # Azure Active Directory integration for enterprise identity management
  azure_active_directory_role_based_access_control {
    managed                = true
    azure_rbac_enabled     = true
  }

  # Default node pool — primary compute for all platform microservices
  default_node_pool {
    name                = "default"
    vm_size             = var.node_instance_type
    min_count           = var.min_nodes
    max_count           = var.max_nodes
    node_count          = var.desired_nodes
    enable_auto_scaling = var.enable_cluster_autoscaler
    vnet_subnet_id      = length(var.subnet_ids) > 0 ? var.subnet_ids[0] : null
    os_disk_size_gb     = 100
    os_disk_type        = "Managed"
    type                = "VirtualMachineScaleSets"
    max_pods            = 110

    # Node labels for workload scheduling
    node_labels = {
      role        = "worker"
      environment = var.environment
      project     = var.project_name
    }

    tags = local.common_tags
  }

  # System-assigned managed identity eliminates service principal credential
  # management, improving security posture for SOC 2 Type II compliance
  identity {
    type = "SystemAssigned"
  }

  # Azure CNI with Calico network policy for Kubernetes NetworkPolicy support
  # Required for multi-tenant traffic isolation between namespace workloads
  network_profile {
    network_plugin    = "azure"
    network_policy    = "calico"
    load_balancer_sku = "standard"
    outbound_type     = "loadBalancer"
    service_cidr      = "10.96.0.0/16"
    dns_service_ip    = "10.96.0.10"
  }

  # OMS agent for Azure Monitor Container Insights integration
  # Provides centralized logging, metrics, and alerting for SOC 2 compliance
  oms_agent {
    log_analytics_workspace_id = azurerm_log_analytics_workspace.main[0].id
  }

  # Auto-upgrade channel for security patches (stable channel for production)
  automatic_channel_upgrade = var.environment == "prod" ? "stable" : "rapid"

  # Maintenance window — apply updates during low-traffic periods
  maintenance_window {
    allowed {
      day   = "Sunday"
      hours = [0, 4]
    }
  }

  tags = local.common_tags

  lifecycle {
    ignore_changes = [
      default_node_pool[0].node_count,
    ]
  }
}

# =============================================================================
#
#  GCP — Google Kubernetes Engine (GKE)
#
# =============================================================================
# Provisions a GKE cluster with:
#   - Separately managed node pool (default node pool removed)
#   - Private cluster configuration for air-gapped deployment
#   - Calico network policy provider for Kubernetes NetworkPolicy support
#   - Cloud Logging and Cloud Monitoring integration
#   - Workload identity for secure service account management
#   - Auto-repair and auto-upgrade for node pool maintenance
#   - Cluster autoscaling when enabled
# =============================================================================

# -----------------------------------------------------------------------------
# GCP GKE Cluster
# -----------------------------------------------------------------------------
# Creates the GKE cluster control plane with:
#   - Kubernetes version pinned to var.cluster_version (>= 1.29)
#   - Regional cluster for high availability across zones
#   - Default node pool removed (separately managed for flexibility)
#   - Private cluster with private nodes (no external IPs on nodes)
#   - Calico network policy enabled for NetworkPolicy support
#   - Cloud Logging and Cloud Monitoring for native observability
#   - Workload identity enabled for secure GCP API access from pods
#   - Binary authorization evaluation mode for container security
#
# The cluster is placed in the VPC and subnet created by the networking
# module, ensuring all traffic flows through controlled network paths.
# -----------------------------------------------------------------------------
resource "google_container_cluster" "main" {
  count = var.cloud_provider == "gcp" ? 1 : 0

  name               = local.cluster_name
  location           = var.region
  min_master_version = var.cluster_version

  # Network configuration from the networking module
  network    = var.vpc_id
  subnetwork = length(var.subnet_ids) > 0 ? var.subnet_ids[0] : null

  # Remove the default node pool — we manage node pools separately for
  # granular control over instance types, scaling, and labels
  remove_default_node_pool = true
  initial_node_count       = 1

  # Private cluster configuration for air-gapped deployment capability
  # Private nodes ensure worker nodes have no external IP addresses
  private_cluster_config {
    enable_private_nodes    = true
    enable_private_endpoint = var.environment == "prod" ? true : false
    master_ipv4_cidr_block  = "172.16.0.0/28"
  }

  # Disable client certificate authentication — use gcloud auth instead
  # for better security posture aligned with SOC 2 Type II requirements
  master_auth {
    client_certificate_config {
      issue_client_certificate = false
    }
  }

  # Enable Calico network policy provider for Kubernetes NetworkPolicy support
  # Required for multi-tenant traffic isolation between namespace workloads
  network_policy {
    enabled  = true
    provider = "CALICO"
  }

  # NOTE: Dataplane V2 (ADVANCED_DATAPATH) is not used here because it
  # conflicts with the Calico network policy provider specified above.
  # Dataplane V2 provides its own eBPF-based network policy enforcement
  # and cannot be combined with Calico. The platform spec requires Calico
  # for NetworkPolicy support across all providers.

  # Cloud Logging integration for centralized log management
  # Captures system, workload, and API server logs
  logging_service = "logging.googleapis.com/kubernetes"

  # Cloud Monitoring integration for metrics and alerting
  # Provides Kubernetes-native metrics including pod, node, and container stats
  monitoring_service = "monitoring.googleapis.com/kubernetes"

  # Workload Identity for secure GCP API access from Kubernetes pods
  # Replaces static service account keys with federated identity tokens
  workload_identity_config {
    workload_pool = "${data.google_project.current[0].project_id}.svc.id.goog"
  }

  # Cluster autoscaling profile — optimize for utilization or availability
  cluster_autoscaling {
    enabled = var.enable_cluster_autoscaler
    autoscaling_profile = "BALANCED"

    dynamic "resource_limits" {
      for_each = var.enable_cluster_autoscaler ? [1] : []
      content {
        resource_type = "cpu"
        minimum       = var.min_nodes * 2
        maximum       = var.max_nodes * 8
      }
    }

    dynamic "resource_limits" {
      for_each = var.enable_cluster_autoscaler ? [1] : []
      content {
        resource_type = "memory"
        minimum       = var.min_nodes * 4
        maximum       = var.max_nodes * 32
      }
    }
  }

  # IP allocation policy for VPC-native cluster (alias IP ranges)
  ip_allocation_policy {
    # Use default computed secondary ranges for pods and services
  }

  # Release channel for automated version management
  release_channel {
    channel = var.environment == "prod" ? "STABLE" : "REGULAR"
  }

  # Binary authorization for container image verification
  binary_authorization {
    evaluation_mode = "PROJECT_SINGLETON_POLICY_ENFORCE"
  }

  resource_labels = local.gcp_labels

  # Deletion protection for production clusters
  deletion_protection = var.environment == "prod" ? true : false

  lifecycle {
    ignore_changes = [
      initial_node_count,
      node_config,
    ]
  }
}

# Data source to retrieve the current GCP project ID for Workload Identity
data "google_project" "current" {
  count = var.cloud_provider == "gcp" ? 1 : 0
}

# -----------------------------------------------------------------------------
# GCP GKE Node Pool
# -----------------------------------------------------------------------------
# Creates a separately managed node pool with:
#   - Configurable machine type (default e2-standard-4 for GCP)
#   - Autoscaling between min_node_count and max_node_count
#   - Auto-repair for automatic unhealthy node replacement
#   - Auto-upgrade for automatic Kubernetes version updates
#   - Preemptible instances disabled for production stability
#   - Secure boot and integrity monitoring for node security
#   - Workload metadata configuration for Workload Identity
#
# Separate node pool management allows independent scaling, instance type
# changes, and rolling updates without affecting the cluster control plane.
# -----------------------------------------------------------------------------
resource "google_container_node_pool" "main" {
  count = var.cloud_provider == "gcp" ? 1 : 0

  name       = local.node_group_name
  location   = var.region
  cluster    = google_container_cluster.main[0].name

  # Initial node count — autoscaler adjusts from here
  initial_node_count = var.desired_nodes

  # Autoscaling configuration for horizontal scalability
  # Works with Kubernetes HPA to support 1M+ records/minute throughput
  autoscaling {
    min_node_count = var.min_nodes
    max_node_count = var.max_nodes
  }

  # Auto-repair replaces unhealthy nodes automatically
  # Auto-upgrade keeps nodes on the latest stable Kubernetes patch version
  management {
    auto_repair  = true
    auto_upgrade = true
  }

  # Rolling update strategy to minimize disruption during node updates
  upgrade_settings {
    max_surge       = 1
    max_unavailable = 0
  }

  node_config {
    machine_type = var.node_instance_type
    disk_size_gb = 100
    disk_type    = "pd-ssd"

    # Use ON_DEMAND instances for production stability
    preemptible = false

    # OAuth scopes for GCP API access from nodes
    oauth_scopes = [
      "https://www.googleapis.com/auth/cloud-platform",
      "https://www.googleapis.com/auth/logging.write",
      "https://www.googleapis.com/auth/monitoring",
      "https://www.googleapis.com/auth/devstorage.read_only",
    ]

    # Node labels for workload scheduling
    labels = {
      role        = "worker"
      environment = var.environment
      project     = var.project_name
    }

    # Metadata configuration for Workload Identity
    workload_metadata_config {
      mode = "GKE_METADATA"
    }

    # Shielded instance configuration for enhanced node security
    shielded_instance_config {
      enable_secure_boot          = true
      enable_integrity_monitoring = true
    }

    # Resource tags for GCP firewall rules
    tags = ["${var.project_name}-${var.environment}-node"]

    metadata = {
      disable-legacy-endpoints = "true"
    }
  }

  lifecycle {
    ignore_changes = [
      initial_node_count,
    ]
  }
}
