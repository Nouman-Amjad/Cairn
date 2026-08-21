/**
 * Development environment.
 *
 * Same modules, cheaper knobs. The differences are all cost decisions and are
 * listed here rather than hidden in a variables file, because "why is dev
 * different" is the question everyone asks six months later.
 */

terraform {
  required_version = ">= 1.9"

  backend "s3" {
    bucket         = "cairn-tfstate"
    key            = "dev/terraform.tfstate"
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
  env    = "dev"
  name   = "cairn-dev"
  region = "eu-west-1"

  tags = {
    Project     = "cairn"
    Environment = local.env
    ManagedBy   = "terraform"
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
  cidr         = "10.1.0.0/16"

  # One NAT instead of three: saves ~$65/mo and costs AZ independence, which
  # dev does not need.
  single_nat_gateway = true
  flow_logs_enabled  = false

  tags = local.tags
}

module "eks" {
  source             = "../../modules/eks"
  cluster_name       = local.name
  private_subnet_ids = module.vpc.private_subnet_ids

  # Public endpoint, restricted to the office range, so a laptop can reach it
  # without the VPN. Not acceptable in prod.
  endpoint_public_access = true
  public_access_cidrs    = var.admin_cidrs

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

  # One GPU, scale-to-zero. A dev cluster that can spin up three A10Gs is a
  # dev cluster that will, once, over a weekend.
  max_gpus = 1

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

  instance_class        = "db.t4g.micro"
  multi_az              = false
  backup_retention_days = 1
  deletion_protection   = false

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

resource "aws_elasticache_cluster" "redis" {
  cluster_id         = "${local.name}-redis"
  engine             = "redis"
  engine_version     = "7.1"
  node_type          = "cache.t4g.micro"
  num_cache_nodes    = 1
  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]
  tags               = local.tags
}

resource "aws_security_group" "redis" {
  name   = "${local.name}-redis"
  vpc_id = module.vpc.vpc_id

  ingress {
    from_port       = 6379
    to_port         = 6379
    protocol        = "tcp"
    security_groups = [module.eks.cluster_security_group_id]
  }

  tags = local.tags
}

output "cluster_name" { value = module.eks.cluster_name }
output "database_address" { value = module.rds.address }
output "redis_address" { value = aws_elasticache_cluster.redis.cache_nodes[0].address }
