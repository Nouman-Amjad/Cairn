output "artifacts_bucket" { value = aws_s3_bucket.artifacts.id }
output "policy_bucket" { value = aws_s3_bucket.policy.id }
output "audit_bucket" { value = aws_s3_bucket.audit.id }
output "kms_key_arn" { value = aws_kms_key.this.arn }
output "kms_key_id" { value = aws_kms_key.this.id }
