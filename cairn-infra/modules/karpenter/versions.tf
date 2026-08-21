terraform {
  required_version = ">= 1.9"
  required_providers {
    aws     = { source = "hashicorp/aws", version = "~> 5.70" }
    kubectl = { source = "gavinbunney/kubectl", version = "~> 1.14" }
  }
}
