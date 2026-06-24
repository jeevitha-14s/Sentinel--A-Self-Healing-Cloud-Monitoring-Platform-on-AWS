resource "aws_sns_topic" "incidents" {
  name = "sentinel-incidents"
}

resource "aws_sns_topic" "alerts" {
  name = "sentinel-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_sns_topic_subscription" "incidents_lambda" {
  topic_arn = aws_sns_topic.incidents.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.remediation.arn
}
