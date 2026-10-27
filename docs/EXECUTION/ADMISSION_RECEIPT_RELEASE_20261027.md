# Gateway rejection receipt release

User explicitly authorized gateway-first merge/deploy on 2026-10-27.
Baseline production/main b59e5e849d8c655bbec64950ac4ceea1164b9a86; PR #125 head 9842df1.
Refreshed main in the PR before fixes. Reproduced MissingGreenlet after a real
PostgreSQL rollback/accepted synchronous overlap. Reproduced quota query selecting
an ordinary-history index. Fix uses snapped auth IDs after rollback and a concurrent
partial PostgreSQL index (migration 0037; rollback drops only this index).
Authoritative product brief and launch checklist now state the explicit opt-in
exception. Legacy key reuse and all money/routing/policy behavior are preserved.

Verification seams: real PostgreSQL overlap, EXPLAIN quota index, concurrent fences,
quota/tenant isolation, HTTP mocked-provider receipts, migrations and full CI.
No paid provider request. Live verification must follow exact merge-SHA deployment.

Guidance: project AGENTS/.agents, diagnosing-bugs, TDD, pytest-skill,
verification-before-completion; companion backend integration and Release It.
