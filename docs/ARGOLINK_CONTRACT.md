# ArgoLink contracts and resale API

Checked against [official documentation](https://argolink.io/en/docs#/) and the
[public catalog](https://argolink.io/api/catalog/v1/models?page_size=100), revision
`5ce662ba88c3d07f`, on 2026-09-23, with the reviewed Self-Developed NSFW additions
documented below on 2026-10-01. The reviewed contracts contain **42 models:
24 text, 9 image, 9 video**. Procurement data lives in `app/contracts/catalog.json`;
retail prices and model availability are configured by the operator in the database.

## Native routes

| Route | Behavior |
| --- | --- |
| `GET /v1/models` | Enabled resale models only |
| `POST /v1/responses` | Responses JSON/SSE; maps `previous_response_id` within the same partner and upstream credential |
| `POST /v1/chat/completions` | Chat Completions JSON/SSE; streamed usage requested for settlement |
| `POST /v1/messages` | Messages JSON/SSE; Bearer or `x-api-key`, forwards Anthropic version/beta headers |
| `POST /v1/images/generations` | All 9 catalog image models, native JSON |
| `POST /v1/images/edits` | Native JSON or multipart, including repeated image fields and masks |
| `POST /v1/videos/generations` | Reviewed catalog video models, asynchronous JSON |
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
| Seedance 2.0 Self-Developed NSFW | 4–15 | 720p, 1080p, 4k | 9 / 3 / 3 / 12 |
| Seedance 2.0 Mini, Fast | 4–15 | 480p, 720p | 9 / 3 / 3 / 12 |
| Seedance 2.5 | 4–30 | 480p, 720p, 1080p | 30 / 10 / 10 / 50 |
| Seedance 2.5 Self-Developed NSFW | 4–30 | 720p, 1080p | 30 / 10 / 10 / 50 |
| Wan 3 | 2–30 | 480p, 720p, 1080p | 10 / 5 / 5 / 20 |
| Grok Video 1.5 | 1–15 | 480p, 720p, 1080p | up to 7 image references; no video/audio references |
| MiniMax H3 | 4–15 | 768p (default), 2k | 9 / 3 / 3 / 15 |

Seedance accepts first/last frames, reference combinations, documented aliases,
and 2.5 video editing. HTTPS references and frame/reference exclusivity are
validated before reserving funds. The 2.0 family requires visual input alongside
audio; 2.5 allows audio alone. Wan requires a prompt and supports its audio toggle.
Grok image references are capped at 720p; its native `image` first-frame input is
preserved, including data URLs/file IDs supported upstream.

The [Seedance 2.0 Self-Developed NSFW](https://argolink.io/en/models/seedance-2.0-self-developed-nsfw)
and [Seedance 2.5 Self-Developed NSFW](https://argolink.io/en/models/seedance-2.5-self-developed-nsfw)
contracts use exact model IDs `seedance-2.0-self-developed-nsfw` and
`seedance-2.5-self-developed-nsfw`. They retain their respective family reference,
frame, audio and billing rules; the 2.5 variant also supports video editing.
Although both NSFW model pages advertise 480p, live API rejections allow only
720p, 1080p or 4k for 2.0 (2026-10-01), and 720p or 1080p for 2.5 (2026-09-30).
The gateway therefore rejects 480p for both exact NSFW IDs before reserving funds;
ordinary Seedance 2.0 and 2.5 still accept it.

Both variants can be published for all partners through the authenticated
`POST /api/v1/catalog/models/{slug}/enable` workflow after configuration of retail
prices and successful integration, documentation and smoke gates. Their IDs are
not assigned a permanent restricted status in code. Availability remains the
database model status: `production` publishes discovery, pricing and the enabled
model documentation; `restricted` retains the existing per-partner grant behavior.
Contract coverage by itself does not confirm a successful live generation.

Rechecked Seedance against the provider documentation on 2026-10-28: frame modes
accept explicit `adaptive`; the 2.0 family also accepts fixed ratios, while 2.5
frames accept only `adaptive`. Text and ordinary reference requests still reject
`adaptive`. Seedance 2.5 edit accepts omitted duration or integer `-1`, and omitted
aspect ratio or `adaptive`; fixed output dimensions are rejected. The first video
is the edit source and up to 30 images may steer it; audio and a second source
video remain rejected. `extend` requires a source video, 4–30 output seconds and
no aspect ratio, while optional image/audio references are preserved. Draft preview
forces 480p; its completed Neironych request ID can be submitted within seven days
as the only `draft_task.id`, which is resolved internally to the pinned ArgoLink
task and credential before a 1080p render. Provider task IDs never cross the public
API. Conservative reservations remain until provider-confirmed actual usage.

Special procurement rates are snapshot at admission: ordinary video input uses
0.0523/0.117/0.289 USD per billed second, extend uses the documented official
0.0615/0.1379/0.3398 rates, and draft preview/full use 0.1028 at 480p and 0.5682
at 1080p respectively. Existing partner retail rates are unchanged; admission
fails closed if the selected retail rate no longer covers the applicable cost.

For a supplied image used as a reference, send `reference_images` rather than
`start_image`. A verified 480p request is:

```json
{
  "model": "seedance-2.5",
  "prompt": "Animate the poster in @Image 1 with a gentle camera move.",
  "reference_images": [{"url": "https://your-upload-host/reference.jpg"}],
  "duration": 4,
  "resolution": "480p",
  "aspect_ratio": "9:16"
}
```

The image URL comes from an upload ticket (`POST /v1/media/uploads` followed by
the client PUT). Image-only references add no billable reference-video seconds.
See [the Seedance reference verification](SEEDANCE_REFERENCE_VERIFICATION_2026-09-28.md).

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

Reference video time is billable. The gateway calculates the documented upper
bound only for internal provider-capital admission. No advance debit or hold is
placed on the partner's RUB balance. Final retail billing uses provider-confirmed
actual seconds and never trusts a client-supplied reference duration. Seedance/Wan report
`usage.billed_seconds`; live Grok responses instead report `video.duration`.
Media metadata/format validation performed by ArgoLink can still reject a job.
Gemini Omni is mentioned in documentation but absent from the reviewed priced
catalog; it is not advertised as an available resale model.

## Financial contract

- Retail RUB and procurement rates are snapshotted before submission. Partner
  balances are not debited or held at admission: one idempotent debit is posted
  only after verified actual usage. Historical reserve entries remain immutable;
  any release or late settlement uses separate compensating ledger entries.
  Internal upstream procurement funding and holds are accounted separately.
- Text tariffs use `million_tokens`: `input_tokens`, `cached_input_tokens`,
  `cache_write_tokens`, `output_tokens`, resolution `default`. Explicit 1-hour
  cache creation additionally requires a configured `cache_write_1h_tokens` tariff
  and capability; it is never priced as a 5-minute write.
- Text admission uses an internal procurement-cost estimate. For omitted output
  maximum, this uses a bounded 8192-token estimate (not an output cap).
  Explicit token limits and opaque multimodal/context inputs affect that estimate.
  No input or output estimate is held on the partner balance. Original upstream
  input is unchanged. Confirmed usage is charged once; tiny charges round to
  0.01 RUB with ROUND_HALF_UP.
- Image tariffs use `generation`, mode `default`, resolution `1K`/`2K`/`4K` as
  applicable. Every returned image is charged. GPT's actual returned pixel size
  determines the tier, including URL outputs inspected through a DNS-pinned
  HTTPS transport. No generated image bytes are retained in the database.
- Video tariffs use `second`, mode `default`, actual output + reference seconds.
- Missing usage, unexpected server responses, interrupted streams or uncertain
  submits do not create partner-balance holds or charges. Internal provider-cost
  exposure remains tracked pending reconciliation; submissions are not automatically
  repeated. Definitive 4xx rejections never charge the partner; 429 preserves
  Retry-After. Terminal SSE usage is settled before emitting the terminal event.
  Any accidental post-policy legacy partner hold is released after 30 minutes;
  pre-policy historical refunds require independently authorized reconciliation.
- Admission checks a positive funded retail balance, current provider-wallet
  procurement capital and current economics; it does not require the entire
  hypothetical retail quote to be prepaid. Final verified charges can leave a
  negative balance, which is auditable and blocks new funded requests until
  repaid. Historical partner coverage is an accounting snapshot, not an
  independent admission gate (product brief §89).

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

Generation admission queries `GET /v1/usage` with the selected partner credential.
The prepaid-funding contract accepts only `isValid: true`, `mode: unrestricted`,
unit USD/USDT and an explicit finite nonnegative `balance`, with no quota/subscription
payload. Money is parsed as Decimal. Quota-only, subscription, malformed, unavailable
and unsupported responses fail closed; Crypto Pay cash is not a fallback funding
source for this route. Active generation reserves, pending credits and required float
are subtracted under the global database admission lock. This balance does not enlarge
safe-to-withdraw or create partner credits. Quota-managed keys require a separate
verified account-balance contract before they can use this guard.

See [the live verification matrix](LIVE_VERIFICATION_2026-09-23.md). Adapter
coverage and a passing isolated contract test do not imply every upstream model
or protocol is currently operational.

## Completed video delivery (2026-09-29)

A live completed Seedance 2.5 object was 11,386,099 bytes. The protected content
proxy repeatedly stalled after about 24 KiB, while the signed object CDN completed
an exact 64 KiB Range read. The gateway resolves the current task metadata before
opening content and reads the signed object directly only for the reviewed exact
HTTPS CDN host `ark-acg-ap-southeast-1.tos-ap-southeast-1.volces.com`.

Partner URLs, authentication, status and Range response headers stay unchanged;
there is no local/S3 result retention. CDN requests use a raw request without
shared-client headers/cookies or default auth, and never follow redirects. Unknown
hosts continue through the fixed protected provider content route. No provider URL
or credential is exposed in the partner response; retrieval retries never submit
another generation or alter billing.

## Reconciliation

`GET /api/v1/providers/reconciliation` lists uncertain requests. POST to
`/reconciliation/{generation_id}` accepts a reason and one of:

- `attach_video` + verified upstream task ID: resumes polling without resubmission;
- `completed` + verified usage units: settles once using the original tariff;
- `not_accepted`: releases once after the operator confirms no paid job exists.

Actions are admin-only and stored in the request audit snapshot. Conflicting
repeat actions fail. Do not release an uncertain request solely because the
client disconnected or a proxy returned 5xx.


## Nano Banana 2 Lite resolution correction (2026-10-03)

The live ArgoLink image catalog advertises `nano-banana-2-lite` at **1K only**,
with procurement **USD 0.01 per image**. The earlier 2K tier was incorrect and
has been removed. Both native image routes reject 2K/4K before submission;
1K requests, including image references, retain the existing contract.
Nano Banana 2 and Pro keep their existing 1K/2K/4K contracts.

Source: https://argolink.io/api/catalog/v1/models?category=image&sort=provider&page_size=100


## Qwen3.8-Max — selective addition (2026-10-14)

The `qwen3.8-max` model is reviewed independently against ArgoLink's live
catalog revision `a2c7ddc452e46f37` and the
[Qwen3.8-Max model card](https://argolink.io/en/models/qwen3.8-max).
This does **not** mark the whole historical catalog revision as current: the
current provider catalog also has unrelated added, removed and repriced models.
Do not call the global contract importer to enable Qwen.

- Native endpoint: `POST /v1/chat/completions` with model `qwen3.8-max`.
  ArgoLink also converts `/v1/responses` and `/v1/messages` requests to Chat
  Completions, with the documented conversion caveats.
- Inputs: text and images; context 1,000,000 tokens, up to 983,616 input tokens;
  up to 131,072 output tokens.
- Reasoning is enabled by default; `reasoning_effort`, `enable_thinking` and
  `thinking_budget` do not control it. Reasoning tokens are billed as output.
- Procurement in USD per 1M tokens: ordinary input `1.30`, output `3.90`,
  cached input `0.1625`, cache write `1.625`. Store all four dimensions
  separately; cached input and writes must not be priced as ordinary input.
- Retail prices are configured in the database using the same live RUB/USD retail
  conversion policy as other active LLM models. Do not hardcode a currency rate or
  a customer retail price in this contract.
- Release gate: add draft model and its four price/capability rows via existing
  admin operations; verify a real provider response and usage before setting
  successful-smoke gate; enable only after verified CI/live runtime matches the
  version containing this contract.
- Previous generation price snapshots, ledgers, individual partner rates and
  unrelated provider models remain unchanged.


## Qwen3.8-Max video-input 422 incident (2026-10-15)

Alibaba Cloud's official `qwen3.8-max` contract accepts direct MP4 input through
`/v1/chat/completions` using a user content part `{"type":"video_url",
"video_url":{"url":"https://.../video.mp4"},"fps":2}`. ArgoLink's resale
contract explicitly advertises **text and images only** for the same slug.

Production evidence (only request/attempt metadata, no partner content):
- Three requests were rejected locally with `422 invalid_idempotency_key`;
  an `Idempotency-Key` of 8–160 characters is required independently of modality.
- A later accepted partner request at 10:04 UTC reached ArgoLink and received
  upstream HTTP 400; the proxy mapped it to `422 provider_rejected_request`.
- A separate *operator-owned* upstream smoke with Alibaba's official `video_url`
  + `fps:2` sample returned upstream 400 and an explicit provider rejection:
  `当前模型不支持该能力：video` (this route does not support video capability).

Until a video-capable provider route has been tested end-to-end with confirmed
procurement and retail coverage, fail `qwen3.8-max` text inference requests
containing a `video_url`, `input_video` or `video` content part **before**
creating a generation, provider attempt, ledger, or partner reserve. Return the
existing generic 422 `invalid_request_contract` body with allowlisted
`X-Validation-Error: video_input_unavailable`; never disclose upstream route
identities or raw media URLs to partners.

Do not transcode clips into sampled stills without an explicit opt-in and billing
review: it is not equivalent to submitting the original video. Do not auto-route
to a different provider whose current procurement exceeds Qwen retail prices.
Remove the admission guard only after a verified video-capable upstream, matching
economic rates, and a successful production video smoke.

Sources:
- https://help.aliyun.com/en/model-studio/vision
- https://help.aliyun.com/en/model-studio/qwen3-8-max
- https://argolink.io/en/models/qwen3.8-max
