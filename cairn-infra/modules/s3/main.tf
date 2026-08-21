/**
 * Buckets: artifacts, the OPA policy bundle, and the WORM audit archive.
 *
 * The audit bucket uses Object Lock in COMPLIANCE mode. Nobody — including
 * the account root — can delete a record inside the retention period. That
 * is the point: an audit trail an operator can rewrite is not an audit trail.
 */

resource "aws_kms_key" "this" {
  description             = "Cairn ${var.env} data"
  enable_key_rotation     = true
  deletion_window_in_days = 30
  tags                    = var.tags
}

resource "aws_kms_alias" "this" {
  name          = "alias/cairn-${var.env}"
  target_key_id = aws_kms_key.this.id
}

resource "aws_s3_bucket" "artifacts" {
  bucket = "cairn-artifacts-${var.env}"
  tags   = var.tags
}

resource "aws_s3_bucket_server_side_encryption_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id

  rule {
    id     = "tiered-expiry"
    status = "Enabled"
    filter { prefix = "artifacts/" }

    # Raw tool payloads are rarely reread after a week and are the bulk of
    # storage. 30 days total, Glacier from day 7.
    transition {
      days          = 7
      storage_class = "GLACIER_IR"
    }
    expiration { days = 30 }

    abort_incomplete_multipart_upload { days_after_initiation = 3 }
  }
}

resource "aws_s3_bucket" "policy" {
  bucket = "cairn-policy-${var.env}"
  tags   = var.tags
}

resource "aws_s3_bucket_versioning" "policy" {
  bucket = aws_s3_bucket.policy.id
  # Rolling back a bad policy bundle should be an S3 version restore, not an
  # archaeology exercise in Git.
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_public_access_block" "policy" {
  bucket                  = aws_s3_bucket.policy.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket" "audit" {
  bucket              = "cairn-audit-${var.env}"
  object_lock_enabled = true
  tags                = var.tags
}

resource "aws_s3_bucket_versioning" "audit" {
  bucket = aws_s3_bucket.audit.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_object_lock_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    default_retention {
      mode = "COMPLIANCE"
      # Seven years. This is a legal-basis decision, documented as such: the
      # audit log is exempt from the user-data deletion path.
      years = 7
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "audit" {
  bucket = aws_s3_bucket.audit.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }
  }
}

resource "aws_s3_bucket_public_access_block" "audit" {
  bucket                  = aws_s3_bucket.audit.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
