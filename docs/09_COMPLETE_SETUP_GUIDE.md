# Complete Setup Guide: Zero to Trading Bot

## Overview

This guide takes you from a blank laptop to a running trading bot. Follow every step in order.

**Time Estimate:** 
- Local setup: 1-2 hours
- AWS setup: 1-2 hours  
- First code running: Day 1-2
- Paper trading: Week 2-3
- Live trading: Week 4+

---

# PART 1: LOCAL MACHINE SETUP (Your Laptop)

## Step 1: Create Project Folder

### On Mac:
```bash
# Open Terminal (Cmd + Space, type "Terminal")

# Navigate to where you want your projects
cd ~

# Create a projects folder if you don't have one
mkdir -p Projects

# Create the bot folder
mkdir -p Projects/polymarket-bot

# Go into it
cd Projects/polymarket-bot

# Verify you're in the right place
pwd
# Should show: /Users/YOUR_USERNAME/Projects/polymarket-bot
```

### On Windows:
```powershell
# Open PowerShell (Windows key, type "PowerShell")

# Navigate to where you want your projects
cd ~

# Create folders
mkdir Projects
mkdir Projects\polymarket-bot

# Go into it
cd Projects\polymarket-bot

# Verify
pwd
# Should show: C:\Users\YOUR_USERNAME\Projects\polymarket-bot
```

---

## Step 2: Install Required Software

### 2.1 Install Python 3.11+

**Mac:**
```bash
# Install Homebrew if you don't have it
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install Python
brew install python@3.11

# Verify
python3 --version
# Should show: Python 3.11.x or higher
```

**Windows:**
1. Go to https://www.python.org/downloads/
2. Download Python 3.11 or higher
3. Run installer
4. **IMPORTANT:** Check "Add Python to PATH" during installation
5. Verify in PowerShell:
```powershell
python --version
```

### 2.2 Install Git

**Mac:**
```bash
# Git comes with Xcode tools
xcode-select --install

# Or via Homebrew
brew install git

# Verify
git --version
```

**Windows:**
1. Go to https://git-scm.com/download/win
2. Download and install
3. Use default options
4. Verify in PowerShell:
```powershell
git --version
```

### 2.3 Install VS Code (Optional but Recommended)

1. Go to https://code.visualstudio.com/
2. Download and install
3. Install the "Python" extension

### 2.4 Install Cursor

1. Go to https://cursor.sh/
2. Download and install
3. This is your AI-powered IDE for building the bot

---

## Step 3: Set Up Git Repository

### 3.1 Create GitHub Account (if you don't have one)
1. Go to https://github.com
2. Sign up for free account

### 3.2 Initialize Local Repository
```bash
# Make sure you're in your project folder
cd ~/Projects/polymarket-bot

# Initialize git
git init

# Create .gitignore file
cat > .gitignore << 'EOF'
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
venv/
ENV/

# Environment
.env
.env.local
*.env

# Credentials
.credentials/
*.pem
*.key

# IDE
.vscode/
.idea/
*.swp
*.swo

# Logs
logs/
*.log

# Data
data/*.db
data/*.sqlite

# OS
.DS_Store
Thumbs.db

# Build
dist/
build/
*.egg-info/
EOF

# Create initial README
cat > README.md << 'EOF'
# Polymarket US Trading Bot

Automated trading bot for Polymarket US sports prediction markets.

## Status
🚧 Under Development

## Setup
See `docs/` folder for documentation.
EOF

# Make first commit
git add .
git commit -m "Initial commit"
```

