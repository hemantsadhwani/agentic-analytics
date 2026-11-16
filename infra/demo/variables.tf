variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "aws_profile" {
  description = "CLI profile of the builder user (never the trading bot's profile)."
  type        = string
  default     = "agentic"
}

variable "name" {
  type    = string
  default = "agentic-demo"
}

variable "lambda_zip" {
  description = "Built by build_lambda.py."
  type        = string
  default     = "build/lambda.zip"
}

variable "demo_enabled" {
  description = "Kill switch. false: every question gets 503, no model is called."
  type        = bool
  default     = true
}

variable "daily_call_cap" {
  description = "Questions admitted per UTC day, across all visitors."
  type        = number
  default     = 300
}

variable "daily_usd_cap" {
  description = "Model spend admitted per UTC day, in US dollars (list-price estimate)."
  type        = number
  default     = 1
}

variable "chat_model" {
  type    = string
  default = "google.gemma-3-12b-it"
}

variable "embed_model" {
  type    = string
  default = "amazon.titan-embed-text-v2:0"
}

variable "api_rate_limit" {
  description = "Steady requests per second for the whole API."
  type        = number
  default     = 2
}

variable "api_burst_limit" {
  type    = number
  default = 5
}

variable "lambda_concurrency" {
  description = "Reserved concurrency for the API Lambda; null = unreserved (needs account headroom)."
  type        = number
  default     = null
}

variable "log_retention_days" {
  type    = number
  default = 14
}
