# AI tool-use (agentic layer)

Plan reference: `plan.md` §9. Implementation: `backend/ai_tools.py` (registry), `backend/ask_pipeline.py::run_agentic_ask` (orchestration loop), `backend/claude.py::_call_anthropic_agentic_turn` (raw Anthropic Messages API tool-use call).

## What this is

By default, the AI answers `/ask` using a **pre-fetch RAG path**: `app.py`/`asgi.py` gather Sefaria refs, customs, wiki context, and Halachipedia results *before* calling the model, and hand it all over as static context. The model cannot decide mid-answer that it needs today's zmanim or that a fact isn't in the Judaic corpus — it only ever sees what was pre-stuffed.

The agentic layer is a second, flag-gated path where the model is given a set of callable tools and decides for itself, turn by turn, which ones it needs — genuine tool-calling (Anthropic Messages API tool-use / function-calling), not a fixed context dump.

## The flag: `AI_AGENTIC_TOOLS`

- Env var, `backend/claude.py`. **Unconditionally off by default in every environment** — unlike `CLERK_ENFORCE_AUTH`'s prod-aware default, this flag has no environment carve-out, per plan.md §9.4's explicit "do not enable the flag by default."
- Off (default): `/ask` behaves exactly as it did before this layer existed — `app.py` calls `claude.ask_claude(...)`, `asgi.py` calls `claude.ask_ai_async(...)`. Neither function was modified by this work; both remain fully independent of `ask_pipeline.run_agentic_ask` (enforced by `tests/test_agent_loop.py::test_scenario_g_ask_claude_and_ask_ai_async_untouched_by_this_module`, which asserts via `inspect.getsource` that neither function's source references `run_agentic_ask`).
- On: both transports instead call `ask_pipeline.run_agentic_ask(...)`, which drives the tool-use loop against the live Anthropic API.
- To enable: set `AI_AGENTIC_TOOLS=true`. There is no default-on rollout plan yet — flipping this changes the live model-call shape (a `tools=[...]` parameter on every `/ask` turn) and should happen deliberately, after soak, not as a side effect of this work. **Do not enable in production before `claude_code_prompts.md` Prompt 29a's D3 (Upstash rate-limit store + middleware unification) lands** — the agentic loop can issue multiple tool-call rounds (and therefore multiple metered model calls) per `/ask` request, multiplying per-question cost, and the roadmap's own ordering (Prompt 20's original prerequisite) gated this specifically on rate limiting being hardened first.

## Source hierarchy (non-negotiable)

Tool descriptions and the orchestrator both encode the same rule the pre-fetch path already followed: **Judaic texts and computed calendar/zmanim data first; `web_search` is last-resort.** See the standing rule added to `.agents/ENGINEERING_RULES.md` under "AI tool-use & agentic layer."

## The tool catalog (22 tools, `backend/ai_tools.py`)

All tools wrap **existing** backend functions — this layer adds no new business logic beyond the two exceptions noted in "Implementation notes" below. Every tool is JSON-schema'd (typed params with bounds: lat/lon ranges, date-string patterns, community enum) and every handler has the uniform signature `async def _h_xxx(arguments: dict, context: dict) -> dict` and **never raises** — `execute_tool()` is the single place that applies a timeout, a narrow exception catch, and health-circuit bookkeeping, so any one handler bug degrades to a `{"error": ...}` result rather than crashing the whole agent loop.

**Texts & sources (tier 1–2):**
| Tool | Backs onto |
|---|---|
| `search_judaic_texts` | Sefaria search + `sefaria.find_refs_for_question` — primary, use first |
| `get_text_by_ref` | `ShelahEngine.get_library_text` / `sefaria_library.get_text` |
| `search_responsa_external` | `search.async_search_halachipedia` + HebrewBooks (tier 2, whitelisted) |
| `get_commentaries` | `sefaria_library.get_linked_texts(ref)` |
| `browse_library` | `sefaria_library.get_library_index` / `get_texts_for_category` |
| `search_library` | `sefaria_library.search_library(query, filters)` |
| `format_source_citation` | `backend/utils/text_engine.py` |

**Calendar & zmanim (deterministic, never model-estimated):**
| Tool | Backs onto |
|---|---|
| `get_zmanim` | `zmanim_engine.get_community_zmanim(lat, lon, tz, community)` |
| `get_hebrew_date` | `calendar_service.PyluachEngine` (+ Hebcal convert) |
| `get_parasha` | `calendar_service.get_parasha` / `zmanim_engine._get_weekly_shabbat_parasha` |
| `get_omer` | `zmanim_engine._get_omer_info` |
| `get_holidays` | `zmanim_engine.get_monthly_events` / Hebcal holidays API |
| `get_daily_study` | Hebcal/Sefaria daily-learning calendars (daf yomi, mishnah yomi) |
| `get_daily_zmanim_summary` | Composed `zmanim_engine` digest — "what do I need to know today" |
| `calculate_hebrew_date_math` | `PyluachEngine` — yahrzeit/anniversary date arithmetic |
| `convert_measurements` | New deterministic shiurim table (Chazon Ish vs. R' Chaim Naeh) — the one genuinely new piece of logic in this layer, called for explicitly by plan.md §9.2b |

**Language & community:**
| Tool | Backs onto |
|---|---|
| `lookup_word_meaning` | `_lookup_hebrew_word_meaning` / `_lookup_english_word_meaning` (Sefaria lexicon, BDB, Jastrow) |
| `translate_text` | `_translate_hebrew_text_online` (Google/MyMemory fallback) |
| `search_community_customs` | `customs.search_customs` + RAG community-knowledge helpers |
| `get_community_profile` | Reimplemented against backend-only data — see Implementation notes |
| `get_prayer_text` | Reimplemented against backend-only data — see Implementation notes |

**Last resort:**
| Tool | Backs onto |
|---|---|
| `web_search` | `search.async_search_wikipedia` + the existing allowlist in `search.py` only — no new provider. Orchestrator-gated (see below). |

**Deliberately not exposed as model tools:** `export_answer` and `save_bookmark` mutate state / produce files — they stay UI-only actions on a finished answer, keeping the agent loop read-only and side-effect-free, per plan.md §9.2's explicit instruction.

## Web-search last-resort gate (§9.3)

Three layers, belt-and-suspenders:

1. **Prompt-level:** the `web_search` tool's own description says "last resort," and the system prompt repeats it.
2. **Orchestration-level (the real gate):** `ask_pipeline.run_agentic_ask` does not include `web_search` in the tool list it offers the model (`ai_tools.get_tool_schemas(include_web_search=...)`) until a `search_judaic_texts` or `search_library` call has already returned an insufficient result *in the current turn* — tracked via `_tool_result_is_insufficient()`, a structural check (error present, or an empty `"results"` list) rather than per-tool special-casing, since both gating tools share the same response shape. Even if the model asks for `web_search` before that condition is met, it is simply absent from the tool list the model can call — there is nothing for the model to invoke.
3. **Attribution:** `run_agentic_ask`'s result dict carries `used_web_search: bool`. Both `app.py` and `asgi.py` branch on the **presence of that key** (not on the flag again) to decide `needs_web_warning`, so the UI's general-web warning and tier-4 attribution note correctly reflect whether the live loop actually reached for the web that turn.

## Orchestration (`ask_pipeline.run_agentic_ask`)

Loop: send the question + the currently-allowed tool schemas → the model returns text and/or `tool_use` blocks → the orchestrator executes every requested tool concurrently via `asyncio.gather` (each `execute_tool()` call is itself async, timeout-boxed, and internally uses `asyncio.to_thread` for any blocking engine call) → results are re-injected as `tool_result` blocks → repeat. Hard cap: **4 rounds** (`AI_AGENTIC_MAX_ROUNDS`); the final round omits `tools` entirely (not an empty list) to force a text answer. Location for `get_zmanim`/calendar tools comes from the caller's resolved `tool_context` (lat/lon/timezone); there is no guessing.

The `AI_AGENTIC_TOOLS` gate at both call sites (`app.py`'s `_run_ask_question_ai_synthesis`, `asgi.py`'s `_run_ask_async_ai_synthesis`) is the only new branch point in either route handler — everything downstream (fallback ladder, response-shape building, safety-output validation) is shared with the non-agentic path.

## Safety, cost, reliability (§9.5)

- `classify_safety()` runs **before** the loop starts — medical/self-harm/abuse queries never reach tool-use; they route straight to referral, identical to the non-agentic path.
- Every tool call is timed out, narrowly caught, and circuit-broken (`health.record_success`/`record_failure` per attempt, fail-open on a dead provider).
- `cost_meter` records the underlying model call each round via the same `record_llm_call` path `_call_anthropic_agentic_turn` shares with the rest of `claude.py`.
- Tool results are sanitized as untrusted content (`claude._sanitize_model_output`, capped at 4000 chars) before being re-injected into the conversation, since `web_search` results in particular are external, unvetted text. On top of that, the handlers that return publicly-editable or crowd-sourced third-party text (`web_search`, `search_responsa_external`, `translate_text`) run each result through `backend/retrieval_guard.py::withhold_injected()` and drop a hit carrying prompt-injection phrasing whole (a dropped `web_search` result comes back as a "withheld" error; a dropped responsa hit simply isn't in `results`; a dropped translation comes back as `translated: false`). Only a source label and a marker count are logged, never the text.

## Implementation notes & deviations from plan.md's literal text

Documented here rather than silently — each is a deliberate, evidence-based call made while implementing Prompt 20, not an oversight:

1. **`get_community_profile` and `get_prayer_text` reimplemented, not wrapped.** Plan.md §9.2b names these as backing onto `_build_trusted_custom_sources` / `SIDDUR_SECTION_MAP` and `_get_prayer_refs` — both of which live in `app.py`, not `backend/`. `backend/ai_tools.py` must import `backend.*` only (never `app`, to stay callable from any transport and from tests without booting Flask) — the same constraint Prompt 20 itself states. Both tools are therefore reimplemented against backend-only data sources instead of wrapping the named app.py functions; see each handler's own docstring in `ai_tools.py` for the specific data-source substitution.
2. **`_THREAD_POOL` not reused for parallel tool execution.** Plan.md §9.4 says to reuse `_THREAD_POOL` (defined in `app.py`) for the `asyncio.gather` + `to_thread` parallel tool dispatch. `backend/ask_pipeline.py` cannot import from `app.py` without violating the same backend-import-discipline rule — `app.py` imports `backend.*`, not the reverse, and reversing that creates a circular import. `run_agentic_ask` instead calls `asyncio.gather()` directly over the already-async `execute_tool()` coroutines, each of which internally uses a bare `asyncio.to_thread` for its own blocking calls — functionally equivalent "parallel, non-blocking" behavior without the illegal import.
3. **Anthropic only, not Gemini.** The pre-fetch path is multi-model (Gemini primary, Claude fallback). The agentic loop (`_call_anthropic_agentic_turn`) is Anthropic-only — Gemini's function-calling API has a different request/response shape and would need its own parsing branch. Scoped out as deliberate follow-up rather than attempted partially; see the new findings section (plan.md, "Findings from implementing Prompt 20") for the concrete next step.
4. **`is_healthy()` off-loop everywhere in this layer.** `is_healthy()` can perform a blocking `requests.get` re-probe when a circuit's recovery interval has elapsed. Every call site added by this work — `_call_anthropic_agentic_turn`'s circuit check and `ai_tools.execute_tool`'s per-tool circuit check — wraps it in `await asyncio.to_thread(health.is_healthy, ...)`, matching the existing pattern in `claude.py`'s other async model-call functions (§26.1). `execute_tool`'s bare (non-`to_thread`) call was a bug introduced during this same implementation pass and fixed before this document was written, not deferred.

## Tests

- `tests/test_ai_tools.py` — registry shape, param validation, and per-tool handler behavior against mocked engines (offline).
- `tests/test_agent_loop.py` — orchestration scenarios (a)–(g) from plan.md §9.6: texts-only answers never expose `web_search`; zmanim/Hebrew-date questions call the right deterministic tool; a non-Judaic factual gap unlocks `web_search` only after a texts search comes back insufficient, and the result carries the web warning; the round cap is enforced; a circuit-open provider is hidden from the model; the flag-off path reproduces today's behavior exactly (verified structurally, not just by value, per the note above).
- `tests/test_claude_agentic_turn.py` — the raw `_call_anthropic_agentic_turn` Messages-API call against the suite's respx-mocked Anthropic endpoint (real SDK `Message` object parsing, not an all-mocked shape).

`pytest -q` is green with `AI_AGENTIC_TOOLS` both unset (default) and `=true`.
