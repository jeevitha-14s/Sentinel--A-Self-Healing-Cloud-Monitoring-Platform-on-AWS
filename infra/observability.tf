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
