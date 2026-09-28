# Partner inference reference review — 2026-09-28

The old public `/docs` listed only connection headers, enabled model IDs and
endpoint families. A customer could not infer request bodies, local constraints,
our asynchronous video responses or authenticated downloads from that page.

Product brief §128 expands this surface to a self-contained RU/EN inference
reference. `/guide` remains an alias. Full application OpenAPI, administrative
and financial routes remain closed. No database migration or inference behavior
change is included.

## Sources and boundary

- Executable contracts: `app/contracts/registry.py`, `app/inference/router.py`,
  `app/inference/service.py`, authentication and media routers.
- [ArgoLink documentation](https://argolink.io/en/docs), current
  [catalog](https://argolink.io/api/catalog/v1/models?page_size=100), and individual
  [Wan](https://argolink.io/en/models/wan-3),
  [MiniMax](https://argolink.io/en/models/minimax-h3),
  [Kimi K3](https://argolink.io/en/models/kimi-k3) pages, reviewed 2026-09-28.
- `app/api/partner_reference.py`, `app/api/text_reference.py` contain original
  customer-facing explanations and examples. These public modules do not embed
  upstream brand names, endpoints, procurement fields or routing metadata.
- Available model rows use the same production-status and known-contract filter
  as `/v1/models`. Static examples and limit tables explicitly do not imply
  availability. No models are enabled and no retail prices are changed by docs.

## Deliberate differences from upstream documentation

| Area | Our documented contract |
| --- | --- |
| Generation POST | Partner key and mandatory 8–160 character `Idempotency-Key` |
| Repeated video POST | Same body/key returns the original request ID |
| Repeated synchronous POST | 409 with own ID; no result replay |
| Video status | Own ID, pending/done/failed/expired; no progress or ETA |
| Video content | Own authenticated URL; supports Range |
| Image references | JSON GPT 16/others 3; multipart GPT 16/Nano 3/Grok 1 |
| Nano | Current local ratio set and 1k/2k acceptance; no claim of new 4k/14-reference parity |
| Grok video | Current no-frame-plus-reference rule, image references capped at 720p |
| Aliases | Original wire body is forwarded; document semantic support, not merely local acceptance |
| LLM model controls | Per-model settings and explicit model-specific limitations |
| Errors | Both detail and error.type envelopes; local contract errors collapse to 422 |

The current live catalog differs from the reviewed procurement snapshot. This
documentation update is **not** a procurement refresh, integration smoke, new
model rollout or proof of complete upstream feature parity. Provider pages also
contain contradictory Kimi K3 URL guidance; the example recommends data URLs
without claiming an unverified HTTPS prohibition.

## Media URL boundary

Video results are proxied through our authenticated API. Upload tickets and image
URL outputs still return signed storage/CDN URLs directly, as the existing
architecture specifies. The reference explains this distinction and never sends
a partner API key to these hosts. Removing a supplier name from documentation
does not guarantee anonymity of every storage hostname or model-generated text.
Complete same-origin media delivery would be a separate runtime change.

## Verification

- Published JSON examples are checked against the actual request validator.
- Duration/resolution tables are checked against video contract bounds.
- Both language pages are checked for private data, working section anchors,
  escaped model labels, and consistency with model discovery.
- The Python code included in HTML is the exact packaged runnable source.
- MockTransport tests exercise upload → create → poll → download, storage auth
  separation, saved request state, ambiguous submit timeouts, terminal failures
  and resuming without creating another paid job.
- Real production customers, balances, keys, quotas, generations and model
  configuration are not modified for these checks.
