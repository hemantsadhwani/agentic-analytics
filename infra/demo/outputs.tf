output "demo_url" {
  description = "The public link (the resume points here)."
  value       = "https://${aws_cloudfront_distribution.demo.domain_name}"
}

output "api_endpoint" {
  description = "Direct API Gateway URL: refuses requests without CloudFront's secret header."
  value       = aws_apigatewayv2_api.api.api_endpoint
}

output "budget_table" {
  value = aws_dynamodb_table.budget.name
}

output "kill_switch" {
  value = "terraform apply -var demo_enabled=false   (or: terraform destroy)"
}
