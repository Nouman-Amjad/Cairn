output "cluster_name" { value = aws_eks_cluster.this.name }
output "cluster_endpoint" { value = aws_eks_cluster.this.endpoint }
output "cluster_ca" { value = aws_eks_cluster.this.certificate_authority[0].data }
output "oidc_provider_arn" { value = aws_iam_openid_connect_provider.this.arn }
output "oidc_provider_url" { value = replace(aws_iam_openid_connect_provider.this.url, "https://", "") }
output "node_role_arn" { value = aws_iam_role.node.arn }
output "node_role_name" { value = aws_iam_role.node.name }

output "cluster_security_group_id" {
  description = "The cluster security group; data-tier ingress is scoped to it."
  value       = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
}
