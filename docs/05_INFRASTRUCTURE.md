# Infrastructure Guide

## Overview

This guide covers deploying the Polymarket US trading bot to AWS for reliable, low-latency execution.

**Recommended Setup:**
- AWS EC2 instance in us-east-1 (Virginia)
- Docker containerization
- CloudWatch monitoring
- Automated restarts with systemd

---

## AWS Region Selection

| Region | Latency to Polymarket US | Recommended |
|--------|--------------------------|-------------|
| us-east-1 (Virginia) | ~5-15ms | ✅ Best choice |
| us-east-2 (Ohio) | ~10-20ms | ✅ Good alternative |
| us-west-2 (Oregon) | ~40-60ms | ⚠️ Acceptable |
| eu-west-1 (Ireland) | ~80-100ms | ❌ Too slow |

**Note:** Polymarket US servers are likely in US East. Choose us-east-1 for lowest latency.

---

## EC2 Instance Selection

### For Development/Paper Trading

**Instance:** `t3.small` or `t3.medium`

| Spec | t3.small | t3.medium |
|------|----------|-----------|
| vCPUs | 2 | 2 |
| RAM | 2 GB | 4 GB |
| Network | Up to 5 Gbps | Up to 5 Gbps |
| Cost | ~$15/month | ~$30/month |

### For Live Trading

**Instance:** `c5.large` or `c5n.large`

| Spec | c5.large | c5n.large |
|------|----------|-----------|
| vCPUs | 2 | 2 |
| RAM | 4 GB | 5.25 GB |
| Network | Up to 10 Gbps | Up to 25 Gbps |
| Cost | ~$62/month | ~$78/month |

**Why c5n:** Enhanced networking for lower latency and higher bandwidth.

---

## EC2 Setup Instructions

### Step 1: Launch Instance

```bash
# Using AWS CLI
aws ec2 run-instances \
  --image-id ami-0c55b159cbfafe1f0 \  # Ubuntu 22.04 LTS
  --instance-type t3.medium \
  --key-name your-key-pair \
  --security-group-ids sg-xxxxxxxx \
  --subnet-id subnet-xxxxxxxx \
  --associate-public-ip-address \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=polymarket-bot}]'
```

### Step 2: Security Group Configuration

Allow inbound:
- SSH (port 22) from your IP only
- (Optional) HTTPS (443) for monitoring dashboard

Allow outbound:
- HTTPS (443) to Polymarket API
- All traffic to AWS services

```bash
aws ec2 authorize-security-group-ingress \
  --group-id sg-xxxxxxxx \
  --protocol tcp \
  --port 22 \
  --cidr YOUR_IP/32
```

### Step 3: Connect and Setup

```bash
# Connect to instance
ssh -i your-key.pem ubuntu@ec2-xx-xx-xx-xx.compute-1.amazonaws.com

# Update system
sudo apt update && sudo apt upgrade -y

# Install Docker
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker ubuntu

# Install Docker Compose
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

# Logout and login again for docker group to take effect
exit
```

---

## Docker Configuration

### Dockerfile

Use the `Dockerfile` at the repo root. For reference:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY src/ ./src/

# Create non-root user and runtime directories
RUN useradd -m botuser \
    && mkdir -p /app/logs /app/data \
    && chown -R botuser:botuser /app
USER botuser

# Set environment
ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Run
CMD ["python", "-m", "src.main"]
```

### docker-compose.yml

Use the `docker-compose.yml` at the repo root. For reference:

```yaml
version: "3.8"

services:
  bot:
    build: .
    container_name: polymarket-bot
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./logs:/app/logs
      - ./data:/app/data
    logging:
      driver: "json-file"
      options:
        max-size: "100m"
        max-file: "5"
    healthcheck:
      test:
        [
          "CMD",
          "python",
          "-c",
          "import urllib.request as u; u.urlopen('http://localhost:8080/health')",
        ]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 10s
    deploy:
      resources:
        limits:
          memory: 2G
        reservations:
          memory: 512M
```

### Build and Run

```bash
# Build
docker-compose build

# Run in background
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

### Environment Variables

All runtime settings are environment-driven (see `src/config.py`). Minimum required:

- `PM_API_KEY_ID`
- `PM_PRIVATE_KEY`
- `MARKET_SLUGS` (comma-separated)

Common optional settings:

