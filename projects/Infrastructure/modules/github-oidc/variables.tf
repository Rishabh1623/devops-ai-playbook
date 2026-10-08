variable "github_subject_prefix" {
  description = <<-EOT
    OIDC sub claim prefix for the repository, e.g. "repo:owner/name", or with
    immutable subjects enabled "repo:owner@<owner_id>/name@<repo_id>"
    (see GET /repos/{owner}/{repo}/actions/oidc/customization/sub)
  EOT
  type        = string
}

variable "github_branch" {
  description = "Branch whose workflow runs may assume the CI role"
  type        = string
}

variable "ecr_repository_arns" {
  description = "ECR repositories the CI role may push to"
  type        = list(string)
}

variable "role_name" {
  description = "Name of the IAM role assumed by GitHub Actions"
  type        = string
}
