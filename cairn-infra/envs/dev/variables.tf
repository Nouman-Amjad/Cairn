variable "admin_cidrs" {
  description = "Ranges allowed to reach the dev EKS public endpoint."
  type        = list(string)
  default     = []
}
