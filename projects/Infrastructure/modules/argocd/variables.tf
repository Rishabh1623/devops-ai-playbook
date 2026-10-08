variable "external_secrets_role_arn" {
  description = "IAM role (IRSA) for External Secrets Operator to read the boutique DB secret"
  type        = string
}
