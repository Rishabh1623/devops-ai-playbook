terraform {
  required_version = ">= 1.10" # S3 native state locking (use_lockfile)

  # State bucket is created by bootstrap/ (versioned, encrypted, TLS only).
  # Locking uses an S3 lock file next to the state; no DynamoDB table needed.
  backend "s3" {
    bucket       = "devops-ai-playbook-tfstate-955510722779"
    key          = "infrastructure/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 3.0"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 3.1"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.2"
    }
  }
}

provider "aws" {
  region = var.region
}
