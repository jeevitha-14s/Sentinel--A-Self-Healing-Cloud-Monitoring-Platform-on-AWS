resource "aws_sns_topic" "incidents" {
  name = "sentinel-incidents"
}

resource "aws_sns_topic" "alerts" {
  name = "sentinel-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = "sjeevitha679@gmail.com"
}
