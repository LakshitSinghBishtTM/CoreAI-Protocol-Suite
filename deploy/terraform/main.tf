# Example: install the CaiPS Helm chart on an existing Kubernetes cluster.
# NOT validated in CI (no cluster or terraform binary there): run `terraform validate`
# and `terraform plan` yourself before applying. Provider keys come from a Secret you
# create outside Terraform, so they never land in Terraform state.

terraform {
  required_version = ">= 1.5"
  required_providers {
    helm       = { source = "hashicorp/helm", version = "~> 2.12" }
    kubernetes = { source = "hashicorp/kubernetes", version = "~> 2.27" }
  }
}

variable "kubeconfig" {
  type    = string
  default = "~/.kube/config"
}

variable "namespace" {
  type    = string
  default = "caips"
}

variable "image_repository" {
  type        = string
  description = "Registry path of the image built from the repo Dockerfile."
}

variable "image_tag" {
  type        = string
  description = "Pin a version; do not use latest in production."
}

variable "existing_secret" {
  type        = string
  default     = "caips-provider-keys"
  description = "Name of a Secret in the namespace holding OPENAI_API_KEY etc."
}

variable "ingress_host" {
  type    = string
  default = ""
}

variable "storage_size" {
  type    = string
  default = "5Gi"
}

provider "kubernetes" {
  config_path = var.kubeconfig
}

provider "helm" {
  kubernetes {
    config_path = var.kubeconfig
  }
}

resource "kubernetes_namespace" "caips" {
  metadata {
    name = var.namespace
  }
}

resource "helm_release" "caips" {
  name      = "caips"
  namespace = kubernetes_namespace.caips.metadata[0].name
  chart     = "${path.module}/../helm/caips"
  atomic    = true # roll back automatically if the pods never become ready
  timeout   = 300

  values = [yamlencode({
    image           = { repository = var.image_repository, tag = var.image_tag }
    existingSecret  = var.existing_secret
    persistence     = { enabled = true, size = var.storage_size }
    ingress         = var.ingress_host == "" ? { enabled = false } : { enabled = true, host = var.ingress_host }
  })]
}

output "release_status" {
  value = helm_release.caips.status
}
