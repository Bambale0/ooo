#!/usr/bin/env bash
# Quick deployment setup after adding SSH key to server

REPO="Bambale0/ooo"
ENV="production"
DEPLOY_HOST="5.35.124.201"
SSH_KEY_PATH="${HOME}/.ssh/neironych_deploy_new"

echo "=== Step 1: Add public key to server ==="
echo ""
echo "Public key to add:"
cat "${SSH_KEY_PATH}.pub"
echo ""
echo "Add this key to server with one of these methods:"
echo ""
echo "Method 1: Via web console (VPS control panel)"
echo "  - Login to your VPS provider dashboard"
echo "  - Open web console for 5.35.124.201"
echo "  - Run: mkdir -p ~/.ssh && chmod 700 ~/.ssh"
echo "  - Run: echo 'PUBLIC_KEY_HERE' >> ~/.ssh/authorized_keys"
echo "  - Run: chmod 600 ~/.ssh/authorized_keys"
echo ""
echo "Method 2: Via existing SSH access (if you have root password)"
echo "  ssh root@5.35.124.201"
echo "  su - deploy  # or create deploy user if not exists"
echo "  mkdir -p ~/.ssh && chmod 700 ~/.ssh"
echo "  echo '$(cat ${SSH_KEY_PATH}.pub)' >> ~/.ssh/authorized_keys"
echo "  chmod 600 ~/.ssh/authorized_keys"
echo ""
read -r -p "Press ENTER after adding the key to server..."

echo ""
echo "=== Step 2: Test SSH connection ==="
ssh -i "$SSH_KEY_PATH" -o ConnectTimeout=10 deploy@"$DEPLOY_HOST" "echo 'SSH connection OK!'"
if [[ $? -ne 0 ]]; then
    echo "ERROR: SSH connection failed. Check that key was added correctly." >&2
    exit 1
fi
echo "✓ SSH connection successful"

echo ""
echo "=== Step 3: Configure GitHub Secrets ==="

if ! gh auth status &> /dev/null; then
    echo "ERROR: Not authenticated with GitHub" >&2
    exit 1
fi

echo "Fetching SSH host key..."
DEPLOY_KNOWN_HOSTS=$(ssh-keyscan -H "$DEPLOY_HOST" 2>/dev/null)

echo "Setting DEPLOY_HOST..."
gh secret set DEPLOY_HOST --repo "$REPO" --env "$ENV" --body "$DEPLOY_HOST"

echo "Setting DEPLOY_SSH_KEY..."
gh secret set DEPLOY_SSH_KEY --repo "$REPO" --env "$ENV" --body "$(cat "$SSH_KEY_PATH")"

echo "Setting DEPLOY_KNOWN_HOSTS..."
gh secret set DEPLOY_KNOWN_HOSTS --repo "$REPO" --env "$ENV" --body "$DEPLOY_KNOWN_HOSTS"

echo ""
echo "✓ All secrets configured!"
gh secret list --repo "$REPO" --env "$ENV"

echo ""
echo "=== Step 4: Add ASALE_API_KEY to production .env ==="
echo "ssh -i $SSH_KEY_PATH deploy@$DEPLOY_HOST"
echo "cd /opt/neironych/shared"
echo "echo 'ASALE_API_KEY=your-actual-asale-key-here' >> .env"
echo ""
echo "=== Step 5: Trigger deployment ==="
echo "gh workflow run deploy.yml --ref main --repo $REPO"
