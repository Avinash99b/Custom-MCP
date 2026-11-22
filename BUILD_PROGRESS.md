# Custom MCP build progress

The supplied Architecture & Build Specification is the implementation contract.
Phases run in order; the next phase starts only after the current phase's
definition of done has been verified. Critic scores are additional review, not
a substitute for tests or manager verification.

## Ordered checklist

- [x] Read-only repository reconnaissance (empty repository; existing local files preserved).
- [x] Resolve requested implementation and critic models.
- [x] Phase 0: skeleton, configuration, Redis, authentication. **Verified; critic 9.0/10.**
- [x] Phase 1: dependency-free SDK, manifest validation, safe package import, registry. **Verified; reviews 9.0/9.0/9.1.**
- [x] Phase 2: scoped storage, logs, plugin host, loader and lifecycle. **Verified; reviews 9.0/9.1/9.0/9.0 and final integration 9.4.**
- [x] Phase 3: exact-schema MCP adapter, dynamic router, live replacement with rollback. **Accepted by user decision; 7 review findings fixed, final critic 8.0; one admission-race finding deferred.**
- [x] Phase 4: API-key connect/disconnect and example plugin. **Advanced under the anti-thrash policy after six sub-8.5 passes (final 8.5); all reproduced findings fixed; two outstanding findings recorded (Phase 3 admission race, Phase 4 in-flight description race). The Phase 4 description-leak variant was later found and fixed in the final whole-tree review (see Phase 9).**
- [x] Phase 5: static dashboard and documented UI walkthrough. **Verified; critic 9.2/10 (after 6 sub-8.5 passes that each fixed a real async-rendering defect).**
- [x] Phase 6: validated backup/restore and settings UI. **Verified; critic 9.2/10 (after 5 passes that fixed real data-preservation/cancellation/credential-retention defects).**
- [x] Phase 7: concurrent health scheduler and state-machine coverage. **Verified; critic 9.4/10 after the total-deadline contention fix.**
- [x] Phase 8: OAuth state, callback, PKCE example and mock-provider integration. **Verified; critic 9.5/10 after four redaction defects and two residual encoding/retention leaks were fixed.**
- [ ] Phase 9: security audit, deployment, documentation and CI.
- [ ] Final independent diff review, full tests, lint and type check.

## Delegation ledger

