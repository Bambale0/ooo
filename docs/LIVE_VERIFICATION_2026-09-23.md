# Live verification — 2026-09-23

Controlled tests used an operator-supplied ArgoLink key and the supplied JPEG. No secrets, upstream job IDs, signed URLs or image bytes are included in this report.

Reference SHA-256: `821309168b56eeddd72daedb8a32c9f67037605ad979ed0bd77c6a59ac1cd627`.

| Model | Category | Observed result |
| --- | --- | --- |
| `gpt-6-astra` | chat | PASS: /v1/chat/completions; Responses returned 502 (string and array input) |
| `gpt-5.6-luna` | chat | NOT VERIFIED: 429 on concurrent Chat; sequential retry and Responses returned 502 |
| `gpt-5.6-terra` | chat | NOT VERIFIED: 502 chat/completions |
| `gpt-5.6-sol` | chat | PASS: /v1/chat/completions; Responses returned 502 (string and array input) |
| `claude-opus-5` | chat | PASS: /v1/messages; default/recommended protocol was not verified |
| `claude-fable-5-1` | chat | PASS: /v1/messages; default/recommended protocol was not verified |
| `claude-fable-5` | chat | PASS: /v1/chat/completions |
| `grok-4.6` | chat | PASS: /v1/responses |
| `gpt-image-2` | image | PASS: reference image edit, one returned image |
| `gpt-image-2.5-flare` | image | PASS: reference image edit, one returned image |
| `gpt-image-2.5-sunburst` | image | PASS: reference image edit, one returned image |
| `grok-imagine-image-quality` | image | PASS: reference image edit, one returned image |
| `seedance-2.5` | video | PASS: reference video generation, protected MP4 / Range 206 |
| `grok-imagine-video-1.5` | video | PASS: reference video generation, protected MP4 / Range 206 |
| `claude-opus-4-8` | chat | PASS: /v1/chat/completions |
| `claude-sonnet-5` | chat | PASS: /v1/chat/completions |
| `deepseek-v4-flash-vision-exp` | chat | PASS: /v1/responses |
| `deepseek-v4-pro-0813` | chat | PASS: /v1/responses |
| `glm-5.1` | chat | PASS: /v1/responses |
| `glm-5.2` | chat | PASS: /v1/responses |
| `glm-5.3` | chat | PASS: /v1/responses |
| `glm-5.3-flash` | chat | PASS: /v1/responses |
| `gpt-5.3-codex-spark` | chat | NOT VERIFIED: 502 chat/completions |
| `gpt-5.4` | chat | NOT VERIFIED: 502 chat/completions |
| `gpt-5.4-mini` | chat | NOT VERIFIED: 502 chat/completions |
| `gpt-5.5` | chat | NOT VERIFIED: 502 chat/completions |
| `grok-4.5` | chat | PASS: /v1/responses |
| `grok-imagine-image` | image | PASS: reference image edit, one returned image |
| `grok-imagine-image-2.0` | image | PASS: reference image edit, one returned image |
| `kimi-k2.6` | chat | PASS: /v1/responses |
| `kimi-k2.7-code` | chat | PASS: /v1/responses |
| `kimi-k3` | chat | PASS: /v1/responses |
| `nano-banana-2` | image | NOT VERIFIED: non-JSON upstream response; no automatic paid retry |
| `nano-banana-2-lite` | image | NOT VERIFIED: 502 images/edits |
| `nano-banana-pro` | image | NOT VERIFIED: non-JSON upstream response; no automatic paid retry |
| `seedance-2.0` | video | PASS: reference video generation, protected MP4 / Range 206 |
| `seedance-2.0-fast` | video | PASS: reference video generation, protected MP4 / Range 206 |
| `seedance-2.0-mini` | video | PASS: reference video generation, protected MP4 / Range 206 |
| `wan-3` | video | PASS: reference video generation, protected MP4 / Range 206 |
| `minimax-h3` | video | NOT VERIFIED: 4s / 768p first-frame job accepted (202), then failed; no additional charge |

**30/40 models** produced a usable result through at least one tested protocol. This is not a claim that every option, tier or protocol passed live.

## Quota and expenditure

Before: used $1.70 of $5. After: actual cumulative usage **$4.144532328**, quota used **$4.14453233**, remaining **$0.8554676700000003**. New test expenditure **$2.444532328**, below the chosen $2.80 test budget.

The catalog changed during the task from `6dbda1a9e5891d82` (39 models) to `5ce662ba88c3d07f` (40 models). The read-only drift guard detected H3 and new 480p tiers for Seedance 2.0, Mini and Fast; their contracts and tests were added before retesting. Four JPEG-reference jobs (H3 768p plus three Seedance 480p, 4 seconds each) were accepted, then returned `failed`. The follow-up reserved an estimated $0.660 under a $0.70 cap, but actual additional usage was $0; the before/after counters remained identical. Earlier Seedance successful modes remain separately recorded; the new 480p configurations are not PASS. The final live sample covers all 40 catalog models, with usable results from 30 and successful reference results from 6 of 7 video models.

During async completion, quota temporarily lagged usage. The final values converged (apart from API numeric representation). The test did not deliberately exhaust the key. HTTP 429 was observed on a concurrent text probe; isolated tests verify releasing reserves on definitive quota/rate rejections and preserving Retry-After.

## Gateway, not only direct provider calls

- Chat Completions and Messages/SSE completed through the resale API; repeated keys returned the original request identity without resubmission.
- GPT Image 2 multipart edit completed; actual 2K output settled a 4 RUB hold to 3 RUB using isolated test retail prices.
- Seedance 2.5 edit with image + video + audio completed through our queue and polling code. Provider reported 10 billable seconds; 936 RUB reserve settled to 156 RUB, procurement $0.78. Protected content returned 206 and 1,024 bytes.
- A transient Responses failure through the gateway remained in reconciliation with its reserve and could not be blindly repeated.
- Telegram getMe succeeded for the supplied test bot; getWebhookInfo reported no webhook. No real messages were sent or updates consumed.

## Findings that affect launch

- Some OpenAI routes returned Cloudflare 502, including a representative non-streaming and streaming Responses probe. Several Chat Completions routes also returned 502. Do not mark these routes as successfully smoked.
- Claude Opus 5 and Fable 5.1 produced empty usage on an initial Chat probe but succeeded through Messages. This does not justify an automatic protocol switch.
- Nano Banana edit probes did not return usable JSON; the additional Lite HTTPS-reference probe returned 502.
- Newly listed MiniMax H3 and new Seedance 480p probes ended in `failed` after acceptance. The smoke report does not establish the underlying failure cause. Do not enable these configurations as verified.
- Grok image edit actual costs differed from the catalog; see `app/contracts/observations.json`. Verify procurement per account/tier and retain conservative enable gates.

## Test scope

Successful API responses do not certify visual quality or all combinations of video/audio media limits. Seedance reference bytes were uploaded using native tickets. The source photo, generated test clip and extracted audio were temporary test inputs, not application-side result storage. Encrypted WAL/PITR was subsequently verified in network-isolated PostgreSQL containers (see production review). Production deployment, live Crypto Pay, off-site restore and infrastructure-specific launch gates require their own environment.
