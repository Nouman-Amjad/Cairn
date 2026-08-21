variable "name" { type = string }
variable "env" { type = string }
variable "vpc_id" { type = string }
variable "data_subnet_ids" { type = list(string) }
variable "cluster_security_group_id" { type = string }
variable "kms_key_id" { type = string }

variable "engine_version" {
  type    = string
  default = "16.4"
}

variable "instance_class" {
  # Tier B: db.t4g.small Multi-AZ. Tier C: db.r6g.large + a read replica.
  type    = string
  default = "db.t4g.small"
}

variable "allocated_storage" {
  type    = number
  default = 50
}

variable "max_allocated_storage" {
  type    = number
  default = 500
}

variable "database_name" {
  type    = string
  default = "cairn"
}

variable "master_username" {
  type    = string
  default = "cairn"
}

variable "multi_az" {
  type    = bool
  default = true
}

variable "read_replica" {
  type    = bool
  default = false
}

variable "backup_retention_days" {
  type    = number
  default = 14
}

variable "deletion_protection" {
  type    = bool
  default = true
}

variable "tags" {
  type    = map(string)
  default = {}
}