| Task | Agent / model | Session | Scope | Status |
| --- | --- | --- | --- | --- |
| Repository map | `explore` / configured default | `ses_e06f4525cffeK2KqCtQJiK1Ofd` | Read-only reconnaissance | Complete |
| Storage/transaction implementation | `general` / `local-omniroute/v41-flash-combo` | `ses_e06f274a3ffeWZXAEWgOjCwqlz` | Storage/log slice complete; new isolated candidate storage transaction and focused tests | Lua commit/receipt, absolute expiry and combined redaction handed off; review pending |
| Example/runtime fixtures | `general` / `local-omniroute/v41-flash-combo` | `ses_e06bbebfaffeP2REgDbEEncopB` | ZIP fixtures complete; Phase 2 only Hello example and `tests/runtime_cases/` | Corrected handoff; manager verified 81 tests/lint/strict types |
| Isolated real Redis validation | `general` / `local-omniroute/v41-flash-combo` | `ses_e06bbebfaffeP2REgDbEEncopB` | Example/fixtures complete; only new `tests/real_redis/` | Dedicated-process/AOF integration fixtures and tests in progress |
| Runtime/host/loader implementation | `general` / `local-omniroute/v41-flash-combo` | `ses_e06b9238dffe11a6RPZaLWY9Cg` | Phase 1 integration complete; Phase 2 new runtime, host, loader and focused tests | Explicit safe-error boundary handed off; 56 focused tests reported |
| Phase 2 lifecycle integration | `general` / `local-omniroute/v41-flash-combo` | `ses_e0685e42dffeqoRrMrM2M8nH1F` | Manager/API/main/shared fixtures/README/dependency metadata after interfaces stabilize | Unload completion fix and internal unknown-outcome guard handed off; receipt tests pending |
| Lifecycle integration critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e06e30eedffel1cjGWYBWBzsNy` | Storage/logging PASS 9.0; now manager/API/main integration | Cancellation slice PASS 9.0; unknown-outcome verification pending |
| Transaction critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e06aa2efeffetwPXPsuy0J9Rfm` | Hello PASS 9.1 and host PASS 9.0; now isolated transaction review | Provider content-blocked HTTP400 before verdict; no transaction score |
| Fresh transaction critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e0634c49effecB9GBYcEzwlFKF` | Fresh read-only transaction review after stopped-edit handoff | MVP-focused slice 8.0: receipt absence incorrectly treated as proven rollback |
| Phase 2 final integration fix | `general` / `local-omniroute/v41-flash-combo` | `ses_e06014a3bffeOeUYUCT36GG1y5` | Only `plugins/manager.py` + `tests/integration/test_runtime_lifecycle.py` | Containment/containment-restore/cancellation + reload restore-order fixes; stopped for review |
| Phase 2 final integration critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e060d92b5ffebHlOVQFzZK584o` | Final read-only unknown-outcome/containment review | 6.5 → 7.5 → 8.0 → **adjudicated PASS 9.4**; all findings resolved |
| Phase 3 adapter | `general` / `local-omniroute/v41-flash-combo` | `ses_e05e9e624ffe5Hz82IuDVfVg3` | `mcp/adapter.py` + `tests/mcp/` | Complete; manager verified 8 mcp / 893 full tests, Ruff/mypy clean |
| Phase 3 router (retry) | `general` / `local-omniroute/v41-flash-combo` | `ses_e05cf6b17ffekRfsnaK9qjfFyi` | `mcp/router.py` + `main.py` wiring + wire tests | Complete; manager verified 912 full / 156 mcp+integration / 15 real-Redis, Ruff/mypy clean |
| Phase 3 gate critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e05b5bf43ffe8YaawKpWReRsoE` | Read-only Phase 3 gate review | 7 findings fixed; final 8.0; one admission race deferred by user |
| Phase 4a connect/disconnect | `general` / `local-omniroute/v41-flash-combo` | `ses_e045d8d09ffeC2YFM05vlTGExc` | `plugins/manager.py`, `api/plugins.py`, `api/errors.py`, `tests/integration/test_connect.py` | In progress |
| Phase 4b apikey_demo example | `general` / `local-omniroute/v41-flash-combo` | `ses_e045d8d08ffeWCRP2tqIynYIdb` | `plugin_examples/apikey_demo/**`, `tests/unit/test_apikey_demo.py` | Complete; manager verified 25 tests |
| Phase 4 gate critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e0455a4daffeBdQSRpuNuu5jhj` | Read-only Phase 4 gate review | 7.0 → 7.5 → 8.0 → 8.3 → 8.3 → 8.5; all findings fixed; Phase 4 advanced per anti-thrash |
| Phase 4 credential containment fix | `general` / `local-omniroute/v41-flash-combo` | `ses_e0443800affet67oiFOt1VvW2C` | All four gate findings: snapshot redaction, sealed generations, description redaction, whitespace key | Complete; manager verified 964 tests, Ruff/mypy clean, 15 real-Redis |
| Phase 4 containment fix v2 (manager) | manager (direct) | — | Reconnect rebuild, candidate-generation seal, revocable data-only shutdown permit, validate-before-rebuild, sealed-generation log drop, pre-wipe + retained description secrets, `tests/integration/test_phase4_containment_2.py` | Complete; manager verified 980 tests, Ruff/mypy clean (76 files), 15 real-Redis |
| Phase 5a static dashboard | `general` / `local-omniroute/v41-flash-combo` | `ses_e03df6147ffeY7Mb20Lbntnwg0` | `static/{index.html,styles.css,app.js}` + dashboard tests | Complete; manager verified 993 tests, Ruff/mypy clean (78 files), 15 real-Redis |
| Phase 5b dashboard docs | `general` / `local-omniroute/v41-flash-combo` | `ses_e03df6145ffetg72tQWJJRKIaA` | `README.md` Dashboard section + `docs/DASHBOARD_WALKTHROUGH.md` | Complete; manager read for accuracy |
| Phase 5 gate critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e03d3e0afffe06RgqZbTZC15Kc` | Read-only Phase 5 gate review | 8.2 → 8.0 → 8.4 → 8.5 → 8.5 → 8.5 → **9.2 PASS**; every finding fixed |
| Phase 6a backup/restore backend | `general` / `local-omniroute/v41-flash-combo` | `ses_e03638e54ffecyS23hPzpCdveJ` | `backup.py`, `api/backup.py`, `main.py`, manager accessors, backup tests | Complete; manager fixed transactional replace/wipe/auth-mode/auto-connect; 1043 tests |
| Phase 6b settings UI + docs | `general` / `local-omniroute/v41-flash-combo` | `ses_e03638e52ffeOgW7WnH9gmWc6U` | Dashboard Settings section + README/walkthrough | Complete; manager verified CSP-clean, ids present |
| Phase 6 gate critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e03463b72ffeflUyTxe5bz7I36` | Read-only Phase 6 gate review | 7.0 → 7.5 → 8.0 → 8.0 → **9.2 PASS**; every finding fixed |
| Phase 7 health scheduler | `general` / `local-omniroute/v41-flash-combo` | `ses_e02fb834effectSQ89YpoVMshz` | `health.py`, manager helpers, `main.py` wiring, health tests, README | Complete; manager verified 1076 tests, Ruff/mypy clean (86 files), 15 real-Redis |
| Phase 7 gate critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e02f10970ffej0W4rQzRcksVG1` | Read-only Phase 7 gate review | Pass 1 FAIL 8.3 (health deadlines excluded the semaphore wait); pass 2 **9.4 PASS** after the total-deadline fix |
| Phase 7 total-deadline fix | manager (direct) | — | `plugins/host.py::_run` single deadline covering capacity wait + execution; `tests/unit/test_host.py` + `tests/integration/test_health_scheduler.py` regressions; README | Complete; manager verified 1080 tests, critic's 3 contention + 59 independent probes pass, Ruff/mypy clean (86 files), 15 real-Redis |
| Phase 8a OAuth backend | `general` / `local-omniroute/v41-flash-combo` | `ses_e02aa2372ffevuLEmpxJhZK0nh` | `plugins/manager.py` OAuth connect/state/complete, `api/oauth.py`, `main.py` route, `storage/redis_client.py` GETDEL, `tests/unit/test_oauth_backend.py` | Complete; manager verified 1146 tests, Ruff/mypy clean (90 files), 15 real-Redis |
| Phase 8b oauth_demo + mock provider | `general` / `local-omniroute/v41-flash-combo` | `ses_e02aa2371ffeSBW1fCxOLM96q4` | `plugin_examples/oauth_demo/**`, `tests/oauth_provider.py`, `tests/unit/test_oauth_demo.py` | Complete; manager verified 38 demo tests, Ruff clean |
| Phase 8c OAuth integration + docs | `general` / `local-omniroute/v41-flash-combo` | `ses_e02934c53ffeNtYyzkSJSqDraB` | `tests/integration/test_oauth.py`, README OAuth section, `docs/OAUTH.md`, dashboard walkthrough | Complete; 10 E2E tests, manager verified 1156 tests, Ruff/mypy clean (91 files), 15 real-Redis |
| Phase 8 gate critic | `general` / `local-omniroute/agentrouter/gpt-6.1-sol` | `ses_e0285ebfeffeyhknhpsI3Wp0nG` | Read-only Phase 8 gate review | Pass 1 FAIL 7.5: functional OAuth/state/callback/PKCE/scheduler all PASS; four redaction defects (sign-in URL secret, auth code, data-stored PKCE verifier, rotated-token history) |
| Phase 8 redaction fix | manager (direct) | — | `plugins/logs.py` retained-set + `pkce:` data scan, `plugins/manager.py` retention + `_redact_url`, `health.py` pre-refresh retention, `main.py` router binding, `tests/integration/test_phase8_containment.py` | Complete; 4 mutation-verified regressions, 1160 tests, Ruff/mypy clean (92 files), 15 real-Redis |
| Phase 8 redaction fix (round 2) | manager (direct) | — | `plugins/manager.py` multi-encoding URL screen + `_with_state_param` encoding preservation, `health.py` post-refresh retention, 2 more regressions | Complete; 6 mutation-verified regressions, 1162 tests, Ruff/mypy clean (92 files), 15 real-Redis |
| Phase 9a deployment artifacts | subagent (`local-omniroute/v41-flash-combo` per ledger) | — | `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `tests/deployment/test_artifacts.py` | Complete; manager ran `docker build`, `docker compose config`, 18 artifact tests |
| Phase 9b CI + 9c docs | subagents (4 parallel slices) | — | `.github/workflows/ci.yml`, `docs/SECURITY.md`, `docs/DEPLOYMENT.md`, README | Complete; manager audited every citation/link and re-ran all jobs locally |
| Phase 9d fresh-deployment smoke | manager (direct) | — | `/tmp/opencode/phase9-smoke/smoke.py` | Complete; 14/14 checks, stack torn down with `down -v` |
| Phase 9 doc-gap fix (manager) | manager (direct) | — | `README.md` `## Writing a plugin` + `plugin_examples/{hello,apikey_demo}/README.md` stale-phase notes | Complete; example verified through the real app/host/loader/MCP wire; 1180 tests, Ruff/mypy clean (94 files) |
| Phase 9 gate critic | `opencode run` subagent, `local-omniroute/literouter/deepseek-v4.1-flash-1017:free` (standing directive) | — | Read-only Phase 9 gate review | In progress |

## Safety and validation notes

- The existing Redis at localhost:6379 is not assumed to belong to this project;
  validation must use fake Redis or a dedicated isolated instance. Never flush it.
- Dependencies belong in a project-local virtual environment, not the unrelated
  PlatformIO interpreter environment.
- Existing `.gitignore` content and ignored `opencode.json` are preserved.
- At the user's direction, the manager launches critics directly; implementation
  agents must not spawn nested agents. The initial review ran on
  `local-omniroute/kaggle-tasks/anthropic/claude-sonnet-5@default`. Critic reviews
  for Phases 1–8 used `local-omniroute/agentrouter/gpt-6.1-sol` (and one
  `antigravity/gemini-3.8-flash-high` session for the Phase 8 closure after a
  provider outage), all verified in the model catalog.
  **Standing directive (from the Phase 8 closure onward): use
  `local-omniroute/literouter/deepseek-v4.1-flash-1017:free` for all future critic
  and help-request subagent sessions.**
  A score strictly greater than 8.5 remains required.
- The user authorizes up to five parallel `v41-flash-combo` implementation agents
  where useful. File ownership must not overlap; shared interfaces are handed
  off before dependent integration. Phase gates remain sequential. Worktrees
  are optional, not required for exclusive new directories in a shared tree.
- For subsequent reviews, the user requests **MVP-scale review, not nitpicking**.
  Critics should score required behavior, practical correctness, security,
  data preservation and lifecycle containment. Style preferences, speculative
  hardening, exotic cases without demonstrated MVP impact and post-MVP features
  must not become blockers or reduce the score. Reproduced credential leaks,
  state/code corruption or violations of the authoritative MVP contract remain
  material. Preserve the >8.5 review gate without expanding project scope.
- **Anti-thrash policy (user directive):** if a phase's critics score below 8.5
  **more than three times**, the manager may proceed to the next phase while
  recording the outstanding errors and presenting them to the user at the end.
  Phase 3 reached exactly three sub-8.5 passes (7.0, 6.5, 8.0); its one deferred
  admission-race finding is recorded above and will be reported at the end.
- The user requested private Android milestone notifications through the
  progress-notifier skill. Percentages describe verified phase progress, not an
  exact time forecast: Phase 0 complete means 1/10 gates, **10% complete / roughly
  90% remaining**. The initial notification endpoint returned HTTP 200. The
  private webhook URL must never be written into this repository.
- Git `origin` already points to the user's requested SSH URL for fetch and push:
  `git@github.com:Avinash99b/Custom-MCP.git`. A read-only, batch-mode SSH
  `git ls-remote --symref origin HEAD` completed successfully and returned no HEAD
  ref. No commit, push, SSH credential change, or remote replacement was needed.

## Integration decisions to verify in later phases

- Authentication remains entirely in `auth/`. Handlers use the middleware's
  principal; MCP requests require bearer authentication, not a dashboard cookie.
- Every plugin method, including constructor/import execution where practical,
  must be kept off the event loop. All SDK hooks go through `PluginHost`.
- Lifecycle operations retain one stable per-slug lock across runtime replacement;
  replacing the runtime object must not accidentally permit overlapping operations.
- A retired MCP app must retain its matching plugin instance and tool definitions.
  An in-flight old request must not call the newly swapped instance by accident.
- Semaphore release on timeout does not cancel the worker. Late completion must
  decrement the leaked-worker count, including races and caller cancellation.
- Archive validation is completed before extraction writes. Reject duplicate
  normalized destinations and file/directory conflicts as well as traversal,
  symlinks, special files, size/count/ratio violations and invalid manifests.
- Restore Phase A rejects archive/package/schema defects without changing live
  state. Phase B isolates runtime/hook/connection failures per plugin. This
  distinction reconciles validate-everything with per-plugin failure handling.
- A failed replacement must preserve the original package and stored state.
  Restore replacement additionally snapshots old state until replacement commits.
- Health observations from an old runtime must not overwrite newer lifecycle
  results after a swap or owner disconnect.
- OAuth callbacks consume and validate slug-bound state before recording even
  provider-declared errors, so a forged public request cannot alter plugin status.
- Redaction applies to persisted logs and public plugin diagnostics, including
  exception/load/connect messages that could contain stored credentials.
- Exact-schema MCP tests must include nested `$defs`/`$ref`, defaults and
  `additionalProperties`, not just simple parameter types.

### Installed FastMCP API verification (preliminary, not Phase 3 implementation)

FastMCP is pinned at **2.14.7**. Runtime inspection confirmed:

- `FastMCP.add_tool(tool)` accepts a `fastmcp.tools.Tool` instance.
- `Tool` has raw `parameters`; subclasses implement async `run(arguments)`.
- `FastMCP.http_app(path="/", stateless_http=True, json_response=True)` creates
  a `StarletteWithLifespan` with its internal route at `/` and an explicit
  `app.router.lifespan_context`.
- Tool-level errors are `fastmcp.exceptions.ToolError`.
- In this pinned version, tool results must be imported from
  **`fastmcp.tools.tool.ToolResult`**, not `fastmcp.tools.ToolResult`. The first
  probe failed on that missing re-export; inspecting the installed package
  established the correct path and a corrected probe succeeded.
- Third-party Authlib imports emit deprecation warnings; no source or dependency
  changes were made in response. Wire-contract tests remain required in Phase 3.
- A real lifespan probe exposed a limitation in the specification's illustrative
  mount/retire pseudocode: entering a FastMCP app's lifespan in the mounting task
  and closing its exit stack in a different retirement task fails with
  `RuntimeError: Attempted to exit cancel scope in a different task than it was
  entered in`, followed by a cross-context token-reset error. No app source was
  changed by the probe. Phase 3 must give each mounted app a dedicated owner task
  that both enters and exits its lifespan; mount awaits readiness and retirement
  signals that same task after drain. This evidence-based implementation detail
  preserves the specified hot-swap behavior rather than using invalid pseudocode.
  A subsequent minimal owner-task probe entered and exited the actual FastMCP
  lifespan successfully (`lifespan exited in owner task`).

## Verification results

Manager independently ran phase-0 checks after the interrupted-work handoff:

- `.venv/bin/python -m pytest -q`: **103 passed in 0.24s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues in 26 source files**.
- Repository status reviewed: new foundation files, no commits; ignored local
  configuration preserved. With an unborn branch, files are untracked and must
  be reviewed directly rather than relying on an empty `git diff`.

### Review blocker

Direct critic invocation failed before reviewing code:

```text
No target in combo review-coder can produce the requested max_tokens=131072;
the highest known output limit in the pool is 65536
```

No critic score exists. Phase 1 has not started. The user resolved the model
choice by explicitly selecting
`kaggle-tasks/anthropic/claude-sonnet-5@default` instead of `review-coder`.
No OpenCode configuration edit or output-limit override has been applied.

### Manager findings addressed in first correction handoff

- Login rate limiter uses Redis instead of the specified in-memory store.
- Session cookie is named `custom_mcp_session` instead of the specified `session`.
- CSRF rejection returns `csrf` rather than the specified `forbidden` REST code.
- CSRF accepts matching Referer despite a present mismatching Origin; malformed
  Referer parsing can raise instead of returning a safe rejection.
- Configuration strips the owner secret rather than preserving its exact value.

Additional directed regression checks cover startup-owned resource cleanup,
malformed configuration URLs, and safe request-id propagation on error responses.
The implementation agent corrected these and stopped editing for review.

Manager independently reran checks on the corrected tree:

- `.venv/bin/python -m pytest -q`: **138 passed in 0.29s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues in 27 source files**.
- `uv lock --check`: **Resolved 103 packages; lock consistent**.

### Additional manager review finding

A focused JSON-logging probe confirmed `RequestContextMiddleware` resets its
request-id context before logging the request summary. The JSON log contains
`request_id: "-"`, despite a UUID in request state. This reproducible defect was
sent to the critic for inclusion in the full review. No pass score exists yet.

### First independent review result

The critic independently reproduced **138 passing tests**, clean Ruff/mypy and
a consistent lock. It scored **7.5/10 (FAIL)** because request-summary and outer
generic-500 diagnostic logs lack the request ID, and the summary status is zero
when an exception propagates before a response. It additionally confirmed the
bearer parser trims a preserved whitespace-edged secret (nonblocking, fails
closed). The original implementation session is making minimal fixes with JSON
log regression/concurrency tests and byte-exact bearer tests. The same critic
session has been resumed on the user-selected next review model for rereview
after the code is stable. Phase 1 remains unstarted.

### Minimal correction verification

The implementation agent corrected request-summary/500 diagnostic correlation,
summary error status and exact bearer parsing, and added JSON log/concurrency
regression tests. Manager reviewed the changed source and new logging tests and
independently ran:

- `.venv/bin/python -m pytest -q`: **153 passed in 0.31s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues in 28 source files**.
- `uv lock --check`: **Resolved 103 packages; lock consistent**.

The requested next-model critic completed the targeted rereview: **PASS 9.0/10**.
It independently ran 65 focused tests, all 153 cumulative tests, Ruff, mypy (28
files), and the lock check. Additional probes verified cancellation handling,
already-started response status, context restoration when logging fails, and
exact whitespace-edged bearer comparison. Phase 0 is complete. Live Redis,
Uvicorn wire behavior and browser walkthrough remain later validation work.

## Phase 1 parallel ownership and handoff

Three non-overlapping implementation agents are active. Validator and registry
interfaces were explicitly agreed before downstream integration. The independent
ZIP fixture agent owns only `tests/security_cases/`; the validator agent cannot
edit those files. Registry integration owns shared fixtures/app wiring; the
validator agent retains pyproject/lock ownership for SDK wheel inclusion.

Manager whole-file SDK review caught a handoff contradicting the explicit SDK
specification (no-argument hooks, altered OAuth/refresh return values, `context`
instead of `ctx`, changed Storage signatures, frozen mutable results, and shared
nested default schemas). The affected branch was stopped and given the exact
required signatures/semantics as a concrete correction, plus introspection and
behavioral contract tests. This is not an approved API redesign. Registry work
does not execute plugin hooks in Phase 1 and uses only agreed manifest/package
interfaces while those corrections are completed. No Phase 1 pass is claimed.

The ZIP fixture agent handed off 38 cases and 120 fixture self-tests. Manager
independently verified **120 passed in 0.13s**, scoped Ruff clean, and mypy clean
in three files. These prove fixture construction, not production validation.
The validator agent was directed to exercise actual validation against the
corpus. Manager review identified two fixture alignment corrections: a root
manifest permits an unrelated nested manifest as data, and the total-size case
must isolate the 5x uncompressed limit rather than also tripping the entry ratio
guard or assuming an unsupported validator parameter. The fixture agent is
correcting those only within its exclusive directory.

Fixture alignment completed: **38 cases (5 valid, 33 invalid)**. Root+nested
manifest is valid; the expansion fixture has compressed archive 11477 bytes
within its 12288-byte limit, total uncompressed 65823 bytes above the 61440-byte
derived cap, and max entry ratio 6.17. Threshold hints map to the actual validator
parameter or a deliberate test-only constant patch. Fixed ZIP timestamps also
remove a discovered fixture determinism issue. Manager independently reran
**120 passing tests in 0.18s**, scoped Ruff and mypy clean (three files), and
handed the stable corpus to the validator agent for actual validation tests.

SDK/validator handoff reported **151 focused tests passing**, scoped lint/types
clean, exact SDK contract corrected, all 38 ZIP corpus cases exercised without
skips. Registry handoff reported **35 focused tests passing**, 459 cumulative
tests passing, and Ruff clean; its reported full type check was blocked by an
SDK test annotation issue, subsequently reported fixed by the validator agent.
These are child reports, not yet manager cumulative verification.

Manager read complete implementation files and directed concrete corrections:

- Import mode must come from the specified query parameter, not only a form field.
- Invalid-package REST responses must retain precise safe validator reasons.
- Plugin summary must expose the required auth/selected-mode/ping/error fields.
- Publication must settle worker-thread filesystem work safely on cancellation.
- Installed-set reconciliation must include failed package records and clear stale
  membership; removal must delete the exact complete plugin namespace.
- Multipart abort must close opened spool files; data subdirectories must not
  resolve through symlinks outside their owner directory.
- ZIP total expansion is capped at **5x the configured package limit**, not 5x
  the actual uploaded ZIP length; valid compressed packages must not be rejected.
- Extraction must preflight both file and directory destinations before writing,
  reject symlink scratch destinations, and retain raw-name safety on crafted ZIPs.
- Manifest semantic versions require ASCII digits; Python entry identifiers must
  not be reserved keywords.

Both agents continue only within their existing exclusive ownership boundaries.

### Phase 1 validation and partial review evidence

Manager independently reviewed corrected manifest/packaging files and ran:

- Focused SDK/manifest/packaging/corpus/fixture pytest: **287 passed in 0.46s**.
- Scoped Ruff: **All checks passed**.
- Scoped mypy: **No issues in 13 files** (repository configuration).
- `uv build --out-dir /tmp/opencode/custom-mcp-phase1-dist`: **sdist/wheel built**.
- Wheel inspection: **all four SDK modules, application validator and static
  assets included**. Temporary build artifacts are outside the repository.

Independent SDK/fixtures partial review scored **8.4/10**, confirming SDK
production code matches the exact contract but finding two strict `Any` return
annotations in raw-header fixture readers, weak defect-specific manifest fixture
assertions, and an SDK test-double delete error for stored `None`. Repository
test mypy settings had hidden the strict fixture typing issue. These were
assigned to their existing exclusive owners; validator code is stable and the
critic is now reviewing manifest/package safety while those test fixes proceed.

All three implementation agents handed off their final corrections and stopped
editing. Manager independently ran cumulative validation on the joined tree:

- `.venv/bin/python -m pytest -q`: **492 passed in 1.23s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues in 46 source files**.
- `uv lock --check`: **Resolved 103 packages; lock consistent**.

SDK/fixture critic corrections include explicit raw-reader integer typing and
actual manifest-defect assertions; SDK test storage now handles stored `None`.
Two critics independently review stable non-overlapping Phase 1 slices in
parallel (package/SDK safety and registry/API integration). Phase 1 is not yet
marked complete and no Phase 2 implementation has started.

### Independent package review findings (Phase 1 remains gated)

- **SDK/fixture closure PASS 9.1/10**: 139 scoped tests, configured Ruff and
  strict fixture typing verified; prior SDK/test findings resolved.
- **Manifest/package slice FAIL 6.8/10** despite 150 focused passing tests.
- A crafted DEFLATED entry understates its expanded size and uses a prefix CRC.
  `zipfile.testzip()` truncates to that declared length before CRC, so the
  validator falsely accepts 1MiB actual expansion as ten bytes. This is false
  acceptance, not a demonstrated filesystem escape or unbounded allocation.
- Small legitimate forced-ZIP64 local size sentinels are incorrectly rejected.
- Data-bearing directory entries bypass the per-entry ratio check.
- Huge semantic-version components and deeply nested JSON escape the manifest
  domain-error boundary (`ValueError`/`RecursionError`).
- Data-descriptor CRC/size fields are not independently verified.

The affected validator branch was stopped and assigned bounded raw compressed
stream verification, actual CRC/size/ratio/total checks, ZIP64/descriptor parsing,
directory-data rejection and precise manifest error handling with regressions.
SDK/fixtures remain stable. A private milestone notification reported the
review blocker (HTTP 200), still **10% verified complete / roughly 90% remaining**.

### Independent registry review finding

Registry/API slice scored **7.5/10 (FAIL)** after independently verifying all
492 tests, Ruff/mypy and lock checks. Two bounded real-worker probes showed
cancelled `remove()` releases the slug lock while a deletion thread is still
running; a replacement can then publish its files, which the old worker deletes.
One probe deleted the new source ZIP while Redis/memory still reported version2.
The integration owner was assigned to settle both removal filesystem workers
under the stable lock and add directory/source-ZIP cancellation regressions.
Publication's analogous worker-settlement path passed a double-cancel probe.

The fixture agent added eight independent stream/metadata cases through
`build_stream_cases()` without changing the original 38-case corpus. They cover
prefix CRC/understated sizes, declared zero output, valid forced ZIP64, directory
payloads, valid/corrupted descriptors, deeply nested JSON and huge numeric JSON.
Manager independently verified **147 fixture tests passed in 0.23s**, configured
Ruff clean and strict `--warn-return-any` mypy clean in three files. The stable
extension was handed to the validator owner for real-production regression tests.

Cancelled-removal correction handed off: both deletion workers now settle while
the per-slug lock remains held; startup re-extraction mutations use the same
pattern. Two deterministic real-worker regression cases cancel twice, start a
replacement, prove it blocks at the lifecycle lock, then verify replacement ZIP,
extracted code, version and Redis membership after deletion settles. The agent
reports **52 registry/API tests** and **98 including app regressions** passing,
scoped Ruff/mypy clean (10 files). The same critic is doing a focused rereview;
manager cumulative checks wait until the package validator is stable again.

Registry cancellation rereview **PASS 9.0/10**: critic independently ran both
real-worker regressions (**2 passed in 0.07s**) and scoped Ruff. It source-reviewed
the helper and startup settlement changes but did not run new mypy, broad tests,
startup stress testing or a separate standalone probe this turn. Registry's
specific blocker is cleared; package-validator review and manager joined checks
still gate Phase 1.

### Bounded ZIP correction handoff and cumulative verification

Validator now independently decodes raw compressed entry ranges with bounded
64KiB chunks, verifies actual size/CRC/end-of-stream, checks ZIP64/local metadata
and descriptors, rejects directory payloads, and contains large-version/deep-JSON
manifest errors. The production tests exercise all eight new stream cases as
well as the original corpus. The owner reports 188 focused tests passing.

Manager read the complete corrected validator and manifest and independently ran:

- Full pytest: **540 passed in 1.38s**.
- Full Ruff: **All checks passed**.
- Full mypy: **No issues in 46 source files**.
- Strict fixture mypy (`--warn-return-any`): **No issues in three files**.
- Lock check: **103 packages; consistent**.
- Source distribution and wheel: **rebuilt successfully** outside the repository.

The critic is rechecking the original reproductions on this stable tree. Manager
also supplied two new code-review concerns for assessment: an unrequested fixed
100MiB decoded ceiling when configured package limits are larger, and raw local
filename equality with central metadata after replacing `testzip()`. No phase
pass is claimed while the review remains incomplete.

Package rereview **7.9/10** confirmed the original prefix-CRC attack is rejected
and twelve legitimate STORED/DEFLATED, ordinary/ZIP64, signed/unsigned descriptor
combinations are accepted. Three remaining reproduced issues were assigned:
remove the unrequested fixed expansion ceiling, reject local/central filename
and encoding mismatch before validated publication, and normalize malformed
UTF8 ZIP-name decoding errors. SDK and registry approvals remain unchanged.

Final targeted corrections handed off: fixed expansion cap removed; raw local
filename/encoding now matches central metadata; undecodable ZIP filenames produce
domain errors. Manager independently ran stable-tree checks:

- Full pytest: **547 passed in 1.42s**.
- Full Ruff: **All checks passed**.
- Full mypy: **No issues in 46 source files**.
- Strict fixture mypy: **No issues in three files**.
- Lock consistency: **103 packages; success**.
- Rebuilt sdist/wheel: **success**.
- Isolated interpreter import from the wheel: **SDK/core imported from the
  wheel itself; mutable connect result and independent tool schemas verified**.
- Repository status/remote reviewed: no commits or pushes; SSH origin retained.

Final package critic judgment **PASS 9.0/10**: independently verified 177 focused
tests, scoped Ruff/mypy, removed-ceiling boundary behavior, local-name mismatch
rejection, CP437 compatibility and UTF8 domain errors. No unresolved material
finding remained in that scope. Registry review **9.0** and SDK/fixture **9.1**
were already complete. Manager joined validation above closes **Phase 1**.

Phase 2 starts with parallel scoped storage/logging, runtime/host/loader, and
Hello/runtime fixture work; integration follows stable interface handoffs.
The milestone notification returned HTTP 200: **20% complete / approximately
80% remaining**, measured by two of ten verified phase gates, not exact time.

## Phase 3 progress

Plan written to `PHASE_3_PLAN.md`. FastMCP 2.14.7 API re-verified against the
installed package (`http_app(path="/", stateless_http=True, json_response=True)`
returns a `StarletteWithLifespan`; tool results import from
`fastmcp.tools.tool.ToolResult`; tools carry a raw `parameters` schema; failures
raise `fastmcp.exceptions.ToolError`).

- **Adapter slice complete** (owner session `ses_e05e9e624ffe5Hz82IuDVfVg3`):
  `src/custom_mcp/mcp/adapter.py` exposes the frozen
  `build_plugin_app(runtime, host) -> StarletteWithLifespan`. One `_PluginTool`
  per cached SDK tool with `parameters=tool.input_schema` unchanged; explicit
  `Draft202012Validator` validation producing the uniform
  `[invalid_arguments] Invalid arguments for tool '<name>': <path> failed the
  '<keyword>' check.`; execution solely via `host.call(runtime, "invoke_tool",
  ...)`; success -> `ToolResult(content=json, structured_content=payload)`;
  failures -> `ToolError("[code] message[ (retryable)]")`; `NOT_CONNECTED` ->
  `[not_connected] Plugin '<slug>' is not connected...`. Manager independently
  verified: **8 mcp tests passed**, full **893 passed**, Ruff/mypy clean.
- **Router slice complete** (owner session `ses_e05cf6b17ffekRfsnaK9qjfFyi`;
  an earlier session `ses_e05e37329ffe1N19l886Q3hZyF` lost context before writing
  anything and was replaced with a self-contained brief):
  `src/custom_mcp/mcp/router.py` — ASGI `MCPRouter` with a dedicated
  lifespan-owner task per mounted app, atomic lookup swap, in-flight drain before
  the old lifespan exits, and correct `path`/`root_path` rewriting for both slash
  forms; MCP-client wire tests (schema/hot-swap/drain/lifespan); plus `main.py`
  `Mount("/mcp", ...)` wiring behind the existing bearer middleware and the
  default-None `PluginManager.on_runtime_change` hook. Owner reports **19 new
  tests / 912 total**.

  Manager independently verified the joined tree: **912 passed**, Ruff clean,
  mypy clean (71 files), `tests/mcp tests/integration` **156 passed**, real-Redis
  **15 passed**. A manager end-to-end probe (real `create_app` + fake Redis,
  install `hello` via the API with bearer, then the real `mcp` streamable-HTTP
  client) confirmed install 201 and, at BOTH `/mcp/hello/` and `/mcp/hello`:
  initialize, `tools/list` = {add, echo}, `echo`/`add` structured content,
  uniform `[invalid_arguments]` on bad args, and no-auth → 401.

  Environment facts re-probed on this tree (Starlette **1.10.0**, mcp 1.30.0,
  httpx 0.28.1): the plugin app has a single route at `/`; its lifespan is
  `app.lifespan` and must be entered/exited in the same task;
  `Mount("/mcp", app=...)` leaves `scope["path"]` full and sets
  `root_path="/mcp"`, so the router rewrites `path`/`root_path`/`raw_path`.

- **Phase 3 gate review: FAIL 7.0/10** (critic session
  `ses_e05b5bf43ffe8YaawKpWReRsoE`) — independently confirmed 912 tests, clean
  Ruff/mypy, 15 real-Redis tests, and a real-app wire reproduction passed all
  gate items EXCEPT three reproduced material findings, all fixed by the manager:
  1. **Stored credential leaked through MCP error content.** A plugin's
     `ToolResult.failure(...)` message (e.g. an upstream rejection body echoing
     `core.api_key`) reached the client verbatim. Fixed in `mcp/adapter.py`: an
     optional redactor is applied to every plugin-derived failure text, failing
     closed to a generic message; `MCPRouter` gains `redact=` bound per slug and
     `main.py` wires `logs.redact`.
  2. **API replacement bypassed the atomic swap.** The manager unmounted the old
     app in `_forward_unload` before loading the new one, 404-ing the endpoint mid
     replacement. Fixed: `_forward_unload` no longer unmounts; the new `mount`
     atomically swaps the lookup and retires the old app after drain, and
     `_unload_runtime_safe` only unmounts a runtime this router published; a
     failed reload now drops the still-mounted old generation.
  3. **Cancelled delete left inconsistent state** (loaded/connected dashboard while
     MCP 404). Fixed: `remove` records the drain cancellation but completes the
     teardown (unmount, shutdown, delete files/state) before re-raising.
  Three new real-app regressions added in `tests/integration/test_mcp_wire.py`
  (`test_stored_credential_is_redacted_in_mcp_error`,
  `test_replace_keeps_endpoint_answering_during_drain`,
  `test_cancelled_delete_is_consistent`). Manager independently verified the
  joined tree: **915 passed**, Ruff clean, mypy clean (71 files), real-Redis
  **15 passed**. Same critic session is re-reviewing.

### Phase 3 gate review — second pass FAIL 6.5/10 (re-review)

The critic confirmed the three original findings **resolved**, but reproduced
four further material issues, all fixed by the manager:

1. **In-flight old call's resources closed during replacement/reload** (`_forward_unload`
   shut the instance down before the router drained). Fixed with a new drain step:
   `MCPRouter.drain(slug)` plus a default-None `PluginManager.on_runtime_drain`
   hook (wired in `main.py`), so `_forward_unload` drains in-flight calls before
   the instance is shut down.
2. **Completed old-call writes lost** (candidate snapshot taken before the old
   call drained). Fixed: the drain now runs before `_forward_unload` returns, so
   `begin_transaction` snapshots storage after the old call's writes land.
3. **Invalid reload left a stale mounted app** (dashboard `load_failed`, MCP kept
   serving old tools; a later DELETE also left it mounted because `remove` skipped
   unmount when `record.runtime is None`). Fixed: the invalid-reload branch
   unmounts, and `remove` always unmounts the slug before deleting.
4. **Credential echoed in the SDK error *code*** bypassed redaction (only the
   message was redacted). Fixed: the adapter redacts the whole rendered
   `[code] message (retryable)` text.

Five new real-app regressions added (`test_inflight_old_call_survives_live_runtime_change`
[replace/reload], `test_inflight_old_write_is_preserved_by_upgrade_migration`,
`test_failed_reload_invalid_package_does_not_serve_old_tools`,
`test_upstream_error_code_is_redacted_too`). Manager independently verified:
**920 passed**, Ruff clean, mypy clean (71 files), real-Redis **15 passed**. Same
critic session is re-reviewing.

### Phase 3 gate review — third pass FAIL 8.0/10 (stopped per user instruction)

The critic confirmed all **seven** prior reproductions fixed (message AND code
redaction, no 404 during replacement, consistent cancelled delete, in-flight
resource survives replace/reload, in-flight write preserved by migration, invalid
reload does not serve stale tools) and all previously-passing gate items intact
(both slash forms; exact 564-byte nested schema; structured+JSON; uniform errors;
disconnected listing + exact `[not_connected]`; bearer/cookie 401; unknown/invalid
404; failed-load replace restores old code/state; 40 generations entered/exited
their same owner task; measured 5.005s bounded drain; no circular wait with all
four host slots held).

One remaining material race reproduces call failure and lost data: the router's
`drain` waits for zero in-flight requests but leaves the old endpoint **accepting
new calls**; the manager then unloads that instance. A call admitted in the window
between drain completion and the new mount either fails (its resource was closed
by the retiring instance's `shutdown`) or its write is overwritten by the
migration snapshot (commit writes `100` instead of `101`).

**Design tension:** closing admission to protect the retiring instance conflicts
with keeping the endpoint reachable during a replacement (the earlier accepted
finding wanted no 404 mid-replacement). Satisfying both needs an admission
boundary the spec does not define — either (a) requests arriving after the drain
*wait* for the new generation and are then served by it (bounded by a timeout),
(b) they get a transient 503, or (c) the old instance stays fully alive until the
new generation is mounted and the migration snapshot is taken at swap time. This
is a Phase-3 design decision, not a mechanical fix.

Per the user's explicit instruction ("if you hit any blockers, stop rather than
trying to brute force through anything"), further iteration on this concurrency
area was **stopped** at the third review round. The tree is stable and green
(**920 passed**, Ruff/mypy clean, real-Redis **15 passed**); Phase 3 remains
**open** pending the user's decision on the admission-boundary semantics. All
seven accepted fixes are retained; nothing is broken.

## Phase 4 progress

Plan written to `PHASE_4_PLAN.md`. Two non-overlapping slices completed and
manager-verified:

- **4a connect/disconnect** (`ses_e045d8d09ffeC2YFM05vlTGExc`): `PluginManager`
  gains `PluginConnectError` + `connect(slug, mode, credentials)` /
  `disconnect(slug)` under the stable slug lock. Connect validates the mode,
  stores `core.api_key` before the `on_api_key_set` hook, runs `connect()`,
  persists `auth_mode`+status, refreshes tools and re-mounts so the live tool set
  swaps; disconnect runs `on_disconnect`, wipes `plugin:{slug}:secret:*`, resets
  `auth_mode=null`/`not_connected`. `api/plugins.py` adds bounded
  `POST /api/plugins/{slug}/connect|disconnect`; `api/errors.py` adds
  `conflict`/`not_loaded` -> 409. **12 tests**.
- **4b apikey_demo** (`ses_e045d8d08ffeWCRP2tqIynYIdb`): `plugin_examples/apikey_demo/`
  (manifest slug `apikey-demo`, `whoami` always + `fetch_item` only with a stored
  key, upstream 401->`auth_expired`, timeout/5xx->retryable `upstream_unavailable`,
  env-configured base URL, single `_http_get` seam) plus a README and **25 tests**.

Manager independently verified the joined tree: **957 passed**, Ruff clean, mypy
clean (73 files), real-Redis **15 passed**. An end-to-end probe confirmed the
`apikey-demo` package installs through the real host and loads (`loaded`, tool
`whoami`) — the hyphenated isolated module name imports correctly. Phase 4 gate
critic session `ses_e0455a4daffeBdQSRpuNuu5jhj` is reviewing.

### Phase 4 gate review — FAIL 7.0/10 (pass 1)

The critic independently verified **957 tests**, clean Ruff/mypy, 15 real-Redis
tests, and the gate items (hook sees stored key, mode/body validation, status
persistence, live tool-set swap, demo credential/transient mapping) — and
reproduced four material findings, now being fixed by session
`ses_e0443800affet67oiFOt1VvW2C`:
1. an in-flight tool failure leaks the credential during disconnect (redaction
   used current secrets, already wiped) -> redact against a per-call secret
   snapshot;
2. a timed-out hook's leaked worker recreates secrets and logs the credential
   after disconnect -> seal a retired generation's context (storage writes and
   plugin logs become no-ops);
3. a credential-bearing tool **description** is exposed in connect/detail JSON
   and MCP `tools/list` -> redact descriptions at `_refresh_tools`;
4. a whitespace-only API key is accepted -> reject it with 422.

### Phase 4 findings fixed and manager re-verified

All four findings were implemented by session `ses_e0443800affet67oiFOt1VvW2C` and
independently read and re-run by the manager:

1. **Per-call secret snapshot redaction.** `PluginLogs.secret_strings(slug)` returns
   an ordered tuple snapshot and `redact_secrets(text, secrets)` is a pure
   redactor over it; `MCPRouter(secret_strings=..., redact=...)` binds them, and
   `_PluginTool` captures the set once at run start then redacts the rendered
   failure against the capture (failing closed to a generic message). An in-flight
   call whose key was already wiped by `disconnect` now surfaces `***`.
2. **Sealed retired generations.** `PluginRuntime.seal` is a `threading.Event`
   threaded into the scoped storage facade and a sealing log wrapper. Once set
   (`disconnect`, `_forward_unload`, `remove`, `shutdown_all`), `Storage.set`/
   `delete` become no-ops and the plugin log callback drops messages, so a
   timed-out hook's leaked worker cannot recreate `derived`/`core.api_key` or
   persist the credential. `connect` clears the seal on reconnect.
3. **Description redaction.** `_refresh_tools` redacts every tool description
   through `_redact_description`, failing closed to `"Redacted."`, so connect and
   detail JSON plus the live `tools/list` cache never carry a credential.
4. **Whitespace key rejection.** `connect` rejects `not candidate.strip()` with
   `invalid_request` (422) and still stores the exact padded value untrimmed.

New `tests/integration/test_phase4_containment.py` (7 cases) drives the real app
and live MCP wire: hook sees the stored key, tool set swaps, disconnect wipes and
resets, in-flight gated failure redacted after wipe, timed-out hook's leaked
worker contained (1s and default timeout), description absent from connect/detail
JSON and `tools/list`, whitespace-only rejected while a padded key is stored
exactly.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **964 passed in 28.95s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 74 source files**.
- `uv lock --check`: **Resolved 104 packages**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.43s**.
- Focused Phase 4 suites (`test_connect`, `test_phase4_containment`,
  `test_apikey_demo`): **44 passed**.

The same Phase 4 gate critic session is re-verifying the four reproductions and
re-scoring.

### Phase 4 gate re-review — FAIL 7.5/10 (pass 2)

The critic independently verified **964 tests**, clean Ruff/mypy (74 files), the
lock check, the **15 real-Redis** tests and **44 focused Phase 4** tests, and
reproduced all four original findings as **RESOLVED**. It then reproduced three
adjacent material defects, all fixed by the manager directly:

1. **Reconnect re-enabled a retired generation's leaked worker.** `connect` called
   `runtime.seal.clear()`, so a worker still blocked from a timed-out
   `on_api_key_set` could write/log again after `disconnect` wiped the secrets and
   a reconnect re-opened the same seal. **Fix:** a seal is now one-way. `connect`
   never clears it; when the current generation is sealed it **rebuilds the
   generation** (`_rebuild_sealed_runtime`): unmount/drain, seal, unload the
   retired instance, load a fresh runtime + fresh `PluginContext` from the same
   extracted code, and continue connect on it. Every leaked worker stays bound to
   the permanently sealed old facades.
2. **Reload/replace-promoted generations had no seal.** `_new_runtime` assigned
   `seal=None` to candidate-transaction contexts; after commit their proxies point
   at live storage, so retirement no longer contained them — a timed-out hook's
   leaked worker recreated a wiped secret. **Fix:** `StorageTransaction` now owns
   an `Event` (`txn.seal`), its `StorageProxy` facades and log wrapper honour it,
   and the candidate generation is built with `seal_override=txn.seal`. A
   promoted generation seals exactly like a fresh-install one.
3. **Sealing dropped legitimate `shutdown` checkpoint writes.** Sealing before
   unload made a plugin's normal `shutdown` `ctx.storage.set("checkpoint", ...)` a
   silent no-op, losing buffered state across reload/replace/`shutdown_all`. **Fix:**
   a new thread-scoped permit (`plugins/seal.py`) lets the worker thread running a
   deliberate `shutdown` persist **data** while the seal is set; leaked workers
   live on other threads and never inherit it. The permit never unblocks the
   secret namespace, so a retired generation cannot re-persist a wiped credential
   through its own teardown write. The scoped storage `_sealed()`, the manager's
   sealed log wrapper and the candidate log wrapper all consult the permit.

New `tests/integration/test_phase4_containment_2.py` (5 cases) drives the real app
and live MCP wire: reconnect-after-disconnect contains the leaked worker while the
new credential stays intact; a reload-promoted generation's late worker is
contained; a replace-promoted generation's late worker is contained; a shutdown
checkpoint survives a reload (fresh instance resumes from it); and a `shutdown`
hook cannot re-persist a wiped secret. Each test was mutation-verified in an
isolated copy: reverting only its fix makes exactly that test fail.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **969 passed in 33.89s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 76 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.42s**.
- `uv lock --check`: **Resolved 104 packages**.

The same Phase 4 gate critic session is re-verifying the three new reproductions
and re-scoring.

### Phase 4 gate re-review — FAIL 8.0/10 (pass 3)

The critic independently verified the joined tree, re-ran its probes and confirmed
all three pass-2 findings **RESOLVED** (reconnect at 1s and default 15s; reload at
1s/15s and replace at 1s; shutdown checkpoints preserved for reload, replace and
`shutdown_all`). It then reproduced two further regressions, both fixed by the
manager:

1. **A rejected connect after disconnect unmounted the endpoint.** The pass-2
   rebuild ran *before* credential validation, so a 422 (`"   "`, missing,
   integer or null key) had already unmounted/rebuilt the loaded, disconnected
   plugin, leaving the endpoint at 404. **Fix:** `connect` now validates the mode
   and the `api_key` credential *before* any state change; it stores the key and
   rebuilds only after validation succeeds.
2. **A timed-out `shutdown` worker retained its write permit.** The pass-2 permit
   was thread-scoped but not revocable, so a shutdown hook that blocked past the
   timeout kept writing to live storage after its call was abandoned — recreating
   deleted state on `remove` and overwriting the replacement checkpoint on
   `replace`. **Fix:** `WritePermit` is now revocable; `PluginHost._abandon`
   (timeout/cancellation) revokes the permit, so an abandoned shutdown worker can
   no longer write. Normal and exceptional shutdown still persist their data.

New cases in `tests/integration/test_phase4_containment_2.py`: four parametrized
rejected-connect cases (whitespace/missing/integer/null) assert the generation,
`load_state`, status and live `tools/list` are unchanged; two abandonment cases
assert a timed-out shutdown worker cannot recreate removed state or overwrite a
replacement checkpoint. Each was mutation-verified: reverting only its fix makes
exactly those tests fail.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **975 passed in 44.27s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 76 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.45s**.
- `uv lock --check`: **Resolved 104 packages**.

The same Phase 4 gate critic session is re-verifying the two new reproductions and
re-scoring.

### Phase 4 gate re-review — FAIL 8.3/10 (pass 4)

The critic independently verified the joined tree and confirmed both pass-3
findings **RESOLVED** (rejected connects leave generation/metadata/endpoint
untouched for whitespace/missing/integer/null keys and unsupported mode/non-object
body; abandoned shutdown workers wrote nothing at the default 5s timeout for
replace/remove/reload). It then reproduced one new material defect:

- **Normal shutdown logged a wiped, cached credential.** A plugin caches the key
  in `on_api_key_set` and logs it in `shutdown`. The pass-3 permit unblocked
  shutdown *logs* as well as data, but `PluginLogs` redacts only against *current*
  secrets — after `disconnect` wiped them, the cached key reached operation JSON,
  detail JSON, persisted logs and stdout. **Fix:** the permit now unblocks **data
  writes only**. A sealed generation always drops logs (both the manager's
  `_sealed_log` and the candidate `_wrap_log`), so a cached credential logged at
  teardown can never escape; the legitimate data checkpoint still persists.

New case `test_sealed_shutdown_log_cannot_leak_cached_credential` in
`tests/integration/test_phase4_containment_2.py` reproduces the critic's plugin
(disconnect → reconnect → retired shutdown logs the cached key) and asserts the key
is absent from the reconnect JSON, detail JSON, persisted logs and stdout while the
shutdown data checkpoint still lands. Mutation-verified: allowing shutdown logs
again makes exactly that test fail.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **976 passed in 44.45s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 76 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.41s**.
- `uv lock --check`: **Resolved 104 packages**.

The same Phase 4 gate critic session is re-verifying the log-containment fix and
re-scoring.

### Phase 4 gate re-review — FAIL 8.3/10 (pass 5)

The critic confirmed the shutdown-log finding **RESOLVED** (12 fresh/promoted
runtime combinations: sealed shutdown logs dropped, checkpoint persisted, retired
secret writes blocked; no credential in API/persisted logs/stdout). It then
reproduced one further material defect:

- **`disconnect` exposed a cached credential in tool descriptions.** `disconnect`
  deleted the scoped secrets *before* `_refresh_tools()`, so a plugin that cached
  its key in memory and echoed it in a `list_tools` description had that
  description published unredacted (disconnect JSON, detail JSON and MCP
  `tools/list`) — current-secret redaction could no longer see the wiped key.
  **Fix:** `disconnect` now captures a **pre-wipe secret snapshot** and passes it
  to `_refresh_tools`, whose description redactor covers the union of the snapshot
  and the current secrets (failing closed to `"Redacted."` when the snapshot is
  unreadable). The tool set is still recomputed after the wipe, so gated tools
  still disappear.

New case `test_disconnect_redacts_cached_credential_in_descriptions`
(parametrized fresh/reload/replace) reproduces the critic's plugin and asserts the
key is absent from the disconnect JSON, detail JSON and MCP `tools/list` with the
description exactly `Upstream credential diagnostic: ***`, then that a later
reconnect works. Mutation-verified: dropping the pre-wipe snapshot makes all three
cases fail.

Manager also reproduced and fixed a **repeat-disconnect variant** the same turn
(not yet reported by the critic): a *second* `disconnect` took an empty snapshot
and re-exposed the cached key through a fresh `list_tools`. `PluginRuntime` now
keeps a `retained_secrets` history for the generation (the `connect` key plus any
pre-wipe snapshot), and every `_refresh_tools` redacts descriptions against the
union of that history and the current secrets. New case
`test_repeated_disconnect_keeps_cached_description_redacted` covers it.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **980 passed in 44.72s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 76 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.41s**.
- `uv lock --check`: **Resolved 104 packages**.

The same Phase 4 gate critic session is re-verifying the description-redaction fix
and re-scoring.

### Phase 4 gate re-review — FAIL 8.5/10 (pass 6)

The critic confirmed the cached-description finding **RESOLVED** for fresh, reload,
replace and repeat-disconnect (its own 8-case matrix, including the repeat-disconnect
variant it independently probed). It then reported one residual narrow race: an
in-flight `list_tools`/`tools/list` that is rendered while a `disconnect` wipe and
`_refresh_tools` race could surface an unredacted description. The critic reviewed
the 979-test tree; the manager's same-turn `retained_secrets` fix (980 tests) keeps
a per-generation history of credential strings and redacts every description
refresh against it, which also closes repeated and in-flight refresh ordering.

### Phase 4 advanced under the anti-thrash policy (outstanding findings recorded)

Phase 4 has now had **six** sub-8.5 critic passes (7.0, 7.5, 8.0, 8.3, 8.3, 8.5).
Per the user's standing directive ("if a phase's critics score below 8.5 more than
three times, proceed to the next phase while recording outstanding errors"), Phase 4
is **advanced** to Phase 5 with the outstanding findings recorded for the final
report rather than iterating further:

- **Phase 4 outstanding finding (narrow, unresolved):** an in-flight
  `list_tools`/`tools/list` rendered concurrently with a `disconnect` wipe and its
  `_refresh_tools` could return a tool description carrying a credential. All
  sequential paths (fresh/reload/replace/repeat-disconnect/reconnect) are redacted;
  the manager's current tree additionally retains per-generation secret history,
  which is expected to cover the race, but it was not independently re-confirmed by
  the critic before advancing.
- **Phase 3 outstanding finding (deferred earlier):** the router `drain` waits for
  zero in-flight calls but leaves the old MCP endpoint admitting new calls before
  the new generation mounts; a call admitted in that window can fail or lose a
  write to the migration snapshot.

Every Phase 4 finding was a reproduced credential leak or lifecycle defect and all
were fixed; the tree is green and materially hardened (980 tests, Ruff/mypy clean,
15 real-Redis). The stored Phase 4 containment fixes are: per-call secret-snapshot
redaction; sealed retired generations (fresh and candidate); a revocable
**data-only** shutdown permit; validate-before-rebuild on connect; sealed
generations always drop logs; pre-wipe and per-generation retained description
secrets.

## Phase 2 interface preparation

Lifecycle integrator read stable whole files and produced an assembly plan before
editing. Manager aligned its proposal with exact SDK result signatures,
typed runtime identity, connection/load-state independence and auto-connect
policy. Plugin call failures must leave successfully loaded tools visible;
connect errors must not be misclassified as load failures. Host-owned per-slug
semaphore/leak accounting must survive runtime generations.

An isolated strict mypy probe importing `jsonschema.Draft202012Validator`
reproduced missing library stubs (`import-untyped`). The integrator was authorized
to add `types-jsonschema` to the development dependencies and update the lock/
project environment, rather than ignoring typing errors. It remains read-only
on lifecycle files until collaborator interfaces are stable.

The development stub dependency was added (`types-jsonschema` version
4.26.0.20261006), with 104 locked packages and no new ignored typing errors.
Manager independently reran the strict one-file probe (**success**) and lock
check (**104 packages; consistent**). The host/loader owner was informed that
typed Draft202012Validator imports are ready.

Storage/logging interfaces were handed off: exact synchronous SDK storage,
explicit async reserved-secret access, context factory requiring the plugin log
callback, current-secret redaction, capped logs and non-TTL snapshots. Manager
read all relevant modules and independently verified **74 focused tests passed
in 0.30s**, scoped Ruff and mypy clean (six files). Interfaces were sent to the
integrator. Minor owner-directed regressions now cover log limit-zero behavior,
tuple/key metadata redaction, invalid Unicode/deep JSON normalization and SCAN
prefix defense before the critic review. No Phase 2 gate is closed yet.

Hello example and 17 runtime fixture packages handed off. Manager independently
verified **65 tests passed in 0.12s**, scoped Ruff clean, strict example/fixture
mypy clean in four files, and read the complete Hello and builder source.
The example uses exact SDK methods, independent schemas and a locked persisted
call counter. Fixture APIs were shared with host and integration owners; a
read-only critic is reviewing this stable slice while other units proceed.

Storage/log corrective handoff reports **85 scoped tests passing**, configured
Ruff/mypy clean and unchanged collaborator APIs. A separate critic is reviewing
that stable unit. Hello/runtime fixture critic scored **8.4/10**: Hello code is
correct and all 17 fixtures were independently exercised, but invocation-defect
fixtures expose no tools, behavioral regression assertions are weak, and the
README example mishandles deletion of stored `None`. The fixture owner is making
those concrete corrections within its existing exclusive scope.

Manager independently reran corrected storage/log checks: **85 tests passed in
0.42s**, scoped Ruff and mypy clean (six files). The critic independently
verified the same suite but scored **8.0/10** after reproducing a serialization
redaction gap: a string secret such as a six-digit credential reappears when
`json.dumps` converts an integer metadata key/value after string-only redaction.
The owner is normalizing metadata to JSON-safe key/scalar representations before
redaction and adding sync/async regression tests. No stdout leak was demonstrated
by that probe; persisted metadata exposure is sufficient to gate the slice.

Runtime fixture corrections handed off: invocation defects advertise valid echo
tools, behavioral execution replaces source-marker checks, and the README delete
example handles stored `None`. Manager independently verified **81 tests passed
in 0.22s**, scoped Ruff and strict mypy clean in four files. The host owner was
given the stable callable fixtures; the critic is performing a targeted rereview.

Hello/runtime-fixture rereview **PASS 9.1/10**: critic independently verified all
81 tests, scoped Ruff and strict mypy across four files. All three earlier
findings are resolved. This approves the example/fixture slice, not production
host/loader guarantees or the full Phase 2 gate.

JSON-aware log correction handed off: final JSON key representations are
normalized before redaction, scalar text matching a current secret is safely
replaced, and unsupported/cyclic/nonfinite metadata raises generic errors.
Manager independently verified **95 storage/log tests passed in 0.47s**, scoped
Ruff and mypy clean in six files. The critic is rechecking the original numeric
metadata leak on this stable slice; collaborator APIs remain unchanged.

Storage/logging rereview **PASS 9.0/10**: critic independently ran all 95 focused
tests, scoped Ruff/mypy and sync/async reproductions proving the original numeric
metadata credential is absent from persisted entries, returned recent logs and
captured stdout. The specific blocker is closed. Hello/fixtures already passed
**9.1**. Runtime/host/loader, lifecycle assembly and joined validation still gate
Phase 2; only two of ten build phases are verified complete.

Runtime/host/loader APIs handed off and lifecycle assembly authorized. Manager
read all three modules and independently ran **43 focused tests passing in
1.23s**, scoped Ruff and mypy clean across six files. The host uses a shared
executor and per-slug leaked-worker accounting; loader mediates imports and
construction, validates auth/tools, and bounds shutdown including semaphore
waiting. A critic is reviewing this stable slice while integration edits only
its assigned manager/API/main/shared-test files. Manager supplied source-review
concerns about event-loop context lookup, failed-instance shutdown, late-import
module cleanup and late-future/log-callback error handling for independent
reproduction before acceptance.

Host/loader critic scored **6.9/10** after independently verifying all 43 tests
and reproducing: plugin-controlled context/schema checks and exception repr on
the event loop outside timeouts; abandoned-future/callback exception diagnostics
with raw credential text; and failed loads clearing constructed instances before
shutdown. Corrections were assigned to the host/loader owner, preserving the
public interfaces. Integration continues in its separate files but must not
revalidate plugin-controlled schemas on the event loop. A late-relative-module
replacement probe did not reproduce module resurrection; that remains a tested
strength rather than an assumed defect.

### Transient provider interruption

The host/loader correction session and lifecycle integration session both stopped
with **`Provider request failed with HTTP 404`**. This was a provider invocation
failure, not a test result. The user explicitly authorized continuation after
identifying it as an upstream transient network error. Both **same child sessions
and same implementation model** were resumed, with instructions to preserve work,
check their last successful state and continue only their assigned ownership.
No reset, revert, new replacement session or model substitution was performed.

Host/loader corrections handed off after resume: worker-side defensive exception
formatting and context checks, consumed late exceptions, generic callback-failure
logs and bounded shutdown before failed-instance cleanup. Manager read the full
corrected modules and independently verified **52 tests passed in 1.68s**,
scoped Ruff and mypy clean across six files. The critic is doing a focused
rereview; manager also requested a probe of arbitrary plugin-exception
`safe_message` attributes being trusted before stdout redaction. Lifecycle
integration continues against the unchanged public APIs.

Host rereview scored **8.1/10**: prior containment/cleanup findings are resolved,
but arbitrary plugin exceptions can mark credential text as `safe_message` or
`detail_safe`, exposing it to the returned error and raw host log before
redaction. Owner is replacing this duck-typed trust with generic plugin-error
text and an explicit internal contract-error carrier, with regressions.

Lifecycle integration handed off load/startup/reload/remove/shutdown, rollback,
cached tools, selected-mode reconnect and logs routes. Its reported cumulative
suite is **800 passing tests** (not yet manager joined verification). Manager
read complete manager/main/API files and independently verified **123 focused
tests passed in 1.45s**, scoped Ruff and mypy clean in five files. A second
critic reviews integration; manager provided source concerns around collaborator
ownership, configured loader timeout, reload/startup/candidate cleanup and
fail-open diagnostic redaction for targeted reproduction.

Lifecycle integration critic **FAIL 6.5/10**, independently running the 800-test
suite and reproducing five missing regressions: a failed candidate mutates live
data/secrets despite file rollback; diagnostic redaction fails open; invalid ZIP
reload drops the runtime before unload; app cleanup closes injected hosts; and
loader timeout ignores the configured plugin-call timeout. Owners were assigned
isolated candidate storage (including TTL preservation and detached late-worker
writes) and integration cleanup/redaction/ownership corrections in parallel.

Host's explicit safe-error carrier fix was handed off with 56 focused tests;
the host critic is doing a fast targeted rereview. No Phase 2 pass is claimed.

Runtime/host/loader rereview **PASS 9.0/10**. Manager independently verified
**56 focused tests passed in 1.69s**, scoped Ruff and mypy clean in six files.
The critic independently verified 56 tests and spoofed-attribute/property probes:
generic returned messages and raw logs/callbacks contain no sentinel credential.
This clears the host-specific blocker. Lifecycle transaction/cleanup corrections
and their joined verification still gate Phase 2.

Integrator handed off fixes for findings 2–5: fail-closed public redaction and
generic callback diagnostics; invalid reload shutdown/module cleanup and
incremental startup runtime discoverability; injected-host ownership plus
cancellation-resistant lifespan cleanup; configured loader timeout wiring.
It reports **133 focused / 814 cumulative tests passing**, scoped Ruff/mypy
clean. A critic is checking these corrections. Finding 1 remains open until
isolated candidate storage is integrated: failed or late candidate workers must
never alter the previous live data/secrets, and successful staged writes must
promote with TTL semantics preserved. No Phase 2 pass is claimed.

Manager independently verified this four-fix handoff: **133 focused tests passed
in 1.56s**, scoped Ruff and mypy clean across three changed files. The 814-test
number remains a child report until the final joined tree is stable.

### Isolated candidate storage handoff and independent probes

StorageTransaction initial API was handed off with staged context, commit,
discard, current staged-secret redaction, atomic delta pipeline and PTTL-aware
snapshots. Manager read the complete module/tests and independently verified
**111 storage/transaction/log tests passed in 0.73s**, scoped Ruff/mypy clean.

The transaction critic stopped before a score with provider rejection:
**`[400]: content-blocked (request id: 20261121002409889103458p2cgp6RD7RVjY)`**.
No model substitution or review approval was inferred.

Two manager bounded fakeredis/thread probes reproduced races in the initial
commit implementation: discard during an awaited worker changed state to
discarded, but completion then promoted live state; cancellation plus discard
returned before the worker later changed live Redis while still marked discarded.
Owner is moving the entire commit/state/proxy-promotion operation into one
worker-held lock, settling commit on cancellation, refusing inconsistent
in-flight discard, and locking proxy backend selection. Integrator was told to
preserve the new published runtime/files when a settled commit completed,
rather than restoring old files over committed new storage.

The lifecycle critic's four-fix rereview scored **8.0/10**: original findings
2–5 are closed, but startup cancellation during post-load connect can still
hide the current runtime, and external lifespan-cleanup cancellation is swallowed.
Both were assigned to the integration owner with precise regressions alongside
transaction integration. Phase 2 remains gated.

The user explicitly authorized fresh critic sessions after blocked requests,
then corrected the fallback model name to **`v41-flash-combo`**, verified available
as `local-omniroute/v41-flash-combo`. A fresh `agentrouter/gpt-6.1-sol` critic
successfully read the review contracts and awaits stable files. If a fresh
primary critic remains blocked, the corrected fallback is now authorized.
Context poisoning is a user-suggested hypothesis, not a confirmed diagnosis.

Integrator handed off staged replacement/reload contexts, old-runtime unload
before candidate publication, commit-state-aware cancellation decisions and
pre-commit candidate cleanup. Startup now registers records before post-load
connect; lifespan tracks external cancellation while completing owned cleanup.
It reports **139 focused tests passing**, scoped Ruff/mypy clean. Final actual
commit-barrier/cancellation integration tests are explicitly held until the
storage owner hands off its corrected worker-side commit/promotion semantics.

Storage commit corrections handed off: state now includes `committing`, in-flight
discard fails immediately, whole promotion runs under a worker-held lock,
cancelled callers wait for settlement, and proxy backend selection is serialized.
Manager read the full corrected storage module and independently verified
**115 focused tests passed in 0.96s**, scoped Ruff and mypy clean in three files.
The fresh critic is reviewing this stable unit; integrator is adding actual
commit-barrier cancellation and late-constructor tests before final handoff.
An additional manager bounded fakeredis/real-worker probe confirmed in-flight
discard is rejected, a doubly cancelled commit remains unfinished until worker
release, cancellation propagates after settled `committed` state, and subsequent
discard does not change that coherent final state.

Final lifecycle handoff includes three real-worker regressions for committed
replacement cancellation, pre-commit candidate cleanup and late constructor
writes; it reports **843 cumulative tests**, clean Ruff/mypy (61 files), strict
fixture typing and package builds. Independent joined validation is pending.

Fresh transaction critic **FAIL 6.5/10**, verifying prior races are fixed but
reproducing three new gaps: invalid Redis TTL arguments can cause partial
MULTI/EXEC writes; an applied commit with a lost reply is incorrectly marked
open; relative expiry promotion can resurrect expired keys; and transaction-only
redaction misses still-live overwritten or rotated credentials. Owner is
implementing server-side prevalidation and a commit receipt/reconciliation path,
absolute expiry semantics, and combined initial/staged/live redaction. Integrator
was warned never to roll back old files as if storage were unchanged when a
commit outcome cannot be established. Phase 2 remains gated.

Lifecycle critic independently verified **843 cumulative tests**, 142 focused
tests, scoped lint/types and the original failed-constructor corruption fix.
It retained score **8.0/10** for two remaining cancellation losses: forward
replacement settles old-runtime unload without propagating cancellation, and
lifespan `_settle` returns from a completion-race branch before recording it.
Integrator is correcting those explicit boundaries in parallel with storage
commit reconciliation, without speculative public recovery APIs or load-state
changes. The revised storage outcome API is still pending.

Both lifecycle cancellation boundaries were corrected: forward unload records
and propagates cancellation after settlement while rollback restores v1; reload
uses the same distinction; lifespan records cancellation before the completed
task branch. Manager independently verified **145 focused tests passed in
2.40s**, scoped Ruff and mypy clean across three changed files. The critic is
rechecking this stable handoff; cumulative validation remains pending the revised
storage commit-outcome implementation.

Revised storage transaction handoff reports **122 focused tests**, scoped lint/
types clean. Commit uses a server-side Lua operation with eager validation,
absolute deadlines, commit receipt and reconciliation; inability to establish
the outcome produces explicit `uncertain` state and `CommitOutcomeUnknown`,
refusing discard. Combined redaction checks initial, staged and active secrets.
Integration was assigned to preserve both code versions and block conflicting
lifecycle work on an unresolved outcome, using existing public statuses/errors
and a minimal internal retained journal, not a new recovery REST API.

The lifecycle critic verified blocked unload and lifespan completion cancellation
but again scored **8.0**: `_forward_unload` checks worker completion before
recording coincident cancellation. The owner was given the exact ordering fix
and replacement/reload completion-race regressions. Verified reference docs also
confirm Redis transactions do not roll back post-EXEC command errors, and PXAT
 is available on the required Redis >=6.2.

MVP-focused transaction rereview **8.0/10**: expiry, active redaction, successful
lost-reply reconciliation and prior cancellation protections pass, with 122
focused tests and clean lint/types. One demonstrated safety defect remains:
receipt-write failure after mutation, or failed compensation, can leave live
candidate values while an absent receipt is interpreted as open/discardable.
Owner is recording explicit pending/committed/rolled-back outcomes, protecting
receipt finalization with compensation, and retaining uncertain state unless
pre-write failure or completed restoration is actually established. Astronomical
TTL rounding observations were explicitly unscored under the MVP-only policy.

An independent agent is adding real-Redis validation only under
`tests/real_redis/`, using its own port-disabled Unix-socket Redis process and
test-owned AOF directory. It must never access or flush localhost:6379. Initial
storage/persistence checks can proceed against stable APIs; final transaction
script checks wait for the implementation handoff. This adds relevant validation
evidence without changing the product scope or shared application files.

Latest integrator handoff fixes coincident forward-unload cancellation and adds
replacement/reload regressions, with **147 focused / 49 lifecycle tests reported**.
It adds a minimal unknown-outcome guard: existing loaded/error statuses, retained
old/new files in a private pending directory/journal without secrets, conflicting
same-slug lifecycle rejection, and startup halt before cleanup/loading unresolved
state. No public recovery endpoint or new load-state enum is introduced.
Receipt-specific tests and joined verification await the storage handoff;
the critic is reviewing only the final cancellation closure in the meantime.

Lifecycle cancellation slice **PASS 9.0/10**: critic independently ran all five
replacement/reload unload-cancellation and lifespan-completion regressions
(**5 passed in 0.11s**), scoped Ruff and mypy clean in three files. No material
finding remains in that slice. Storage transaction approval, unknown-outcome
integration checks, real-Redis evidence and joined validation still gate Phase 2.

Real-Redis validation handed off: `tests/real_redis/` now holds **15 tests** using
its own port-disabled Unix-socket Redis 7.0.15 with `appendonly yes`,
`appendfsync always` and a test-owned AOF directory; the external
`127.0.0.1:6379` instance is never touched. Coverage includes candidate-discard /
late-write isolation, commit delta + live proxy rebind, committed receipt with TTL
preservation, no TTL resurrection on commit, and AOF restart. The owner reports
**15 passed**, scoped Ruff and strict mypy clean; the manager will independently
rerun it in the joined verification.

Unknown-outcome integration handoff (final): `plugins/manager.py` now inspects the
authoritative `txn.state` after the commit worker settles (fixing an earlier bug
where a raised `CommitOutcomeUnknown` bypassed the uncertain branch and rolled
back over possibly-live data). The uncertain branch preserves both versions in a
private `tmp/pending-<uuid>/` journal (`old.zip`, `old_dir`, secret-free
`journal.json`), keeps the loaded candidate visible with status `error`, blocks
same-slug lifecycle with the generic `internal_error`, and makes `startup()` halt
before cleanup/load. `_forward_unload` records cancellation before the done-check
(mirroring `main._settle`). New receipt-specific tests exercise unknown /
lost-reply-committed / rolled-back outcomes, restart halting and post-unknown ctx
unpromotion. No new public endpoint or load-state value was added.

Manager independently reran the joined tree after the handoff:

- `.venv/bin/python -m pytest -q`: **878 passed in 10.52s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 64 source files**.
- `uv lock --check`: **Resolved 104 packages in 1ms**.

The manager also read the assembled `manager.py`, `main.py`, `api/plugins.py`,
`api/errors.py` and `storage/manager.py`: `PluginUpgradeUncertain` (a
`RuntimeError`) and the `_require_not_pending` guard both fall through to the
generic `internal_error` handler with no leaked detail; `reload`/`install` map
only the defined domain errors. An independent MVP-scoped integration critic is
performing the final read-only review; Phase 2 closes only on a score strictly
greater than 8.5.

### Final integration critic — first pass FAIL 6.5/10

The critic independently verified **878 passing tests**, clean Ruff/mypy and
**15 real-Redis tests** (private port-0 Unix-socket Redis 7.0.15, external
`127.0.0.1:6379` untouched; a TCP-blocked rerun made zero external connections).
It then reproduced three material defects:

1. **Queued lifecycle requests bypassed pending containment.** `reload`/`remove`
   checked `_require_not_pending` only before awaiting the slug lock and never
   inside it, so a request queued during an in-flight `uncertain` replacement ran
   afterward — a queued DELETE returned 204 and deleted the preserved new ZIP,
   live data and secrets while the slug stayed pending; a queued reload returned
   200 and re-committed.
2. **Reload ignored the commit decision.** `_replace_extracted` deleted the old
   extraction before the commit, so on `uncertain` the pending dir held only
   `journal.json`; a confirmed rollback left `load_state=loaded` with
   `runtime_for(slug) is None`.
3. **Cancellation was lost on non-committed outcomes.** The `uncertain` and
   rolled-back/pre-write branches raised their outcome error instead of
   `CancelledError` when the caller had been cancelled.

Manager confirmed finding 1 in code (`install` re-checks inside the lock;
`reload`/`remove` did not) and delegated a bounded fix to a `v41-flash-combo`
implementation agent owning only `manager.py` and the lifecycle test file.

### Fix handoff and independent joined verification

- Containment: `reload`/`remove` re-assert `_require_not_pending` inside the lock.
- Reload: `_replace_extracted(..., keep_backup=True)` retains the pre-reload
  extraction; success deletes it, `uncertain` moves it to `pending-*/old_dir`,
  and a confirmed rollback/pre-write/conflict restores the previous runtime under
  the same lock (never `loaded` with no runtime).
- Cancellation: `_commit_candidate` raises `CancelledError` when a cancellation
  was observed for both the `uncertain` and rollback/pre-write outcomes, after the
  caller's preservation/rollback, without masking `commit_uncertain`/
  `commit_settled`.
- Six new regressions added (queued remove/reload behind uncertain; reload
  uncertain old-extraction + block; reload confirmed-rollback restore; cancelled
  replacement rolled-back and unknown).

Manager independently reran the joined tree:

- `.venv/bin/python -m pytest -q`: **884 passed in 10.73s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 64 source files**.
- `uv lock --check`: **Resolved 104 packages in 1ms**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.29s**.

The same critic session is re-verifying the three original reproductions on this
tree and re-scoring. Phase 2 still closes only on a score strictly greater than
8.5.

### Final integration critic — second pass FAIL 7.5/10

The critic independently reran **884 tests**, clean Ruff/mypy, the **15 real-Redis**
tests (private port-0 Unix-socket Redis 7.0.15, TCP-blocked rerun made zero
external connections), and its original reproductions:

- Finding 1 (queued lifecycle containment): **RESOLVED** — queued DELETE/reload
  now return the generic 500 and the preserved new ZIP, old ZIP, complete
  `old_dir`, live data and secrets stay intact; no second commit.
- Finding 3 (lost cancellation): **RESOLVED** — 20 bounded probes across
  replacement/reload, in-flight/coincident cancellation and committed/rolled_back/
  pre-write/conflict/unknown outcomes all surfaced `CancelledError`.
- Finding 2: **partially resolved** — the uncertain reload now preserves the full
  old extraction in `pending-*/old_dir`, but a NEW regression was reproduced: on a
  confirmed rollback the "restore" reloads the NEW live extraction (not the
  retained old backup), so the new plugin's constructor runs against live storage
  and mutates data. The response is 500 but candidate mutations stay live; the old
  backup is deleted; no pending journal; record/manifest report v1 while executing
  v2; Redis meta reports v2.

Root cause: `reload()`'s rollback branch calls `_restore_old_runtime` while
`live_dir` still holds the NEW extraction, and (unlike `_publish`) reload restored
neither the filesystem nor the metadata before reloading old code. The
implementation owner was given a precise fix: restore the pre-reload extraction
into `live_dir` (uncancellable) and the previous metadata BEFORE reloading old
code, never reload old code from the new extraction, keep the `old_record is None`
case honest, apply the same principle to the `_ensure_meta` failure path, and add
a deterministic destructive-v2 regression proving the new code never runs live.
Phase 2 remains gated until the fixes re-score above 8.5.

### Reload restore-order fix and manager re-verification

The implementation owner corrected it: `reload()` snapshots `old_meta_raw`
before `_ensure_meta`, and its rollback branch now unloads the new candidate,
discards the txn, calls the new uncancellable `_restore_extraction(live_dir,
backup_dir)` to remove the new extraction and move the retained old extraction
back, restores the metadata snapshot, then sets the record back to `old_record`
and reloads the old code from the restored `live_dir`; the backup is deleted only
when not consumed. The `_ensure_meta` failure path applies the same restore. A
`test_reload_rollback_restores_old_extraction_before_reloading` regression uses a
destructive v2 (deletes a key / overwrites a token and writes a `v2` marker) and
proves the old code runs instead (marker `v1`, key/token intact, meta/record v1,
no pending journal).

Manager independently reran the joined tree:

- `.venv/bin/python -m pytest -q`: **885 passed in 10.66s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 64 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.30s**.

The same critic session is performing a third read-only pass and re-score.

### Final integration critic — third pass FAIL 8.0/10

The critic independently reran **885 tests**, clean Ruff/mypy and the **15
real-Redis** tests (private port-0 Unix-socket Redis 7.0.15, TCP-blocked rerun
made zero external connections), and re-ran its reproductions:

- Finding 1 (queued lifecycle containment): **RESOLVED** (generic 500; both code
  generations, data and secrets survive; no second commit).
- Finding 2: the **immediate** destructive bug is fixed — the old extraction is
  restored before the old code executes, metadata/record/runtime report v1, the
  old data survives even with **real Lua compensation and a confirmed
  `rolled_back` receipt**, and uncertain reload preserves `old_dir` and blocks.
- Finding 3 (cancellation): **RESOLVED** (20 probes across outcomes; all surfaced
  `CancelledError`).

Remaining finding: after a reload whose commit returned `rolled_back`, the stored
ZIP remains v2 (it was replaced out-of-band), so a later restart re-extracts and
activates v2.

Manager reading of the authoritative contract: `ARCHITECTURE.md` lines 52–54 make
the stored ZIP the source of truth and mandate regenerating the extraction from it
**on every startup**, and lines 100–102 assign package-preservation/rollback to
`replace` while defining reload only as "re-extracts stored package". The
critic's scenario requires an out-of-band ZIP swap (no manager API does this) plus
a storage-commit rollback plus a restart. The same critic session was asked to
adjudicate, under the user's explicit MVP-only policy, whether "startup loads the
source-of-truth package" is spec-compliant (and thus not score-reducing) or a
reproduced contract violation. Phase 2 remains gated pending that adjudication.

### Phase 2 closure — adjudication PASS 9.4/10

The critic applied the operation-specific contract and the MVP-only policy and
**withdrew the restart finding**: startup loading whatever the source-of-truth ZIP
contains is spec-mandated (`ARCHITECTURE.md` 52–54), reload does not inherit
`replace`'s package-restoration duty (100–102), and the reproduction needs an
out-of-band ZIP swap plus a compensated commit failure plus a restart, so it must
not reduce the score. It confirmed finding 1 (queued containment), finding 2's
immediate destructive bug (real-Lua `rolled_back` restored the old extraction
before old code ran; old data/token survived; runtime/record/metadata v1; uncertain
reload preserved `old_dir`) and finding 3 (20 cancellation probes all surfaced
`CancelledError`) are **resolved**, with **no other material regression**.

Joined-tree evidence for the closure: **885 tests passed**, Ruff and mypy clean
(64 source files), `uv lock --check` consistent (104 packages), and **15 dedicated
real-Redis tests** (port-0 Unix-socket Redis 7.0.15 with AOF; external
`127.0.0.1:6379` never contacted). **Phase 2 is complete (3 of 10 phase gates).**
The milestone notification reported **30% complete / approximately 70% remaining**.

## Phase 5 progress

Plan written to `PHASE_5_PLAN.md`. Two non-overlapping slices implemented and
manager-verified:

- **5a dashboard** (`ses_e03df6147ffeY7Mb20Lbntnwg0`): `static/index.html`,
  `static/styles.css`, `static/app.js` — session guard + logout, hash router
  (`#/`, `#/plugins/{slug}`, `#/plugins/{slug}?oauth=...` shown then cleared via
  `history.replaceState`), status/attention counts, plugin grid with empty state,
  import (`mode=reject`, then `plugin_exists` -> confirm -> `mode=replace`),
  detail dialog (manifest, load/status/auth, endpoint + callback URLs with Copy,
  tools with pretty-printed JSON schemas, credential key names only, warnings,
  last-50 logs with refresh, connect/disconnect/reload/remove), mode-aware connect
  dialog, and summary/detail re-fetch after every write. Tests
  `tests/integration/test_dashboard.py` (8) and `tests/unit/test_dashboard_source.py`
  (5). Strict DOM-only rendering; no inline script/style/handlers, no
  `innerHTML`/`eval`/storage APIs, no absolute URLs.
