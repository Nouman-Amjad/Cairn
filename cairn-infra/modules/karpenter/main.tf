/**
 * Karpenter: IAM, the controller's IRSA role, and the NodePools.
 *
 * The GPU pool is the interesting one. Spot first with on-demand fallback,
 * limited to three GPUs, and `consolidateAfter` deliberately matched to the
 * KEDA cooldown — two autoscalers with different opinions about when a node
 * is idle is a thrash loop that bills by the hour.
 */

locals {
  namespace       = "karpenter"
  service_account = "karpenter"
}

resource "aws_iam_role" "controller" {
  name = "${var.cluster_name}-karpenter"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = var.oidc_provider_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${var.oidc_provider_url}:sub" = "system:serviceaccount:${local.namespace}:${local.service_account}"
          "${var.oidc_provider_url}:aud" = "sts.amazonaws.com"
        }
      }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "controller" {
  name = "karpenter-controller"
  role = aws_iam_role.controller.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Provision"
        Effect = "Allow"
        Action = [
          "ec2:CreateFleet",
          "ec2:CreateLaunchTemplate",
          "ec2:CreateTags",
          "ec2:DeleteLaunchTemplate",
          "ec2:RunInstances",
          "ec2:TerminateInstances",
          "ec2:Describe*",
          "pricing:GetProducts",
          "ssm:GetParameter",
        ]
        Resource = "*"
      },
      {
        Sid      = "PassNodeRole"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = var.node_role_arn
      },
      {
        Sid      = "InterruptionQueue"
        Effect   = "Allow"
        Action   = ["sqs:DeleteMessage", "sqs:GetQueueUrl", "sqs:ReceiveMessage"]
        Resource = aws_sqs_queue.interruption.arn
      },
      {
        Sid      = "ReadCluster"
        Effect   = "Allow"
        Action   = "eks:DescribeCluster"
        Resource = "arn:aws:eks:${var.region}:${var.account_id}:cluster/${var.cluster_name}"
      },
    ]
  })
}

# Spot interruption gives a two-minute warning. This queue is how Karpenter
# hears about it in time to cordon and drain rather than lose an in-flight
# investigation.
resource "aws_sqs_queue" "interruption" {
  name                      = "${var.cluster_name}-karpenter"
  message_retention_seconds = 300
  sqs_managed_sse_enabled   = true
  tags                      = var.tags
}

resource "aws_sqs_queue_policy" "interruption" {
  queue_url = aws_sqs_queue.interruption.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = ["events.amazonaws.com", "sqs.amazonaws.com"] }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.interruption.arn
    }]
  })
}

resource "aws_cloudwatch_event_rule" "interruption" {
  for_each = {
    spot_interruption = { source = ["aws.ec2"], detail-type = ["EC2 Spot Instance Interruption Warning"] }
    rebalance         = { source = ["aws.ec2"], detail-type = ["EC2 Instance Rebalance Recommendation"] }
    state_change      = { source = ["aws.ec2"], detail-type = ["EC2 Instance State-change Notification"] }
    health            = { source = ["aws.health"], detail-type = ["AWS Health Event"] }
  }
  name          = "${var.cluster_name}-${each.key}"
  event_pattern = jsonencode({ source = each.value.source, "detail-type" = each.value["detail-type"] })
  tags          = var.tags
}

resource "aws_cloudwatch_event_target" "interruption" {
  for_each  = aws_cloudwatch_event_rule.interruption
  rule      = each.value.name
  target_id = "karpenter"
  arn       = aws_sqs_queue.interruption.arn
}

# ------------------------------------------------------------- NodePools

