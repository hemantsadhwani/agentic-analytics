# The API: API Gateway HTTP API (throttled) -> one Lambda. Least privilege: the function may
# invoke two Bedrock models in this region, update one DynamoDB table, and write its logs.

resource "random_password" "origin" {
  length  = 40
  special = false
}

resource "aws_dynamodb_table" "budget" {
  name         = "${local.name}-budget"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "day"
  attribute {
    name = "day"
    type = "S"
  }
  ttl {
    attribute_name = "expires"
    enabled        = true
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${local.name}-api"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "api" {
  statement {
    sid     = "BedrockTwoModelsInRegion"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${var.region}::foundation-model/${var.chat_model}",
      "arn:aws:bedrock:${var.region}::foundation-model/${var.embed_model}",
    ]
  }
  statement {
    sid       = "BudgetCounter"
    actions   = ["dynamodb:UpdateItem", "dynamodb:GetItem"]
    resources = [aws_dynamodb_table.budget.arn]
  }
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }
}

resource "aws_iam_role_policy" "api" {
  name   = "least-privilege"
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

resource "aws_lambda_function" "api" {
  function_name                  = "${local.name}-api"
  role                           = aws_iam_role.api.arn
  runtime                        = "python3.12"
  architectures                  = ["arm64"]
  handler                        = "agentic_reporting.demo_api.lambda_handler"
  filename                       = "${path.module}/${var.lambda_zip}"
  source_code_hash               = filebase64sha256("${path.module}/${var.lambda_zip}")
  memory_size                    = 1024
  timeout                        = 29 # API Gateway's own limit is 30 s
  reserved_concurrent_executions = var.lambda_concurrency
  ephemeral_storage { size = 1024 }
  environment {
    variables = {
      DEMO_ENABLED      = tostring(var.demo_enabled)
      LLM_PROVIDER      = "bedrock-converse"
      LLM_MODEL         = var.chat_model
      EMBEDDER          = "titan"
      BUDGET_TABLE      = aws_dynamodb_table.budget.name
      DAILY_CALL_CAP    = tostring(var.daily_call_cap)
      DAILY_USD_CAP     = tostring(var.daily_usd_cap)
      ORIGIN_SECRET     = random_password.origin.result
      DEMO_WORKDIR      = "/tmp/demo"
      PYTHONIOENCODING  = "utf-8"
      ANTHROPIC_API_KEY = ""
    }
  }
  depends_on = [aws_cloudwatch_log_group.api, aws_iam_role_policy.api]
}

resource "aws_apigatewayv2_api" "api" {
  name          = "${local.name}-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 29000
}

resource "aws_apigatewayv2_route" "ask" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "POST /api/ask"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_route" "health" {
  api_id    = aws_apigatewayv2_api.api.id
  route_key = "GET /api/health"
  target    = "integrations/${aws_apigatewayv2_integration.api.id}"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_rate_limit  = var.api_rate_limit
    throttling_burst_limit = var.api_burst_limit
  }
}

resource "aws_lambda_permission" "api" {
  statement_id  = "AllowApiGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}
