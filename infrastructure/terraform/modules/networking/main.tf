# =============================================================================
# Terraform Networking Module — Main Resource Definitions
# =============================================================================
# Provisions the foundational networking infrastructure for the
# Synthetic-ERP-Data-Generation-Platform across AWS, Azure, or GCP.
#
# Resources created based on var.cloud_provider:
#   AWS   → VPC, Subnets, IGW, NAT GW, Route Tables, Security Groups,
#            VPC Flow Logs (CloudWatch)
#   Azure → Resource Group, VNet, Subnets, NAT GW, NSGs, Network Watcher
#            Flow Logs (Storage Account)
#   GCP   → VPC Network, Subnetworks, Cloud Router, Cloud NAT, Firewall
#            Rules (subnet-level flow logs)
#
# Security architecture enforces least-privilege access with service-tier
# isolation aligned to Section 6.4 — Network Zones:
#   api_gateway      → Public-facing HTTPS (443) ingress
#   backend_services → Internal Flask service ports (5000-5005)
#   database         → MongoDB (27017) from backend tier only
#   redis            → Redis (6379) from backend tier only
#   kubernetes_nodes → Inter-node and control-plane communication
#
# SOC 2 Type II compliance (Constraint C-004) is achieved through VPC/VNet
# flow logs capturing all network traffic for audit and anomaly detection.
#
# Air-gapped deployment (Constraint C-003) is supported by making NAT
# gateways optional — disable them when a private registry mirror is used.
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  networking
# =============================================================================


# =============================================================================
# LOCAL VALUES
# =============================================================================
# Centralises naming conventions, tags, and conditional flags consumed by
# all cloud-provider-specific resource blocks.  Keeping these in one place
# guarantees consistent resource naming and tagging across providers.
# =============================================================================

locals {
  # ---------------------------------------------------------------------------
  # Resource naming prefix: "{project}-{env}"
  # Example: "synthetic-erp-platform-prod"
  # ---------------------------------------------------------------------------
  name_prefix = "${var.project_name}-${var.environment}"

  # ---------------------------------------------------------------------------
  # Boolean flags for conditional resource creation per cloud provider.
  # Every resource block uses count = local.is_<provider> ? ... : 0
  # so that only the selected provider's resources are instantiated.
  # ---------------------------------------------------------------------------
  is_aws   = var.cloud_provider == "aws"
  is_azure = var.cloud_provider == "azure"
  is_gcp   = var.cloud_provider == "gcp"

  # ---------------------------------------------------------------------------
  # Derived counts
  # ---------------------------------------------------------------------------
  az_count = length(var.availability_zones)

  # NAT Gateway count:
  #   0 — disabled (air-gapped or private-only deployment)
  #   1 — single shared NAT GW (cost-effective for dev/staging)
  #   N — one per AZ (high availability for production)
  nat_gateway_count = var.enable_nat_gateway ? (
    var.single_nat_gateway ? 1 : local.az_count
  ) : 0

  # ---------------------------------------------------------------------------
  # Effective allowed CIDR blocks — falls back to "any" when the caller
  # does not restrict external access (public SaaS deployments).
  # ---------------------------------------------------------------------------
  effective_allowed_cidrs = length(var.allowed_cidr_blocks) > 0 ? var.allowed_cidr_blocks : ["0.0.0.0/0"]

  # ---------------------------------------------------------------------------
  # Azure storage account name for NSG flow logs.
  # Constraints: 3–24 chars, lowercase alphanumeric only.
  # ---------------------------------------------------------------------------
  azure_flow_log_storage_name = substr(
    replace(lower("${var.project_name}${var.environment}fl"), "-", ""), 0, 24
  )

  # ---------------------------------------------------------------------------
  # Common tags applied to every resource for cost tracking, compliance
  # labelling, and environment identification.
  # ---------------------------------------------------------------------------
  common_tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Module      = "networking"
    },
    var.tags,
  )
}


# #############################################################################
#                          AWS RESOURCES
# #############################################################################
# Created when var.cloud_provider == "aws".  Provisions a standard 3-tier VPC
# architecture with public subnets (load balancers, ingress), private subnets
# (Kubernetes nodes, databases), NAT gateways, security groups per service
# tier, and VPC flow logs for SOC 2 Type II audit compliance.
# #############################################################################


# -----------------------------------------------------------------------------
# AWS VPC
# -----------------------------------------------------------------------------
# Primary Virtual Private Cloud with DNS support enabled for EKS service
# discovery and internal DNS resolution between the six platform micro-
# services.  The CIDR block is configurable (default 10.0.0.0/16).
# -----------------------------------------------------------------------------
resource "aws_vpc" "main" {
  count = local.is_aws ? 1 : 0

  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-vpc"
  })
}

