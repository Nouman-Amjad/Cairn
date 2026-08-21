variable "cluster_name" { type = string }
variable "private_subnet_ids" { type = list(string) }

variable "kubernetes_version" {
  type    = string
  default = "1.31"
}

variable "endpoint_public_access" {
  type    = bool
  default = false
}

variable "public_access_cidrs" {
  type    = list(string)
  default = []
}

variable "addon_versions" {
  type = object({
    vpc_cni    = string
    coredns    = string
    kube_proxy = string
    ebs_csi    = string
  })
  default = {
    vpc_cni    = "v1.19.0-eksbuild.1"
    coredns    = "v1.11.3-eksbuild.2"
    kube_proxy = "v1.31.2-eksbuild.3"
    ebs_csi    = "v1.38.1-eksbuild.1"
  }
}

variable "tags" {
  type    = map(string)
  default = {}
}
