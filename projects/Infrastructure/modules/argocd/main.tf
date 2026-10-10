resource "kubernetes_namespace_v1" "argocd" {
  metadata {
    name = "argocd"
  }
}

resource "kubernetes_namespace_v1" "monitoring" {
  metadata {
    name = "monitoring"
  }
}

terraform {
  required_providers {
    kubernetes = {
      source  = "hashicorp/kubernetes"
    }
    helm = {
      source  = "hashicorp/helm"
    }
  }
}


resource "helm_release" "argocd" {
  name       = "argocd"
  namespace  = kubernetes_namespace_v1.argocd.metadata[0].name
  repository = "https://argoproj.github.io/argo-helm"
  chart      = "argo-cd"
  version    = "6.7.0"

  create_namespace = false

  values = [
    yamlencode({
      server = {
        service = {
          type = "ClusterIP" 
        }
      }
      configs = {
        params = {
          "server.insecure" = true
        }
      }
    })
  ]
}

resource "helm_release" "monitoring" {
  name       = "kube-prometheus-stack"
  namespace  = kubernetes_namespace_v1.monitoring.metadata[0].name

  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  version    = "56.21.0"

  timeout          = 600
  create_namespace = false

  values = [
    yamlencode({
      grafana = {
        service = {
          type = "ClusterIP"
        }
      }

      # Private only. Kira's metrics and health tools run inside the cluster
      # and query it at kube-prometheus-stack-prometheus.monitoring.svc:9090.
      prometheus = {
        service = {
          type = "ClusterIP"
        }
      }

      alertmanager = {
        service = {
          type = "ClusterIP"
        }
      }
    })
  ]

  depends_on = [
    kubernetes_namespace_v1.monitoring
  ]
}

resource "kubernetes_namespace_v1" "external_secrets" {
  metadata {
    name = "external-secrets"
  }
}

# Syncs the boutique DB credentials from AWS Secrets Manager into the
# boutique-secrets Kubernetes Secret (see gitops/k8s/database/external-secret.yml)
resource "helm_release" "external_secrets" {
  name       = "external-secrets"
  namespace  = kubernetes_namespace_v1.external_secrets.metadata[0].name
  repository = "https://charts.external-secrets.io"
  chart      = "external-secrets"
  version    = "2.12.0"

  create_namespace = false

  values = [
    yamlencode({
      installCRDs = true
      serviceAccount = {
        name = "external-secrets"
        annotations = {
          "eks.amazonaws.com/role-arn" = var.external_secrets_role_arn
        }
      }
    })
  ]
}

# Kira's fetch_recent_changes tool (#10) reads the boutique Application's
# sync history. Read-only, this one Application only.
resource "kubernetes_role_v1" "aiops_assistant_read_app" {
  metadata {
    name      = "aiops-assistant-read-app"
    namespace = kubernetes_namespace_v1.argocd.metadata[0].name
  }

  rule {
    api_groups     = ["argoproj.io"]
    resources      = ["applications"]
    resource_names = ["boutique"]
    verbs          = ["get"]
  }
}

resource "kubernetes_role_binding_v1" "aiops_assistant_read_app" {
  metadata {
    name      = "aiops-assistant-read-app"
    namespace = kubernetes_namespace_v1.argocd.metadata[0].name
  }

  role_ref {
    api_group = "rbac.authorization.k8s.io"
    kind      = "Role"
    name      = kubernetes_role_v1.aiops_assistant_read_app.metadata[0].name
  }

  subject {
    kind      = "ServiceAccount"
    name      = "aiops-assistant"
    namespace = "boutique"
  }
}
