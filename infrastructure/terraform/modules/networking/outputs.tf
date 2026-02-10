# =============================================================================
# Terraform Networking Module — Output Definitions
# =============================================================================
# Exposes key network resource identifiers to downstream Terraform modules
# (kubernetes, database, storage, security). Each output uses conditional logic
# to return the appropriate resource attribute based on the active cloud
# provider (AWS, Azure, or GCP).
#
# The try() function is used for single-value outputs so that only the
# provider whose resources actually exist (count > 0) contributes a value.
# For list outputs, concat() of splat expressions is used — splat on a
# resource with count=0 yields an empty list, so only the active provider's
# resources contribute elements.
#
# Consumed by:
#   infrastructure/terraform/main.tf  → passes to kubernetes, database,
#                                        storage, and security modules
#
# Project: Synthetic-ERP-Data-Generation-Platform
# Module:  networking
# =============================================================================

# -----------------------------------------------------------------------------
# VPC / VNet Identifier
# -----------------------------------------------------------------------------
# The primary virtual network identifier required by every downstream module
# for resource placement. All Kubernetes clusters, database instances, cache
# nodes, and storage endpoints are deployed within this network boundary.
#
# Provider mapping:
#   AWS   → aws_vpc.main[0].id              (VPC ID, e.g. vpc-0abc123…)
#   Azure → azurerm_virtual_network.main[0].id (Resource ID)
#   GCP   → google_compute_network.main[0].id  (Self-link URI)
# -----------------------------------------------------------------------------
output "vpc_id" {
  description = "The ID of the VPC/VNet created for the platform"

  value = try(
    aws_vpc.main[0].id,
    azurerm_virtual_network.main[0].id,
    google_compute_network.main[0].id,
    ""
  )
}

# -----------------------------------------------------------------------------
# Public Subnet Identifiers
# -----------------------------------------------------------------------------
# List of public subnet IDs across availability zones. Public subnets host
# internet-facing resources:
#   - NGINX Ingress Controller (Kubernetes Ingress)
#   - External Application Load Balancers / Cloud Load Balancers
#   - Bastion hosts (if deployed for troubleshooting)
#
# These subnets have direct routes to an Internet Gateway (AWS), are
# associated with public IP configurations (Azure), or have external IP
# access enabled (GCP).
#
# Provider mapping:
#   AWS   → aws_subnet.public[*].id
#   Azure → azurerm_subnet.public[*].id
#   GCP   → google_compute_subnetwork.public[*].id
#
# Note: concat() merges the splat results; only the active provider
# contributes non-empty elements since inactive resources have count=0.
# -----------------------------------------------------------------------------
output "public_subnet_ids" {
  description = "List of public subnet IDs for load balancers and ingress controllers"

  value = concat(
    aws_subnet.public[*].id,
    azurerm_subnet.public[*].id,
    google_compute_subnetwork.public[*].id
  )
}

# -----------------------------------------------------------------------------
# Private Subnet Identifiers
# -----------------------------------------------------------------------------
# List of private subnet IDs across availability zones. Private subnets host
# all internal platform workloads:
#   - Kubernetes worker nodes (EKS/AKS/GKE node pools)
#   - MongoDB 7.0 StatefulSet instances
#   - Redis 7.x cache cluster nodes
#   - All six backend microservices (API Gateway, Generation Engine,
#     Profiling Service, Quality Service, Compliance Service, Provisioning
#     Service)
#
# Private subnets have no direct internet-routable addresses; outbound
# traffic flows through NAT Gateways (when enabled) for container image
# pulls, metrics export, and external API calls.
#
# Provider mapping:
#   AWS   → aws_subnet.private[*].id
#   Azure → azurerm_subnet.private[*].id
#   GCP   → google_compute_subnetwork.private[*].id
# -----------------------------------------------------------------------------
output "private_subnet_ids" {
  description = "List of private subnet IDs for Kubernetes nodes and databases"

  value = concat(
    aws_subnet.private[*].id,
    azurerm_subnet.private[*].id,
    google_compute_subnetwork.private[*].id
  )
}

