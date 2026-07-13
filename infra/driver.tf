# Driver Lambda: periodically POSTs a rotating prompt to the app's /chat
# endpoint (via the ALB) so telemetry flows to Bronto continuously, not just
# when someone uses the demo by hand. Same shape as the AgentCore demo driver.

data "archive_file" "driver" {
  type        = "zip"
  source_dir  = "${path.module}/lambda"
  output_path = "${path.module}/builds/driver.zip"
}

resource "aws_iam_role" "driver" {
  name = "${var.project}-driver"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "driver" {
  name = "${var.project}-driver"
  role = aws_iam_role.driver.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "Logs"
      Effect   = "Allow"
      Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "arn:aws:logs:*:*:*"
    }]
  })
}

resource "aws_lambda_function" "driver" {
  function_name    = "${var.project}-driver"
  role             = aws_iam_role.driver.arn
  runtime          = "python3.12"
  handler          = "driver.handler"
  filename         = data.archive_file.driver.output_path
  source_code_hash = data.archive_file.driver.output_base64sha256
  timeout          = 90
  memory_size      = 128

  environment {
    variables = {
      APP_BASE_URL = "http://${aws_lb.app.dns_name}"
    }
  }
}

resource "aws_cloudwatch_event_rule" "driver_periodic" {
  name                = "${var.project}-driver-periodic"
  schedule_expression = var.schedule_expression
  description         = "Periodically POST to the demo /chat endpoint to keep telemetry flowing to Bronto."
}

resource "aws_cloudwatch_event_target" "driver_periodic" {
  rule = aws_cloudwatch_event_rule.driver_periodic.name
  arn  = aws_lambda_function.driver.arn
}

resource "aws_lambda_permission" "driver_events" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.driver.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.driver_periodic.arn
}
