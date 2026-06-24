resource "aws_cloudwatch_log_group" "sentinel" {
  name              = "/sentinel/app"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_metric_filter" "app_errors" {
  name           = "sentinel-app-errors"
  log_group_name = aws_cloudwatch_log_group.sentinel.name

  pattern = "{ $.level = \"ERROR\" }"

  metric_transformation {
    name          = "AppErrors"
    namespace     = "Sentinel"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "app_errors" {
  alarm_name          = "sentinel-app-errors"
  alarm_description   = "Fires once per incident (OK→ALARM). ALARM→ALARM does not re-fire — free dedup, no state store."

  namespace           = "Sentinel"
  metric_name         = "AppErrors"
  statistic           = "Sum"
  period              = 60
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [aws_sns_topic.incidents.arn]
}
