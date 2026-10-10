# Isolated input-video measurement for reserves

The owner approved full video/audio decoding on 2026-10-09, without transcoding
or changing media quality. This feature remains disabled unless the separately
reviewed checker overlay and immutable checker image are explicitly deployed.

## Financial scope

Only Seedance 2.5 family video-reference requests are eligible; edit is limited to
the documented `seedance-2.5` edit contract. Other models and their fallback lanes
keep existing quotes. Prices, FX snapshots and actual-usage settlement do not
change. For edit, reserve input plus the same output duration; for reference mode,
reserve requested output plus every measured input. Round each component upward.
AAC frame padding can make a nominal five-second input reserve six seconds.

This is a conservative local estimate, not a guarantee of the provider's exact
billing algorithm. Argo publishes billed usage after completion and does not
document an authoritative preflight duration endpoint. Unsupported, inconsistent,
busy or slow inputs retain the established maximum reserve; a partial decode is
never evidence for a smaller amount.

## Input identity and network boundary

The application downloads public HTTPS input through the existing IP-pinned
transport: every DNS address must be public, redirects are rejected, proxy
inheritance is disabled, and no provider credentials accompany input/storage
URLs. Downloads are streamed into private temporary files, capped at 100 MiB per
input. At most two preparations per process and one per partner run immediately;
there is no preparation queue holding database connections. No financial or
credential row lock is held during preparation. A concurrent duplicate may win
admission using the ordinary maximum; the losing preparation can leave only a
free expiring copy, never a second reserve or paid job. Across app processes,
free staging copies are not globally deduplicated.

The checker receives only bytes, their length and SHA-256 over a bounded Unix
socket protocol. It receives no URL, prompt, account information, key, arbitrary
path or decoder option. It fully decodes supported H.264/AAC MP4/MOV tracks and
cross-checks container timing, decoded frames, timestamps and actual audio sample
counts/rate. Ambiguous tracks, variable cadence and conflicting edit/timing
interpretations are rejected. No trimming, padding, remuxing or re-encoding occurs.

The exact checked bytes are uploaded with a fresh server-owned Argo ticket, then
downloaded once for byte-size/SHA-256 verification. The write URL is never stored,
logged or exposed. Provider storage is a trusted boundary, not a documented
write-once store. The financial snapshot preserves original request identity and
separately persists effective read URLs, evidence, hash and expiry. Retries use
those same references, never a fresh download of a mutable original URL.

## Containment and bounds

The separate image contains only the checker and pinned FFmpeg runtime, with no
application secrets or database drivers. Proposed runtime: UID/GID 10001,
network_mode:none, read-only root, all capabilities dropped, no-new-privileges,
default seccomp, no Docker socket/host paths/ports/env-file, two CPUs, 2 GiB memory,
no extra swap, 64 processes and 384 MiB private noexec/nosuid/nodev tmpfs. The only
shared mount is a tiny socket tmpfs, read-only in the application. Startup checks
required containment; the broker is non-dumpable. A fail-closed per-child seccomp
filter further denies IPC, filesystem mutation, foreign-process access and helper
forks while allowing decoder threads.

One checker job runs at a time. Input/output sizes, native allocations, CPU time,
processes and pipe output are bounded. Cancellation kills/reaps native processes
and erases temporary files. The whole application preparation has a 45-second
deadline covering download, check, upload and readback. Failure keeps the maximum.

At least one hour of read lifetime must remain when admitting and immediately
before dispatch, including after provider pacing. An expired never-submitted job
uses existing safe cancellation; a previous provider obligation remains held for
reconciliation. Expiry never authorizes a replacement paid submission.

## Deployment gates

No production activation is implied by a draft PR or passing unit tests. Required:
independent security review; real Unix-socket and hardened-container CI; synthetic
full-decode and adversarial timestamp tests; existing finance/recovery regression
suite; image vulnerability and decoder-reachability review; capacity checks.
Resolve and record an official Python base digest, build a separate checker image,
scan it and pin the tested resulting digest before rollout.

The existing-host release path currently replaces only app/worker/webhook/telegram
and preserves host Compose topology. It does not activate this overlay or a new
service. The release owner must approve and verify the exact checker image,
socket-only mount, app socket setting and resource limits through the supported
host procedure. Do not weaken deployment gates or use the standalone topology as
an alternative. Disabling the optional socket setting restores maximum quotes;
already admitted measured jobs retain their stored media and expiry checks.

Unit, synthetic native-decoding and in-memory protocol checks do not replace
the mandatory real Unix-socket/container and PostgreSQL CI gates.

Sources:
- https://argolink.io/en/docs (Seedance uploads, reference limits and actual usage)
- https://argolink.io/en/models/seedance-2.5 (edit input/output billing)


## Integration update (2026-10-10)

Merged with production video-input pricing: reference_videos selects edit retail
even in reference mode with photo. Verified preflight only changes reserved
billable seconds, not pricing, provider procurement rates, or actual settlement.
Unverifiable input retains maximum; clients cannot declare trusted duration.

CI-verified app releases now include checker sources in the immutable release
bundle for separately approved host activation. Regular deploys never auto-enable
the socket or new Compose service. Keep it disabled until image scanning,
hardening validation and a documented existing-host overlay with rollback.
