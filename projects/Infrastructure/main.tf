module "vpc" {
  source = "./modules/vpc"

  vpc_name     = var.vpc_name
  cidr_block   = var.vpc_cidr
  subnet_cidrs = [for s in var.subnets : s.cidr_block]
  availability_zones = [for s in var.subnets : s.availability_zone]
  cluster_name     = var.cluster_name
}


module "eks" {
  source = "./modules/eks"

  cluster_name     = var.cluster_name
  node_group_name  = var.node_group_name

  instance_types = var.instance_types
  min_size       = var.min_size
  desired_size   = var.desired_size
  max_size       = var.max_size

  subnet_ids = module.vpc.subnet_ids
  depends_on = [module.vpc]
}

module "ecr" {
  source = "./modules/ecr"

  repositories = var.repositories
}


data "aws_eks_cluster_auth" "eks" {
  name = module.eks.cluster_name
}

provider "kubernetes" {
  alias                  = "eks"
  host                   = module.eks.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
  token                  = data.aws_eks_cluster_auth.eks.token
}

provider "helm" {
  alias = "eks"

  kubernetes =  {
    host                   = module.eks.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks.cluster_certificate_authority_data)
    token                  = data.aws_eks_cluster_auth.eks.token
  }
}


module "argocd" {
  source = "./modules/argocd"

  external_secrets_role_arn = module.eks.external_secrets_role_arn

  providers = {
    kubernetes = kubernetes.eks
    helm       = helm.eks
  }

  depends_on = [module.eks]
}


module "github_oidc" {
  source = "./modules/github-oidc"

  # Repo uses immutable OIDC subjects (owner and repo IDs, not just names)
  github_subject_prefix = "repo:Rishabh1623@183885132/devops-ai-playbook@1404909131"
  github_branch         = "project-demo"
  role_name             = "github-actions-ci"
  ecr_repository_arns   = module.ecr.repository_arns
}