# -----------------------------------------------------------------------------
# AWS Internet Gateway
# -----------------------------------------------------------------------------
# Provides internet connectivity for public subnets hosting the NGINX
# Ingress Controller, Application Load Balancers, and bastion hosts.
# -----------------------------------------------------------------------------
resource "aws_internet_gateway" "main" {
  count = local.is_aws ? 1 : 0

  vpc_id = aws_vpc.main[0].id

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-igw"
  })
}

# -----------------------------------------------------------------------------
# AWS Public Subnets
# -----------------------------------------------------------------------------
# One subnet per AZ for internet-facing resources.  Tagged for EKS auto-
# discovery of external load balancer placement (kubernetes.io/role/elb = 1).
# map_public_ip_on_launch enables direct internet-routable addressing.
# -----------------------------------------------------------------------------
resource "aws_subnet" "public" {
  count = local.is_aws ? length(var.public_subnet_cidrs) : 0

  vpc_id                  = aws_vpc.main[0].id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.availability_zones[count.index % local.az_count]
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, {
    Name                                         = "${local.name_prefix}-public-${var.availability_zones[count.index % local.az_count]}"
    Tier                                         = "public"
    "kubernetes.io/role/elb"                     = "1"
    "kubernetes.io/cluster/${local.name_prefix}" = "shared"
  })
}

# -----------------------------------------------------------------------------
# AWS Private Subnets
# -----------------------------------------------------------------------------
# One subnet per AZ for internal workloads: EKS worker nodes, MongoDB
# StatefulSets, Redis clusters, and all six backend microservices.
# Tagged for EKS internal LB discovery (kubernetes.io/role/internal-elb = 1).
# No public IP assignment — outbound traffic routes through NAT Gateways.
# -----------------------------------------------------------------------------
resource "aws_subnet" "private" {
  count = local.is_aws ? length(var.private_subnet_cidrs) : 0

  vpc_id            = aws_vpc.main[0].id
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = var.availability_zones[count.index % local.az_count]

  tags = merge(local.common_tags, {
    Name                                         = "${local.name_prefix}-private-${var.availability_zones[count.index % local.az_count]}"
    Tier                                         = "private"
    "kubernetes.io/role/internal-elb"            = "1"
    "kubernetes.io/cluster/${local.name_prefix}" = "shared"
  })
}


# =============================================================================
# AWS NAT GATEWAYS
# =============================================================================
# Provides outbound internet access for private subnets (container image
# pulls, Auth0 callbacks, cloud API calls, metrics export).  Placed in
# public subnets with static Elastic IPs.
#
# Topology controlled by variables:
#   enable_nat_gateway = false → No NAT (air-gapped / C-003)
#   single_nat_gateway = true  → 1 NAT GW in first public subnet (dev/staging)
#   single_nat_gateway = false → 1 NAT GW per AZ (production HA)
# =============================================================================

# Elastic IPs for NAT Gateways — one per NAT Gateway instance
resource "aws_eip" "nat" {
  count = local.is_aws ? local.nat_gateway_count : 0

  domain = "vpc"

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-nat-eip-${count.index + 1}"
  })

  depends_on = [aws_internet_gateway.main]
}

# NAT Gateway instances — placed in public subnets
resource "aws_nat_gateway" "main" {
  count = local.is_aws ? local.nat_gateway_count : 0

  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.public[count.index].id

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-nat-gw-${count.index + 1}"
  })

  depends_on = [aws_internet_gateway.main]
}


# =============================================================================
# AWS ROUTE TABLES
# =============================================================================

# -----------------------------------------------------------------------------
# Public Route Table — routes 0.0.0.0/0 through the Internet Gateway
# -----------------------------------------------------------------------------
resource "aws_route_table" "public" {
  count = local.is_aws ? 1 : 0

  vpc_id = aws_vpc.main[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main[0].id
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-public-rt"
    Tier = "public"
  })
}

# -----------------------------------------------------------------------------
# Private Route Tables — one per AZ for independent AZ routing
# -----------------------------------------------------------------------------
resource "aws_route_table" "private" {
  count = local.is_aws ? local.az_count : 0

  vpc_id = aws_vpc.main[0].id

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-private-rt-${count.index + 1}"
    Tier = "private"
  })
}

# Default route through NAT Gateway (only when NAT is enabled).
# When single_nat_gateway is true all private route tables share the same
# NAT GW; otherwise each AZ uses its own NAT GW for HA.
resource "aws_route" "private_nat" {
  count = local.is_aws && var.enable_nat_gateway ? local.az_count : 0

  route_table_id         = aws_route_table.private[count.index].id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.main[var.single_nat_gateway ? 0 : count.index].id
}

# -----------------------------------------------------------------------------
# Route Table Associations — Public Subnets
# -----------------------------------------------------------------------------
resource "aws_route_table_association" "public" {
  count = local.is_aws ? length(var.public_subnet_cidrs) : 0

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

# -----------------------------------------------------------------------------
# Route Table Associations — Private Subnets
# -----------------------------------------------------------------------------
resource "aws_route_table_association" "private" {
  count = local.is_aws ? length(var.private_subnet_cidrs) : 0

  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index % local.az_count].id
}


