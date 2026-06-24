locals {
  lambda_function_name = "sentinel-remediation"
}

# ── ZIP package ───────────────────────────────────────────────────────────────

data "archive_file" "remediation" {
  type        = "zip"
  source_file = "${path.module}/../lambda/remediation.py"
  output_path = "${path.module}/../lambda/remediation.zip"
}

# ── IAM role ──────────────────────────────────────────────────────────────────

resource "aws_iam_role" "sentinel_lambda" {
  name = "sentinel-lambda"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Permissions derived from prior AccessDenied hits in docs/iam-scratch.md:
#   ssm:SendCommand   — Feature 1 spike (document + instance resource pairing required)
#   sns:Publish       — Feature 7 anticipated, confirmed this feature
#   logs:*            — Feature 5 awslogs driver AccessDenied pattern
resource "aws_iam_role_policy" "sentinel_lambda" {
  name = "sentinel-lambda"
  role = aws_iam_role.sentinel_lambda.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SSMSendCommand"
        Effect = "Allow"
        Action = "ssm:SendCommand"
        Resource = [
          "arn:aws:ssm:${var.aws_region}::document/AWS-RunShellScript",
          aws_instance.sentinel.arn,
        ]
      },
      {
        Sid      = "SSMGetCommand"
        Effect   = "Allow"
        Action   = "ssm:GetCommandInvocation"
        Resource = "*" # no resource-level scope available for this action
      },
      {
        Sid      = "SNSPublishAlerts"
        Effect   = "Allow"
        Action   = "sns:Publish"
        Resource = aws_sns_topic.alerts.arn
      },
      {
        Sid    = "LambdaLogs"
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.lambda_function_name}",
          "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${local.lambda_function_name}:*",
        ]
      },
    ]
  })
}

# ── Lambda function ───────────────────────────────────────────────────────────

resource "aws_lambda_function" "remediation" {
  function_name    = local.lambda_function_name
  role             = aws_iam_role.sentinel_lambda.arn
  runtime          = "python3.12"
  handler          = "remediation.handler"
  filename         = data.archive_file.remediation.output_path
  source_code_hash = data.archive_file.remediation.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      INSTANCE_ID      = aws_instance.sentinel.id
      ALERTS_TOPIC_ARN = aws_sns_topic.alerts.arn
    }
  }
}

# ── SNS invoke permission ─────────────────────────────────────────────────────

resource "aws_lambda_permission" "sns_invoke" {
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.remediation.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.incidents.arn
}
