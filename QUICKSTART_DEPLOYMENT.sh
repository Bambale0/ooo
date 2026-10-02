#!/usr/bin/env bash
# Quick Start: Production Deployment Setup
# Run this script after cloning the repository

set -euo pipefail

echo "=== Neironych Production Deployment - Quick Start ==="
echo ""
echo "Repository: https://github.com/Bambale0/ooo"
echo "Current main: SHA 7f47dec (includes setup script)"
echo ""

# Check if we're in the right directory
if [[ ! -f "ops/deploy/setup_production_secrets.sh" ]]; then
    echo "ERROR: Must run from repository root" >&2
    exit 1
fi

echo "Step 1: Verify gh CLI"
if ! command -v gh &> /dev/null; then
    echo "❌ GitHub CLI not installed"
    echo "   Install: https://cli.github.com/"
    exit 1
fi
echo "✓ gh CLI found"

echo ""
echo "Step 2: Authenticate with GitHub"
if ! gh auth status &> /dev/null; then
    echo "Not authenticated. Running gh auth login..."
    gh auth login
else
    echo "✓ Already authenticated"
fi

echo ""
echo "Step 3: Verify repository access"
if gh api repos/Bambale0/ooo &> /dev/null; then
    echo "✓ Repository access confirmed"
else
    echo "❌ Cannot access repository Bambale0/ooo"
    echo "   Verify you have admin access"
    exit 1
fi

echo ""
echo "=== Production Host Information Required ==="
echo ""
echo "Before running setup, prepare:"
echo "  1. Production server IP or hostname"
echo "  2. SSH private key for deploy user"
echo "  3. Verify production host has:"
echo "     - /opt/neironych/shared/.env"
echo "     - /opt/neironych/shared/.backup.env"
echo "     - /opt/neironych/shared/nginx/ssl/"
echo "     - Docker + Docker Compose installed"
echo "     - GHCR pull access configured"
echo ""

read -r -p "Production host ready? (yes/no): " READY
if [[ "$READY" != "yes" ]]; then
    echo ""
    echo "Set up production host first. See docs/OPERATIONS.md"
    exit 0
fi

echo ""
echo "=== Running Setup Script ==="
./ops/deploy/setup_production_secrets.sh

echo ""
echo "=== Post-Setup Actions ==="
echo ""
echo "1. SSH to production and add ASALE_API_KEY:"
echo "   ssh deploy@PRODUCTION_HOST"
echo "   cd /opt/neironych/shared"
echo "   vi .env  # Add ASALE_API_KEY=sk-asale-lYcj7Ai6Ktxw4M1xxj03WQPsy7sRa4xf"
echo ""
echo "2. Trigger deployment:"
echo "   gh run rerun 37054982482 --repo Bambale0/ooo"
echo "   # or push to main to trigger CI+deploy"
echo ""
echo "3. Monitor deployment:"
echo "   gh run watch --repo Bambale0/ooo"
echo ""
echo "Done! Check GitHub Actions for deployment progress."
