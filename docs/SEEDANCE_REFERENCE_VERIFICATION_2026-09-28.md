# Seedance 2.5: reference verification — 2026-09-28

Scope: the operator-supplied JPEG as `reference_images`, with an operator-supplied
ArgoLink test key. Reference SHA-256:
`821309168b56eeddd72daedb8a32c9f67037605ad979ed0bd77c6a59ac1cd627`.
Credentials, signed media URLs, private job IDs and media bytes are excluded from Git.

## Provider evidence

The [current catalog](https://argolink.io/api/catalog/v1/models?page_size=100)
revision is `2f175f3af2a80acb`. Seedance 2.5's category, endpoint and procurement
match the reviewed entry: $0.078/$0.17/$0.43 per second at 480p/720p/1080p.
Other models were not re-certified or imported. The
[official contract](https://argolink.io/en/docs) was checked for frame ratios,
reference modes and edit controls.

The catalog as a whole has drifted: 7 added models, 3 removed models and 17 changed
reviewed entries. A full catalog import still refuses that drift. This Seedance
verification does not authorize replacing other models or changing retail prices.

| Path | Input | Result |
| --- | --- | --- |
| Project provider adapter, live ArgoLink | One JPEG reference, 4s, 480p, 9:16 | PASS: completed, billed 4s, $0.312 actual key usage |

The 480p job completed after approximately 6½ minutes. Upload ticket returned 201,
image PUT returned 200, protected content Range returned 206 with 1,024 bytes.
The complete 2,021,728-byte MP4 was downloaded and inspected with ffprobe:
480×854, 24 fps H.264 video, AAC audio, container duration 4.064 seconds.
A sampled frame retained the supplied illustrated family and robot. The requested
9:16 composition crops some of the poster lettering; this is not a guarantee of
pixel-exact reference reproduction or typography preservation.

## Changes

- Native and normalized Seedance frame validation now accepts documented
  `adaptive`; fixed ratios remain restricted to the 2.0 family in frame mode.
- Seedance 2.5 edits preserve documented `duration: -1` / `aspect_ratio: adaptive`
  controls and the existing conservative reserve; fixed dimensions are rejected.
- The smoke runner accepts explicitly budgeted unrestricted keys. Unknown quota,
  non-finite budgets and budgets beyond an explicit quota fail closed.
- `--video-mode reference` uses `reference_images`. The default remains
  `first_frame`. Targeted `--model` runs compare selected models' actual category,
  endpoint and Decimal procurement data, so unrelated catalog changes do not
  block a verified Seedance smoke. Full-catalog and catalog-import gates remain.
- Smoke completion also requires readable video content; inaccessible content
  and polling timeout are not reported as PASS.
- A retry scheduling test now treats SQLite's timezone-less UTC timestamps as
  UTC, matching application behavior in non-UTC development environments.

## Repeatable bounded smoke

Use a private JSON file containing `argolink_key`; never place it in the repository.
From an installed project environment:

```bash
python -m ops.smoke.argolink \
  --secret-file /private/credentials.json \
  --report /private/seedance-reference-new-run.json \
  --model seedance-2.5 \
  --video-mode reference \
  --reference '/path/to/reference.JPG' \
  --budget-usd 0.32 \
  --execute
```

Without `--execute`, this only checks quota and the selected catalog entry. An
existing report path is refused to prevent accidental replay. The runner does
not retry uncertain paid submissions. A timed-out/uncertain job needs provider
reconciliation before starting another run. The public smoke report excludes
upstream job IDs and is not a durable job-resumption database.

## Verification boundaries

Local validation: **288 tests passed**, including the PostgreSQL integration
cases, with no skips. All 20 migrations applied to a fresh isolated PostgreSQL 16
database; `alembic check` found no schema drift. Ruff and `git diff --check` passed.

Contract tests cover frame/reference separation, documented aliases, edit
dimension controls, and rejection before financial reservation. Gateway tests
cover reference queue submission, repeat requests, repeated settlement, protected
content and actual usage accounting. Live frame/adaptive/edit combinations and
1080p were not exercised in this run.

This targeted verification does not mark all project launch gates complete.
Production payment settlement, deployment infrastructure, off-site recovery and
the remaining provider configurations retain their separate acceptance gates.

Skills used: `team-lead`, `bot-tester`, and project-local `systematic-debugging`
and `verification-before-completion`.
