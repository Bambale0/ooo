# Isolated local media consistency measurement

## Scope and financial meaning

This opt-in component provides a **conservative local duration estimate**, not a
guarantee about ArgoLink's eventual billable seconds. Only completed provider
usage is authoritative for settlement. The application retains its conservative
reservation behavior when the checker is absent, busy, unsupported, inconsistent,
or over a resource/deadline limit. No historical snapshots or charges change.

The returned rational duration is the maximum of corroborated container timing,
decoded H.264 cadence/presentation span and decoded AAC sample duration. AAC
priming and the entire final padded AAC frame are retained conservatively. A
nominal five-second video with audio can therefore round up to six seconds. The
caller rounds each component upward without binary floating-point money math.

Nothing is trimmed, padded, resampled, remuxed, re-encoded into an output asset,
or altered in the uploaded input. FFmpeg's full-decode null sink creates no media
file. SHA-256 before and after decoding must match the exact staged/uploaded bytes.

## Boundary

The application fetches with its existing pinned public-HTTP protections and
uploads outside the checker. It sends only bytes through a private Unix socket:

- Request: `NMP1`, unsigned 32-bit byte length, 32-byte SHA-256
- Checker responds with a bounded ready/error message before receiving the body
- Body: exactly the declared bytes, streamed in 64 KiB chunks, maximum 100 MiB
- Response: at most 1,024 bytes of JSON containing rational numerator/denominator
  pairs, unchanged hash and `video/mp4` or `video/quicktime`

There are no URL, filesystem path, protocol, codec, filter or arbitrary option
fields. The application calls `media_probe.client.measure_file` on a seekable
read-only staged file; the client rewinds it, does not close it, and accepts the
remaining total-preflight budget. Fetch + check + upload share a maximum of 45
seconds. The server permits one active job and immediately refuses additional
jobs without queuing/spooling their bodies. EOF, cancellation, timeout, shutdown
and errors cancel/reap decoder children and remove the private temporary input.
Client teardown aborts the transport synchronously, so a stalled close cannot
extend the caller deadline or swallow external cancellation.

## Validation

The deliberately narrow subset is self-contained nonfragmented MP4/MOV with an
`ftyp` major brand from the small allowlist, one H.264 video and optional one AAC-LC
audio track. Supported H.264 is 8-bit 4:2:0 Baseline/Main/High, constant 1–60 fps,
at most 1,800 frames, dimensions at most 4,096 per side and 4,096 × 2,160 pixels.
AAC-LC is mono/stereo at 32, 44.1 or 48 kHz, at most 1,500 frames. Maximum decoded
estimate is 31 seconds to account conservatively for a nominal 30-second input's
codec overhead; model-specific admission limits remain the caller's decision.

A narrow box-policy check excludes multiple/rate-changing/empty edits, fragments,
compressed movie headers and external data references. It never establishes
financial duration. Canonical single-entry priming/composition-delay edits must
agree with independently decoded raw timing; complex edits fail closed.

FFprobe fully reads packets and decodes every frame with edit lists disabled.
Frame counts, packet coverage, PTS, CFR cadence, dimensions, decoded sample counts
and declared track/container timing must agree. FFmpeg independently performs a
complete error-fatal decode to a null sink. The identity `ashowinfo` filter also
checks actual decoded audio sample rate/channels/counts, which FFprobe frame JSON
does not expose completely. Any decoder error, nonzero exit, truncated evidence,
missing frame, unexplained timing discrepancy or unsafe structure fails closed.

Native options permit only the local-file protocol, MOV demuxer and H.264/AAC
codecs. External MOV references and absolute external paths are disabled. Output
is capped (32 KiB metadata, 8 MiB packet/frame evidence, 64 KiB errors, 1 MiB full
FFmpeg log). Children have 1.5 GiB address-space, 40-second CPU, no regular-file
output, no core dump, and 64-open-file limits, supplementing container limits.

Each native child additionally receives a fail-closed filter through libseccomp
(not hand-maintained syscall numbers). It denies sockets, file writes/mutations,
ptrace/process-vm/pidfd access, non-thread forks/clones, process-group escapes,
signals to other processes and indirect io_uring mutation. Read-only opens and
ordinary same-thread-group clones remain permitted. All other descriptors are
closed before exec. The entire process group is killed even after its leader
exits; Docker init reaps orphaned descendants. The long-lived broker sets and
verifies PR_SET_DUMPABLE=0 before listening to prevent same-UID decoder reads of
its /proc memory/descriptors. These controls confine a compromised decoder; they
do not make its own measurement trustworthy proof of provider billing.

