variable "cluster_name" { type = string }
variable "region" { type = string }
variable "account_id" { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider_url" { type = string }
variable "node_role_arn" { type = string }
variable "node_role_name" { type = string }

variable "max_gpus" {
  description = "Hard ceiling on GPU nodes. The cost blast radius of a scaling bug."
  type        = number
  default     = 3
}

variable "tags" {
  type    = map(string)
  default = {}
}
