/**
 * RDS PostgreSQL 16 with pgvector.
 *
 * pgvector rather than a dedicated vector database: the corpus is ~5k runbook
 * chunks plus a growing trajectory archive, where HNSW in Postgres answers in
 * single-digit milliseconds. What a second stateful system would actually
 * cost is a second backup story, a second failure mode, and a distributed
 * transaction between "the runbook row exists" and "its embedding exists".
 * Revisit at ~2M vectors.
 */

resource "aws_db_subnet_group" "this" {
  name       = "${var.name}-db"
  subnet_ids = var.data_subnet_ids
  tags       = var.tags
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "Cairn database"
  vpc_id      = var.vpc_id

  ingress {
    description     = "Postgres from the cluster"
    from_port       = 5432
    to_port         = 5432
    protocol        = "tcp"
    security_groups = [var.cluster_security_group_id]
  }

  tags = var.tags
}

resource "aws_db_parameter_group" "this" {
  name   = "${var.name}-pg16"
  family = "postgres16"

  parameter {
    name  = "shared_preload_libraries"
    value = "pg_stat_statements"
    apply_method = "pending-reboot"
  }

  parameter {
    # A query that runs longer than this during an incident is a query nobody
    # is waiting for any more.
    name  = "statement_timeout"
    value = "30000"
  }

  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "60000"
  }

  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }

  tags = var.tags
}

resource "random_password" "master" {
  length  = 32
  special = false
}

resource "aws_secretsmanager_secret" "dsn" {
  name       = "cairn/${var.env}/database-dsn"
  kms_key_id = var.kms_key_id
  tags       = var.tags
}

resource "aws_secretsmanager_secret_version" "dsn" {
  secret_id = aws_secretsmanager_secret.dsn.id
  secret_string = jsonencode({
    dsn = "postgresql+asyncpg://${var.master_username}:${random_password.master.result}@${aws_db_instance.this.address}:5432/${var.database_name}"
  })
}

resource "aws_db_instance" "this" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.instance_class

  allocated_storage     = var.allocated_storage
  max_allocated_storage = var.max_allocated_storage
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.kms_key_id

  db_name  = var.database_name
  username = var.master_username
  password = random_password.master.result

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.this.name
  publicly_accessible    = false

  multi_az                = var.multi_az
  backup_retention_period = var.backup_retention_days
  backup_window           = "02:00-03:00"
  maintenance_window      = "sun:03:30-sun:04:30"
  copy_tags_to_snapshot   = true

  # Trajectories are the eval corpus and the postmortem record. Losing them
  # to a `terraform destroy` typo is not a recoverable mistake.
  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.name}-final-${formatdate("YYYYMMDDhhmm", timestamp())}"

  performance_insights_enabled = true
  monitoring_interval          = 60
  monitoring_role_arn          = aws_iam_role.monitoring.arn
  enabled_cloudwatch_logs_exports = ["postgresql", "upgrade"]

  auto_minor_version_upgrade = true
  apply_immediately          = false

  tags = var.tags

  lifecycle {
    ignore_changes = [final_snapshot_identifier, password]
  }
}

resource "aws_db_instance" "replica" {
  count               = var.read_replica ? 1 : 0
  identifier          = "${var.name}-replica"
  replicate_source_db = aws_db_instance.this.identifier
  instance_class      = var.instance_class
  # Trajectory reads and eval queries move off the writer at Tier C.
  publicly_accessible = false
  skip_final_snapshot = true
  tags                = var.tags
}

resource "aws_iam_role" "monitoring" {
  name = "${var.name}-rds-monitoring"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "monitoring" {
  role       = aws_iam_role.monitoring.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}
