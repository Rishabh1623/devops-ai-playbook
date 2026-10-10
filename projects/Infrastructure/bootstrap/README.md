# Terraform state bucket (bootstrap)

The main stack in `projects/Infrastructure` stores its state in S3:

| Setting | Value |
|---------|-------|
| Bucket | `devops-ai-playbook-tfstate-955510722779` |
| Key | `infrastructure/terraform.tfstate` |
| Locking | `use_lockfile = true` (S3 lock file, Terraform >= 1.10, no DynamoDB) |
| Bucket settings | versioning, SSE-S3 encryption, public access blocked, TLS-only policy, old versions kept 90 days, `prevent_destroy` |

This folder creates that bucket. It runs once, before the main stack, because a stack can't create the bucket its own backend lives in.

```bash
cd projects/Infrastructure/bootstrap
terraform init
terraform apply
```

Its own state is local (`bootstrap/terraform.tfstate`, gitignored) and covers only the bucket. If that file is lost, nothing breaks: re-import the bucket with `terraform import aws_s3_bucket.tfstate devops-ai-playbook-tfstate-955510722779` (and the other `aws_s3_bucket_*` resources the same way).

## Recovering an older state version

Every state write creates a new S3 object version:

```bash
aws s3api list-object-versions --bucket devops-ai-playbook-tfstate-955510722779 \
  --prefix infrastructure/terraform.tfstate --query 'Versions[].[VersionId,LastModified]'
aws s3api get-object --bucket devops-ai-playbook-tfstate-955510722779 \
  --key infrastructure/terraform.tfstate --version-id <id> old.tfstate
```

## Stuck lock

If a run is interrupted, a `terraform.tfstate.tflock` object can be left behind. Check nobody else is running Terraform, then `terraform force-unlock <lock id>` from `projects/Infrastructure`.
