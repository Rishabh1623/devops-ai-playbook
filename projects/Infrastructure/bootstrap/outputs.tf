output "state_bucket" {
  description = "Use as `bucket` in the main stack's backend block (provider.tf)"
  value       = aws_s3_bucket.tfstate.id
}