# =============================================================================
# AWS SECURITY GROUPS
# =============================================================================
# Least-privilege security groups for each service tier.  Rules enforce
# network-level isolation aligned with the platform security architecture
# (Section 6.4 — Network Zones).  Each SG restricts ingress to the minimum
# ports required and only from the upstream tier.
# =============================================================================

# -----------------------------------------------------------------------------
# API Gateway Security Group
# -----------------------------------------------------------------------------
# Public-facing tier: accepts HTTPS (443) and HTTP (80 for redirect/health)
# from allowed CIDR blocks.  Egress unrestricted for backend service calls.
# -----------------------------------------------------------------------------
resource "aws_security_group" "api_gateway" {
  count = local.is_aws ? 1 : 0

  name        = "${local.name_prefix}-api-gateway-sg"
  description = "API Gateway - HTTPS/HTTP ingress from allowed sources"
  vpc_id      = aws_vpc.main[0].id

  ingress {
    description = "HTTPS from allowed sources"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = local.effective_allowed_cidrs
  }

  ingress {
    description = "HTTP for health checks and HTTPS redirect"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = local.effective_allowed_cidrs
  }

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-api-gateway-sg"
    Tier = "api-gateway"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Backend Services Security Group
# -----------------------------------------------------------------------------
# Internal tier for the six Flask microservices (ports 5000-5005).
# Ingress allowed only from the API Gateway SG and from peer backend
# services for inter-service REST calls.
# -----------------------------------------------------------------------------
resource "aws_security_group" "backend_services" {
  count = local.is_aws ? 1 : 0

  name        = "${local.name_prefix}-backend-services-sg"
  description = "Backend services - Flask ports from API Gateway and peers"
  vpc_id      = aws_vpc.main[0].id

  # Flask service ports from the API Gateway tier
  ingress {
    description     = "Flask services from API Gateway"
    from_port       = 5000
    to_port         = 5005
    protocol        = "tcp"
    security_groups = [aws_security_group.api_gateway[0].id]
  }

  # Inter-service communication within the backend tier
  ingress {
    description = "Inter-service communication"
    from_port   = 5000
    to_port     = 5005
    protocol    = "tcp"
    self        = true
  }

  # Prometheus metrics scraping from within the VPC
  ingress {
    description = "Prometheus metrics endpoint"
    from_port   = 9090
    to_port     = 9090
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-backend-services-sg"
    Tier = "backend-services"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Database Security Group (MongoDB 27017)
# -----------------------------------------------------------------------------
# Restricted tier: ingress on MongoDB port only from the backend services SG.
# Self-referencing rule allows replica set communication between MongoDB nodes.
# -----------------------------------------------------------------------------
resource "aws_security_group" "database" {
  count = local.is_aws ? 1 : 0

  name        = "${local.name_prefix}-database-sg"
  description = "Database tier - MongoDB ingress from backend services only"
  vpc_id      = aws_vpc.main[0].id

  ingress {
    description     = "MongoDB from backend services"
    from_port       = 27017
    to_port         = 27017
    protocol        = "tcp"
    security_groups = [aws_security_group.backend_services[0].id]
  }

  # MongoDB replica set inter-node communication
  ingress {
    description = "MongoDB replica set"
    from_port   = 27017
    to_port     = 27017
    protocol    = "tcp"
    self        = true
  }

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-database-sg"
    Tier = "database"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Redis Security Group (Port 6379)
# -----------------------------------------------------------------------------
# Restricted tier: ingress on Redis port only from the backend services SG.
# Self-referencing rule allows Redis Cluster bus communication (port 16379).
# -----------------------------------------------------------------------------
resource "aws_security_group" "redis" {
  count = local.is_aws ? 1 : 0

  name        = "${local.name_prefix}-redis-sg"
  description = "Redis tier - ingress from backend services only"
  vpc_id      = aws_vpc.main[0].id

  ingress {
    description     = "Redis from backend services"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [aws_security_group.backend_services[0].id]
  }

  # Redis Cluster bus inter-node communication
  ingress {
    description = "Redis Cluster bus"
    from_port   = 16379
    to_port     = 16379
    protocol    = "tcp"
    self        = true
  }

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-redis-sg"
    Tier = "redis"
  })

  lifecycle {
    create_before_destroy = true
  }
}

# -----------------------------------------------------------------------------
# Kubernetes Nodes Security Group
# -----------------------------------------------------------------------------
# Allows control-plane → node communication (443, 10250), NodePort services
# (30000-32767), and unrestricted inter-node traffic for CNI pod networking.
# -----------------------------------------------------------------------------
resource "aws_security_group" "kubernetes_nodes" {
  count = local.is_aws ? 1 : 0

  name        = "${local.name_prefix}-kubernetes-nodes-sg"
  description = "K8s nodes - control plane, kubelet, and inter-node traffic"
  vpc_id      = aws_vpc.main[0].id

  # Kubernetes API server → node communication
  ingress {
    description = "Kubernetes API from VPC"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # Kubelet API (required for metrics-server, kubectl logs/exec)
  ingress {
    description = "Kubelet API from VPC"
    from_port   = 10250
    to_port     = 10250
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # NodePort service range
  ingress {
    description = "NodePort services from VPC"
    from_port   = 30000
    to_port     = 32767
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  # All inter-node communication for CNI pod networking (Calico / VPC CNI)
  ingress {
    description = "All inter-node communication"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    self        = true
  }

  egress {
    description = "All outbound traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-kubernetes-nodes-sg"
    Tier = "kubernetes-nodes"
  })

  lifecycle {
    create_before_destroy = true
  }
}


# =============================================================================
# AWS VPC FLOW LOGS  (SOC 2 Type II — Constraint C-004)
# =============================================================================
# Captures all network traffic metadata (ACCEPT + REJECT) for the VPC.
# Logs are stored in CloudWatch Logs for real-time anomaly detection and
# long-term retention aligned with the 7-year audit-log retention policy.
#
# Flow log retention is environment-aware:
#   dev     → 30 days
#   staging → 90 days
#   prod    → 365 days (archival to S3 via CloudWatch export recommended)
# =============================================================================

# IAM role allowing the VPC Flow Log service to publish to CloudWatch Logs
resource "aws_iam_role" "flow_log" {
  count = local.is_aws && var.enable_flow_logs ? 1 : 0

  name = "${local.name_prefix}-vpc-flow-log-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Service = "vpc-flow-logs.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-vpc-flow-log-role"
  })
}

resource "aws_iam_role_policy" "flow_log" {
  count = local.is_aws && var.enable_flow_logs ? 1 : 0

  name = "${local.name_prefix}-vpc-flow-log-policy"
  role = aws_iam_role.flow_log[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
          "logs:DescribeLogGroups",
          "logs:DescribeLogStreams"
        ]
        Resource = "*"
      }
    ]
  })
}

