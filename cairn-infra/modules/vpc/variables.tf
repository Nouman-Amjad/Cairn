variable "name" { type = string }
variable "cluster_name" { type = string }
variable "region" { type = string }

variable "cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "single_nat_gateway" {
  description = "One NAT for the whole VPC. Saves ~$65/mo; costs AZ independence."
  type        = bool
  default     = false
}

variable "flow_logs_enabled" {
  type    = bool
  default = true
}

variable "flow_logs_bucket_arn" {
  type    = string
  default = null
}

variable "tags" {
  type    = map(string)
  default = {}
}
