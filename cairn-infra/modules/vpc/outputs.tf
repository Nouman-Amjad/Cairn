output "vpc_id" { value = aws_vpc.this.id }
output "cidr" { value = aws_vpc.this.cidr_block }
output "public_subnet_ids" { value = aws_subnet.public[*].id }
output "private_subnet_ids" { value = aws_subnet.private[*].id }
output "data_subnet_ids" { value = aws_subnet.data[*].id }

output "data_subnet_cidrs" {
  description = "Used by the orchestrator NetworkPolicy to allow Postgres/Redis egress."
  value       = aws_subnet.data[*].cidr_block
}
