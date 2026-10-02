# GitHub autodeploy for an existing host

A Git push alone is not production delivery. The release workflow follows a
successful **push CI on main in this repository**, uses the exact SHA, rejects
superseded runs and retains the explicit `PRODUCTION_DEPLOY_ENABLED=true` gate.
PR/fork CI cannot start a production release. `production` environment protections
still apply. The gate is a GitHub Actions variable, not a file committed to Git.

## One-time operator setup

Keep the existing Actions secrets `DEPLOY_HOST`, `DEPLOY_SSH_KEY` and
`DEPLOY_KNOWN_HOSTS`. Pin the real server key; never disable host-key checking.
Optional repository/environment variables:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DEPLOY_ROOT` | `/opt/neironych` | Existing runtime directory |
| `DEPLOY_SSH_USER` | `deploy` | Already provisioned SSH deployment user |
| `DEPLOY_SSH_PORT` | `22` | SSH port |
| `DEPLOY_USE_SUDO` | `false` | Use already-authorized, noninteractive sudo on this host |

The workflow does not provision SSH users, sudo permissions, secret values or
registry credentials. Verify SSH access, Docker/Compose and Python 3.10+ and GHCR
pull access under the chosen execution identity before enabling the repository
variable `PRODUCTION_DEPLOY_ENABLED=true`. It first checks SSH/Docker access before
building/publishing. After enabling the gate, rerun a successful **main CI** for
the current head (not a PR run) to deploy without an empty source commit.

## Automatic topology selection

When `DEPLOY_ROOT/compose.host.yml` exists, the workflow selects the existing-host
path. It uses the host's `docker-compose.prod.yml`, `.env` and `compose.host.yml`.
The resolved Compose project must already contain exactly one running `app`,
`worker`, `webhook_worker` and `telegram` with a common image and recorded revision.
This preserves `ooo`, loopback port 18000, host proxy, DNS/egress overrides, limits,
volumes and existing credentials without hardcoding those values in the script.

Only the four application processes are recreated with `--no-deps`. PostgreSQL,
Redis and host Nginx are neither replaced nor restarted. The new image is pulled
by SHA tag, its OCI revision label is checked, and its local content-addressed
image ID is used for the actual switch. All four processes must run that image
and report the expected `APP_REVISION`. API readiness is verified inside its
container; it does not guess the host's published port.

This path deliberately supports **code-only releases**. `alembic current
--check-heads` must pass before any service replacement. A pending migration
stops deployment; apply it separately using the host's verified encrypted backup
and migration procedure, then rerun main CI. Repo Compose/infrastructure changes
also require a separately reviewed host-configuration update. Never use the
standalone backup/PITR layout as a substitute for the host's real backup system.

If there is no host override, the original standalone versioned Compose/PITR/
containerized-Nginx deployment path remains available.

## Recovery and evidence

A host lock prevents overlapping executions of the new host release script. Before
switching, a private `release-backups/<sha-prefix>-<unique>/` directory receives
`.env`, runtime Compose files, `REVISION` and `rollback.json`. No secret values or
raw Docker/Compose output are emitted into Actions logs.

On a partial startup, readiness failure or selector-persistence error, the script
restores the **actual previous local image ID**, original `.env`, `REVISION` and
`current` link, then verifies all four processes and API readiness again. A failed
rollback remains a failed deployment; inspect the host using the private recovery
manifest. A hard host crash or killed runner requires reconciliation from that
manifest; the script does not claim crash-proof distributed transactions.

On success, only `NEIRONYCH_IMAGE` and `APP_REVISION` in `.env` are updated (all
other bytes are preserved), together with `REVISION` and `current`. This prevents
the normal host start script from reverting to an old image after a reboot.
Repeated delivery of the already-running revision is verified without restarting
containers. Progress markers are `DEPLOY_PREPARE`, `DEPLOY_SWITCH`,
`DEPLOY_ROLLBACK`, `ROLLBACK_VERIFIED`, `DEPLOY_VERIFIED` or
`DEPLOY_ALREADY_VERIFIED`.

Automated unit tests use a fake Docker boundary; they do not assert that SSH
credentials, registry access or the live host have been provisioned. Only a
successful Actions deployment plus a live revision check proves production delivery.