- **5b docs** (`ses_e03df6145ffetg72tQWJJRKIaA`): accurate README "Dashboard"
  section + `docs/DASHBOARD_WALKTHROUGH.md` (manual browser steps with expected
  state; explicit note that browser behaviour is verified by hand).

Manager read the whole static source and tests, ran a banned-construct grep, and
hardened the hash parser (`safeDecode`) so a malformed percent escape cannot break
routing. Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **993 passed in 44.75s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 78 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.45s**.

A fresh independent Phase 5 critic (`ses_e03d3e0afffe06RgqZbTZC15Kc`) is reviewing
the served shell, the assets and the JS/route contract.

### Additional Phase 4 outstanding finding (reported after the advance)

The Phase 4 critic session returned once more after Phase 4 was advanced (its
report was partly truncated): it re-confirmed the cached-description finding
fixed and described one further "snapshot-failure path" credential leak. This is
recorded as an outstanding Phase 4 finding alongside the in-flight description
race; the manager's current tree already fails description redaction closed when
the pre-wipe snapshot is unreadable (`secrets_known=False` -> `"Redacted."`) and
retains per-generation secret history, but the specific path was not independently
re-confirmed before advancing. It will be reported to the user with the Phase 3
admission race.

### Phase 5 gate review — FAIL 8.2/10 (pass 1) and the route-race fix