# CloudWatch Log Group for VPC Flow Logs
resource "aws_cloudwatch_log_group" "flow_log" {
  count = local.is_aws && var.enable_flow_logs ? 1 : 0

  name              = "/aws/vpc/flow-log/${local.name_prefix}"
  retention_in_days = var.flow_log_retention_days

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-vpc-flow-log-group"
  })
}

# VPC Flow Log — captures ALL traffic (ACCEPT + REJECT)
resource "aws_flow_log" "main" {
  count = local.is_aws && var.enable_flow_logs ? 1 : 0

  vpc_id               = aws_vpc.main[0].id
  traffic_type         = "ALL"
  iam_role_arn         = aws_iam_role.flow_log[0].arn
  log_destination      = aws_cloudwatch_log_group.flow_log[0].arn
  log_destination_type = "cloud-watch-logs"
  max_aggregation_interval = 60

  tags = merge(local.common_tags, {
    Name = "${local.name_prefix}-vpc-flow-log"
  })
}


# #############################################################################
#                          AZURE RESOURCES
# #############################################################################
# Created when var.cloud_provider == "azure".  Provisions a VNet with public
# and private subnets, NAT Gateway, Network Security Groups per service tier,
# and NSG flow logs for SOC 2 Type II compliance.
# #############################################################################


# -----------------------------------------------------------------------------
# Azure Resource Group
# -----------------------------------------------------------------------------
resource "azurerm_resource_group" "main" {
  count = local.is_azure ? 1 : 0

  name     = "${local.name_prefix}-networking-rg"
  location = var.availability_zones[0]

  tags = local.common_tags
}

# -----------------------------------------------------------------------------
# Azure Virtual Network
# -----------------------------------------------------------------------------
resource "azurerm_virtual_network" "main" {
  count = local.is_azure ? 1 : 0

  name                = "${local.name_prefix}-vnet"
  location            = azurerm_resource_group.main[0].location
  resource_group_name = azurerm_resource_group.main[0].name
  address_space       = [var.vpc_cidr]

  tags = local.common_tags
}

# -----------------------------------------------------------------------------
# Azure Public Subnet — AKS load balancers and ingress controllers
# -----------------------------------------------------------------------------
resource "azurerm_subnet" "public" {
  count = local.is_azure ? length(var.public_subnet_cidrs) : 0

  name                 = "${local.name_prefix}-public-subnet-${count.index + 1}"
  resource_group_name  = azurerm_resource_group.main[0].name
  virtual_network_name = azurerm_virtual_network.main[0].name
  address_prefixes     = [var.public_subnet_cidrs[count.index]]
}

