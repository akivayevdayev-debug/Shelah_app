# Data Protection Impact Assessment — Automated Religious Guidance (plan.md §8.D.4)

**Status:** basic DPIA, written 2026-08-17 as an engineering-informed
first pass, not a substitute for a counsel-reviewed DPIA before public
launch (same caveat as the legal documents in `templates/*.html` — see
`docs/PRIVACY_OPERATIONS.md` and `privacy.html` §10 for related
placeholders). A DPIA is a living document — revisit it whenever the
processing changes materially (new AI provider, new data category, new
automated decision).

## 1. Is a DPIA required?

GDPR Art. 35(1) requires a DPIA when processing is "likely to result in a
high risk to the rights and freedoms of natural persons," and Art. 35(3)
specifically flags large-scale processing of special-category data.
Sh'elah's core feature — an AI system that answers user-submitted
questions about Jewish religious practice — routinely touches:

- **Religious belief and practice** (Art. 9 special category, on its
  face — every question is, by definition, about the user's or a third
  party's religious observance).
- **Health data** (medical questions intersecting halacha — fasting,
  medication, pregnancy).
- **Data concerning sex life** (niddah, mikveh, marital-intimacy
  questions — an explicit, foreseeable category of query for this app,
  not an edge case).
- Occasionally, data revealing **mental health status** or **safety
  risk** (self-harm, abuse-adjacent language — see
  `docs/AGE_AND_SAFETY_POLICY.md`'s safety classifier).

That combination — special-category data, an automated system generating
the response, and a users base that includes minors (13+) — clears the
Art. 35(3) bar on its own. A DPIA is required, and this is it.

## 2. Description of the processing

**Nature.** A user submits a free-text question through the web app. The
backend retrieves relevant source text (Sefaria, the local customs corpus,
Halachipedia) and sends the question plus retrieved excerpts to an LLM
(Google Gemini primary, Anthropic Claude fallback) to synthesize an
answer. The answer, sources, and (for signed-in users) the interaction are
stored in Supabase. No human reviews individual questions or answers in
the normal path.

**Scope.** Every `/ask` request, from both signed-in and anonymous users.
Anonymous users' questions are not linked to a persistent identity beyond
an IP-derived rate-limit key; signed-in users' questions are stored against
their Clerk `user_id` in `ask_history` (90-day retention, then deleted —
see `docs/PRIVACY_OPERATIONS.md` §5).

**Context.** Users come to Sh'elah specifically to ask religious-practice
questions — the special-category nature of the data is the product, not
an incidental byproduct. This cuts both ways for risk: it means users have
a reasonable expectation that their question is religion-related (lower
surprise/risk), but also means the *volume* of special-category data this
app processes is unusually high for its size (higher aggregate exposure if
a breach occurs).

**Purposes.** Generate an educational answer to a halachic question, with
citations, always paired with a referral to consult a qualified rabbi —
never presented as a binding ruling (`privacy.html` §9, `terms.html`,
`templates/ai-disclosure.html`).

**Automated decision-making assessment (GDPR Art. 22).** The AI output is
automated, but Art. 22 targets decisions with "legal or similarly
significant effects." A generated answer that is explicitly non-binding,
paired with a rabbi referral, and does not gate access to any service,
benefit, or opportunity does not meet that bar — this is confirmed in
`privacy.html` §9. The safety classifier's *referral* decisions (routing
medical/self-harm/abuse-adjacent queries away from model synthesis) are
themselves automated, but the effect is protective (directing the user to
human/professional help sooner, not withholding anything) — see §4 below.

## 3. Necessity and proportionality

- **Is sending question text to a third-party LLM necessary?** Yes — the
  core feature is synthesis over a corpus too large for a static lookup
  table, and no on-device/self-hosted model of comparable halachic
  reasoning quality is a realistic alternative for a solo-operated
  project at this stage.
- **Is it proportionate?** The system sends the minimum needed: question
  text and retrieved source excerpts only. `privacy.html` §4 states, and
  `backend/claude.py`/`backend/ask_pipeline.py` confirm by construction,
  that **no account identifiers (name, email) are sent to Gemini or
  Claude** — the LLM never receives anything that would let it (or a
  breach of it) link a question back to a real identity, only to
  whatever the question text itself reveals.
- **Is storing ask history necessary?** Only for signed-in users, and only
  to power the user's own "my history" feature — not used for anything
  else, not sold, not used to train models without explicit consent
  (`privacy.html` §2). 90-day auto-deletion (§5 of
  `docs/PRIVACY_OPERATIONS.md`) bounds how long it persists.

## 4. Risk assessment

| Risk | Likelihood | Severity | Mitigation (implemented) |
|---|---|---|---|
| A breach of Supabase/Gemini/Claude/Sentry exposes a user's religious-practice questions, linkable to their identity | Low–Medium (standard SaaS breach surface) | High (special-category data, some of it health/sex-life-adjacent) | No account identifiers sent to AI providers; RLS on user-scoped tables; error telemetry scrubs question/answer text before reaching Sentry (`docs/OBSERVABILITY.md`); 90-day auto-deletion bounds exposure window; encryption at rest (Supabase) and in transit (TLS 1.2+) |
| The AI generates an unsafe response to a medical emergency, self-harm, or abuse disclosure | Low (three-layer defense) | Critical (life/safety) | `classify_safety()` routes `medical`/`mental_health_or_self_harm`/`abuse_or_minor_safety` queries to a referral template **before model synthesis is ever invoked** — the model cannot generate a "ruling" on these because it's never called; system-prompt directive (Layer 1) and post-generation output scan (Layer 3) are additional backstops; see `docs/AGE_AND_SAFETY_POLICY.md` |
| A minor receives sexually explicit or graphic content in response to an intimacy-adjacent halachic question | Low | High (minor-safety) | Age-appropriate directive constrains tone/explicitness for all users regardless of stated age (no age is actually collected, so this must hold universally, not just for self-identified minors); `validate_model_output()` scans and replaces explicit content post-generation as a second layer |
| The AI presents an answer as authoritative/binding when halacha requires case-specific rabbinic judgment | Medium (inherent to any AI-generated religious content) | Medium (could lead to a real-world harmful action taken on bad guidance) | Persistent, non-dismissible disclaimer banner on every answer (`renderDisclaimerBanner()`); explicit "not a rabbinic ruling" language in the system prompt, `ai-disclosure.html`, `terms.html`; sources cited so a user (or their rabbi) can verify against the primary text rather than trusting the synthesis alone |
| Over-refusal: legitimate, well-sourced halachic questions (e.g. fasting with a medical condition) get blanket-blocked as "medical," denying users real educational value | Low (already tuned) | Low–Medium (harms the app's core value, not a rights violation, but noted since an earlier broader classifier design did this — see `docs/AGE_AND_SAFETY_POLICY.md`'s "Adversarial review") | Classifier deliberately narrowed to first-person acute-emergency framing only; routine medical-halacha questions synthesize normally |
| A user's account and all associated special-category data persist indefinitely after they stop using the app | Medium (no engagement without action) | Medium | Self-serve deletion (`docs/PRIVACY_OPERATIONS.md` §1) removes this friction; automated 90-day deletion of ask history/usage logs bounds the highest-volume special-category table even for users who never explicitly delete their account |
| Anonymous (non-signed-in) users' questions are somehow re-identified via IP/rate-limit key | Low | Medium | Rate-limit keys are not linked to any account or persisted beyond the rate-limit window; not stored in `ask_history` (that table is signed-in-only) |

## 5. Consultation

No formal Data Protection Officer or external consultation has occurred
(`privacy.html` §10 already states no DPO/Art. 27 representative has been
designated). Given the solo-operator scale, the recommended next step
before public launch is a review of this DPIA by counsel alongside the
rest of the plan.md §8.G business/compliance scaffolding, not a full
external DPO engagement.

## 6. Outcome

Residual risk after the mitigations in §4 is assessed as **acceptable to
proceed**, conditional on:

1. The DPA checklist in `docs/PRIVACY_OPERATIONS.md` §3 actually being
   executed with each processor before public launch.
2. The retention job (`docs/PRIVACY_OPERATIONS.md` §5) running in
   production, not just existing in code.
3. Counsel review of this assessment, particularly the Art. 22 automated-
   decision-making conclusion in §2 and the special-category processing
   basis, before scaling beyond the current solo-operator/early-access
   stage.

Revisit this DPIA if: a new AI provider is added, the app begins sending
any account-identifying data to an LLM, the retention windows in
`privacy.html` §3 change, or the safety-classifier referral categories in
`docs/AGE_AND_SAFETY_POLICY.md` change materially.