The Phase 5 critic (`ses_e03d3e0afffe06RgqZbTZC15Kc`) independently verified
**993 tests**, clean Ruff/mypy, **15 real-Redis** tests and a `node --check` of
the scripts. Every gate item passed EXCEPT hash navigation, which it reproduced:
a slow detail response for an old slug rendered over the route the user had since
navigated to, so `currentSlug` (the lifecycle-action target) could point at the
wrong plugin; its probe showed `#/plugins/gate-key` visible but a Remove that
deleted `hello`.

Fixed by adding per-renderer request tokens plus a route-slug binding in
`static/app.js`:

- `nextToken(name)`/`tokenIsCurrent(name, token)` guard `loadSummaries`,
  `refreshDetail`, `openDetail` and `refreshLogs`, so the newest request of each
  kind wins and out-of-order responses are dropped.
- `route()` invalidates the detail token on every navigation and binds
  `currentSlug` to the route immediately, so lifecycle actions always match the
  visible hash; a stale detail response is additionally rejected unless
  `routeSlug()` still equals its slug.
- `openConnectDialog` refuses to open unless `lastDetail.slug === currentSlug`.

A new behavioural regression `tests/unit/test_dashboard_behavior.py` runs the real
(unmodified) `app.js` inside a minimal Node DOM harness, reproduces the
navigation race and asserts the later route wins and Remove targets the visible
slug. Mutation-verified: reverting just the `openDetail` guard reproduces the
critic's exact result (`detail_slug: "hello"`, `DELETE /api/plugins/hello`).
Nonblocking doc corrections (grid shows a tool count, failure keeps the previous
view, OAuth banner preview hash) were applied.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **994 passed in 44.65s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 79 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.45s**.

