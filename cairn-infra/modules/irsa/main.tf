/**
 * One IAM role per service account, scoped to exactly what that service
 * needs.
 *
 * The point of the exercise is the Anthropic key: readable by cairn-router
 * and by nothing else. If the orchestrator is compromised through prompt
 * injection it cannot exfiltrate a credential it was never able to read.
 */

locals {
  # secrets: Secrets Manager name prefixes this role may read
  # s3:      buckets it may read/write
  # extra:   any additional statements
  services = {
    gateway = {
      secrets = ["cairn/${var.env}/database-dsn", "cairn/${var.env}/internal-jwt-*"]
      s3      = []
    }
    orchestrator = {
      secrets = ["cairn/${var.env}/database-dsn", "cairn/${var.env}/internal-jwt-*"]
      s3      = [var.artifacts_bucket]
    }
    router = {
      # The only role in the account that can read this.
      secrets = ["cairn/${var.env}/anthropic-api-key", "cairn/${var.env}/internal-jwt-*"]
      s3      = []
    }
    approval = {
      secrets = [
        "cairn/${var.env}/database-dsn",
        "cairn/${var.env}/internal-jwt-*",
        "cairn/${var.env}/slack-*",
      ]
      s3 = []
    }
    "mcp-observability" = {
      secrets = ["cairn/${var.env}/database-dsn", "cairn/${var.env}/internal-jwt-*"]
      s3      = [var.artifacts_bucket]
    }
    "mcp-runbooks" = {
      secrets = ["cairn/${var.env}/database-dsn", "cairn/${var.env}/internal-jwt-*"]
      s3      = []
    }
    "mcp-actions" = {
      secrets = [
        "cairn/${var.env}/database-dsn",
        "cairn/${var.env}/internal-jwt-*",
        "cairn/${var.env}/jira-token",
        "cairn/${var.env}/argocd-token",
        "cairn/${var.env}/slack-bot-token",
      ]
      s3 = []
    }
    ui = {
      secrets = []
      s3      = []
    }
    vllm = {
      secrets = []
      s3      = []
    }
  }
}

data "aws_caller_identity" "current" {}

resource "aws_iam_role" "service" {
  for_each = local.services

  name = "cairn-${each.key}-${var.env}"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          # Bound to one namespace and one service account. A role that any
          # pod can assume is not a boundary.
          "${var.oidc_provider_url}:sub" = "system:serviceaccount:${var.namespace}:cairn-${each.key}"
          "${var.oidc_provider_url}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
  tags = merge(var.tags, { service = each.key })
}

resource "aws_iam_role_policy" "secrets" {
  for_each = { for k, v in local.services : k => v if length(v.secrets) > 0 }

  name = "secrets"
  role = aws_iam_role.service[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [
          for name in each.value.secrets :
          "arn:aws:secretsmanager:${var.region}:${data.aws_caller_identity.current.account_id}:secret:${name}*"
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.kms_key_arn
      },
    ]
  })
}

resource "aws_iam_role_policy" "artifacts" {
  for_each = { for k, v in local.services : k => v if length(v.s3) > 0 }

  name = "artifacts"
  role = aws_iam_role.service[each.key].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = [for b in each.value.s3 : "arn:aws:s3:::${b}/artifacts/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = [for b in each.value.s3 : "arn:aws:s3:::${b}"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = var.kms_key_arn
      },
    ]
  })
}

# mcp-actions is the only role that may change anything in the cluster, and
# only through the approval path. Scaling is namespaced and read-limited.
resource "aws_iam_role_policy" "actions_extra" {
  name = "read-deploy-metadata"
  role = aws_iam_role.service["mcp-actions"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["eks:DescribeCluster"]
      Resource = "arn:aws:eks:${var.region}:${data.aws_caller_identity.current.account_id}:cluster/${var.cluster_name}"
    }]
  })
}

# The OPA bundle lives in S3 and every tool server reads it.
resource "aws_iam_role_policy" "policy_bundle" {
  for_each = toset(["mcp-observability", "mcp-runbooks", "mcp-actions"])

  name = "policy-bundle"
  role = aws_iam_role.service[each.value].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["s3:GetObject"]
      Resource = "arn:aws:s3:::${var.policy_bucket}/bundles/*"
    }]
  })
}
