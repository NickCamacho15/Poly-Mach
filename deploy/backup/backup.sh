#!/bin/bash

set -euo pipefail

DATE=$(date +%Y-%m-%d)
BACKUP_DIR="/home/ubuntu/backups"
BOT_DIR="/home/ubuntu/polymarket-bot"

# Create backup directory
mkdir -p "$BACKUP_DIR"

# Backup logs and data
tar -czf "$BACKUP_DIR/bot-backup-$DATE.tar.gz" \
  "$BOT_DIR/logs" \
  "$BOT_DIR/data"

# Upload to S3
aws s3 cp "$BACKUP_DIR/bot-backup-$DATE.tar.gz" \
  s3://your-bucket/backups/

# Keep only last 7 local backups
find "$BACKUP_DIR" -name "*.tar.gz" -mtime +7 -delete