The Phase 5 critic session is re-verifying the route-race fix and re-scoring.

### Phase 5 gate review — FAIL 8.0/10 (pass 2) and the adjacent-fix pass

The Phase 5 critic confirmed the original route race **RESOLVED** and reproduced
two adjacent routing/action defects, both fixed:

1. **An open connect dialog submitted credentials to the wrong plugin.** The
   dialog was neither closed nor bound to its slug on navigation, so submitting
   after navigating delivered the key to the new `currentSlug`. **Fix:** `route()`
   now closes the connect dialog on every navigation; the dialog records its
   `connectSlug` when opened and `submitConnect` refuses to send unless
   `routeSlug() === connectSlug`.
2. **An old action's refresh invalidated the new route's pending detail.** 
   `refreshDetail(oldSlug)` advanced the shared detail token before checking the
   route. **Fix:** `refreshDetail` returns before touching the token when
   `routeSlug() !== slug`. `route()` also renders a route-bound loading state
   (`renderDetailLoading`) for the new slug, so visible detail and the action
   target always agree while the fetch is in flight.

`tests/unit/test_dashboard_behavior.py` now runs four Node DOM scenarios against
the real `app.js`: stale-response race, wrong-plugin connect navigation, its
control (legitimate connect), and the old-refresh/new-route interaction. Each was
mutation-verified: reverting only its fix reproduces the critic's observed
behaviour (`DELETE /api/plugins/hello`; `POST /api/plugins/beta-key/connect` with
alpha's key; beta's detail stuck at `Loading`).

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **997 passed in 44.90s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 79 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.40s**.