## Packaging and activation gates

`Dockerfile.media-probe` builds a separate standard-library-only service image.
It deliberately has no app dependencies, settings imports, keys, `.env` or DB
credentials. A build requires an explicitly supplied immutable official
`python:3.12-slim-trixie@sha256:…` base. The currently tested FFmpeg package is
`7:7.1.5-0+deb13u1`, verified in the
[Debian package tracker](https://security-tracker.debian.org/tracker/source-package/ffmpeg).
The tracker lists open issues in other FFmpeg components: do not describe this
package or an unscanned image as vulnerability-free. Image/dependency scanning
and reachability review remain prerequisites to activation.

`docker-compose.media-probe.yml` is a proposed opt-in overlay, not an update to
any running production configuration. Only including it sets the application's
`MEDIA_PROBE_SOCKET`. The checker does not inherit the production runtime anchor:

- UID/GID 10001, network none, read-only root, all capabilities dropped
- No new privileges; Docker default seccomp retained; no privileged mode
- 2 CPUs, 2 GiB memory including swap, 64 PIDs, private IPC namespace
- Private 384 MiB noexec/nosuid/nodev tmpfs for input files
- Separate 1 MiB shared Unix-socket volume, read-only in the app; no host ports or arbitrary host binds
- No Docker socket, DB service, application files, env file or secrets

Startup checks Linux cgroup v2 limits, nonroot identity, environment allowlist,
capability/no-new-privileges/seccomp status, loopback-only interfaces, read-only
root and bounded private tmpfs. These are defense-in-depth checks, not a substitute
for verifying the actual container configuration or Docker's seccomp profile.

The app image must include the client/contract package. Its Docker builder needs
`COPY media_probe ./media_probe` before installing the application package.
A distinct immutable checker-image build and deployment wiring are required.
Existing deployment workflows do not automatically activate this overlay.

## Verification status and required CI

The local tests use generated synthetic fixtures only. The regression creates a
real MP4 whose displayed duration is `1.000000` but whose decoded AAC samples span
over four seconds; it is rejected. Synthetic five-second/20 MiB and twenty-second/
80 MiB inputs are accepted unchanged. In-memory transport tests cover streaming,
one-job concurrency, timeouts, cancellation, cleanup and invalid requests.

Real Unix-socket and container behavior require the dedicated runtime CI gate.
Socket integration tests require `MEDIA_PROBE_RUN_UDS_TESTS=1` on a supported
Linux test host; they are explicitly skipped otherwise. Passing local unit and
synthetic-decoder tests is not a substitute for this runtime gate.

The new `Media probe containment` workflow uses pinned checkout/setup actions,
read-only repository permissions and no secrets. It runs actual Unix-socket unit
integration, resolves and records the official Python manifest digest, builds a
local checker image, and runs `ops/smoke/media_probe.py`. The smoke gate verifies
Docker containment, actual UDS/full-decode success for 5/20-second synthetic media,
the adversarial timing rejection, malformed/truncated rejection, post-error
recovery, cleanup, and refusal to start as root. It also verifies native child
socket-replacement denial, forbidden helper forks, broker /proc privacy, and
whole-group cleanup using a trusted deliberately unfiltered fork control fixture.
Actual-image checks also exercise output/memory exhaustion, a short CPU deadline,
external cancellation and live UDS cancellation/deadline recovery.
A separate installed-app-image import check proves the disabled feature can load
its packaged client. It publishes no image. This gate
must pass for the exact final commit, and the deployment image must then be built,
scanned and pinned separately before activation. A written workflow is not a
passed runtime check.

Relevant external references: [FFprobe](https://ffmpeg.org/ffprobe.html),
[FFmpeg MOV options](https://ffmpeg.org/ffmpeg-formats.html), and
[FFmpeg codec limits](https://ffmpeg.org/ffmpeg-codecs.html).

The workflow does not perform a vulnerability scan of the built image. A scan
and reachability review of the exact deployment-image digest remain explicit
release gates in addition to the runtime tests.
