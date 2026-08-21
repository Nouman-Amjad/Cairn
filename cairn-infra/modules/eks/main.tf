/**
 * EKS with a small on-demand system node group and everything else on
 * Karpenter.
 *
 * Karpenter rather than Cluster Autoscaler because it provisions in ~45s
 * against the managed node group's ~3 minutes. For a workload with 90-second
 * GPU cold starts, that two-minute difference is the whole user experience.
 */

locals {
  tags = merge(var.tags, { "karpenter.sh/discovery" = var.cluster_name })
}

resource "aws_kms_key" "eks" {
  description             = "EKS secrets envelope encryption for ${var.cluster_name}"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = var.tags
}

resource "aws_eks_cluster" "this" {
  name     = var.cluster_name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_private_access = true
    # The API server is not on the internet. Access is via the VPN or a
    # bastion; a public endpoint with an allow-list is one firewall rule
    # away from being neither.
    endpoint_public_access = var.endpoint_public_access
    public_access_cidrs    = var.public_access_cidrs
  }

  encryption_config {
    provider { key_arn = aws_kms_key.eks.arn }
    resources = ["secrets"]
  }

  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = false
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator"]

  tags       = local.tags
  depends_on = [aws_iam_role_policy_attachment.cluster]
}

resource "aws_iam_role" "cluster" {
  name = "${var.cluster_name}-cluster"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

# ------------------------------------------------------------ system nodes

resource "aws_iam_role" "node" {
  name = "${var.cluster_name}-node"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}

resource "aws_eks_node_group" "system" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "system"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  # On-demand, deliberately: CoreDNS, Karpenter itself and the controllers
  # cannot live on the thing that provisions capacity.
  capacity_type  = "ON_DEMAND"
  instance_types = ["m6a.large"]

  scaling_config {
    desired_size = 2
    min_size     = 2
    max_size     = 4
  }

  update_config { max_unavailable = 1 }

  labels = { workload = "system" }
  tags   = local.tags

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }
}

# --------------------------------------------------------------- addons

resource "aws_eks_addon" "this" {
  for_each = {
    vpc-cni            = { version = var.addon_versions.vpc_cni }
    coredns            = { version = var.addon_versions.coredns }
    kube-proxy         = { version = var.addon_versions.kube_proxy }
    aws-ebs-csi-driver = { version = var.addon_versions.ebs_csi }
  }

  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  addon_version               = each.value.version
  resolve_conflicts_on_update = "PRESERVE"

  # Pod-to-pod encryption without a service mesh. See docs/security.md for
  # why a mesh was rejected at this team size.
  configuration_values = each.key == "vpc-cni" ? jsonencode({
    env = {
      ENABLE_POD_ENI                    = "true"
      ENABLE_PREFIX_DELEGATION          = "true"
      POD_SECURITY_GROUP_ENFORCING_MODE = "standard"
    }
  }) : null

  depends_on = [aws_eks_node_group.system]
  tags       = var.tags
}

data "tls_certificate" "oidc" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "this" {
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.oidc.certificates[0].sha1_fingerprint]
  tags            = var.tags
}
