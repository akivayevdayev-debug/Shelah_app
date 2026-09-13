# Age & Safety Policy

**Status:** engineering implementation of plan.md §8.B-AGE. The age-gate
*mechanism* was originally a click-through consent modal; it has since been
replaced with a static, non-blocking footer notice (see "Age notice at
sign-up" below) — the minimum-age *decision* itself is unchanged. That
decision is a product/legal policy choice, not a purely technical one — it
should be confirmed with counsel before public launch (see
`docs/LAUNCH_CHECKLIST.md`), same as the rest of the §8.A/§8.G legal
scaffolding.

## Minimum age

**13+**, with the EU/UK defaulting to 16+ unless a jurisdiction-specific
parental-consent flow is added later. Rationale: 13 is the lowest floor
that avoids COPPA's verifiable-parental-consent burden in the US. Sh'elah's
content — halachic and educational text — is written for any user of any
age who has a question; it is general-audience material, not content
designed around, marketed toward, or aimed at a particular age cohort.

- No behavioral advertising, no sale/share of personal data for
  under-18 users.
- No *actual knowledge* of under-13 users is retained — no age is
  collected or verified (see below), so there is no signal to trigger
  retention in the first place. Any self-identified under-13 account
  should still be closed on discovery.

### COPPA "directed to children" — risk note (2026-09-06)

An earlier draft of the rationale above described the 13+ floor as
"matching the natural bar/bat-mitzvah-age audience for a halachic learning
tool." That phrasing has been removed, not because it was inaccurate as a
description of who ends up using the app, but because COPPA's "directed to
children" test (16 C.F.R. § 312.2) is fact-based and independent of any
stated minimum age — the FTC's own COPPA FAQ states directly that a site
can be found directed to children even where its own ToS prohibits
under-13 use, and the April 2025 final rule amendments (compliance
deadline 2026-04-22, already passed) added "representations to consumers"
as an explicit factor in that test. A stated rationale like "matching the
bar/bat-mitzvah-age audience" would itself have been exactly this kind of
representation, cutting toward a "directed to children" finding rather
than away from it. Removing that framing is a wording mitigation, not a
legal cure — it reduces one specific piece of self-supplied evidence; it
does not resolve whether the app's actual subject matter, audience
composition, or presentation would independently be found "directed to
children" under the test's other factors (visual design, subject matter,
music/animated characters — none of which apply here — language, presence
of child celebrities/models, etc.). That determination is genuinely
fact-specific and attorney territory, not something a wording change
settles. If it were ever found "directed to children," the current
footer-only, no-screening design (finalized as a deliberate operator
decision below) is the highest-risk posture available under a
"mixed-audience" finding, which would permit age-screening and
differentiated treatment but not the current zero-screening approach.
This risk is being knowingly carried forward, consistent with the
footer-only decision below and the project-wide decision to forgo
attorney review (`akiva_tasks.md` T14) — it is not resolved, only
disclosed here for the record.

## Age notice at sign-up (implemented)

There is no click-through age gate. The homepage renders a persistent,
non-blocking footer (`.site-footer` in `templates/index.html`) present on
every page load, stating in plain text: *"By using this site you confirm
you are at least 13 years old (16+ in the EU/UK)"*, alongside links to
Terms of Service, Privacy Policy, AI Disclosure, Acceptable Use, DMCA,
Accessibility, and Licenses. There is no checkbox, no modal, no
click-through, and nothing blocks interaction with the app pending
agreement — continued use of the Service is treated as the user's
confirmation that they meet the age requirement, the same legal theory
used by most footer/browsewrap notices. `templates/terms.html` §3
(Eligibility & Age Requirements) states the same requirement contractually
and points here for the full rationale.

No age or date of birth is collected, no attestation is persisted, and no
consent record is written — this is informational text only, not a
verification mechanism. The `/api/accept-legal` endpoint
(`backend/routes_user.py::accept_legal`) and the `LEGAL_TERMS_VERSION`
/ `LEGAL_PRIVACY_VERSION` constants and Supabase `user_preferences`
columns (`age_attested`, `age_attested_at`, `legal_accepted`,
`legal_accepted_at`) it can write to still exist and still work end to
end, but nothing in the current frontend calls it — it is dead code kept
in place for a future real clickwrap moment (e.g. at Clerk sign-up), not
part of the live consent flow. Do not describe it as "implemented" for age
gating purposes elsewhere in the docs; it is unwired.

## Age-appropriate output layer (implemented, `backend/claude.py`)

### Layer 1 — system-prompt directive