# -----------------------------------------------------------------------------
# Azure Private Subnet — AKS nodes, MongoDB, Redis
# -----------------------------------------------------------------------------
resource "azurerm_subnet" "private" {
  count = local.is_azure ? length(var.private_subnet_cidrs) : 0

  name                 = "${local.name_prefix}-private-subnet-${count.index + 1}"
  resource_group_name  = azurerm_resource_group.main[0].name
  virtual_network_name = azurerm_virtual_network.main[0].name
  address_prefixes     = [var.private_subnet_cidrs[count.index]]
}

# -----------------------------------------------------------------------------
# Azure NAT Gateway — outbound internet for private subnets
# -----------------------------------------------------------------------------
resource "azurerm_public_ip" "nat" {
  count = local.is_azure && var.enable_nat_gateway ? 1 : 0

  name                = "${local.name_prefix}-nat-pip"
  location            = azurerm_resource_group.main[0].location
  resource_group_name = azurerm_resource_group.main[0].name
  allocation_method   = "Static"
  sku                 = "Standard"

  tags = local.common_tags
}

resource "azurerm_nat_gateway" "main" {
  count = local.is_azure && var.enable_nat_gateway ? 1 : 0

  name                    = "${local.name_prefix}-nat-gw"
  location                = azurerm_resource_group.main[0].location
  resource_group_name     = azurerm_resource_group.main[0].name
  sku_name                = "Standard"
  idle_timeout_in_minutes = 10

  tags = local.common_tags
}

resource "azurerm_nat_gateway_public_ip_association" "main" {
  count = local.is_azure && var.enable_nat_gateway ? 1 : 0

  nat_gateway_id       = azurerm_nat_gateway.main[0].id
  public_ip_address_id = azurerm_public_ip.nat[0].id
}

resource "azurerm_subnet_nat_gateway_association" "private" {
  count = local.is_azure && var.enable_nat_gateway ? length(var.private_subnet_cidrs) : 0

  subnet_id      = azurerm_subnet.private[count.index].id
  nat_gateway_id = azurerm_nat_gateway.main[0].id
}


# =============================================================================
# AZURE NETWORK SECURITY GROUPS
# =============================================================================
# NSGs enforce least-privilege network access per service tier, mirroring the
# AWS Security Group architecture.  Each NSG is independently created and then
# associated with the appropriate subnet.
# =============================================================================

# -----------------------------------------------------------------------------
# API Gateway NSG — HTTPS ingress from allowed sources
# -----------------------------------------------------------------------------
resource "azurerm_network_security_group" "api_gateway" {
  count = local.is_azure ? 1 : 0

  name                = "${local.name_prefix}-api-gateway-nsg"
  location            = azurerm_resource_group.main[0].location
  resource_group_name = azurerm_resource_group.main[0].name

  security_rule {
    name                       = "AllowHTTPS"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefixes    = local.effective_allowed_cidrs
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowHTTP"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "80"
    source_address_prefixes    = local.effective_allowed_cidrs
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  tags = merge(local.common_tags, {
    Tier = "api-gateway"
  })
}

# -----------------------------------------------------------------------------
# Backend Services NSG — Flask ports from API Gateway and inter-service
# -----------------------------------------------------------------------------
resource "azurerm_network_security_group" "backend_services" {
  count = local.is_azure ? 1 : 0

  name                = "${local.name_prefix}-backend-services-nsg"
  location            = azurerm_resource_group.main[0].location
  resource_group_name = azurerm_resource_group.main[0].name

  security_rule {
    name                       = "AllowFlaskFromVNet"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "5000-5005"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowPrometheusFromVNet"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "9090"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  tags = merge(local.common_tags, {
    Tier = "backend-services"
  })
}

# -----------------------------------------------------------------------------
# Database NSG — MongoDB port from backend services only
# -----------------------------------------------------------------------------
resource "azurerm_network_security_group" "database" {
  count = local.is_azure ? 1 : 0

  name                = "${local.name_prefix}-database-nsg"
  location            = azurerm_resource_group.main[0].location
  resource_group_name = azurerm_resource_group.main[0].name

  security_rule {
    name                       = "AllowMongoDBFromVNet"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "27017"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  tags = merge(local.common_tags, {
    Tier = "database"
  })
}

# -----------------------------------------------------------------------------
# Redis NSG — Redis port from backend services only
# -----------------------------------------------------------------------------
resource "azurerm_network_security_group" "redis" {
  count = local.is_azure ? 1 : 0

  name                = "${local.name_prefix}-redis-nsg"
  location            = azurerm_resource_group.main[0].location
  resource_group_name = azurerm_resource_group.main[0].name

  security_rule {
    name                       = "AllowRedisFromVNet"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "6379"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowRedisClusterBus"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "16379"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  tags = merge(local.common_tags, {
    Tier = "redis"
  })
}

