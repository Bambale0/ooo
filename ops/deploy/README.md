# Production Deployment Scripts

## setup_production_secrets.sh

Configures GitHub secrets required for automated production deployment via GitHub Actions.

### Prerequisites

1. **GitHub CLI installed and authenticated:**
   ```bash
   # Install gh CLI
   # macOS: brew install gh
   # Linux: https://github.com/cli/cli/blob/trunk/docs/install_linux.md
   # Windows: scoop install gh
   
   # Authenticate
   gh auth login
   ```

2. **Repository admin access** to `Bambale0/ooo`

3. **SSH key for production deployment:**
   ```bash
   # Generate if not exists
   ssh-keygen -t ed25519 -C "github-deploy@neironych" -f ~/.ssh/deploy_neironych
   
   # Add public key to production server
   ssh-copy-id -i ~/.ssh/deploy_neironych.pub deploy@PRODUCTION_HOST
   ```

4. **Production server accessible** via SSH

### Usage

```bash
cd /d/code/dev/ooo
./ops/deploy/setup_production_secrets.sh
```

The script will prompt for:
- Production host address (IP or hostname)
- Path to SSH private key file
- Confirmation before setting secrets

### What It Does

1. Validates `gh` CLI authentication and repository access
2. Retrieves SSH host key via `ssh-keyscan`
3. Sets three environment secrets for `production` environment:
   - `DEPLOY_HOST` - Production server address
   - `DEPLOY_SSH_KEY` - SSH private key content
   - `DEPLOY_KNOWN_HOSTS` - SSH host fingerprint
4. Verifies secrets were set successfully

### After Running

1. **Verify production host readiness:**
   ```bash
   ssh deploy@PRODUCTION_HOST
   
   # Check directory structure
   ls -la /opt/neironych/shared/
   
   # Should contain:
   # .env
   # .backup.env
   # nginx/ssl/fullchain.pem
   # nginx/ssl/privkey.pem
   ```

2. **Add ASALE_API_KEY to production .env:**
   ```bash
   ssh deploy@PRODUCTION_HOST
   cd /opt/neironych/shared
   
   # Add to .env
   echo 'ASALE_API_KEY=sk-asale-lYcj7Ai6Ktxw4M1xxj03WQPsy7sRa4xf' >> .env
   
   # Verify
   grep ASALE_API_KEY .env
   ```

3. **Verify Docker/Compose access:**
   ```bash
   ssh deploy@PRODUCTION_HOST
   docker --version
   docker compose version
   docker login ghcr.io  # Use GitHub PAT with packages:read scope
   ```

4. **Trigger deployment:**
   ```bash
   # Rerun the failed workflow
   gh run rerun 37054982482 --repo Bambale0/ooo
   
   # Or wait for next push to main (auto-triggers)
   ```

### Production Host Setup

If production server is not yet configured, follow [docs/OPERATIONS.md](../../docs/OPERATIONS.md):

```bash
# On production host as root or sudo user
useradd -m -s /bin/bash deploy
mkdir -p /opt/neironych/shared/{nginx/ssl,releases}
chown -R deploy:deploy /opt/neironych

# As deploy user
sudo -u deploy bash
cd /opt/neironych/shared

# Create .env from repository .env.example
# IMPORTANT: Use production values, not test defaults
cat > .env << 'EOF'
APP_ENV=production
APP_NAME=neironych
API_PREFIX=/api/v1
DATABASE_URL=postgresql+asyncpg://neironych:PRODUCTION_PASSWORD@postgres:5432/neironych
# ... (see .env.example for full list)
ASALE_API_KEY=sk-asale-lYcj7Ai6Ktxw4M1xxj03WQPsy7sRa4xf
EOF

# Create .backup.env (see ops/backup/README.md)
# Install TLS certificates in nginx/ssl/
# Set up Docker and log in to GHCR
```

### Troubleshooting

**Error: "gh CLI not found"**
```bash
# Install GitHub CLI
# https://cli.github.com/
```

**Error: "Not authenticated with GitHub"**
```bash
gh auth login
# Follow prompts
```

**Error: "Cannot access repository"**
- Verify you have admin access to `Bambale0/ooo`
- Check authentication: `gh auth status`

**Error: "Could not fetch SSH host key"**
- Verify production host is reachable: `ping PRODUCTION_HOST`
- Check SSH port is open: `nc -zv PRODUCTION_HOST 22`
- Verify firewall allows SSH connections

**Error: "SSH key file not found"**
- Check path is correct
- Use absolute path or `~/` for home directory
- Ensure private key (not .pub) is used

### Security Notes

⚠️ **Secrets Security:**
- SSH private key is stored as GitHub encrypted secret
- Never commit private keys to Git
- Rotate keys periodically
- Use dedicated deploy key with minimal permissions

⚠️ **ASALE_API_KEY:**
- Must be added manually to production `.env`
- Never committed to Git
- Stored only on production host
- Verify `.env` permissions: `chmod 600 /opt/neironych/shared/.env`

### Related Documentation

- [../../docs/OPERATIONS.md](../../docs/OPERATIONS.md) - Production operations guide
- [../../docs/HOST_AUTODEPLOY.md](../../docs/HOST_AUTODEPLOY.md) - Deployment workflow details
- [../../DEPLOYMENT_SUMMARY_20261002.md](../../DEPLOYMENT_SUMMARY_20261002.md) - Current deployment status