- `PM_BASE_URL`, `PM_WS_URL`
- `TRADING_MODE` (`paper` or `live`), `INITIAL_BALANCE`
- Risk: `RISK_MAX_POSITION_PER_MARKET`, `RISK_MAX_PORTFOLIO_EXPOSURE`,
  `RISK_MAX_DAILY_LOSS`, `RISK_KELLY_FRACTION`, `RISK_MIN_EDGE`,
  `RISK_MIN_TRADE_SIZE`, `RISK_MAX_CORRELATED_EXPOSURE`,
  `RISK_MAX_POSITIONS`, `RISK_MAX_DRAWDOWN_PCT`
- Logging: `LOG_LEVEL`, `LOG_FILE`, `LOG_JSON`
- Health: `HEALTH_HOST`, `HEALTH_PORT`
- Integrations: `OPTICODDS_API_KEY`, `DISCORD_WEBHOOK`

Use decimal strings for money/percent values (e.g., `50.00`, `0.25`).

---

## Systemd Service (Alternative to Docker)

If you prefer running without Docker:

### /etc/systemd/system/polymarket-bot.service

Use the repo-provided unit file at `deploy/systemd/polymarket-bot.service`:

```ini
[Unit]
Description=Polymarket Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/polymarket-bot
Environment=PYTHONPATH=/home/ubuntu/polymarket-bot
EnvironmentFile=/home/ubuntu/polymarket-bot/.env
ExecStart=/home/ubuntu/polymarket-bot/venv/bin/python -m src.main
Restart=always
RestartSec=10
StandardOutput=append:/home/ubuntu/polymarket-bot/logs/bot.log
StandardError=append:/home/ubuntu/polymarket-bot/logs/bot-error.log

[Install]
WantedBy=multi-user.target
```

### Enable and Start

```bash
sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot
sudo systemctl start polymarket-bot

# Check status
sudo systemctl status polymarket-bot

# View logs
journalctl -u polymarket-bot -f
```

---

## Network Optimization

### Linux Kernel Tuning

Add to `/etc/sysctl.conf`:

```bash
# TCP optimization for low latency
net.ipv4.tcp_low_latency = 1
net.ipv4.tcp_nodelay = 1

# Increase buffer sizes
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216

# Enable TCP BBR congestion control (better than CUBIC for trading)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# Reduce TIME_WAIT sockets
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15

# Enable TCP Fast Open
net.ipv4.tcp_fastopen = 3
```

Apply:
```bash
sudo sysctl -p
```

---

## Monitoring with CloudWatch

### CloudWatch Agent Configuration

Install CloudWatch agent:

```bash
wget https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb
sudo dpkg -i amazon-cloudwatch-agent.deb
```

Copy `deploy/cloudwatch/amazon-cloudwatch-agent.json` to
`/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json`:

```json
{
  "agent": {
    "metrics_collection_interval": 60,
    "run_as_user": "root"
  },
  "logs": {
    "logs_collected": {
      "files": {
        "collect_list": [
          {
            "file_path": "/home/ubuntu/polymarket-bot/logs/bot.log",
            "log_group_name": "polymarket-bot",
            "log_stream_name": "{instance_id}/bot",
            "timezone": "UTC"
          },
          {
            "file_path": "/home/ubuntu/polymarket-bot/logs/trades.log",
            "log_group_name": "polymarket-bot",
            "log_stream_name": "{instance_id}/trades",
            "timezone": "UTC"
          }
        ]
      }
    }
  },
  "metrics": {
    "metrics_collected": {
      "cpu": {
        "measurement": ["cpu_usage_idle", "cpu_usage_user", "cpu_usage_system"],
        "metrics_collection_interval": 60
      },
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 60
      },
      "net": {
        "measurement": ["bytes_sent", "bytes_recv"],
        "metrics_collection_interval": 60
      }
    }
  }
}
```

Start agent:
```bash
sudo /opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
  -a fetch-config \
  -m ec2 \
  -s \
  -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json
```

### CloudWatch Alarms

Create alarms for:

1. **High CPU Usage:**
```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "polymarket-bot-high-cpu" \
  --metric-name CPUUtilization \
  --namespace AWS/EC2 \
  --statistic Average \
  --period 300 \
  --threshold 80 \
  --comparison-operator GreaterThanThreshold \
  --evaluation-periods 2 \
  --alarm-actions arn:aws:sns:us-east-1:YOUR_ACCOUNT:your-topic
```