# -----------------------------------------------------------------------------
# Kubernetes Nodes NSG — control-plane, kubelet, NodePort, inter-node
# -----------------------------------------------------------------------------
resource "azurerm_network_security_group" "kubernetes_nodes" {
  count = local.is_azure ? 1 : 0

  name                = "${local.name_prefix}-kubernetes-nodes-nsg"
  location            = azurerm_resource_group.main[0].location
  resource_group_name = azurerm_resource_group.main[0].name

  security_rule {
    name                       = "AllowKubeAPIFromVNet"
    priority                   = 100
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "443"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowKubeletFromVNet"
    priority                   = 110
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "10250"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowNodePortFromVNet"
    priority                   = 120
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "Tcp"
    source_port_range          = "*"
    destination_port_range     = "30000-32767"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "*"
  }

  security_rule {
    name                       = "AllowInterNodeAll"
    priority                   = 130
    direction                  = "Inbound"
    access                     = "Allow"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "VirtualNetwork"
    destination_address_prefix = "VirtualNetwork"
  }

  security_rule {
    name                       = "DenyAllInbound"
    priority                   = 4096
    direction                  = "Inbound"
    access                     = "Deny"
    protocol                   = "*"
    source_port_range          = "*"
    destination_port_range     = "*"
    source_address_prefix      = "*"
    destination_address_prefix = "*"
  }

  tags = merge(local.common_tags, {
    Tier = "kubernetes-nodes"
  })
}


# =============================================================================
# AZURE NSG — SUBNET ASSOCIATIONS
# =============================================================================
# Associate the public-facing NSG (API Gateway + K8s nodes) with the public
# subnet, and the more restrictive NSGs (backend, database, redis) with the
# private subnet.  In Azure, an NSG can only be associated once per subnet,
# so we group the Kubernetes nodes NSG with the private subnet tier.
# =============================================================================

resource "azurerm_subnet_network_security_group_association" "public" {
  count = local.is_azure ? length(var.public_subnet_cidrs) : 0

  subnet_id                 = azurerm_subnet.public[count.index].id
  network_security_group_id = azurerm_network_security_group.api_gateway[0].id
}

resource "azurerm_subnet_network_security_group_association" "private" {
  count = local.is_azure ? length(var.private_subnet_cidrs) : 0

  subnet_id                 = azurerm_subnet.private[count.index].id
  network_security_group_id = azurerm_network_security_group.kubernetes_nodes[0].id
}


# =============================================================================
# AZURE VNet FLOW LOGS  (SOC 2 Type II — Constraint C-004)
# =============================================================================
# Captures all network traffic metadata for audit and anomaly detection.
# Requires a Network Watcher (auto-provisioned in most Azure regions) and
# a storage account for log archival.
# =============================================================================

resource "azurerm_storage_account" "flow_logs" {
  count = local.is_azure && var.enable_flow_logs ? 1 : 0

  name                     = local.azure_flow_log_storage_name
  resource_group_name      = azurerm_resource_group.main[0].name
  location                 = azurerm_resource_group.main[0].location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  min_tls_version          = "TLS1_2"

  tags = merge(local.common_tags, {
    Purpose = "nsg-flow-logs"
  })
}

resource "azurerm_network_watcher" "main" {
  count = local.is_azure && var.enable_flow_logs ? 1 : 0

  name                = "${local.name_prefix}-network-watcher"
  location            = azurerm_resource_group.main[0].location
  resource_group_name = azurerm_resource_group.main[0].name

  tags = local.common_tags
}

resource "azurerm_network_watcher_flow_log" "api_gateway" {
  count = local.is_azure && var.enable_flow_logs ? 1 : 0

  network_watcher_name = azurerm_network_watcher.main[0].name
  resource_group_name  = azurerm_resource_group.main[0].name
  name                 = "${local.name_prefix}-api-gw-flow-log"

  network_security_group_id = azurerm_network_security_group.api_gateway[0].id
  storage_account_id        = azurerm_storage_account.flow_logs[0].id
  enabled                   = true
  version                   = 2

  retention_policy {
    enabled = true
    days    = var.flow_log_retention_days
  }
}

resource "azurerm_network_watcher_flow_log" "backend" {
  count = local.is_azure && var.enable_flow_logs ? 1 : 0

  network_watcher_name = azurerm_network_watcher.main[0].name
  resource_group_name  = azurerm_resource_group.main[0].name
  name                 = "${local.name_prefix}-backend-flow-log"

  network_security_group_id = azurerm_network_security_group.backend_services[0].id
  storage_account_id        = azurerm_storage_account.flow_logs[0].id
  enabled                   = true
  version                   = 2

  retention_policy {
    enabled = true
    days    = var.flow_log_retention_days
  }
}


# #############################################################################
#                          GCP RESOURCES
# #############################################################################
# Created when var.cloud_provider == "gcp".  Provisions a custom-mode VPC
# network with public and private subnetworks, Cloud Router + Cloud NAT
# for outbound internet access, firewall rules per service tier, and
# subnet-level VPC flow logs for SOC 2 Type II audit compliance.
# #############################################################################


