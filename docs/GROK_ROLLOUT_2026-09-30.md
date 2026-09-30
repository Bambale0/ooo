# Grok 4.5 rollout

User request: enable Grok 4.5 with procurement +25% and make it the primary Neuronych assistant model. API baseline c3fcbf18; runtime baseline 377e3da9.

Existing reviewed model contract, native protocol adapter, authenticated catalog/price/capability/enable controls and immutable price history are reused. Prices are configured in the DB, not hardcoded in this change. No migrations or historical repricing.

Acceptance: configured input $2/M, cached input $0.30/M, output $6/M converted to RUB at publication FX; unchanged unrelated prices; paid usage includes separately reported reasoning exactly once; ordinary local function schemas do not trigger a 2M-token opaque hold. Actual opaque media/provider tools retain the existing conservative hold. Missing output caps retain fail-closed conservative reservation; the application must send a finite configured output cap.

Live evidence: ArgoLink text and function calls work. Image requests return 200 but incorrectly identify synthetic red/blue halves through both Chat Completions and Responses, so visual understanding is not verified. Tool usage can report prompt=314, completion=11, reasoning=134, total=459; ordinary text includes reasoning inside completion. Reconcile only when explicit total proves which representation was used; contradictory totals fail closed.

Verification plan: regression tests for local function vs opaque inputs and inclusive/separate reasoning, existing native settlement/replay tests, complete CI, authenticated control-plane activation, public pricing/docs/model discovery and real application response after bounded-output app rollout. No guard bypasses or balance credits.
