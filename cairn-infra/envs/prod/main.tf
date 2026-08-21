/**
 * Production environment.
 *
 * Terraform and the Kubernetes manifests live in separate repos with separate
 * lifecycles on purpose. This runs at human speed — VPCs change quarterly —
 * while ArgoCD runs at commit speed. Merging them means every prompt tweak
 * triggers a plan against your VPC, which is how a five-minute feedback loop
 * and a cluster nobody wants to touch happen.
 */

terraform {
  required_version = ">= 1.9"

  backend "s3" {
    bucket         = "cairn-tfstate"
    key            = "prod/terraform.tfstate"
    region         = "eu-west-1"
    dynamodb_table = "cairn-tflock"
    encrypt        = true
  }

  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    kubectl = { source = "gavinbunney/kubectl", version = "~> 1.14" }
    tls     = { source = "hashicorp/tls", version = "~> 4.0" }
    random  = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

locals {
  env    = "prod"
  name   = "cairn-prod"
  region = "eu-west-1"

  tags = {
    Project     = "cairn"
    Environment = local.env
    ManagedBy   = "terraform"
    Owner       = "platform"
  }
}

provider "aws" {
  region = local.region
  default_tags { tags = local.tags }
}

data "aws_caller_identity" "current" {}

provider "kubectl" {
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_ca)
  load_config_file       = false
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks.cluster_name]
  }
}

module "s3" {
  source = "../../modules/s3"
  env    = local.env
  tags   = local.tags
}

module "vpc" {
  source       = "../../modules/vpc"
  name         = local.name
  cluster_name = local.name
  region       = local.region
  cidr         = "10.0.0.0/16"

  # One NAT per AZ in prod: an AZ outage should not take egress with it.
  single_nat_gateway   = false
  flow_logs_enabled    = true
  flow_logs_bucket_arn = "arn:aws:s3:::cairn-audit-${local.env}"

  tags = local.tags
}

module "eks" {
  source             = "../../modules/eks"
  cluster_name       = local.name
  private_subnet_ids = module.vpc.private_subnet_ids
  kubernetes_version = "1.31"

  # Private API only. Access via the VPN.
  endpoint_public_access = false

  tags = local.tags
}

module "karpenter" {
  source            = "../../modules/karpenter"
  cluster_name      = module.eks.cluster_name
  region            = local.region
  account_id        = data.aws_caller_identity.current.account_id
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url
  node_role_arn     = module.eks.node_role_arn
  node_role_name    = module.eks.node_role_name

  # Hard ceiling. This number is the cost blast radius of a scaling bug.
  max_gpus = 3

  tags = local.tags
}

module "rds" {
  source                    = "../../modules/rds"
  name                      = local.name
  env                       = local.env
  vpc_id                    = module.vpc.vpc_id
  data_subnet_ids           = module.vpc.data_subnet_ids
  cluster_security_group_id = module.eks.cluster_security_group_id
  kms_key_id                = module.s3.kms_key_id

  # Tier C: trajectory reads and eval queries move off the writer.
  instance_class = "db.r6g.large"
  multi_az       = true
  read_replica   = true

  backup_retention_days = 14
  deletion_protection   = true

  tags = local.tags
}

module "irsa" {
  source            = "../../modules/irsa"
  env               = local.env
  region            = local.region
  cluster_name      = module.eks.cluster_name
  oidc_provider_arn = module.eks.oidc_provider_arn
  oidc_provider_url = module.eks.oidc_provider_url
  kms_key_arn       = module.s3.kms_key_arn
  artifacts_bucket  = module.s3.artifacts_bucket
  policy_bucket     = module.s3.policy_bucket

  tags = local.tags
}

resource "aws_elasticache_subnet_group" "redis" {
  name       = "${local.name}-redis"
  subnet_ids = module.vpc.data_subnet_ids
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id       = "${local.name}-redis"
  description                = "Cairn cache, rate limits and the SSE event bus"
  engine                     = "redis"
  engine_version             = "7.1"
  node_type                  = "cache.t4g.small"
  num_cache_clusters         = 2
  automatic_failover_enabled = true

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  transit_encryption_enabled = true

  # Events and rate-limit counters are reconstructible; the trajectory in
  # Postgres is the record. Losing this cache costs a reconnect, not data.
  snapshot_retention_limit = 0

  tags = local.tags
}

resource "aws_security_group" "redis" {
  name   = "${local.name}-redis"
  vpc_id = module.vpc.vpc_id

  ingress {
    description     = "Redis from the cluster"
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [module.eks.cluster_security_group_id]
  }

  tags = local.tags
}

# --------------------------------------------------------------- outputs

output "cluster_name" { value = module.eks.cluster_name }
output "database_address" { value = module.rds.address }
output "redis_address" { value = aws_elasticache_replication_group.redis.primary_endpoint_address }
output "artifacts_bucket" { value = module.s3.artifacts_bucket }
output "policy_bucket" { value = module.s3.policy_bucket }

output "helm_values" {
  description = "Paste into cairn-deploy/values/prod.yaml when infra changes."
  value = {
    database       = { host = module.rds.address, port = 5432 }
    redis          = { host = aws_elasticache_replication_group.redis.primary_endpoint_address, port = 6379 }
    s3             = { bucket = module.s3.artifacts_bucket, kmsKeyId = module.s3.kms_key_arn }
    policy         = { bundleUrl = "s3://${module.s3.policy_bucket}/bundles/cairn.tar.gz" }
    network        = { databaseCidr = module.vpc.data_subnet_cidrs[0] }
    serviceAccount = { annotations = module.irsa.role_arns }
  }
}
