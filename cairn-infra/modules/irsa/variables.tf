variable "env" { type = string }
variable "region" { type = string }
variable "cluster_name" { type = string }
variable "oidc_provider_arn" { type = string }
variable "oidc_provider_url" { type = string }
variable "kms_key_arn" { type = string }
variable "artifacts_bucket" { type = string }
variable "policy_bucket" { type = string }

variable "namespace" {
  type    = string
  default = "cairn"
}

variable "tags" {
  type    = map(string)
  default = {}
}
