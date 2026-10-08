variable "github_repository" {
  description = "GitHub repository allowed to assume the CI role, as owner/name"
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