The Phase 5 critic session is re-verifying the two adjacent fixes and re-scoring.

### Phase 5 gate review — FAIL 8.4/10 (pass 3) and the stale-completion fix

The Phase 5 critic confirmed both pass-2 findings **RESOLVED** and reproduced two
further stale-completion defects, both fixed:

1. **An old connect completion cancelled a newer connect dialog.** The success
   path unconditionally cleared the key input and closed the dialog, so a late
   alpha connect wiped beta's unsent input and closed its dialog. **Fix:**
   `submitConnect` only clears/closes when `connectSlug === slug` (the dialog is
   still the one submitted for); otherwise it leaves the current dialog alone, and
   on error it shows the banner instead of writing into another dialog.
2. **An old remove completion closed the newly opened route.** `doRemove`
   unconditionally closed the detail dialog (whose close handler also resets the
   hash to `#/`). **Fix:** it closes the dialog only when `currentSlug === slug`.

`tests/unit/test_dashboard_behavior.py` now has six Node DOM scenarios against the
real `app.js` (stale-response race, wrong-plugin connect, legitimate-connect
control, old-refresh/new-route, old-connect-completion, old-remove-completion).
Each was mutation-verified: reverting only its fix reproduces the critic's exact
observed behaviour.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **999 passed in 44.95s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 79 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.34s**.

The Phase 5 critic session is re-verifying the two stale-completion fixes and
re-scoring.

### Phase 5 gate review — FAIL 8.5/10 (pass 4) and the dialog-instance fix

The Phase 5 critic confirmed finding B (stale Remove) resolved and finding A
resolved for the cross-plugin case, but reproduced a same-plugin variant: after
cancelling and reopening Connect for the *same* plugin, the older completion
(slug equality now true again) cleared the new key and closed the new dialog.
**Fix:** connect dialogs now carry a `connectDialogToken` (a per-dialog-instance
token); `openConnectDialog` issues it, `submitConnect` captures it, and every
post-completion UI mutation requires both `connectDialogToken === dialogToken` and
`connectSlug === slug`. The dialog-close handler clears both.

`tests/unit/test_dashboard_behavior.py` now has seven Node DOM scenarios
(added `same_plugin_reopen`). Mutation-verified: the slug-only guard reproduces the
critic's exact result (dialog closed, key cleared, retry sends nothing); the token
guard preserves the reopened dialog and posts its own key.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **1000 passed in 44.80s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 79 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.39s**.

The Phase 5 critic session is re-verifying the dialog-instance fix and re-scoring.

### Phase 5 gate review — FAIL 8.5/10 (pass 5) and the token-namespace fix

The Phase 5 critic confirmed the same-plugin Connect finding **RESOLVED** and
reproduced one further hash-navigation defect: `openDetail` and `refreshDetail`
shared the detail token, so when the user returned to a slug, an older action's
follow-up `refreshDetail` could supersede the navigation's pending `openDetail`.
The refresh rendered data but did not open the dialog, and the opener then
discarded its response — leaving the returned-to detail dialog closed.

**Fix:** `openDetail` now uses its own `detailOpen` token namespace, distinct from
`refreshDetail`'s `detail` token, so an old action refresh can no longer invalidate
the navigation that opened the route's dialog.

