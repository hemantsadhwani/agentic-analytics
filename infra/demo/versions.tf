# The public demo: one page and one API, serverless, in Mumbai (ap-south-1).
#
#   browser -> CloudFront ─┬─ /*      -> S3 (private; CloudFront reads it via origin access control)
#                          └─ /api/*  -> API Gateway HTTP API (throttled) -> Lambda -> Bedrock (in-region)
#                                                                        └-> DynamoDB (daily budget)
#
# Cost at rest is close to zero: no servers, no NAT, no load balancer. Each question costs
# its Bedrock tokens (about $0.0003 on Gemma 3 12B); a DynamoDB counter caps calls and dollars
# per day, and DEMO_ENABLED is the kill switch.
#
# Local state on purpose: a demo stack that is applied, recorded and destroyed. The enterprise
# stack would keep state in S3 with locking.
terraform {
  required_version = ">= 1.9"
  required_providers {
    aws    = { source = "hashicorp/aws", version = "~> 5.70" }
    random = { source = "hashicorp/random", version = "~> 3.6" }
  }
}

provider "aws" {
  region  = var.region
  profile = var.aws_profile
  default_tags {
    tags = {
      project    = "agentic"
      stack      = "demo"
      managed_by = "terraform"
    }
  }
}

data "aws_caller_identity" "this" {}

locals {
  name       = var.name
  account_id = data.aws_caller_identity.this.account_id
  repo_root  = abspath("${path.module}/../..")
}
