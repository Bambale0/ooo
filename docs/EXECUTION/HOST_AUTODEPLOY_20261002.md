# Existing-host autodeploy - 2026-10-02

Baseline: 7ad76c8217bd7f886d406c70b6bd9dd7809b4d5f (main, PR #68).
Scope: owner requests delivery through GitHub and automatic production deployment.

Observed: deploy.yml is gated by PRODUCTION_DEPLOY_ENABLED and assumes standalone
project neironych, port 8000, shared secrets and containerized Nginx/PITR. The
recorded production host instead uses root .env + compose.host.yml (project ooo,
loopback 18000), existing host proxy/egress and local rollback images.

Plan: preserve standalone behavior; add an existing-host code-release path selected
by the host override. Preserve host configuration, secrets, volumes and proxy.
Check all four runtime processes and readiness; preserve the actual local image
for rollback, not a guessed registry tag. Persist image/revision for normal host
restarts. Do not arm the GitHub deploy gate without verified SSH prerequisites.
Host-mode schema changes fail closed pending a separately backed-up migration.

Checks: test first with a fake command boundary, run local regressions, inspect
workflow trust/SSH boundaries, publish branch + PR, verify exact-SHA CI. No live
customer updates, provider calls or financial data are needed by these tests.
Guidance: AGENTS.md; .agents deployment-procedures and verification-before-completion.

Local evidence: 17 tests passed (fake Docker boundary, file persistence and Bash
syntax), compileall passed. Initial test collection failed before the new module
was implemented. Full application CI and production execution are not yet claimed.
The existing standalone deployment contract tests have not been edited.
GitHub activation still requires its Actions variable and verified SSH/GHCR access.

Review added a private remote upload directory (0700), checksum verification
before extraction, and a regression test. Final focused local suite: 18 passed.
PR #71 includes the change; exact-SHA CI and live activation remain separate gates.