2. **Instance Status Check:**
```bash
aws cloudwatch put-metric-alarm \
  --alarm-name "polymarket-bot-status-check" \
  --metric-name StatusCheckFailed \
  --namespace AWS/EC2 \
  --statistic Maximum \
  --period 60 \
  --threshold 1 \
  --comparison-operator GreaterThanOrEqualToThreshold \
  --evaluation-periods 2 \
  --alarm-actions arn:aws:sns:us-east-1:YOUR_ACCOUNT:your-topic
```

---

## Alert Notifications

### Discord Webhook (Recommended)

Create a Discord webhook and add to your bot:

```python
import aiohttp

class DiscordNotifier:
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        
    async def send(self, message: str, level: str = "info"):
        colors = {
            "info": 0x3498db,
            "warning": 0xf39c12,
            "error": 0xe74c3c,
            "success": 0x2ecc71
        }
        
        embed = {
            "title": f"Polymarket Bot - {level.upper()}",
            "description": message,
            "color": colors.get(level, 0x3498db),
            "timestamp": datetime.utcnow().isoformat()
        }
        
        async with aiohttp.ClientSession() as session:
            await session.post(
                self.webhook_url,
                json={"embeds": [embed]}
            )
```

### Telegram Bot (Alternative)

```python
import aiohttp

class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{bot_token}"
        
    async def send(self, message: str):
        async with aiohttp.ClientSession() as session:
            await session.post(
                f"{self.base_url}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": message,
                    "parse_mode": "Markdown"
                }
            )
```

---

## Backup Strategy

### Daily Backups

Use `deploy/backup/backup.sh` and copy it to `/home/ubuntu/backup.sh`:

```bash
#!/bin/bash

DATE=$(date +%Y-%m-%d)
BACKUP_DIR="/home/ubuntu/backups"
BOT_DIR="/home/ubuntu/polymarket-bot"

# Create backup directory
mkdir -p $BACKUP_DIR

# Backup logs and data
tar -czf $BACKUP_DIR/bot-backup-$DATE.tar.gz \
  $BOT_DIR/logs \
  $BOT_DIR/data \
  $BOT_DIR/config

# Upload to S3
aws s3 cp $BACKUP_DIR/bot-backup-$DATE.tar.gz \
  s3://your-bucket/backups/

# Keep only last 7 local backups
find $BACKUP_DIR -name "*.tar.gz" -mtime +7 -delete
```

Add to crontab:
```bash
crontab -e
# Add: 0 0 * * * /home/ubuntu/backup.sh
```

---

## Cost Estimation

### Paper Trading (t3.medium)

| Resource | Monthly Cost |
|----------|-------------|
| EC2 t3.medium | $30 |
| EBS Storage (20GB) | $2 |
| Data Transfer | ~$5 |
| CloudWatch | ~$5 |
| **Total** | **~$42/month** |

### Live Trading (c5n.large)

| Resource | Monthly Cost |
|----------|-------------|
| EC2 c5n.large | $78 |
| EBS Storage (50GB) | $5 |
| Data Transfer | ~$10 |
| CloudWatch | ~$10 |
| S3 Backups | ~$2 |
| **Total** | **~$105/month** |

---

## Security Best Practices

1. **Never commit credentials** - Use environment variables or AWS Secrets Manager

2. **Restrict SSH access** - Only allow your IP in security group

3. **Enable MFA** on AWS account

4. **Use IAM roles** for EC2 instead of access keys where possible

5. **Regular updates:**
   ```bash
   sudo apt update && sudo apt upgrade -y
   ```

6. **Enable automatic security updates:**
   ```bash
   sudo apt install unattended-upgrades
   sudo dpkg-reconfigure unattended-upgrades
   ```

7. **Rotate API keys** periodically

---

## Deployment Checklist

### Pre-Deployment

- [ ] EC2 instance launched in us-east-1
- [ ] Security group configured (SSH + outbound HTTPS)
- [ ] Docker installed and configured
- [ ] Environment variables set in .env
- [ ] CloudWatch agent configured

### Deployment

- [ ] Code pushed to server
- [ ] Docker image built successfully
- [ ] Container running with `docker-compose up -d`
- [ ] Health check passing
- [ ] Logs showing market data received

### Post-Deployment

- [ ] CloudWatch alarms active
- [ ] Discord/Telegram notifications working
- [ ] Backup script scheduled
- [ ] Paper trading verified for 24+ hours before live