### 3.3 Create GitHub Repository
1. Go to https://github.com/new
2. Name: `polymarket-bot`
3. Make it **Private** (important - don't share your trading code!)
4. Don't initialize with README (you already have one)
5. Click "Create repository"

### 3.4 Connect Local to GitHub
```bash
# Add GitHub as remote (replace YOUR_USERNAME)
git remote add origin https://github.com/YOUR_USERNAME/polymarket-bot.git

# Push your code
git branch -M main
git push -u origin main
```

---

## Step 4: Set Up Python Environment

```bash
# Make sure you're in project folder
cd ~/Projects/polymarket-bot

# Create virtual environment
python3 -m venv venv

# Activate it
# Mac/Linux:
source venv/bin/activate
# Windows:
# .\venv\Scripts\activate

# Your prompt should now show (venv)

# Upgrade pip
pip install --upgrade pip

# Create requirements.txt
cat > requirements.txt << 'EOF'
# Core
python-dotenv==1.0.1
pydantic==2.5.3
pydantic-settings==2.1.0
PyYAML==6.0.1

# HTTP & WebSocket
httpx==0.26.0
websockets==12.0
aiohttp==3.9.1

# Cryptography (for Ed25519 signing)
cryptography==41.0.7

# Data
pandas==2.1.4
numpy==1.26.3

# Async utilities
asyncio-throttle==1.0.2

# Logging
structlog==24.1.0

# Database
aiosqlite==0.19.0

# Testing
pytest==7.4.4
pytest-asyncio==0.23.3

# Development
black==24.1.1
mypy==1.8.0
EOF

# Install all packages
pip install -r requirements.txt

# Verify installation
python -c "import cryptography; import websockets; import httpx; print('All packages installed!')"
```

---

## Step 5: Create Project Structure

```bash
# Create all directories
mkdir -p src/{api,data,strategies,execution,state,utils}
mkdir -p tests
mkdir -p config
mkdir -p logs
mkdir -p data
mkdir -p docs

# Create __init__.py files
touch src/__init__.py
touch src/api/__init__.py
touch src/data/__init__.py
touch src/strategies/__init__.py
touch src/execution/__init__.py
touch src/state/__init__.py
touch src/utils/__init__.py
touch tests/__init__.py

# Create placeholder main file
cat > src/main.py << 'EOF'
"""
Polymarket US Trading Bot - Entry Point
"""

import asyncio
import structlog

logger = structlog.get_logger()


async def main():
    logger.info("Bot starting...")
    # TODO: Initialize components
    logger.info("Bot ready!")


if __name__ == "__main__":
    asyncio.run(main())
EOF

# Create config file
cat > src/config.py << 'EOF'
"""
Configuration management.
"""

import os
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Polymarket API
    pm_api_key_id: str = Field(default="", env="PM_API_KEY_ID")
    pm_private_key: str = Field(default="", env="PM_PRIVATE_KEY")
    pm_base_url: str = "https://api.polymarket.us"
    pm_ws_url: str = "wss://api.polymarket.us/v1/ws"
    
    # Trading
    trading_mode: str = "paper"  # "paper" or "live"
    
    # Risk
    max_position_per_market: float = 50.0
    max_portfolio_exposure: float = 250.0
    max_daily_loss: float = 25.0
    kelly_fraction: float = 0.25
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
EOF

# Create .env.example
cat > .env.example << 'EOF'
# Polymarket US API Credentials
# Get these from https://polymarket.us/developer
PM_API_KEY_ID=your-api-key-uuid-here
PM_PRIVATE_KEY=your-base64-private-key-here

# Sports Data (optional - for live arbitrage)
OPTICODDS_API_KEY=your-opticodds-key-here

# Alerting (optional)
DISCORD_WEBHOOK=https://discord.com/api/webhooks/xxx/yyy
EOF

# Create actual .env (copy from example)
cp .env.example .env
echo "⚠️  Remember to edit .env with your real API keys!"
```

---

## Step 6: Get Polymarket US API Keys

### 6.1 Access Developer Portal
1. Go to https://polymarket.us
2. Log in to your account (or create one)
3. Complete any required verification
4. Go to https://polymarket.us/developer

### 6.2 Create API Key
1. Click "Create API Key"
2. Give it a name (e.g., "Trading Bot")
3. You'll receive:
   - **API Key ID** (UUID format)
   - **Private Key** (Base64 encoded)
4. **SAVE THESE IMMEDIATELY** - you won't see the private key again!

### 6.3 Add Keys to .env
```bash
# Edit .env file
# Mac:
nano .env
# or
open -e .env

# Windows:
notepad .env
```

Replace the placeholder values:
```
PM_API_KEY_ID=abc12345-1234-5678-9abc-def012345678
PM_PRIVATE_KEY=YourBase64PrivateKeyHere==
```

---

## Step 7: Copy Documentation to Project

1. Download the `polymarket-bot-docs.zip` I provided
2. Extract it
3. Move contents to your docs folder:

```bash
# Mac (assuming download is in Downloads folder)
cp -r ~/Downloads/polymarket-bot-docs/* ~/Projects/polymarket-bot/docs/

# Verify
ls docs/
# Should show: README.md, 00_PROJECT_OVERVIEW.md, etc.
```

---

## Step 8: Test Your Setup

```bash
# Make sure venv is activated
source venv/bin/activate  # Mac/Linux
# .\venv\Scripts\activate  # Windows

# Run a test
python -c "
from src.config import settings
print('Config loaded!')
print(f'API Key ID: {settings.pm_api_key_id[:8]}...' if settings.pm_api_key_id else 'No API key set')
print(f'Mode: {settings.trading_mode}')
print(f'Max position: \${settings.max_position_per_market}')
"
```

If you see output without errors, your local setup is complete!

---

## Step 9: Commit Your Progress

```bash
git add .
git commit -m "Project structure and configuration"
git push
```

---

# PART 2: AWS EC2 SETUP (Cloud Server)

This is where your bot will run 24/7.

## Step 10: Create AWS Account

### 10.1 Sign Up
1. Go to https://aws.amazon.com/
2. Click "Create an AWS Account"
3. Enter email and password
4. Choose "Personal" account type
5. Enter payment info (free tier available for 12 months)
6. Verify phone number
7. Choose "Basic (Free)" support plan

### 10.2 Enable MFA (Important!)
1. Go to IAM console: https://console.aws.amazon.com/iam/
2. Click on your username (top right) → Security credentials
3. Enable MFA (use Google Authenticator or similar)

---

## Step 11: Create EC2 Key Pair

This lets you SSH into your server.

### 11.1 Go to EC2 Console
1. Go to https://console.aws.amazon.com/ec2/
2. Make sure you're in **us-east-1** region (top right dropdown)

### 11.2 Create Key Pair
1. Left sidebar → "Key Pairs"
2. Click "Create key pair"
3. Name: `polymarket-bot-key`
4. Key pair type: RSA
5. Private key format: `.pem` (Mac/Linux) or `.ppk` (Windows with PuTTY)
6. Click "Create key pair"
7. **The .pem file downloads automatically - SAVE IT SECURELY!**

```bash
# Mac/Linux: Move key to safe location and set permissions
mkdir -p ~/.ssh
mv ~/Downloads/polymarket-bot-key.pem ~/.ssh/
chmod 400 ~/.ssh/polymarket-bot-key.pem
```

---

## Step 12: Create Security Group

This is your firewall.

### 12.1 Create Group
1. EC2 Console → Left sidebar → "Security Groups"
2. Click "Create security group"
3. Settings:
   - Name: `polymarket-bot-sg`
   - Description: `Security group for Polymarket trading bot`
   - VPC: Leave default
   
### 12.2 Add Inbound Rules
Click "Add rule" for each:

| Type | Port | Source | Description |
|------|------|--------|-------------|
| SSH | 22 | My IP | SSH access |

### 12.3 Outbound Rules
Leave default (allow all outbound) - your bot needs to reach Polymarket API.

4. Click "Create security group"

---

## Step 13: Launch EC2 Instance

### 13.1 Launch Instance
1. EC2 Console → Click "Launch instance"

### 13.2 Configure Instance
Fill in these settings:

**Name:** `polymarket-bot`

**Application and OS Images:**
- Click "Ubuntu"
- Select "Ubuntu Server 24.04 LTS"
- Architecture: 64-bit (x86)

**Instance type:**
- For paper trading: `t3.small` (~$15/month)
- For live trading later: `t3.medium` (~$30/month)

**Key pair:**
- Select `polymarket-bot-key` (created earlier)

**Network settings:**
- Click "Edit"
- Select existing security group: `polymarket-bot-sg`

**Configure storage:**
- 20 GB gp3 (default is fine)

### 13.3 Launch
1. Click "Launch instance"
2. Wait for it to start (1-2 minutes)
3. Click on the instance ID to view details
4. Note the **Public IPv4 address** (you'll need this)

---

## Step 14: Connect to Your Server

### 14.1 SSH Connection

**Mac/Linux:**
```bash
# Replace with your instance's public IP
ssh -i ~/.ssh/polymarket-bot-key.pem ubuntu@YOUR_INSTANCE_IP

# Example:
ssh -i ~/.ssh/polymarket-bot-key.pem ubuntu@54.123.45.67
```

**Windows (PowerShell):**
```powershell
ssh -i C:\Users\YOU\.ssh\polymarket-bot-key.pem ubuntu@YOUR_INSTANCE_IP
```

**First time connecting:** Type `yes` when asked about fingerprint.

### 14.2 You Should See
```
Welcome to Ubuntu 24.04 LTS...
ubuntu@ip-xxx-xxx-xxx-xxx:~$
```

🎉 You're now on your cloud server!

---

## Step 15: Set Up Server Environment

Run these commands on your EC2 instance:

```bash
# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and tools
sudo apt install -y python3.11 python3.11-venv python3-pip git

# Verify Python
python3.11 --version

# Install Docker (optional, for later)
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker ubuntu

# Create project directory
mkdir -p ~/polymarket-bot
cd ~/polymarket-bot

# Clone your repository (replace with your GitHub URL)
git clone https://github.com/YOUR_USERNAME/polymarket-bot.git .

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

---

## Step 16: Configure Server Environment

```bash
# Still on EC2 instance, in ~/polymarket-bot

# Create .env file with your API keys
nano .env
```

Paste your credentials:
```
PM_API_KEY_ID=your-actual-api-key-id
PM_PRIVATE_KEY=your-actual-private-key
```

Save: `Ctrl+X`, then `Y`, then `Enter`

```bash
# Verify config loads
source venv/bin/activate
python -c "from src.config import settings; print(f'API Key: {settings.pm_api_key_id[:8]}...')"
```

---

## Step 17: Set Up Auto-Start (systemd)

This makes your bot start automatically if the server reboots.

```bash
# Create service file
sudo nano /etc/systemd/system/polymarket-bot.service
```

Paste:
```ini
[Unit]
Description=Polymarket Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/polymarket-bot
Environment=PATH=/home/ubuntu/polymarket-bot/venv/bin
ExecStart=/home/ubuntu/polymarket-bot/venv/bin/python -m src.main
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Save and exit (`Ctrl+X`, `Y`, `Enter`)

```bash
# Enable service
sudo systemctl daemon-reload
sudo systemctl enable polymarket-bot

# Start service (once bot is ready)
# sudo systemctl start polymarket-bot

# Check status
# sudo systemctl status polymarket-bot

# View logs
# journalctl -u polymarket-bot -f
```

---

## Step 18: Set Up Deployment Script

On your **local machine**, create a deploy script:

```bash
# In your local project folder
cat > deploy.sh << 'EOF'
#!/bin/bash
# Deploy script for Polymarket Bot

# Configuration
SERVER="ubuntu@YOUR_EC2_IP"  # <-- Replace with your IP
KEY="~/.ssh/polymarket-bot-key.pem"
REMOTE_DIR="/home/ubuntu/polymarket-bot"

echo "📦 Pushing to GitHub..."
git push

echo "🚀 Deploying to server..."
ssh -i $KEY $SERVER << 'ENDSSH'
    cd ~/polymarket-bot
    git pull
    source venv/bin/activate
    pip install -r requirements.txt
    sudo systemctl restart polymarket-bot
    echo "✅ Deployed!"
ENDSSH

echo "🎉 Deployment complete!"
EOF

chmod +x deploy.sh
```

**Edit the script** to replace `YOUR_EC2_IP` with your actual EC2 IP address.

Now you can deploy with:
```bash
./deploy.sh
```

---

# PART 3: DEVELOPMENT WORKFLOW

## Daily Workflow

### Starting Work (Local)
```bash
cd ~/Projects/polymarket-bot
source venv/bin/activate
cursor .  # Open in Cursor IDE
```

### Making Changes
1. Write code in Cursor
2. Test locally
3. Commit and push:
```bash
git add .
git commit -m "Description of changes"
git push
```

### Deploying to Server
```bash
./deploy.sh
```

### Checking Server Status
```bash
ssh -i ~/.ssh/polymarket-bot-key.pem ubuntu@YOUR_EC2_IP

# Once connected:
sudo systemctl status polymarket-bot
journalctl -u polymarket-bot -f  # Live logs (Ctrl+C to exit)
```

---

# PART 4: CHECKLIST

## Local Setup Checklist
- [ ] Project folder created
- [ ] Python 3.11+ installed
- [ ] Git installed and configured
- [ ] Cursor IDE installed
- [ ] Virtual environment created
- [ ] Dependencies installed
- [ ] Project structure created
- [ ] GitHub repository created
- [ ] Code pushed to GitHub
- [ ] Polymarket API keys obtained
- [ ] .env file configured
- [ ] Documentation copied to docs/

## AWS Setup Checklist
- [ ] AWS account created
- [ ] MFA enabled
- [ ] EC2 key pair created
- [ ] Key pair file secured
- [ ] Security group created
- [ ] EC2 instance launched
- [ ] SSH connection working
- [ ] Server environment set up
- [ ] Git repo cloned to server
- [ ] Python venv created on server
- [ ] .env configured on server
- [ ] systemd service created
- [ ] Deploy script created

---

# PART 5: NEXT STEPS

Now that your environment is ready:

1. **Open Cursor** in your project folder
2. **Tell Cursor:** 
   > "Read the documentation in the docs/ folder, especially 04_IMPLEMENTATION_PLAN.md, and help me build Phase 1: the Ed25519 authentication module."

3. **Follow the implementation plan** step by step

4. **Test locally** before deploying to AWS

5. **Paper trade for 2+ weeks** before using real money

---

# TROUBLESHOOTING

## "Permission denied" on SSH
```bash
chmod 400 ~/.ssh/polymarket-bot-key.pem
```

## "Module not found" errors
```bash
# Make sure venv is activated
source venv/bin/activate
pip install -r requirements.txt
```

## Can't connect to EC2
1. Check security group allows SSH from your IP
2. Check instance is running
3. Verify you're using the correct IP address

## Git push rejected
```bash
git pull --rebase
git push
```

## Server out of memory
Upgrade to larger instance (t3.medium) in EC2 console.

---

# COST SUMMARY

## AWS Monthly Costs (Estimated)

| Resource | Paper Trading | Live Trading |
|----------|--------------|--------------|
| EC2 t3.small/medium | $15-30 | $30-60 |
| EBS Storage (20GB) | $2 | $2 |
| Data Transfer | $5 | $10 |
| **Total** | **~$22/mo** | **~$42/mo** |

## Optional Services

| Service | Cost | When Needed |
|---------|------|-------------|
| OpticOdds | $99-199/mo | Live arbitrage |
| Sportradar | $500+/mo | Competitive HFT |
| CloudWatch | $5-10/mo | Production monitoring |

---

# QUICK REFERENCE

## Important Paths

| What | Location |
|------|----------|
| Project (local) | `~/Projects/polymarket-bot/` |
| Project (server) | `/home/ubuntu/polymarket-bot/` |
| SSH key | `~/.ssh/polymarket-bot-key.pem` |
| Logs (server) | `journalctl -u polymarket-bot -f` |

## Important Commands

| Action | Command |
|--------|---------|
| Activate venv | `source venv/bin/activate` |
| Run bot locally | `python -m src.main` |
| Deploy to server | `./deploy.sh` |
| SSH to server | `ssh -i ~/.ssh/polymarket-bot-key.pem ubuntu@YOUR_IP` |
| Check bot status | `sudo systemctl status polymarket-bot` |
| View live logs | `journalctl -u polymarket-bot -f` |
| Restart bot | `sudo systemctl restart polymarket-bot` |
| Stop bot | `sudo systemctl stop polymarket-bot` |

---

Good luck! 🚀