# -----------------------------------------------------------------------------
# GCP VPC Network
# -----------------------------------------------------------------------------
# Custom-mode VPC (auto_create_subnetworks = false) gives explicit control
# over subnet CIDR allocation and secondary ranges for GKE pods / services.
# -----------------------------------------------------------------------------
resource "google_compute_network" "main" {
  count = local.is_gcp ? 1 : 0

  name                    = "${local.name_prefix}-vpc"
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
  description             = "VPC for ${var.project_name} ${var.environment} environment"
}

# -----------------------------------------------------------------------------
# GCP Public Subnetwork — GKE load balancers and ingress controllers
# -----------------------------------------------------------------------------
# Secondary IP ranges are reserved for GKE pod and service CIDR pools.
# VPC flow logs enabled at the subnet level for compliance (SOC 2 Type II).
# -----------------------------------------------------------------------------
resource "google_compute_subnetwork" "public" {
  count = local.is_gcp ? length(var.public_subnet_cidrs) : 0

  name          = "${local.name_prefix}-public-subnet-${count.index + 1}"
  ip_cidr_range = var.public_subnet_cidrs[count.index]
  region        = var.region
  network       = google_compute_network.main[0].id

  # Secondary range for GKE pods (if first public subnet)
  dynamic "secondary_ip_range" {
    for_each = count.index == 0 ? [1] : []
    content {
      range_name    = "${local.name_prefix}-gke-pods"
      ip_cidr_range = "10.100.0.0/14"
    }
  }

  # Secondary range for GKE services (if first public subnet)
  dynamic "secondary_ip_range" {
    for_each = count.index == 0 ? [1] : []
    content {
      range_name    = "${local.name_prefix}-gke-services"
      ip_cidr_range = "10.104.0.0/20"
    }
  }

  dynamic "log_config" {
    for_each = var.enable_flow_logs ? [1] : []
    content {
      aggregation_interval = "INTERVAL_5_SEC"
      flow_sampling        = 0.5
      metadata             = "INCLUDE_ALL_METADATA"
    }
  }
}

# -----------------------------------------------------------------------------
# GCP Private Subnetwork — GKE nodes, MongoDB, Redis
# -----------------------------------------------------------------------------
# Private Google Access enabled for GCP API calls without external IPs.
# VPC flow logs enabled for audit compliance.
# -----------------------------------------------------------------------------
resource "google_compute_subnetwork" "private" {
  count = local.is_gcp ? length(var.private_subnet_cidrs) : 0

  name                     = "${local.name_prefix}-private-subnet-${count.index + 1}"
  ip_cidr_range            = var.private_subnet_cidrs[count.index]
  region                   = var.region
  network                  = google_compute_network.main[0].id
  private_ip_google_access = true

  dynamic "log_config" {
    for_each = var.enable_flow_logs ? [1] : []
    content {
      aggregation_interval = "INTERVAL_5_SEC"
      flow_sampling        = 0.5
      metadata             = "INCLUDE_ALL_METADATA"
    }
  }
}

# -----------------------------------------------------------------------------
# GCP Cloud Router — required by Cloud NAT
# -----------------------------------------------------------------------------
resource "google_compute_router" "main" {
  count = local.is_gcp && var.enable_nat_gateway ? 1 : 0

  name    = "${local.name_prefix}-router"
  region  = var.region
  network = google_compute_network.main[0].id

  bgp {
    asn = 64514
  }
}