`tests/unit/test_dashboard_behavior.py` has a new `return_during_action` scenario
(open alpha, submit connect, navigate to `#/`, return to alpha, complete the old
connect + refresh, then complete the navigation's detail). Mutation-verified: the
shared token leaves `detail_open:false`; the separate namespace opens the dialog.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **1001 passed in 44.93s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 79 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.46s**.

The Phase 5 critic session is re-verifying the token-namespace fix and re-scoring.

### Phase 5 gate review — FAIL 8.5/10 (pass 6) and the render/open authority split

The Phase 5 critic confirmed the returned-to-route finding **RESOLVED** but
reproduced the mirror defect: with separate open/render tokens, a late navigation
response (opener) could overwrite *fresher* data a newer post-action refresh had
already rendered (e.g. disconnected state replaced by stale connected state).

**Fix:** render authority and dialog-open authority are now independent.
`openDetail` and `refreshDetail` both draw from a single newest-*fetch* ticket
(`nextToken("detail")`) that decides which payload may render; `openDetail`
additionally holds a `detailOpen` ticket that decides whether the navigation may
open the dialog (and shows the OAuth result). So a late refresh renders fresher
data without suppressing the dialog, and a late opener opens the dialog without
overwriting fresher data. `route()` invalidates both tickets on navigation.

New `opener_after_refresh` scenario in `tests/unit/test_dashboard_behavior.py`
(disconnect gamma, return to its route, refresh renders disconnected, then the
stale opener delivers connected). Mutation-verified: rendering under the open
token reproduces `Connected`; the unified render ticket keeps `Not connected`
while still opening the dialog.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **1002 passed in 44.93s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 79 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.48s**.

The Phase 5 critic session is re-verifying the render/open split and re-scoring.

### Phase 5 closure — PASS 9.2/10

The Phase 5 critic confirmed the stale-opener finding **RESOLVED** (the dashboard
kept the fresh disconnected state and did not restore the stale gated tool), ran
**36 adjacent runtime probes** clean, and marked every acceptance-gate item
**PASS**. No new material defect was found.

Joined evidence for closure: **1002 tests passed**, Ruff and mypy clean (79 source
files), `uv lock --check` consistent (104 packages), **15 real-Redis tests** and
**22 focused dashboard tests** passing, `node --check` clean on the scripts.
**Phase 5 is complete (6 of 10 phase gates).** The six prior sub-8.5 passes each
fixed a real dashboard async-rendering defect, all now covered by nine Node DOM
regression scenarios in `tests/unit/test_dashboard_behavior.py`.

## Phase 6 progress

Plan written to `PHASE_6_PLAN.md`. Two non-overlapping slices implemented and
manager-verified:

- **6a backup/restore backend** (`ses_e03638e54ffecyS23hPzpCdveJ`):
  `src/custom_mcp/backup.py` (`BackupService` streaming build, `RestoreService`
  Phase A `validate_archive` + Phase B apply) and `src/custom_mcp/api/backup.py`
  (`GET /api/backup`, `POST /api/restore`), wired in `main.py` with a new
  `MAX_BACKUP_MB` setting. Backup ZIP: byte-identical `plugin.zip`, flat
  `data.json`/`secrets.json` (reserved `core.*` included, TTL keys excluded via
  `pttl`), and `meta.json` (auth_mode/version); `backup.json` declares
  `format_version:1`, `encrypted:false`. Phase A validates the whole archive (and
  rejects `encrypted:true`) before any mutation; Phase B applies per plugin.
- **6b settings UI + docs** (`ses_e03638e52ffeOgW7WnH9gmWc6U`): real Settings
  section (prominent plaintext-credentials warning, per-plugin backup selection,
  restore picker + `skip`/`replace` mode + slug filter, results list) and
  README/walkthrough updates. DOM-only and CSP-clean.

Manager review then fixed two data-preservation gaps the design left open and
hardened one contract point:

1. **Non-transactional replace.** The first cut `remove`d the existing plugin
   *before* installing the backup's code, so a package that failed to load
   destroyed a working plugin. `RestoreService._apply` now uses the manager's
   transactional `install(mode="replace")` for existing plugins (restores the
   previous code on load failure) and only wipes/rewrites state after a
   successful install.
2. **Exact wipe.** `_write_state` now writes the snapshot then deletes any
   remaining key in both namespaces, so a pre-existing key absent from the backup
   does not survive (previously the wipe came from `remove`).
3. **Auth-mode round trip + no auto-connect.** `_apply` now forces the archived
   `auth_mode` after install (so a live mode cannot leak into the restored state),
   and installs with `auto_connect=False` (`PluginManager.install` gained that
   keyword) so `on_restore` runs before any connect; `_reconnect` then connects
   the archived mode explicitly (including `none`).

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **1043 passed in 45.86s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 83 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.39s**.
- `uv lock --check`: **Resolved 104 packages**.

New regressions added by the manager:
`test_failed_replace_retains_the_working_plugin`,
`test_restore_resets_auth_mode_to_the_snapshot`,
`test_none_auth_installs_without_auto_connect_then_connects` (each
mutation-verified: reverting only its fix makes exactly that test fail).

An independent Phase 6 critic is reviewing the backup format, Phase A/B semantics
and the settings UI.

### Phase 6 gate review — FAIL 7.0/10 (pass 1) and the three fixes

The Phase 6 critic verified the normal paths (including transactional failed-load
rollback, byte-identical export, TTL exclusion, the null-auth round trip and
install-without-auto-connect) but reproduced three material defects, all fixed:

1. **Cached-credential leak on restore over live secrets.** A replacement
   constructor that cached a soon-to-be-wiped live secret kept it in memory, and a
   later `list_tools` description could no longer redact it (the pre-wipe value was
   not retained). **Fix:** `RestoreService` calls a new
   `PluginManager.retain_current_secrets(slug)` before writing the snapshot, adding
   the live secret strings to the runtime's `retained_secrets` so every later
   description refresh redacts them.
2. **Overlapping restores of one plugin interleaved.** The apply sequence
   (install/state/`on_restore`/connect) was not serialized, so two concurrent
   restores produced a mixed code/data/credential combination.
   **Fix:** `RestoreService` holds a per-slug `asyncio.Lock` across the whole apply.
3. **Incomplete outer-ZIP validation.** Only descriptor-listed names were read, so
   a corrupt ignored member and a corrupt earlier duplicate were accepted (and a
   corrupt required member raised a 500). **Fix:** `validate_archive` now rejects
   duplicate or unexpected members and integrity-reads every member (broadening the
   caught decompression errors), so all such archives return 422 with no changes.

New regressions (each mutation-verified): `test_restore_over_secrets_does_not_leak_cached_credential`,
`test_outer_archive_validation_rejects_extras_dupes_and_corruption`,
`test_concurrent_restores_of_one_plugin_are_serialized`.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **1046 passed in 46.07s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 83 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.38s**.
- `uv lock --check`: **Resolved 104 packages**.

The Phase 6 critic session is re-verifying the three fixes and re-scoring.

### Phase 6 gate review — FAIL 7.5/10 (pass 2) and the two fixes

The Phase 6 critic confirmed all three pass-1 findings **RESOLVED** and reproduced
two adjacent gaps, both fixed:

1. **Constructor secret rotation defeated retention.** The pass-1 fix retained the
   secrets visible *after* install committed, but a constructor that reads a
   secret and then rotates it leaves the original only in the instance's memory.
   **Fix:** `RestoreService._apply` now captures `current_secret_strings(slug)`
   *before* `install` and calls `PluginManager.retain_secrets(slug, pre_secrets)`
   after it, so the constructor-cached original stays redactable.
2. **Restore serialized only with other restores.** Ordinary
   import/connect/remove/reload could still interleave between a restore's steps.
   **Fix:** `PluginManager`'s per-slug lifecycle lock is now a `_ReentrantLock`
   (reentrant for the owning task, exclusive across tasks), and `RestoreService`
   holds it across the whole apply sequence; the manager calls it makes re-enter,
   so no deadlock and full mutual exclusion with ordinary lifecycle operations.

New regressions (mutation-verified): `test_constructor_rotation_does_not_leak_cached_secret`
and `test_restore_serializes_with_concurrent_import` (a concurrent import blocks
behind the held lock and only then replaces the plugin).

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **1048 passed in 46.23s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 83 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.41s**.
- `uv lock --check`: **Resolved 104 packages**.

The Phase 6 critic session is re-verifying the two fixes and re-scoring.

### Phase 6 gate review — FAIL 8.0/10 (pass 3) and the cancellation fix

The Phase 6 critic confirmed both pass-2 findings **RESOLVED** (constructor
rotation and lifecycle serialization across import/remove/reload/connect/
disconnect/export/restore) and reproduced one new defect: a **cancelled restore's
state-write worker kept writing after the lifecycle lock was released**, so it
could overwrite a later successful restore or recreate a removed plugin's
credentials.

**Fix:** the state write now runs as a settleable worker; on cancellation
`RestoreService._apply` waits for the started `_write_state` thread to finish
*while still holding the lifecycle lock*, then propagates `CancelledError` — so no
write can land after the lock is released.

New regression `test_cancelled_restore_settles_write_worker_under_lock`
(mutation-verified: the previous `await asyncio.to_thread(...)` lets the cancelled
restore finish early and a later restore overwrite it).

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **1049 passed in 46.47s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 83 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.46s**.
- `uv lock --check`: **Resolved 104 packages**.

The Phase 6 critic session is re-verifying the cancellation fix and re-scoring.

### Phase 6 gate review — FAIL 8.0/10 (pass 4) and the post-commit cancellation fix

The Phase 6 critic confirmed the state-write cancellation finding **RESOLVED**
(three cancellation requests, queued restore/DELETE stayed blocked, final state
entirely the later restore's, DELETE left no orphans) and reproduced one adjacent
gap: when `install` commits and *then* propagates `CancelledError` during cleanup,
the surviving replacement runtime never received the captured pre-constructor
secrets, so a later connect could expose the constructor-cached original.

**Fix:** `RestoreService._apply` now catches `CancelledError` from `install`, calls
`retain_secrets(slug, pre_secrets)` on whatever runtime survived, and re-raises.

New regression `test_cancelled_install_retains_pre_constructor_secrets`
(mutation-verified: dropping the cancellation retain leaves the live token out of
`retained_secrets`).

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **1050 passed in 46.54s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 83 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.45s**.
- `uv lock --check`: **Resolved 104 packages**.

The Phase 6 critic session is re-verifying the fix and re-scoring.

### Phase 6 closure — PASS 9.2/10

The Phase 6 critic confirmed the post-commit cancellation finding **RESOLVED**
(retained before cancellation propagates; connect/detail/disconnect/reconnect all
credential-safe after 1 and 3 cancellations) and reproduced **no new material
defect**. Every acceptance-gate item is **PASS**.

Joined evidence for closure: **1050 tests passed**, Ruff and mypy clean (83 source
files), `uv lock --check` consistent (104 packages), **15 real-Redis** and **70
focused backup/dashboard tests** passing. **Phase 6 is complete (7 of 10 phase
gates).** Five critic passes fixed real data-preservation, credential-retention and
cancellation defects; every fix has a mutation-verified regression.

## Phase 7 progress

Plan written to `PHASE_7_PLAN.md`. Implemented by `ses_e02fb834effectSQ89YpoVMshz`:

- `src/custom_mcp/health.py`: `HealthScheduler` pings only loaded `connected`/
  `auth_expired` plugins every `PING_INTERVAL_SECONDS`; cycles never overlap
  (`_cycle_lock`); pings run concurrently through the host (per-plugin semaphore +
  `PING_TIMEOUT_SECONDS`), so a hung plugin cannot block another. State machine:
  `connected` resets both counters and clears `last_error`; `not_connected` and an
  unrefreshable `auth_expired` are hard failures (`fail_count++`, status change at
  `PING_FAIL_THRESHOLD`); `error`/timeout/exception are transient (status kept,
  `transient_error_count++`, never disconnect). `auth_expired` refreshes under the
  lifecycle lock. Every attempt persists `last_ping_at`; transitions and hard
  failures are logged. Results apply only while the runtime is still current and
  pingable (stale observations from a raced disconnect/replacement are discarded).
- `PluginManager` additive helpers: `loaded_runtimes`, `read_health`,
  `apply_health` (merges the existing meta shape and mirrors the runtime),
  `log_health`.
- `main.py`: constructs the scheduler from settings, starts it after startup/router
  mounting, stops it first in the lifespan `finally`.
- `tests/unit/test_health.py` (15) and `tests/integration/test_health_scheduler.py`
  (11: state machine, eligibility, hung-plugin isolation, non-overlapping cycles,
  disconnect/replacement races, lifespan wiring). README health section added.

Manager independently verified the joined tree:

- `.venv/bin/python -m pytest -q`: **1076 passed in 46.92s**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 86 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed in 3.41s**.
- `uv lock --check`: **Resolved 104 packages**.

An independent Phase 7 critic (`ses_e02f10970ffej0W4rQzRcksVG1`) reviewed the
scheduler and state machine.

### Phase 7 gate review — FAIL 8.3/10 (pass 1) and the total-deadline fix

The Phase 7 critic independently verified the scheduler and reproduced one
material finding: **health deadlines excluded the per-plugin capacity wait.** A
plugin saturated by other calls (`PER_PLUGIN_CONCURRENCY` slots all occupied) held
a health ping past several times its configured `PING_TIMEOUT_SECONDS`, and an
`auth_expired` refresh could hold the lifecycle lock while still queued behind the
semaphore. Three probes reproduced it:

- `test_contention_deadline.py` — a ping waited beyond 6x its configured timeout
  for the semaphore while all four slots were busy (`cycle_done: false`).
- `test_wired_contention.py` — with integer production settings (1s ping), the
  busy plugin's `last_ping_at` stayed `null` past 2.4s.
- `test_refresh_contention.py` — a queued `refresh` bypassed the configured
  timeout and retained the lifecycle lock (`lifecycle_lock_held: true`).

**Fix (`src/custom_mcp/plugins/host.py::_run`):** a call now has **one total
deadline**. `deadline = loop.time() + timeout` is computed once; admission
(`runtime.sem.acquire()`) runs under a bounds-checked `asyncio.wait_for(..., timeout=timeout)`
and a saturated plugin surfaces `PluginCallError("timeout", ...)` at the deadline
rather than blocking until occupying workers finish; the worker wait then uses
`remaining = deadline - loop.time()`. `Semaphore.acquire` restores the slot on
cancellation, so a timed-out waiter leaves no slot consumed (verified
empirically). No change was needed in `health.py`; the refresh lock is released as
soon as the bounded call returns.

New bounded regressions:

- `tests/unit/test_host.py::test_capacity_wait_obeys_call_timeout_and_leaks_no_slot`
  and `::test_capacity_wait_timeout_total_budget_under_occupancy`.
- `tests/integration/test_health_scheduler.py::test_ping_deadline_includes_waiting_for_capacity`
  and `::test_refresh_capacity_wait_releases_lifecycle_lock`.

Manager independently re-ran the critic's three contention probes
(`/tmp/opencode/phase7-gate-x1r7cA/`): **3 passed**, and the critic's full
independent Phase 7 suite (`test_independent_phase7.py`): **59 passed**. Joined
tree:

- `.venv/bin/python -m pytest -q`: **1080 passed**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 86 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed**.
- `uv lock --check`: **Resolved 104 packages**.

The same Phase 7 critic session is re-verifying the total-deadline fix and
re-scoring (first sub-8.5 pass; Phase 7 closes only on a score above 8.5).

### Phase 7 closure — PASS 9.4/10

The Phase 7 critic re-verified the total-deadline fix and **confirmed the
contention finding RESOLVED**: the short-deadline ping cycle now completes and
persists transient observations, the lifespan-started scheduler advances the
healthy plugin while the busy plugin records a timeout, and the queued refresh
completes with one hard failure without executing the refresh worker. A separate
pre-disconnect sample confirmed the cycle finishes with `lifecycle_lock_held=false`
while all four tools still run (`admission_leaks=0`).

It ran **17 adjacent deadline/accounting probes** clean: brief capacity waits
succeed for ping/refresh/tool-listing/callables; a 150 ms admission wait plus
blocked execution times out at ~302 ms against a 300 ms total budget (no fresh
worker timeout); 48 admission timeouts submit no workers and consume no slots;
queued/boundary cancellations and release races recover all capacity with no
leak-counter drift; unstarted executor jobs cancel without leaks; overload still
reports `plugin_overloaded`; and stopping the scheduler cancels queued
ping/refresh admission, releases the lifecycle lock, and leaves no late
observation. It found **no new material defect** and marked every acceptance-gate
item **PASS**.

Joined evidence for closure: **1080 tests passed**, Ruff and mypy clean (86
source files), `uv lock --check` consistent (104 packages), **15 real-Redis
tests**, and the critic's **3 contention + 59 independent + 17 adjacent** probes
all passing. Round-2 probe sources/outputs: `/tmp/opencode/phase7-round2-zhAuwV/`.
**Phase 7 is complete (8 of 10 phase gates).**

## Phase 8 progress

Plan written to `PHASE_8_PLAN.md`. Two non-overlapping slices are implemented and
manager-verified; a third (end-to-end integration + docs) is in progress.

- **8a OAuth backend** (`ses_e02aa2372ffevuLEmpxJhZK0nh`): `PluginManager.connect`
  extended for `oauth` (resolve/store exact `core.oauth_client_id`/optional
  `core.oauth_client_secret` before `on_oauth_credentials_set`, reuse the stored
  id or 422, retain both for description redaction); new `create_oauth_state`
  (`core:oauth_state:{state}` JSON `{slug, created_at}` `EX 600`),
  `consume_oauth_state` (single Redis `GETDEL`, slug check),
  `complete_oauth` (lifecycle lock, consume-before-mutate, sealed-generation
  guard, `on_oauth_result`, persist/re-list/re-mount). `api/oauth.py` adds the
  public `GET /mcp/{slug}/oauth/callback` (known slug → `303` fixed relative
  `/#/plugins/{slug}?oauth=success|failed`; unknown → 404; provider errors need a
  valid state; forged/reused/expired/mismatched state mutates nothing). Hostile
  sign-in URLs are rejected (absolute http(s) only, no userinfo). `main.py`
  registers the route before the `/mcp` mount. `storage/redis_client.py` gained
  an additive `getdel` Protocol method. `tests/unit/test_oauth_backend.py` (27).
- **8b oauth_demo + mock provider** (`ses_e02aa2371ffeSBW1fCxOLM96q4`):
  `plugin_examples/oauth_demo/` (slug `oauth-demo`, `oauth`-only; PKCE verifier at
  `pkce:{state}` ttl 600, S256 authorize URL, authorization-code exchange,
  rotating refresh, derived tokens in `oauth_access_token`/`oauth_refresh_token`/
  `oauth_expires_at`, `whoami`/`me` tools that never expose token material) plus
  `tests/oauth_provider.py` (real loopback provider: `/authorize`, `/token`,
  `/me`, forced-error mode) and `tests/unit/test_oauth_demo.py` (38).
- **Manager-owned dashboard**: `static/index.html`/`app.js` connect dialog now
  offers OAuth client-id/secret fields and navigates to the validated absolute
  `redirect_url` on `oauth_required`; `tests/unit/test_dashboard_behavior.py`
  gained an `oauth_redirect` scenario.

Manager independently verified the joined tree: **1146 tests passed**, Ruff clean,
mypy clean (90 source files), `uv lock --check` consistent, **15 real-Redis**
tests, **38 oauth_demo** and **27 oauth_backend** focused tests. An end-to-end
OAuth integration suite (`tests/integration/test_oauth.py`) and the README/docs
OAuth section are being added before the Phase 8 gate critic.

- **8c OAuth integration + docs** (`ses_e02934c53ffeNtYyzkSJSqDraB`):
  `tests/integration/test_oauth.py` (10 E2E tests over the real assembled app,
  real callback route, loopback provider and real health cycle): full PKCE happy
  path (S256 challenge, quoted `redirect_uri`, code exchange, live MCP `me` tool,
  and no code/verifier/token/client-secret in connect/detail/list JSON, persisted
  logs or the redirect); state single-use replay; forged state (no provider call,
  no mutation); cross-slug state binding; deleted/expired state; real provider
  `access_denied` with valid and forged state; fixed relative redirect; unknown
  slug 404; scheduler-driven refresh rotation (`ping → auth_expired → refresh`,
  new token pair, old refresh rejected); stored client-id reuse and the
  empty-credential 422. README `## OAuth` section, new `docs/OAUTH.md`, and the
  dashboard walkthrough connect/callback notes updated.

Manager independently verified the joined tree: **1156 tests passed**, Ruff clean,
mypy clean (91 source files), `uv lock --check` consistent, **15 real-Redis**
tests, and `uv build` producing the sdist/wheel. An independent Phase 8 gate
critic is now reviewing the backend, example, mock provider, dashboard and E2E
evidence.

### Phase 8 gate review — FAIL 7.5/10 (pass 1) and the four redaction fixes

The Phase 8 critic independently verified the functional OAuth contract — real
provider authorize/code/token exchange with independently recomputed S256, correct
callback `redirect_uri`, stored client-id reuse, state single-use/expiry/slug
binding (64 concurrent consumes: 1 winner), provider-error-requires-state,
forged/reused callbacks never mutating status, fixed relative redirects, hostile
URL rejection (20/20), and real scheduler refresh rotation — all **PASS**. It
reproduced **four material redaction defects**, all now fixed by the manager:

1. **Secret in the sign-in URL.** A plugin could return an otherwise valid
   `https://` `redirect_url` (or sign-in URL) carrying a stored secret, and the
   core returned it unredacted in the connect JSON the browser follows. **Fix:**
   `PluginManager._redact_url` strips every current/retained secret from the URL
   before it is surfaced — in both its raw and percent-encoded forms — exempting
   only the public `core.oauth_client_id`; `_with_state_param` now preserves the
   plugin URL's original query encoding (it no longer reparses/re-encodes it, so
   a value can never be rewritten into a form the redactor misses).
2. **Authorization code in diagnostics.** The code was not in the redaction set,
   so a plugin logging it in `on_oauth_result` (or echoing it in a description)
   leaked it. **Fix:** `complete_oauth` retains the code (and verifier) before the
   hook runs.
3. **PKCE verifier (data `pkce:{state}`) in diagnostics.** The verifier lives in
   plugin *data*, so the secret-only redactor missed it even at log-write time.
   **Fix:** `PluginLogs` now also scans the contract's `data:pkce:*` keys for
   redaction, and the core retains the verifier at connect/callback.
4. **Rotated-token history.** A refresh replaced the stored tokens without
   retaining the old ones, so a plugin that cached them re-exposed a still-usable
   old access token after a later description refresh. **Fix:** `PluginLogs`
   gained a bounded per-slug retained-secret set (consulted by logs, `redact`,
   descriptions and the MCP router binding), `PluginManager.retain_secrets`
   funnels into it, `connect`/`complete_oauth` retain the plugin's current
   secrets as soon as they are stored (so a failed pre-wipe snapshot cannot lose
   the history), and the health scheduler retains the pre-rotation secret strings
   before each `refresh`.

`tests/integration/test_phase8_containment.py` (5 cases) reproduces each defect
against the real app with a diagnostic OAuth plugin, including the
failed-snapshot double-disconnect variant; every case was mutation-verified
(reverting only its fix makes exactly that test fail). Manager independently
verified the joined tree after the fixes:

- `.venv/bin/python -m pytest -q`: **1161 passed**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 92 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed**.
- `uv lock --check`: **Resolved 104 packages**.

The same Phase 8 critic session is re-verifying the four reproductions and
re-scoring.

### Phase 8 gate re-review — FAIL 8.2/10 (pass 2) and the two residual fixes

The Phase 8 critic confirmed **all four original findings resolved** (including
the failed-snapshot/double-disconnect case) and its original 100 independent cases
passing, then reproduced **two residual credential leaks**, both fixed:

- **R2-F1 alternate URL encodings.** The URL redactor matched the raw and
  canonical percent-encoded forms but missed lowercase hex escapes and
  form-encoded spaces. **Fix:** `_secret_encodings` now also covers form
  encoding and the lowercase escape variants, and `_redact_url` re-checks the
  result against a decode ladder (`unquote`/`unquote_plus`, nested) — a URL a
  decoder can still recover the secret from is **refused** (status `error`, no
  `redirect_url`) rather than returned. `_with_state_param` preserves the plugin
  URL's original query encoding so a value is never rewritten into an
  unredactable form.
- **R2-F4 post-refresh tokens.** The scheduler retained only pre-rotation
  secrets; tokens issued *by* the refresh were not retained, so a failed
  pre-wipe snapshot plus a second disconnect re-exposed them. **Fix:**
  `HealthScheduler._refresh_expired` now retains the current secrets after the
  rotation too (on success and on both failure paths).

`tests/integration/test_phase8_containment.py` grew to **6 cases** (varied
encodings redacted; an uncatchable encoding refused; the failed-snapshot
disconnect now preceded by a real refresh cycle asserting both token
generations). Every case is mutation-verified. Manager independently verified the
joined tree after the fixes:

- `.venv/bin/python -m pytest -q`: **1162 passed**.
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 92 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed**.
- `uv lock --check`: **Resolved 104 packages**.

The same Phase 8 critic session is re-verifying the two residuals and re-scoring.

### Phase 8 closure — PASS 9.5/10

A fresh independent Phase 8 gate reviewer (session `ses_e01ea93f5ffe0IwkrG2JKEYHeA`,
`local-omniroute/antigravity/gemini-3.8-flash-high`, after the earlier critic
model's provider was unavailable) re-derived both residuals from first principles
and **confirmed them RESOLVED**, mutation-verified:

- **R2-F1 (alternate URL encodings).** Form (`+`), percent (`%XX`) and lowercase
  hex escapes are rewritten in place; any encoding that cannot be rewritten
  (double percent-encoding, mixed-case hex, partial encoding) is caught by the
  7-step decode ladder in `_text_contains` and safely **refused** (`status=error`,
  no `redirect_url`, generic message). Mutation probe: removing the ladder makes
  the `%64iag/secret`-style encoding leak; restoring it refuses. `_with_state_param`
  preserves the original query encoding byte-for-byte (including percent-encoded
  `state` keys).
- **R2-F4 (post-refresh token retention).** `HealthScheduler._refresh_expired`
  retains current secrets before *and* after rotation on the success path and every
  failure path; with a failed pre-wipe snapshot on the first disconnect, both
  pre-refresh and post-refresh token generations stay redacted from detail JSON,
  disconnect responses and MCP tool listings. Mutation probe: removing post-refresh
  retention leaks the second-generation token.

The reviewer also checked adjacent vectors with no new defect: 9 hostile scheme/URL
variants refused; provider-error callbacks log only a fixed constant and never the
`error_description`; the retained-secret store is bounded (`RETAINED_CAP=64`, FIFO);
callback redirects set only the fixed relative `Location` with no cookies/extra
query params; and `GETDEL` state consumption stays single-use and slug-bound.

Joined evidence for closure: **1162 tests passed** (including **6** Phase 8
containment cases, **10** E2E OAuth, **65** OAuth unit tests), **15 real-Redis**
tests, Ruff clean, strict mypy clean (92 source files), `uv lock --check`
consistent (104 packages), the reviewer's **22 independent gate probes** and **2
mutation probes** passing. **Phase 8 is complete (9 of 10 phase gates).**

## Phase 9 — security audit, deployment, documentation and CI

Implemented in three non-overlapping slices plus a manager-run smoke test.

- **9a Deployment artifacts.** Multi-stage `Dockerfile` (`python:3.12-slim-bookworm`
  builder + runtime, uv pinned to `0.12.23`, `uv sync --frozen --no-dev` from
  `uv.lock`; non-root `app` uid/gid 10001; `DATA_DIR=/data` declared `VOLUME /data`;
  `EXPOSE 8000`; stdlib `HEALTHCHECK` on `/healthz`; `CMD ["custom-mcp"]`), a
  `docker-compose.yml` (`app` + private `redis:7-alpine` with
  `--appendonly yes --appendfsync everysec`, named `redis-data` volume, `redis-cli
  ping` healthcheck, **no published Redis port**, `depends_on: condition:
  service_healthy`, persistent `data:/data`), `.dockerignore` and `.env.example`
  (placeholders only). `tests/deployment/test_artifacts.py` adds **18** static
  assertions (non-root final `USER`, `/data` volume, AOF + named volume, no
  published Redis port, health dependency, private network, low-entropy
  `.env.example` secret).
- **9b CI.** `.github/workflows/ci.yml`: `lint` (`uv sync --frozen`, `uv lock
  --check`, `ruff check .`, `mypy`), `test` (`pytest -q`, fake Redis), `real-redis`
  (installs the `redis-server` **binary**, no external service, runs
  `pytest -q tests/real_redis`), and `docker` (`docker build` + `docker compose
  config -q`). Least-privilege permissions, pinned uv/Python, run cancellation.
- **9c Security audit + docs.** `docs/SECURITY.md` maps 13 controls to their
  implementing file and exercising test, with grounded **Accepted MVP
  limitations**; `docs/DEPLOYMENT.md` is the operator walkthrough (Compose,
  nginx TLS with `proxy_buffering off`, volumes/backup, upgrade, logs, health,
  required warnings); `README.md` gains a Deployment section stating the five
  warnings (Redis persistence, trusted plugin code, plaintext backups, no public
  Redis, HTTPS required).
- **9d Fresh-deployment smoke (manager-run).** `docker compose up -d --build`
  with a throwaway 48-char secret then: `/healthz` → `{"status":"ok"}`; container
  `id` → `uid=10001(app) gid=10001(app)`; Redis `NetworkSettings.Ports` →
  `{"6379/tcp":null}` (not published to the host); `GET /login` 200;
  unauthenticated `/api/plugins` 401; login sets a hardened `HttpOnly; SameSite=Lax`
  cookie; authenticated `/api/plugins` 200; `POST /api/plugins/import` (`hello`)
  201; real MCP client `initialize` → `hello`, `tools/list` → `{echo, add}`,
  `tools/call echo` → `{"text":"smoke"}`; then `docker compose down -v`.

Joined evidence on the frozen tree:

- `.venv/bin/python -m pytest -q`: **1180 passed** (+18 deployment tests).
- `.venv/bin/ruff check .`: **All checks passed**.
- `.venv/bin/mypy`: **No issues found in 94 source files**.
- `.venv/bin/python -m pytest -q tests/real_redis`: **15 passed**.
- `uv lock --check`: **Resolved 104 packages**.
- `uv build`: **sdist + wheel built** (`/tmp/opencode/phase9-dist`).

### Phase 9 manager re-verification before the gate review

The manager independently re-ran the whole Phase 9 evidence set and an
independent fresh-deployment smoke (`/tmp/opencode/phase9-smoke/smoke.py`):

- `docker build -t custom-mcp:verify .`: **image built**; `docker compose config
  -q`: **valid**.
- Fresh Compose stack (throwaway 48-char `MCP_SECRET`, `BASE_URL=http://127.0.0.1:8000`):
  **14/14 checks passed** — `compose up --build`; `/healthz` -> `{"status":"ok"}`;
  container `id` -> `uid=10001(app) gid=10001(app)`; Redis
  `NetworkSettings.Ports` -> `{"6379/tcp":null}`; `GET /login` 200;
  unauthenticated `/api/plugins` 401; wrong secret 401; login sets
  `HttpOnly; SameSite=Lax` cookie; authenticated `/api/plugins` 200; import of
  `hello` 201 (with the `Origin` header the browser sends — without it the CSRF
  guard correctly returns `forbidden`); `hello` `load_state=loaded`; real MCP
  client `initialize` -> `hello`, `tools/list` -> `{add, echo}`, `tools/call
  echo` -> `{"text":"smoke"}`; `docker compose down -v` removed the containers
  and both volumes.
- `tests/real_redis`: **15 passed** (private unix-socket Redis; external
  `127.0.0.1:6379` never contacted); `tests/deployment`: **18 passed**.
- Docs audit: every `src/custom_mcp/...` path and every `test_...` identifier
  cited in `docs/SECURITY.md` exists (**45/45** test IDs, **12/12** file paths);
  all internal Markdown links and `#anchors` across README, `docs/*.md` and
  `plugin_examples/*/README.md` resolve (**0 broken**).

Two real documentation gaps against the Phase 9 gate were found and fixed:

1. **README had no plugin-authoring guidance** (the gate requires README
   "setup/authoring/backup/HTTPS/proxy/no Redis exposure guidance"). Added a
   `## Writing a plugin` section: the full manifest field table (slug/version/
   entry/auth-mode rules, `none` exclusivity, `min_core_version`), an SDK example
   (`MCPPlugin` with the five required methods), the enforced authoring rules
   (explicit `type: object` schema, fresh schema objects, scoped JSON storage,
   `core.*` reserved, `ToolResult.failure` redaction, own HTTP timeouts, bounded
   calls that cannot kill hung workers), the stdlib ZIP build, install, and
   MCP-call steps, plus links to the three examples and `SDK_CONTRACT.md`.
   The example was not copied on trust: it was extracted from the README and
   driven through the **real app assembly**, host and loader
   (`/tmp/opencode/phase9-doc-example/verify.py`): import 201, `load_state=loaded`,
   `status=connected`, `tools/list` -> `[echo]` with `type: object` schema, live
   MCP `echo` -> `{"text":"hi"}`, missing argument rejected with the uniform
   `[invalid_arguments]` error, unknown tool handled.
2. **Two example READMEs carried stale phase text** — `plugin_examples/hello`
   said the raw MCP endpoint "is not available yet" and `apikey_demo` said it
   "appears in Phase 3+", both false since Phase 3. Replaced with accurate
   install + `{BASE_URL}/mcp/{slug}/` bearer-call instructions.
   `grep` now finds no stale "later phase"/"not available yet" claims.

Post-fix joined evidence: **1180 passed**, Ruff clean, mypy clean (94 source
files), `uv lock --check` consistent (104 packages), **15 real-Redis**,
**18 deployment** tests, `uv build` sdist + wheel.

### Phase 9 gate review — PASS 9.3/10

An independent Phase 9 gate critic (`local-omniroute/literouter/deepseek-v4.1-flash-1017:free`,
per the standing critic-model directive) reproduced every gate item with live
evidence:

- **Deployment.** `docker build` exit 0; container `id` → `uid=10001(app)
  gid=10001(app)`, `/data` owned by `app`, no secret/Redis env baked in;
  `docker compose config -q` exit 0 with `depends_on: service_healthy`, Redis
  `--appendonly yes --appendfsync everysec` on `redis-data:/data`, app `data:/data`
  and **no** Redis `ports:`; live `NetworkSettings.Ports` → `{"6379/tcp":null}`;
  `CONFIG GET appendonly/appendfsync` → `yes`/`everysec`; AOF survived a real
  `docker compose restart redis`.
- **Tests/lint/types/build.** `pytest -q` **1180 passed**; Ruff clean; mypy clean
  (94 files); `uv lock --check` consistent (104 packages); `pytest -q
  tests/deployment` **18 passed**; `pytest -q tests/real_redis` **15 passed**;
  `uv build` produced sdist + wheel.
- **Smoke.** Live `docker compose up -d --build`: Redis healthy before app;
  10/10 checks (healthz, login, unauth/wrong-secret 401, hardened cookie, bearer
  MCP, cookie-only MCP 401, cookie CSRF 403); `down -v` removed both volumes and
  the network.
- **Docs.** `docs/SECURITY.md`: 19/19 cited source paths, 29/29 explicit test refs
  and 65/65 test identifiers resolve; 0 broken Markdown links/anchors; README
  authoring example executed and manifest-rule table verified against
  `Manifest`; example "Try it locally" snippets run verbatim.

The critic found **one material, documentation-only defect**, fixed by the
manager: `plugin_examples/{hello,apikey_demo,oauth_demo}/README.md` cited the
import endpoint as `POST /api/plugins` (the real route is
`POST /api/plugins/import`; the bare path is GET-only, so the documented call
would 405/404). All three corrected; no remaining bare references.

During review the critic also closed two genuine gate gaps that 9c had missed
(within the gate's README "setup/authoring/backup/HTTPS/proxy/no-Redis-exposure"
requirement): a new README **`## Writing a plugin`** section (manifest field
table, a validated SDK example, enforced authoring rules, ZIP build/install and
MCP-call steps, links to the three examples and `SDK_CONTRACT.md`), and corrected
`plugin_examples/hello/README.md` / `plugin_examples/apikey_demo/README.md`,
whose "MCP endpoint not available yet / Phase 3+" notes were stale.

**Phase 9 is complete (10 of 10 phase gates).**

### Final independent whole-tree review — two material defects found and fixed

A final independent whole-tree review (same pinned model) went beyond the Phase 9
gate and reproduced two material defects the per-phase gates had missed. Both were
reproduced by the manager, fixed, and covered by mutation-verified regressions in
`tests/integration/test_final_review_regressions.py`:

1. **Published-state credential leak (blocker).** `_load_runtime` copied the
   loader's **raw** tool listing into `record.tools` and only redacted it later in
   `_refresh_tools`, while `detail()` serves `record.tools` verbatim. A plugin
   whose `list_tools` embeds a stored credential therefore exposed it through
   `GET /api/plugins/{slug}` for the whole duration of a slow `connect` on every
   reload/replace/startup. **Fix:** a new `_redact_listed_tools` redacts the
   listing before it is ever published, called from `_load_runtime` and
   `_rebuild_sealed_runtime` (same redaction as the post-connect refresh, failing
   closed to `Redacted.`). Regression:
   `test_reload_never_publishes_an_unredacted_description` fails if the load-time
   redaction is reverted.
2. **Silent lost write (material).** `MCPRouter.drain` had a 5 s budget, but a
   legitimate tool call is bounded only by `TOOL_TIMEOUT_SECONDS` (30 s default).
   A call that outlasted the drain was sealed with its write dropped by
   `ScopedStorage.set` yet still returned **success**, and the replacement then
   snapshotted the pre-write storage. **Fix:** `create_app` builds the router with
   `drain_timeout = TOOL_TIMEOUT_SECONDS + 5`, matching the router's documented
   promise that an in-flight call finishes before the seal/snapshot. Regression:
   `test_slow_inflight_call_write_survives_a_replace` fails with the default 5 s
   drain.

Neither defect was reachable through the MCP `tools/list` surface (which redacts
at serve time): finding 1 was the dashboard/API detail surface, finding 2 a storage
durability path. After the fixes the reviewer's own reproductions pass
(`LEAKED: False`; `counter_after: '101'`).

The **Phase 3 admission race** (a new call admitted between drain completion and
the mount swap, failing against the retired instance) remains the only recorded
deferred item; only its more severe lost-write variant is fixed here.

Joined evidence after the fixes: **1182 passed** (+2 final-review regressions),
Ruff clean, mypy clean (95 source files), **15 real-Redis**, `uv lock --check`
consistent (104 packages), `uv build` sdist + wheel. Both regressions are
mutation-verified (reverting either fix fails exactly its test).

#### Re-review addendum — N1/N2 fixed

The same reviewer re-verified both fixes (RESOLVED, mutation-proven) and raised
two follow-on issues, both now fixed in the tree and mutation-verified here:

- **N1 (drain amplification / Docker grace).** Counting the long-lived SSE `GET`
  stream as in-flight made every retirement wait the full budget, and the budget
  was paid twice (manager drain + mount retire); at ~35 s that exceeded Docker's
  default 10 s grace, risking SIGKILL mid-shutdown. **Fix:** `MCPRouter` drains
  only `POST` requests (the only method that can carry a tool call), pays the
  drain budget at most once per generation (`drain_spent`, re-armed by a new
  request), and `shutdown()` shares one drain+close budget across generations;
  `docker-compose.yml` sets `stop_grace_period: 100s`. Regressions:
  `test_idle_sse_stream_does_not_delay_a_replace`,
  `test_one_replace_pays_the_drain_budget_once`,
  `test_shutdown_is_bounded_with_an_open_sse_stream`.
- **N2 (schema/name redaction).** `input_schema` and tool names were still served
  verbatim. **Fix:** `_redact_schema_leaves` redacts every string leaf and key of
  the schema (default/enum/const/title/$defs...), and `_redact_tool` redacts the
  name and drops any tool whose name embeds a secret, whose redaction fails, or
  whose redacted schema no longer validates — never logging the name. Regressions:
  `test_schema_embedded_secret_is_redacted_on_both_surfaces`,
  `test_secret_in_a_tool_name_drops_only_that_tool`,
  `test_schema_that_cannot_survive_redaction_is_dropped`.

Final joined evidence: **1191 passed** (8 final-review regressions), Ruff clean,
mypy clean (96 source files), **15 real-Redis**, `uv lock --check` consistent
(104 packages), `uv build` sdist + wheel, `docker compose config -q` ok. Every
final-review regression is mutation-verified.
