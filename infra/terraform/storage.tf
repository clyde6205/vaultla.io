# Vaultla.io — ciphertext storage: S3 (hot -> warm -> Glacier Deep Archive) + KMS.
# Objects are ALREADY client-side encrypted; SSE-KMS here is defence in depth, not the trust anchor.

variable "environment" { type = string }
variable "region"      { type = string }

resource "aws_kms_key" "vault" {
  description             = "Vaultla ${var.environment}: SSE for ciphertext + sealing of server key shares"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "vault" {
  name          = "alias/vaultla-${var.environment}"
  target_key_id = aws_kms_key.vault.key_id
}

resource "aws_s3_bucket" "vault" {
  bucket = "vaultla-${var.environment}-vaults"
  # Legacy data: refuse accidental deletion of a non-empty bucket.
  force_destroy = false
}

resource "aws_s3_bucket_public_access_block" "vault" {
  bucket                  = aws_s3_bucket.vault.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "vault" {
  bucket = aws_s3_bucket.vault.id
  rule { object_ownership = "BucketOwnerEnforced" }
}

resource "aws_s3_bucket_versioning" "vault" {
  bucket = aws_s3_bucket.vault.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "vault" {
  bucket = aws_s3_bucket.vault.id
  rule {
    bucket_key_enabled = true
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.vault.arn
    }
  }
}

# Programmatic tiering: Standard -> Standard-IA (30d) -> Deep Archive (90d).
# NOTE: Deep Archive bills a 180-day minimum and restores take ~12-48h; Chronos requests the
# restore when a vault matures (see app/chronos/lambda_handler.py).
resource "aws_s3_bucket_lifecycle_configuration" "vault" {
  bucket     = aws_s3_bucket.vault.id
  depends_on = [aws_s3_bucket_versioning.vault]

  rule {
    id     = "tier-to-deep-archive"
    status = "Enabled"
    filter { prefix = "tenants/" }
    transition {
      days = 30
      storage_class = "STANDARD_IA"
    }
    transition {
      days = 90
      storage_class = "DEEP_ARCHIVE"
    }
    noncurrent_version_expiration { noncurrent_days = 30 }
  }

  rule {
    id     = "abort-incomplete-uploads"
    status = "Enabled"
    filter {}
    abort_incomplete_multipart_upload { days_after_initiation = 7 }
  }
}

data "aws_iam_policy_document" "tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.vault.arn, "${aws_s3_bucket.vault.arn}/*"]
    principals {
      type = "*"
      identifiers = ["*"]
    }
    condition {
      test = "Bool"
      variable = "aws:SecureTransport"
      values = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "vault" {
  bucket     = aws_s3_bucket.vault.id
  policy     = data.aws_iam_policy_document.tls_only.json
  depends_on = [aws_s3_bucket_public_access_block.vault]
}

# Browsers PUT/GET ciphertext directly using presigned URLs.
resource "aws_s3_bucket_cors_configuration" "vault" {
  bucket = aws_s3_bucket.vault.id
  cors_rule {
    allowed_methods = ["PUT", "GET", "HEAD"]
    allowed_origins = var.web_origins
    allowed_headers = ["*"]
    expose_headers  = ["ETag", "x-amz-checksum-sha256"]
    max_age_seconds = 3000
  }
}
variable "web_origins" { type = list(string) }