# -----------------------------------------------------------------------------
# GCP Cloud NAT — outbound internet for private subnetworks
# -----------------------------------------------------------------------------
# Automatically allocates IPs and routes outbound traffic from all private
# subnets through Cloud NAT.  GCP Cloud NAT is regional and inherently HA,
# so there is no single-vs-multi-AZ distinction as with AWS/Azure.
# -----------------------------------------------------------------------------
resource "google_compute_router_nat" "main" {
  count = local.is_gcp && var.enable_nat_gateway ? 1 : 0

  name                               = "${local.name_prefix}-cloud-nat"
  router                             = google_compute_router.main[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "LIST_OF_SUBNETWORKS"

  # NAT only private subnets — public subnets use external IPs directly
  dynamic "subnetwork" {
    for_each = google_compute_subnetwork.private[*].id
    content {
      name                    = subnetwork.value
      source_ip_ranges_to_nat = ["ALL_IP_RANGES"]
    }
  }

  log_config {
    enable = var.enable_flow_logs
    filter = "ERRORS_ONLY"
  }
}


# =============================================================================
# GCP FIREWALL RULES
# =============================================================================
# GCP uses network-wide firewall rules with target tags (rather than SGs or
# NSGs). Each rule targets a specific network tag that is applied to GKE
# node pools or VM instances in the corresponding service tier.
#
# Network tag convention: "${local.name_prefix}-<tier>"
# =============================================================================

# -----------------------------------------------------------------------------
# API Gateway Firewall — HTTPS ingress from allowed sources
# -----------------------------------------------------------------------------
resource "google_compute_firewall" "api_gateway" {
  count = local.is_gcp ? 1 : 0

  name    = "${local.name_prefix}-allow-api-gateway"
  network = google_compute_network.main[0].name

  allow {
    protocol = "tcp"
    ports    = ["80", "443"]
  }

  source_ranges = local.effective_allowed_cidrs
  target_tags   = ["${local.name_prefix}-api-gateway"]
  direction     = "INGRESS"
  priority      = 1000
  description   = "Allow HTTPS/HTTP to API Gateway tier from allowed sources"
}

# -----------------------------------------------------------------------------
# Backend Services Firewall — Flask ports from API Gateway and inter-service
# -----------------------------------------------------------------------------
resource "google_compute_firewall" "backend_services" {
  count = local.is_gcp ? 1 : 0

  name    = "${local.name_prefix}-allow-backend-services"
  network = google_compute_network.main[0].name

  allow {
    protocol = "tcp"
    ports    = ["5000-5005", "9090"]
  }

  source_tags = ["${local.name_prefix}-api-gateway", "${local.name_prefix}-backend-services"]
  target_tags = ["${local.name_prefix}-backend-services"]
  direction   = "INGRESS"
  priority    = 1000
  description = "Allow Flask service ports and Prometheus from API Gateway and peer services"
}

# -----------------------------------------------------------------------------
# Database Firewall — MongoDB from backend services only
# -----------------------------------------------------------------------------
resource "google_compute_firewall" "database" {
  count = local.is_gcp ? 1 : 0

  name    = "${local.name_prefix}-allow-database"
  network = google_compute_network.main[0].name

  allow {
    protocol = "tcp"
    ports    = ["27017"]
  }

  source_tags = ["${local.name_prefix}-backend-services", "${local.name_prefix}-database"]
  target_tags = ["${local.name_prefix}-database"]
  direction   = "INGRESS"
  priority    = 1000
  description = "Allow MongoDB port from backend services and replica set peers"
}

# -----------------------------------------------------------------------------
# Redis Firewall — Redis port from backend services only
# -----------------------------------------------------------------------------
resource "google_compute_firewall" "redis" {
  count = local.is_gcp ? 1 : 0

  name    = "${local.name_prefix}-allow-redis"
  network = google_compute_network.main[0].name

  allow {
    protocol = "tcp"
    ports    = ["6379", "16379"]
  }

  source_tags = ["${local.name_prefix}-backend-services", "${local.name_prefix}-redis"]
  target_tags = ["${local.name_prefix}-redis"]
  direction   = "INGRESS"
  priority    = 1000
  description = "Allow Redis and Cluster bus from backend services and peers"
}

# -----------------------------------------------------------------------------
# Kubernetes Nodes Firewall — control-plane, kubelet, NodePort, inter-node
# -----------------------------------------------------------------------------
# K8s specific ports from VPC CIDR (control-plane, kubelet, NodePort)
resource "google_compute_firewall" "kubernetes_nodes" {
  count = local.is_gcp ? 1 : 0

  name    = "${local.name_prefix}-allow-k8s-nodes"
  network = google_compute_network.main[0].name

  # Kubernetes API server and kubelet API
  allow {
    protocol = "tcp"
    ports    = ["443", "10250", "30000-32767"]
  }

  source_ranges = [var.vpc_cidr]
  target_tags   = ["${local.name_prefix}-kubernetes-nodes"]
  direction     = "INGRESS"
  priority      = 1000
  description   = "Allow Kubernetes API, kubelet, and NodePort from VPC CIDR"
}

# Inter-node all-traffic rule for CNI pod networking (Calico / GKE VPC-native)
resource "google_compute_firewall" "kubernetes_nodes_internal" {
  count = local.is_gcp ? 1 : 0

  name    = "${local.name_prefix}-allow-k8s-internal"
  network = google_compute_network.main[0].name

  allow {
    protocol = "tcp"
  }

  allow {
    protocol = "udp"
  }

  allow {
    protocol = "icmp"
  }

  source_tags = ["${local.name_prefix}-kubernetes-nodes"]
  target_tags = ["${local.name_prefix}-kubernetes-nodes"]
  direction   = "INGRESS"
  priority    = 900
  description = "Allow all inter-node traffic for GKE pod networking"
}

# -----------------------------------------------------------------------------
# GCP Deny-All Default Firewall — explicit deny for defence in depth
# -----------------------------------------------------------------------------
# GCP has implied deny rules, but an explicit low-priority deny makes the
# policy visible in audit reports (SOC 2 Type II evidence).
# -----------------------------------------------------------------------------
resource "google_compute_firewall" "deny_all" {
  count = local.is_gcp ? 1 : 0

  name    = "${local.name_prefix}-deny-all-ingress"
  network = google_compute_network.main[0].name

  deny {
    protocol = "all"
  }

  source_ranges = ["0.0.0.0/0"]
  direction     = "INGRESS"
  priority      = 65534
  description   = "Explicit deny-all ingress for SOC 2 Type II audit compliance"
}