resource "kubectl_manifest" "node_class" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.k8s.aws/v1"
    kind       = "EC2NodeClass"
    metadata   = { name = "default" }
    spec = {
      amiFamily                  = "AL2023"
      amiSelectorTerms           = [{ alias = "al2023@latest" }]
      role                       = var.node_role_name
      subnetSelectorTerms        = [{ tags = { "karpenter.sh/discovery" = var.cluster_name } }]
      securityGroupSelectorTerms = [{ tags = { "karpenter.sh/discovery" = var.cluster_name } }]
      blockDeviceMappings = [{
        deviceName = "/dev/xvda"
        ebs        = { volumeSize = "60Gi", volumeType = "gp3", encrypted = true, deleteOnTermination = true }
      }]
      metadataOptions = {
        # IMDSv2 only, hop limit 1: a compromised pod cannot reach the node's
        # instance credentials.
        httpTokens              = "required"
        httpPutResponseHopLimit = 1
      }
      tags = var.tags
    }
  })
}

resource "kubectl_manifest" "gpu_node_class" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.k8s.aws/v1"
    kind       = "EC2NodeClass"
    metadata   = { name = "gpu" }
    spec = {
      amiFamily                  = "AL2023"
      amiSelectorTerms           = [{ alias = "al2023@latest" }]
      role                       = var.node_role_name
      subnetSelectorTerms        = [{ tags = { "karpenter.sh/discovery" = var.cluster_name } }]
      securityGroupSelectorTerms = [{ tags = { "karpenter.sh/discovery" = var.cluster_name } }]
      blockDeviceMappings = [{
        deviceName = "/dev/xvda"
        # 200 GiB: the vLLM image with baked-in weights is ~12 GiB and the
        # hostPath model cache lives here too.
        ebs = { volumeSize = "200Gi", volumeType = "gp3", iops = 4000, encrypted = true, deleteOnTermination = true }
      }]
      metadataOptions = { httpTokens = "required", httpPutResponseHopLimit = 1 }
      tags            = merge(var.tags, { workload = "inference" })
    }
  })
}

resource "kubectl_manifest" "app_pool" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = "app" }
    spec = {
      template = {
        metadata = { labels = { workload = "app" } }
        spec = {
          nodeClassRef = { group = "karpenter.k8s.aws", kind = "EC2NodeClass", name = "default" }
          requirements = [
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["spot", "on-demand"] },
            { key = "karpenter.k8s.aws/instance-family", operator = "In", values = ["m6a", "m6i", "m7a"] },
            { key = "karpenter.k8s.aws/instance-size", operator = "In", values = ["large", "xlarge"] },
            { key = "kubernetes.io/arch", operator = "In", values = ["amd64"] },
          ]
          expireAfter = "720h"
        }
      }
      limits     = { cpu = "64" }
      disruption = { consolidationPolicy = "WhenEmptyOrUnderutilized", consolidateAfter = "2m" }
    }
  })
}

resource "kubectl_manifest" "gpu_pool" {
  yaml_body = yamlencode({
    apiVersion = "karpenter.sh/v1"
    kind       = "NodePool"
    metadata   = { name = "gpu" }
    spec = {
      template = {
        metadata = { labels = { workload = "inference" } }
        spec = {
          nodeClassRef = { group = "karpenter.k8s.aws", kind = "EC2NodeClass", name = "gpu" }
          taints       = [{ key = "nvidia.com/gpu", value = "true", effect = "NoSchedule" }]
          requirements = [
            # Spot first, on-demand fallback. g5 spot depth in eu-west-1 is
            # good enough to be a strategy rather than a hope.
            { key = "karpenter.sh/capacity-type", operator = "In", values = ["spot", "on-demand"] },
            # A10G at 600 GB/s. Decode on an 8B is memory-bandwidth-bound, so
            # the L4's 300 GB/s costs 50% of decode throughput to save 20% of
            # the sticker price.
            { key = "node.kubernetes.io/instance-type", operator = "In", values = ["g5.xlarge", "g5.2xlarge"] },
          ]
          expireAfter = "168h"
        }
      }
      limits = { "nvidia.com/gpu" = var.max_gpus }
      disruption = {
        consolidationPolicy = "WhenEmpty"
        # Matched to the KEDA cooldown on purpose.
        consolidateAfter = "10m"
      }
    }
  })
  depends_on = [kubectl_manifest.gpu_node_class]
}
