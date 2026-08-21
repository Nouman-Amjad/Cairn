variable "env" { type = string }

variable "tags" {
  type    = map(string)
  default = {}
}