# -----------------------------------------------------------------------------
# NAT Gateway Identifiers
# -----------------------------------------------------------------------------
# List of NAT Gateway identifiers providing outbound internet access from
# private subnets. NAT Gateways allow Kubernetes nodes and backend services
# to reach external endpoints (container registries, Auth0, cloud APIs)
# without exposing private IP addresses.
#
# Topology varies by configuration:
#   - single_nat_gateway=true  → One NAT Gateway shared across all AZs
#                                 (cost-effective for dev/staging)
#   - single_nat_gateway=false → One NAT Gateway per AZ for high availability
#                                 (recommended for production)
#
# May be empty if enable_nat_gateway=false (air-gapped deployments per C-003
# where a private registry mirror eliminates the need for outbound internet).
#
# Provider mapping:
#   AWS   → aws_nat_gateway.main[*].id
#   Azure → azurerm_nat_gateway.main[*].id
#   GCP   → google_compute_router_nat.main[*].name (GCP Cloud NAT uses name)
# -----------------------------------------------------------------------------
output "nat_gateway_ids" {
  description = "NAT Gateway IDs for outbound internet access from private subnets"

  value = concat(
    aws_nat_gateway.main[*].id,
    azurerm_nat_gateway.main[*].id,
    google_compute_router_nat.main[*].name
  )
}

# -----------------------------------------------------------------------------
# Security Group / NSG / Firewall Rule Identifiers
# -----------------------------------------------------------------------------
# Map of security group identifiers keyed by service tier. Each tier enforces
# least-privilege network access rules aligned with the platform's security
# architecture (Section 6.4 — Network Zones):
#
#   api_gateway      — Ingress HTTPS (443) from allowed_cidr_blocks;
#                      egress to backend_services tier
#   backend_services — Ingress from api_gateway on service ports;
#                      egress to database and redis tiers
#   database         — Ingress MongoDB (27017) from backend_services only
#   redis            — Ingress Redis (6379) from backend_services only
#   kubernetes_nodes — Ingress from Kubernetes control plane;
#                      inter-node communication for pod networking
#
# Downstream modules (kubernetes, database) attach these security groups /
# NSGs to their respective compute and data resources.
#
# Provider mapping:
#   AWS   → aws_security_group.<tier>[0].id
#   Azure → azurerm_network_security_group.<tier>[0].id
#   GCP   → google_compute_firewall.<tier>[0].name
#
# Note: GCP firewall rules are referenced by name rather than numeric ID,
# as GCP uses network tags and firewall rule names for traffic control.
# -----------------------------------------------------------------------------
output "security_group_ids" {
  description = "Map of security group/NSG IDs keyed by service tier for assignment to compute resources"

  value = {
    api_gateway = try(
      aws_security_group.api_gateway[0].id,
      azurerm_network_security_group.api_gateway[0].id,
      google_compute_firewall.api_gateway[0].name,
      ""
    )

    backend_services = try(
      aws_security_group.backend_services[0].id,
      azurerm_network_security_group.backend_services[0].id,
      google_compute_firewall.backend_services[0].name,
      ""
    )

    database = try(
      aws_security_group.database[0].id,
      azurerm_network_security_group.database[0].id,
      google_compute_firewall.database[0].name,
      ""
    )

    redis = try(
      aws_security_group.redis[0].id,
      azurerm_network_security_group.redis[0].id,
      google_compute_firewall.redis[0].name,
      ""
    )

    kubernetes_nodes = try(
      aws_security_group.kubernetes_nodes[0].id,
      azurerm_network_security_group.kubernetes_nodes[0].id,
      google_compute_firewall.kubernetes_nodes[0].name,
      ""
    )
  }
}

# -----------------------------------------------------------------------------
# VPC / VNet CIDR Block
# -----------------------------------------------------------------------------
# The primary CIDR block assigned to the virtual network, exported for
# reference by other modules that need to know the overall address space
# (e.g., security module for network ACLs, database module for private
# endpoint configuration).
#
# Provider mapping:
#   AWS   → aws_vpc.main[0].cidr_block         (e.g. "10.0.0.0/16")
#   Azure → azurerm_virtual_network.main[0].address_space[0]
#   GCP   → Falls back to var.vpc_cidr because google_compute_network
#            with auto_create_subnetworks=false does not carry a top-level
#            CIDR; individual subnets define their own ranges.
# -----------------------------------------------------------------------------
output "vpc_cidr_block" {
  description = "The CIDR block of the VPC/VNet"

  value = try(
    aws_vpc.main[0].cidr_block,
    azurerm_virtual_network.main[0].address_space[0],
    var.vpc_cidr
  )
}
