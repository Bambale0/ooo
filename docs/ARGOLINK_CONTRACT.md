# ArgoLink contracts and resale API

Checked against [official documentation](https://argolink.io/en/docs#/) and the
[public catalog](https://argolink.io/api/catalog/v1/models?page_size=100), revision
`5ce662ba88c3d07f`, on 2026-09-23. The reviewed snapshot contains **40 models:
24 text, 9 image, 7 video**. Procurement data lives in `app/contracts/catalog.json`;
retail prices are always configured by the operator.

## Native routes

| Route | Behavior |
| --- | --- |
| `GET /v1/models` | Enabled resale models only |
| `POST /v1/responses` | Responses JSON/SSE; maps `previous_response_id` within the same partner and upstream credential |
| `POST /v1/chat/completions` | Chat Completions JSON/SSE; streamed usage requested for settlement |
| `POST /v1/messages` | Messages JSON/SSE; Bearer or `x-api-key`, forwards Anthropic version/beta headers |
| `POST /v1/images/generations` | All 9 catalog image models, native JSON |
| `POST /v1/images/edits` | Native JSON or multipart, including repeated image fields and masks |
| `POST /v1/videos/generations` | All 6 catalog video models, asynchronous JSON |
| `GET /v1/videos/{id}` | Tenant-isolated status; own request IDs |
| `GET /v1/videos/{id}/content` | Protected MP4 stream, Range supported |
| `POST /v1/media/uploads` | Provider upload ticket; client PUTs bytes directly to storage |

Use a **partner key**, never an upstream key. Paid POST requests require
`Idempotency-Key` (8–160 characters). The same video request returns its original
ID. A repeated synchronous request returns 409 with its original `request_id`;
result files are not retained or replayed. Returned image URL metadata remains in
generation history (`result_url` / `result_urls`), per product brief §53; CDN links
can expire and should be downloaded promptly. A different body under the same key is
an idempotency conflict. Retry a definitively rejected request with a new key;
never change the key to retry an ambiguous outcome without reconciliation.

The existing `/api/v1/generations` video API remains compatible with its existing
schema. New native controls belong on `/v1/...`; they are not silently discarded
by the older normalized schema. Native model options such as tools, thinking,
structured output and vision inputs pass through. The provider validates their
model-specific semantics. There is no automatic conversion to a different model
or protocol when an upstream route fails.

Cancellation before provider submission is available at
`POST /api/v1/generations/{id}/cancel`; it is tenant-scoped and idempotent.
An already submitted/uncertain job returns 409 because the reviewed provider
contract does not expose a safe cancellation endpoint.

## Video reference contracts

| Family | Output seconds | Resolutions | Images / videos / audios / combined |
| --- | --- | --- | --- |
| Seedance 2.0 | 4–15 | 480p, 720p, 1080p, 4k | 9 / 3 / 3 / 12 |
| Seedance 2.0 Mini, Fast | 4–15 | 480p, 720p | 9 / 3 / 3 / 12 |
| Seedance 2.5 | 4–30 | 480p, 720p, 1080p | 30 / 10 / 10 / 50 |
| Wan 3 | 2–30 | 480p, 720p, 1080p | 10 / 5 / 5 / 20 |
| Grok Video 1.5 | 1–15 | 480p, 720p, 1080p | up to 7 image references; no video/audio references |
| MiniMax H3 | 4–15 | 768p (default), 2k | 9 / 3 / 3 / 15 |

Seedance accepts first/last frames, reference combinations, documented aliases,
and 2.5 video editing. HTTPS references and frame/reference exclusivity are
validated before reserving funds. The 2.0 family requires visual input alongside
audio; 2.5 allows audio alone. Wan requires a prompt and supports its audio toggle.
Grok image references are capped at 720p; its native `image` first-frame input is
preserved, including data URLs/file IDs supported upstream.

MiniMax H3 requires a prompt, accepts first frame alone or first+last frame, and
supports `adaptive` aspect ratio with references. Audio references need an image
or video; generated audio cannot be disabled. Native aliases and HTTPS upload
tickets are supported. For MiniMax `size`, the documented 1366x768 mapping is
recognized; other pixel dimensions require explicit `resolution` because the
current docs do not specify the 2k short-side boundary consistently. This is a
billing validation constraint, not a silent choice of a different output tier.

The catalogue changed during verification: revision `5ce662ba88c3d07f` added H3
and three Seedance 480p tiers. Procurement uses the current live catalog (H3
0.044/0.072 USD per second), not older cached pages with different prices/limits.
New tiers still need operator-set retail prices and successful configuration smoke.

Reference video time is billable. The gateway reserves the documented upper bound
for the request's reference inputs, then settles reported actual seconds. It never
trusts a client-supplied reference duration. Seedance/Wan report
`usage.billed_seconds`; live Grok responses instead report `video.duration`.
Media metadata/format validation performed by ArgoLink can still reject a job.
Gemini Omni is mentioned in documentation but absent from the reviewed priced
catalog; it is not advertised as an available resale model.

## Financial contract

- Retail RUB and procurement rates are snapshotted before submission. Actual
  usage adjusts the reserve with separate, idempotent ledger entries. Old prices
  and original reservation snapshots are not rewritten.
- Text tariffs use `million_tokens`: `input_tokens`, `cached_input_tokens`,
  `cache_write_tokens`, `output_tokens`, resolution `default`. Explicit 1-hour
  cache creation additionally requires a configured `cache_write_1h_tokens` tariff
  and capability; it is never priced as a 5-minute write.
- Text holds use a conservative input allowance and requested output maximum.
  Opaque multimodal/context references or an omitted output maximum need a larger
  hold. These holds do not alter the native request. Final charges use actual
  usage; tiny charges round once to 0.01 RUB with ROUND_HALF_UP.
- Image tariffs use `generation`, mode `default`, resolution `1K`/`2K`/`4K` as
  applicable. Every returned image is charged. GPT's actual returned pixel size
  determines the tier, including URL outputs inspected through a DNS-pinned
  HTTPS transport. No generated image bytes are retained in the database.
- Video tariffs use `second`, mode `default`, actual output + reference seconds.
- Missing usage, unexpected server responses, interrupted streams or uncertain
  submits retain their reserve for reconciliation. They are not automatically
  submitted again. Definitive 4xx rejections release the reserve; 429 preserves
  Retry-After. Terminal SSE usage is settled before emitting the terminal event.
- Admission checks retail balance, current wallet-backed working capital and
  current economics. Historical partner coverage is an accounting snapshot, not
  an independent admission gate (product brief §89).

## Import, prices and upstream differences

Admin routes under `/api/v1/catalog`:

1. `GET /contracts` — reviewed models, tariff dimensions, procurement review flags.
2. `GET /contracts/drift` — read-only comparison with the live catalog.
3. `POST /contracts/import` — imports draft models/capabilities; updates reviewed
   procurement on existing prices without changing retail. Refuses unreviewed drift.
4. `PUT /pricing` — explicit retail and positive procurement rates.
5. Set successful integration/docs/smoke gates, then `POST /models/{slug}/enable`.

The live tests found Grok edit charges different from the public catalog:
`grok-imagine-image` $0.04, `grok-imagine-image-2.0` $0.08, and quality $0.0086 for
one default edit in the tested account, versus catalog $0.015. Consequently those
procurement rates are **not overwritten automatically by catalog import**. Edit
requests configured below the observed cost floor fail before reservation. Verify
account/tier-specific procurement before enabling these variants for customers.

`GET /api/v1/providers/partners/{partner_id}/upstream-usage` is admin-only. Live
quota counters converged with actual usage after asynchronous settlement; an
in-flight quota read is not a reservation or a hard concurrency guarantee.

See [the live verification matrix](LIVE_VERIFICATION_2026-09-23.md). Adapter
coverage and a passing isolated contract test do not imply every upstream model
or protocol is currently operational.

## Reconciliation

`GET /api/v1/providers/reconciliation` lists uncertain requests. POST to
`/reconciliation/{generation_id}` accepts a reason and one of:

- `attach_video` + verified upstream task ID: resumes polling without resubmission;
- `completed` + verified usage units: settles once using the original tariff;
- `not_accepted`: releases once after the operator confirms no paid job exists.

Actions are admin-only and stored in the request audit snapshot. Conflicting
repeat actions fail. Do not release an uncertain request solely because the
client disconnected or a proxy returned 5xx.
