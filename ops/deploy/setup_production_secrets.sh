#!/usr/bin/env bash
# Production Deployment Setup Script
# Configures GitHub secrets for automated production deployment

set -euo pipefail

REPO="Bambale0/ooo"
ENV="production"

echo "=== Production Deployment Setup for ${REPO} ==="
echo ""
echo "This script will configure GitHub secrets required for production deployment."
echo "You will be prompted for:"
echo "  1. Production host address"
echo "  2. SSH private key (will be read from file)"
echo "  3. SSH known_hosts entry"
echo ""

# Check gh CLI is installed and authenticated
if ! command -v gh &> /dev/null; then
    echo "ERROR: gh CLI not found. Install from https://cli.github.com/" >&2
    exit 1
fi

if ! gh auth status &> /dev/null; then
    echo "ERROR: Not authenticated with GitHub. Run: gh auth login" >&2
    exit 1
fi

# Verify repository access
if ! gh api "repos/${REPO}" &> /dev/null; then
    echo "ERROR: Cannot access repository ${REPO}. Check permissions." >&2
    exit 1
fi

echo "✓ GitHub CLI authenticated"
echo ""

# Prompt for production host
read -r -p "Production host address (e.g., 123.45.67.89 or production.example.com): " DEPLOY_HOST
if [[ -z "$DEPLOY_HOST" ]]; then
    echo "ERROR: DEPLOY_HOST cannot be empty" >&2
    exit 1
fi

# Prompt for SSH key file
read -r -p "Path to SSH private key file (e.g., ~/.ssh/deploy_key): " SSH_KEY_PATH
SSH_KEY_PATH="${SSH_KEY_PATH/#\~/$HOME}"  # Expand ~
if [[ ! -f "$SSH_KEY_PATH" ]]; then
    echo "ERROR: SSH key file not found: $SSH_KEY_PATH" >&2
    exit 1
fi

echo ""
echo "Fetching SSH host key from ${DEPLOY_HOST}..."
DEPLOY_KNOWN_HOSTS=$(ssh-keyscan -H "$DEPLOY_HOST" 2>/dev/null)
if [[ -z "$DEPLOY_KNOWN_HOSTS" ]]; then
    echo "ERROR: Could not fetch SSH host key from ${DEPLOY_HOST}" >&2
    echo "Is the host reachable and SSH port open?" >&2
    exit 1
fi

echo "✓ SSH host key retrieved"
echo ""

# Show what will be configured
echo "=== Configuration Summary ==="
echo "Repository: ${REPO}"
echo "Environment: ${ENV}"
echo "DEPLOY_HOST: ${DEPLOY_HOST}"
echo "SSH Key: ${SSH_KEY_PATH}"
echo "Known Hosts: ${DEPLOY_KNOWN_HOSTS:0:60}..."
echo ""

read -r -p "Configure these secrets? (yes/no): " CONFIRM
if [[ "$CONFIRM" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "=== Setting GitHub Secrets ==="

# Set DEPLOY_HOST as environment secret
echo "Setting DEPLOY_HOST..."
gh secret set DEPLOY_HOST \
  --repo "$REPO" \
  --env "$ENV" \
  --body "$DEPLOY_HOST"
echo "✓ DEPLOY_HOST set"

# Set DEPLOY_SSH_KEY as environment secret
echo "Setting DEPLOY_SSH_KEY..."
gh secret set DEPLOY_SSH_KEY \
  --repo "$REPO" \
  --env "$ENV" \
  --body "$(cat "$SSH_KEY_PATH")"
echo "✓ DEPLOY_SSH_KEY set"

# Set DEPLOY_KNOWN_HOSTS as environment secret
echo "Setting DEPLOY_KNOWN_HOSTS..."
gh secret set DEPLOY_KNOWN_HOSTS \
  --repo "$REPO" \
  --env "$ENV" \
  --body "$DEPLOY_KNOWN_HOSTS"
echo "✓ DEPLOY_KNOWN_HOSTS set"

echo ""
echo "=== Verification ==="

# List secrets (names only, values are masked)
echo "Environment secrets for ${ENV}:"
gh secret list --repo "$REPO" --env "$ENV"

echo ""
echo "✓ Production secrets configured successfully!"
echo ""
echo "Next steps:"
echo "  1. SSH to ${DEPLOY_HOST} and verify /opt/neironych/shared/.env exists"
echo "  2. Add ASALE_API_KEY to production .env:"
echo "     ssh deploy@${DEPLOY_HOST}"
echo "     cd /opt/neironych/shared"
echo "     echo 'ASALE_API_KEY=your-actual-asale-key-here' >> .env"
echo "  3. Verify Docker/Compose and GHCR access on production host"
echo "  4. Trigger deployment:"
echo "     gh workflow run deploy.yml --ref main --repo ${REPO}"
echo "     # or wait for next push to main"
echo ""
