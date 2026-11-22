# Acceptance and regression matrix

Each phase is a gate. Run focused tests first, then the cumulative suite, lint,
and type checks. The manager reviews actual changes independently of child-agent
test reports. Tests must not use or flush an existing Redis installation.

| Phase | Required evidence before advancing |
| --- | --- |
| 0 | Invalid configuration fails fast; public route matching is exact; API unauthorized JSON and browser login redirect; constant-time bearer/login handling including Unicode; sessions and hardened cookie flags; cookie CSRF origin/referer; bearer bypass; MCP rejects cookie-only auth; five failed logins trigger rate limiting; Redis health failure; no-store and CSP headers. |
| 1 | Valid root/nested zip import; specific manifest failures; duplicate slug conflict; traversal, absolute/drive/backslash/NUL paths, symlinks/special files, zip bomb, size/count/ratio and duplicate destinations rejected; validation errors leave no package/extraction/registry changes; unsupported imports warn only; remove deletes only the chosen slug. |
| 2 | Hello loads and tools cache; relative imports isolated; SDK auth-mode/schema/name contracts validated; exception and malformed-return handling; bounded concurrency; timeout and late worker accounting; overloaded fast failure; scoped key/value/TTL restrictions and SCAN; secret redaction and log cap; shutdown/module cleanup; failed plugin isolation; startup extracts from original bytes and reconnects. |
| 3 | Actual streamable HTTP MCP initialize/list/call using bearer at both slash forms; cached tools listed while disconnected; exact inputSchema including refs/defaults/additionalProperties; structured and JSON text output; uniform argument errors; mounted lifespan entered/exited; live swap removes old tools; in-flight old call remains bound to old instance; failed upgrade restores old package/runtime/state. |
| 4 | API-key hook observes core.api_key already stored; supported mode/body validation; connect status persisted; changed tool set swapped; secrets absent from every JSON response and public diagnostic; disconnect hook and secret wipe; API-key demo exercises credential rejection/transient upstream mapping. |
| 5 | Static CSP-compatible login/dashboard; DOM-only rendering; hash navigation; status counts/attention/grid; import replacement confirmation; detail URLs/tools/schema/key names/warnings/logs; connect/disconnect/reload/remove; no secret persistence in browser; manual walkthrough documented. |
| 6 | Byte-identical package export; data/secrets/core keys/auth mode round trip; TTL keys excluded; plaintext warning; subset and unknown slug behavior; complete Phase A rejection writes nothing; encrypted format rejected; skip/replace/selective restore; exact state wipe on successful replace; per-plugin runtime failure isolation; on_restore failure non-fatal. |
| 7 | All ping result rows; transient failures never disconnect or count as hard; hard threshold; refresh success and failure; last ping/meta/log updates; no pings for disconnected/OAuth-required; hung plugin does not block another; no overlapping cycles; stale runtime observations do not undo disconnect/replacement. |
| 8 | Local mock provider authorize/code/token exchange with PKCE; correct callback redirect_uri; stored client credential reuse; state single use/expiry/slug binding; provider error requires valid state; forged/reused callback cannot mutate status; fixed relative redirect; refresh rotation and scheduler integration. |
| 9 | Full cumulative tests; ruff and meaningful public-function type check; isolated real Redis integration job with AOF; security checklist audit; non-root Docker app and persistent volumes; Compose startup health dependencies; fresh deployment smoke; README setup/authoring/backup/HTTPS/proxy/no Redis exposure guidance. |

## Test infrastructure expectations

- Unit/in-process integration: paired `fakeredis` sync/async clients sharing a
  `FakeServer`; explicit app lifespan context and `httpx.ASGITransport`.
- Real Redis integration: dedicated test process/container, random port or
  explicit CI service, database and filesystem under test ownership only.
- Plugin fixtures: ZIP builders with controllable manifests, SDK implementations,
  malicious archive metadata, synchronous delays and threading events.
- Concurrency tests: events/barriers and bounded waits rather than fragile
  arbitrary timing assumptions.
- MCP contract tests: exercise the wire endpoint with an MCP client, not only
  direct adapter method calls.
- Dashboard check: browser walkthrough if available; do not claim browser
  validation from a source-only assertion.

## Reporting

Record exact commands, counts, failures/skips, critic score and review session.
Separate an unavailable deployment/tool from a passing check. A numeric review
score does not supersede a failing test, unresolved vulnerability, or scope error.

## Critic scope: MVP only

At the user's direction, subsequent critics focus on demonstrated MVP-scale
correctness, required behavior, security, data preservation and lifecycle safety.
Do not penalize style preferences, optional refinements, hypothetical hardening
without practical evidence, or post-MVP features. Optional notes must be clearly
separated and must not lower the score. Reproduced credential leaks, failed-upgrade
corruption and required lifecycle/auth/protocol failures still block approval.
