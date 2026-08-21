output "role_arns" {
  description = "Service account annotations for the Helm chart."
  value       = { for name, role in aws_iam_role.service : name => role.arn }
}