`AGE_APPROPRIATE_DIRECTIVE` is a single hoisted constant appended to both
`CORE_SYSTEM_PROMPT` and `SIMPLE_SYSTEM_PROMPT`, so it applies to every
answer regardless of mode or which model (Gemini primary, Claude fallback)
responds. It instructs: assume a reader as young as 13; use clinical,
educational language for sensitive halachic areas (niddah, mikveh,
intimacy, marital relations, bodily functions); never render sexually
explicit, graphic, or titillating detail; for genuinely intimate topics,
give principles + source citations and direct the reader to "learn the
practical details with a rabbi, teacher, or parent." This constrains
explicitness and tone only — it does not reduce scholarly depth, multiple
authorities, or machloket coverage.

### Layer 2 — pre-synthesis safety routing (`classify_safety()`)

Runs before every model call (both the sync `ask_claude` path via
`run_protected_ai_wrapper` and the async `ask_ai_async` path). Classifies
the query into one of:

| Class | Behavior |
|---|---|
| `ok` | Normal synthesis. |
| `sensitive_intimate` | Normal synthesis, under the age-appropriate directive (niddah/mikveh/intimacy topics stay fully answerable). |
| `medical` | **Referral, no ruling.** Deliberately narrow — only first-person, present-tense, acute-emergency framing ("I am having chest pain right now", "I can't breathe"), routed to "call emergency services immediately; pikuach nefesh overrides virtually every other halachic consideration." Routine personal-health-and-halacha questions (fasting with pregnancy/diabetes/illness, medication on Shabbat) are **not** referred — they are exactly the kind of well-sourced halachic Q&A (Shulchan Aruch OC 617, extensive poskim) this app exists to answer, and an earlier broader design that caught them was a confirmed high-severity over-refusal bug (see "Adversarial review" below). |
| `mental_health_or_self_harm` | **Referral, no ruling.** Self-harm/suicide language routes to a crisis-resource referral (988 in the US) + rabbi/trusted-adult, never a halachic answer. |
| `abuse_or_minor_safety` | **Referral, no ruling.** Abuse-adjacent language routes to emergency services / abuse-hotline / professional referral, never procedural or halachic guidance. |
| `dangerous_or_illegal` | Reuses the existing `_detect_out_of_scope_subject` refusal path (hateful content, calls to violence, explicit content). |

The three referral classes **bypass model synthesis entirely** — the model
is never called, eliminating the risk of an AI-generated "ruling" on a
medical/self-harm/abuse situation. Referral text is bilingual (en/he).

This is heuristic and best-effort (regex-based, same philosophy as the
pre-existing `_detect_out_of_scope_subject`): ambiguous signals default
toward `ok` so ordinary halachic Q&A (Shabbat, kashrut, brachot, and
routine medical-adjacent questions like "what medication can I take on
Shabbat") is never over-refused. Layers 1 and 3 are the backstops for what
this heuristic misses — **never rely on regex classification alone.**

### Layer 3 — post-generation output check (`validate_model_output()`)

Defense in depth: even if a query wasn't caught by `classify_safety()` and
the model produces explicit content despite the system-prompt directive,
`validate_model_output()` scans the response text for explicit-content
markers and replaces the body with a principles-plus-referral template
before it ever reaches the user. This is a distinct block reason
(`blocked_explicit_content`) from the pre-existing internal-instruction
leak check (`blocked_internal_instructions`).

### Structured-output fields

`age_safe: bool` and `safety_class: str` are added to every structured
answer payload, backward-compatibly (`_normalize_structured_response`
defaults them to `True`/`"ok"` when absent, so older cached payloads and
any caller not yet aware of these fields keep working unchanged).

`safety_class` (plus `rabbinic_disclaimer`) is also surfaced on `meta` in
every `/ask` response shape (both `app.py` and `asgi.py` — see
`_run_ask_question_ai_synthesis`/`_security_blocked_ask_payload`/
`_run_ask_async_ai_synthesis`), which `templates/index.html`'s
`renderDisclaimerBanner()` reads to render the persistent, non-dismissible
"educational information, not a halachic ruling" banner (plan.md §8.B.1)
above every answer in both the AI modal and reader view — switching to a
visually distinct, prominent referral variant (icon + amber theme tokens,
never color alone) for `medical`/`mental_health_or_self_harm`/
`abuse_or_minor_safety`/`dangerous_or_illegal`.

## Mode & language parity

The directive and safety routing apply identically across all answer modes
(`balanced`, `practical`, `sources`, `strict`) and both `answer_language`
values (`en`, `he`) — referral text exists in both languages; regression
tests confirm ordinary halachic questions are unaffected in either
language.

## Adversarial review (2026-07-30)

A multi-lens adversarial review of the first implementation confirmed 17
real bugs before this layer shipped, spanning severe over-refusal on
extremely common personal halacha questions (pregnancy + fasting, diabetes
+ fasting — the single most common class of question this app exists to
answer), a critical wiring bug where every referral response was silently
discarded before reaching the user (the error string didn't match the
`security_blocked` prefix every `/ask` caller gates on), an explicit-content
block that only scrubbed one of four structured-output fields, a
hidden-Unicode classification-evasion gap, and zero Hebrew coverage on the
post-generation explicit-content check. All 17 were fixed and each has a
dedicated regression test in `tests/test_safety_classifier.py` pinned to
the exact failing example the review found — see that file's module
docstring and the `*OverRefusalRegression`/`test_error_string_starts_with_security_blocked`/
`test_explicit_content_block_clears_summary_steps_and_sources` test classes.

The `medical` class in particular was redesigned from "personal medical
situation + halachic practice keyword" (too broad — caught routine,
well-sourced halacha questions) to "first-person acute emergency framing
only" (narrow, high-precision).

