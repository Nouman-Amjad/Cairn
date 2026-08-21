output "address" { value = aws_db_instance.this.address }
output "port" { value = aws_db_instance.this.port }
output "security_group_id" { value = aws_security_group.db.id }
output "dsn_secret_arn" { value = aws_secretsmanager_secret.dsn.arn }
output "replica_address" { value = try(aws_db_instance.replica[0].address, null) }
