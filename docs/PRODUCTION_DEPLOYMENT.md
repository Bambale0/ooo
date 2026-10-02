# Production Deployment Guide

## Prerequisites

All production secrets are configured in GitHub Actions environment `production`:

- ✅ `DEPLOY_HOST`: 5.35.124.201
- ✅ `DEPLOY_SSH_KEY`: SSH private key for deploy user
- ✅ `DEPLOY_KNOWN_HOSTS`: SSH host fingerprints
- ✅ `ASALE_API_KEY`: Asale provider API key

## Server Setup

### SSH Access

The deployment uses SSH key authentication as user `deploy` on port 2022:

```bash
ssh -i ~/.ssh/deploy_key -p 2022 deploy@5.35.124.201
```

**Important:** Ensure `~/.ssh/authorized_keys` exists for the `deploy` user with correct permissions:

```bash
# On server as deploy user
chmod 700 ~/.ssh
chmod 600 ~/.ssh/authorized_keys
```

### Application Directory

Production application runs from `/opt/neironych/`:

- Shared config: `/opt/neironych/shared/.env`
- Application code: deployed by GitHub Actions

## Deployment Flow

1. **Feature development** → PR to `dev` branch
2. **Testing on dev** → validation and integration testing
3. **Production release** → PR from `dev` to `main`
4. **Automated deployment** → GitHub Actions deploys `main` to production

## Manual Deployment Trigger

If automatic deployment doesn't trigger, run manually:

```bash
gh workflow run deploy.yml --repo Bambale0/ooo --ref main
```

## Verification

After deployment, verify:

1. Application is running: `systemctl status neironych-*`
2. Migrations applied: check database schema version
3. API health: `curl https://api.neironych.ru/health`
4. Logs: `journalctl -u neironych-* -f`

## Rollback

To rollback to a previous version:

1. Find the last good commit SHA
2. Create a PR reverting to that SHA
3. Merge through normal flow

## Support

For deployment issues, check:
- GitHub Actions logs: https://github.com/Bambale0/ooo/actions
- Server logs: `/var/log/neironych/`
- SSH connectivity from Actions runner
