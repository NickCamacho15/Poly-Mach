#!/bin/bash
# Setup script for the Polymarket US Rust trading bot on Ubuntu EC2.
# Run this on your EC2 instance after cloning the repo.
#
# Usage:
#   chmod +x deploy/setup-rust-bot.sh
#   ./deploy/setup-rust-bot.sh

set -euo pipefail

echo "=== Polymarket US Rust Bot - EC2 Setup ==="

# 1. Install Rust if not already installed.
if ! command -v cargo &> /dev/null; then
    echo "[1/5] Installing Rust..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
else
    echo "[1/5] Rust already installed: $(rustc --version)"
fi

# 2. Install system dependencies for building.
echo "[2/5] Installing build dependencies..."
sudo apt-get update -qq
sudo apt-get install -y -qq pkg-config libssl-dev build-essential

# 3. Build release binary.
echo "[3/5] Building release binary (this takes 2-5 minutes on first build)..."
cd "$(dirname "$0")/../rust-bot"
cargo build --release
echo "    Binary: $(pwd)/target/release/polymarket-us-bot"
echo "    Size: $(du -h target/release/polymarket-us-bot | cut -f1)"

# 4. Create log directory.
echo "[4/5] Creating log directory..."
mkdir -p "$(dirname "$0")/../logs"

# 5. Verify .env file exists.
ENV_FILE="$(dirname "$0")/../.env"
if [ -f "$ENV_FILE" ]; then
    echo "[5/5] .env file found."

    # Check for required variables.
    if grep -q "PM_API_KEY_ID=your" "$ENV_FILE" || grep -q "PM_API_KEY_ID=$" "$ENV_FILE"; then
        echo ""
        echo "WARNING: PM_API_KEY_ID is not configured in .env"
        echo "         Edit $ENV_FILE with your real API credentials before running."
    fi
else
    echo "[5/5] WARNING: No .env file found at $ENV_FILE"
    echo "         Copy .env.example to .env and fill in your API credentials:"
    echo "         cp .env.example .env"
    echo "         nano .env"
fi

echo ""
echo "=== Setup Complete ==="
echo ""
echo "To run in paper mode (recommended first):"
echo "  cd rust-bot && TRADING_MODE=paper cargo run --release"
echo ""
echo "To install as a systemd service:"
echo "  sudo cp deploy/systemd/polymarket-rust-bot.service /etc/systemd/system/"
echo "  sudo systemctl daemon-reload"
echo "  sudo systemctl enable polymarket-rust-bot"
echo "  sudo systemctl start polymarket-rust-bot"
echo "  sudo journalctl -u polymarket-rust-bot -f"
echo ""
echo "To stop the Python bot first:"
echo "  sudo systemctl stop polymarket-bot"
echo "  sudo systemctl disable polymarket-bot"