## Tests

`tests/test_safety_classifier.py` covers: `classify_safety()` routing per
class, referral results never containing a ruling, the post-generation
explicit-content check (including that ordinary niddah/mikveh discussion
is *not* blocked), backward-compatible structured-output defaults, the
directive's presence in both system prompts, end-to-end wiring through
`run_protected_ai_wrapper`/`ask_claude`/`ask_ai_async` (including that the
model is never invoked for a referral-class query), bilingual referral
text, and a dedicated no-over-refusal regression suite for ordinary
Shabbat/kashrut/calendar questions.

`tests/test_routes_user.py::TestAcceptLegalAuthenticated` covers the age-
attestation persistence path (present when attested, absent — not defaulted
to true — when omitted).

## Operator decision (2026-09-04, Akiva)

**The footer-only enforcement model is final — not a placeholder pending a
future clickwrap build.** Explicit decision: no restricted sign-up, no
technical age gate, no jurisdiction-based blocking of anyone using the app.
The footer notice plus `terms.html` §3's representation ("By using the
Service, you represent that you meet this age requirement; misrepresenting
your age is a material breach of these Terms") is the entire mechanism. A
user who is underage, or who is old enough but simply disregards the
notice, is using the Service on the basis of a false representation they
made themselves — under §3 plus the existing "AS IS"/assumption-of-risk/
liability-cap clauses (`terms.html` §§13–14), that outcome is the user's
own responsibility, not the operator's. No further code or content change
implements this decision — the ToS language already in place was written
to say exactly this; this entry records that it is being relied on as
designed, not left open pending something stronger.

This changes the disposition of two items below (**real clickwrap
consent** is explicitly declined, not merely deferred; **EU 16+
jurisdiction detection** likewise, since geo-based enforcement is exactly
the kind of "physical restriction on everyone using the app" the decision
above rules out) without touching the underlying age *threshold* (still
13+/16+ per "Minimum age" above) or the attorney-review recommendation,
which remains a separate, still-open question about the policy's legal
soundness, not about which enforcement mechanism to build.

## What is not yet done

- **Attorney/legal review** of the 13+ / EU 16+ policy choice itself (see
  `docs/LAUNCH_CHECKLIST.md`) — a live, still-open item. The operator
  decision above is a product/liability-allocation choice made without
  counsel; it does not substitute for counsel's view on whether that
  allocation actually holds in every jurisdiction this app is reachable
  from.
- **Under-13 account closure procedure.** No automated detection/closure
  flow exists yet for a user who later self-identifies as under 13 — this
  is a privacy-operations item, tracked in `docs/PRIVACY_OPERATIONS.md`.
  Unaffected by the decision above (closing a *known* under-13 account on
  discovery is a data-retention obligation, not an age-gate mechanism).
- ~~EU 16+ jurisdiction detection.~~ **Declined 2026-09-04** — see Operator
  decision above. Kept here only so a future reader doesn't rediscover
  this as an open gap; do not build it without a new, explicit decision
  overriding this one.
- ~~Real clickwrap consent.~~ **Declined 2026-09-04** — see Operator
  decision above. `/api/accept-legal` and its Supabase columns
  (`age_attested`, `age_attested_at`, `legal_accepted`,
  `legal_accepted_at`) remain unwired dead code by design, not by
  omission; do not wire them without a new, explicit decision overriding
  this one.
