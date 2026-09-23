# Vaultla.io — Chronos Protocol scheduling: EventBridge Scheduler -> Lambda, daily.
# IMPORTANT NETWORK NOTE: the Lambda needs outbound UDP/123 (NTP). Put it in private subnets
# behind a NAT gateway and allow UDP 123 egress in its security group + NACLs. If NTP is
# blocked, Chronos fails CLOSED (nothing matures) and the DLQ alarm fires.

variable "chronos_package"    { type = string } # path to zip built from services/api
variable "private_subnet_ids" { type = list(string) }
variable "chronos_sg_id"      { type = string }
variable "chronos_db_secret"  { type = string } # Secrets Manager ARN holding CHRONOS_DATABASE_URL
variable "alarm_topic_arn"    { type = string }

resource "aws_sqs_queue" "chronos_dlq" {
  name                      = "vaultla-${var.environment}-chronos-dlq"
  message_retention_seconds = 1209600
  sqs_managed_sse_enabled   = true
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "chronos" {
  name               = "vaultla-${var.environment}-chronos"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "chronos_vpc" {
  role       = aws_iam_role.chronos.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

# Least privilege: Chronos can request restores and list; it CANNOT read objects or use KMS.
data "aws_iam_policy_document" "chronos" {
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.vault.arn]
  }
  statement {
    actions   = ["s3:RestoreObject"]
    resources = ["${aws_s3_bucket.vault.arn}/tenants/*"]
  }
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [var.chronos_db_secret]
  }
}
resource "aws_iam_role_policy" "chronos" {
  role   = aws_iam_role.chronos.id
  policy = data.aws_iam_policy_document.chronos.json
}

resource "aws_lambda_function" "chronos" {
  function_name    = "vaultla-${var.environment}-chronos"
  role             = aws_iam_role.chronos.arn
  runtime          = "python3.12"
  handler          = "app.chronos.lambda_handler.handler"
  filename         = var.chronos_package
  source_code_hash = filebase64sha256(var.chronos_package)
  timeout          = 300
  memory_size      = 512
  reserved_concurrent_executions = 1   # one scanner at a time (SKIP LOCKED is the second guard)
  vpc_config {
    subnet_ids = var.private_subnet_ids
    security_group_ids = [var.chronos_sg_id]
  }
  environment {
    variables = {
      VAULTLA_ENV = var.environment
      VAULT_BUCKET = aws_s3_bucket.vault.bucket
      KMS_KEY_ID = aws_kms_key.vault.arn
      # CHRONOS_DATABASE_URL: inject from Secrets Manager at deploy time (never plaintext in state).
    }
  }
}

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}
resource "aws_iam_role" "scheduler" {
  name               = "vaultla-${var.environment}-chronos-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}
resource "aws_iam_role_policy" "scheduler" {
  role = aws_iam_role.scheduler.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = "lambda:InvokeFunction", Resource = aws_lambda_function.chronos.arn },
    { Effect = "Allow", Action = "sqs:SendMessage",       Resource = aws_sqs_queue.chronos_dlq.arn },
  ] })
}

resource "aws_scheduler_schedule" "chronos_daily" {
  name                         = "vaultla-${var.environment}-chronos-daily"
  schedule_expression          = "cron(0 3 * * ? *)"   # 03:00 UTC daily
  schedule_expression_timezone = "UTC"
  flexible_time_window { mode = "OFF" }
  target {
    arn      = aws_lambda_function.chronos.arn
    role_arn = aws_iam_role.scheduler.arn
    retry_policy {
      maximum_retry_attempts = 5
      maximum_event_age_in_seconds = 3600
    }
    dead_letter_config { arn = aws_sqs_queue.chronos_dlq.arn }
  }
}

resource "aws_cloudwatch_metric_alarm" "chronos_dlq" {
  alarm_name          = "vaultla-${var.environment}-chronos-failed"
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  dimensions          = { QueueName = aws_sqs_queue.chronos_dlq.name }
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [var.alarm_topic_arn]
}
