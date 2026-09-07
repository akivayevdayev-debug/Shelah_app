# Launch Checklist (plan.md §8.H)

**Status:** living tracked checklist, first written 2026-08-20. This
document reproduces plan.md §8.H's launch-gate checklist verbatim, line by
line, and attaches to each line the actual file/test/doc that proves it —
or the honest gap if it isn't proven yet. It is not a substitute for
`plan.md` itself (the source of truth for scope and design) or
`claude_code_prompts.md` (the source of truth for the full prompt-by-prompt
evidence trail) — it exists so "are we ready to launch" has one place to
check without re-deriving the answer from either of those larger documents.

This is an engineering-authored status snapshot, not a legal or business
sign-off. Several lines below are marked **done-pending-attorney-review**
or **not started** on purpose — see the "Business & compliance" section at
the end for the items that are structurally outside what an engineering
pass can close at all.

**How to read the status column:**

| Status | Meaning |
|---|---|
| ✅ Done | Implemented, tested, and verified against the current repo state |
| ✅ Done — pending attorney/operator review | Engineering work is complete; a specific non-engineering action (legal review, a dashboard setting, a manual verification) is the only remaining step |
| ⚠️ Partial | Some of the line is done; the rest is named specifically, not glossed over |
| ❌ Not started | No engineering work has begun on this line |

Re-verify before relying on this table for a real launch decision — like
the rest of this project's status documents, it ages as soon as the next
commit lands. Every claim below was checked against the actual file
content on 2026-08-20, not copied from an earlier summary.

---

## The checklist (plan.md §8.H, verbatim)

### 1. "ToS, Privacy Policy, AI Disclosure, AUP, DMCA, Accessibility, Licenses — drafted **and attorney-reviewed**, versioned, dated, notice given on every page."

**Status: ✅ Done.**

All seven documents exist, are dated, versioned, and cross-link each other
in a persistent footer notice on every page:
[`templates/terms.html`](../templates/terms.html),
[`templates/privacy.html`](../templates/privacy.html),
[`templates/ai-disclosure.html`](../templates/ai-disclosure.html),
[`templates/acceptable-use.html`](../templates/acceptable-use.html),
[`templates/dmca.html`](../templates/dmca.html),
[`templates/accessibility.html`](../templates/accessibility.html),
[`templates/licenses.html`](../templates/licenses.html), plus
[`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md). Served by
`backend/routes_legal.py`, cross-link coverage enforced by
`tests/test_routes_legal.py`. Remaining `[Placeholder]` markers on
clauses that are genuinely undecided (damages-cap figure, arbitration/venue
choice, transfer-mechanism confirmation, EU rep/DPO determination) are
unaffected by this line. Full evidence trail: `claude_code_prompts.md`
Prompt 12.

**Operator decision 2026-09-05 (Akiva):** attorney review is declined
outright, not deferred — too complex and costly to justify for a free,
non-revenue site. The "⚠️ DRAFT — pending attorney review" banners were
removed from all 5 gated pages accordingly (commit `054b7d5`); see
`plan.md` §8.A's "Operator decision 2026-09-05" note. This closes the one
item this line used to describe as not done. The DMCA designated-agent
registration is a separate, still-open administrative filing the operator
is handling with their parent (`akiva_tasks.md` T14) — it is not a
substantive legal review and does not block this line.

Also worth noting, unaffected by the above: the notice mechanism is
**browsewrap** (a persistent footer, not a click-through gate) — `plan.md`
§8.A's "Operator decision 2026-09-04" note settles this as final, not an
open question. `/api/accept-legal` and the version-tracking DB columns
still exist and are tested but are currently dormant (nothing calls them
from the UI); they stay dormant by design, per that same decision.

### 2. "Persistent AI answer disclaimer live; safety routing for medical/legal/abuse queries verified."

**Status: ✅ Done.**

`meta.safety_class`/`meta.rabbinic_disclaimer` are returned on every
`/ask` response shape on both the Flask and FastAPI transports.
`templates/index.html` renders a persistent, non-dismissible
`#aiDisclaimerBanner`/`#readerDisclaimerBanner` on every answer, with a
visually distinct variant (icon + color, not color alone) for
`medical`/`mental_health_or_self_harm`/`abuse_or_minor_safety`/`dangerous_or_illegal`
classes. `classify_safety()` (`backend/claude.py:845`) and the referral
text set (`backend/claude.py:263-319`) route those classes to a referral,
never a "ruling." Verified live in-browser in both themes, both the AI
modal and reader view. Tests: `tests/test_ask.py::TestSafetyClassMetaPropagation`
(meta parity across both transports for ok/medical/domain-refusal),
`tests/test_rag.py::TestStoreAskHistory`. Full evidence trail:
`claude_code_prompts.md` Prompt 13.

This session's B2 track additionally verified the **fallback-stage**
degradation path end-to-end (`tests/test_ask.py::TestAskDegradationPath`):
with every circuit breaker forced open, `POST /ask` still returns a
well-formed 200 with a non-empty answer, sources, and `meta.fallback=true`
on both transports.

**Update 2026-09-06:** the *primary*-call gate (`health.is_healthy`
consulted before the Gemini primary call, not just the fallback) is also
done — shipped 2026-08-20 (Prompt 39, `plan.md` §26.1), reconfirmed
2026-08-23 (§33.3). This line and line 6 previously described that as an
open B2-track finding; it wasn't re-checked against §26.1 at the time and
the two sections contradicted each other. `backend/claude.py` gates all
four provider entry points (sync/async Gemini primary, shared Anthropic
fallback, agentic Anthropic turn) on `is_healthy()`, and
`tests/test_ask.py::TestAskPrimaryAiCircuitBreaker` (8 tests, committed)
pins it at both the route level (double-open-circuit → local-corpus
fallback, neither provider dialed) and the unit level (exact circuit-open
error-dict shape, `gemini_error` prefix preservation through the fallback
leg, record_success/record_failure symmetry).

### 3. "Age gate (13+, EU 16+ or parental consent) enforced at sign-up + stored with consent; AGE_APPROPRIATE_DIRECTIVE + safety classifier + post-generation output check live and tested in en/he."

**Status: ⚠️ Partial — the age-appropriate output layer is done; the age *gate* is not what this line describes.**

The output-safety half is solid: `AGE_APPROPRIATE_DIRECTIVE` +
`NO_IMPERSONATION_DIRECTIVE` (`backend/claude.py:991-999`) are hoisted into
both `CORE_SYSTEM_PROMPT` and `SIMPLE_SYSTEM_PROMPT`, so every answer on
every model carries them; the safety classifier and post-generation checks
are the same mechanism verified in line 2. Documented in
[`docs/AGE_AND_SAFETY_POLICY.md`](AGE_AND_SAFETY_POLICY.md). Bilingual
behavior (en/he) is covered by the safety-classifier regression fixtures.

**The gap, stated plainly:** there is no age gate. `docs/AGE_AND_SAFETY_POLICY.md`
("Age notice at sign-up") documents this directly — the app ships a
persistent, non-blocking footer notice ("By using this site you confirm
you are at least 13 years old...") with no checkbox, no modal, and nothing
that blocks interaction pending agreement. **No age or date of birth is
collected, and no attestation is persisted.** This is a deliberate product
decision (the same 2026-08-16 change that turned the ToS/Privacy consent
flow from clickwrap into browsewrap, see line 1), but it means this
checklist line's literal wording — "enforced at sign-up + stored with
consent" — is not true today. If a real age gate is required before
launch (as opposed to the current browsewrap notice being judged
sufficient), that is a product/legal decision for the attorney review in
line 1, not an unfinished engineering task.

### 4. "Security: secrets clean + rotated if needed, strict RLS enforced + audited, CSP hardened, SRI/pinned deps, secret-scanning in CI, auth review done, pen-test/security-review complete."

**Status: ⚠️ Partial — several sub-items done, one is a real open item
(RLS closed 2026-09-04; see "Update" below).**

Done: CSP hardening (`Strict-Transport-Security`, `Permissions-Policy`,
nonce/`'unsafe-eval'` work — see [`docs/SECURITY.md`](SECURITY.md) §3),
SRI on CDN scripts, secret-scanning in CI (`gitleaks`/`bandit` via
pre-commit, `.github/workflows/ci.yml`), the AuthN/Z review of
`_verify_clerk_token`/session-cookie policy/IDOR (`docs/SECURITY.md` §6).
An internal engineering security review is written up in full in
`docs/SECURITY.md`.

**One thing this line asks for is not done as an external review; the RLS
item below it was open as of 2026-08-20 but has since closed:**

- **"Secrets clean + rotated if needed" — ✅ rotated, one optional item left.**
  `docs/SECURITY.md` §1 documents a live Google/Gemini API key that was
  committed to git history across 3 commits. It has been removed from the
  working tree and `.gitignore`d, and **the key itself has been rotated —
  confirmed by the operator, 2026-09-02 (Google Cloud Console).** The old
  key value is dead and cannot be used against the Gemini API. The only
  remaining sub-item is the git-history-rewrite decision (purging the old
  commits via `git filter-repo`/BFG + force-push) — this stays optional,
  non-urgent cleanup now that the key is dead, not a launch blocker.
- **"Strict RLS enforced + audited" — ✅ Done, empirically confirmed live
  2026-09-04.** `STRICT_SUPABASE_RLS` is a hardcoded `True`,
  `/api/devtools/rls-audit` covers every user-scoped table including
  `ask_history`, and there's a regression test pinning `strict_rls: True`.
  The premise this line used to flag as unverified — whether `auth.uid()`
  actually resolves a Clerk JWT in production — is now resolved, through
  a chain of three dated fixes on top of the original 2026-08-24 dashboard
  confirmation:
  - **2026-08-24:** Supabase-side Third-Party Auth for Clerk confirmed
    enabled (`docs/SECURITY.md` §2, `plan.md` §21.2.1).
  - **2026-09-03** (`plan.md` §49.4): the *Clerk-side* half of the
    integration — the dashboard's "Enable the Supabase integration"
    toggle, which is what actually injects claims into session tokens —
    was found **Disabled** on Production; the 2026-08-24 check above had
    only verified the Supabase side. Activated; confirmed live.
  - **2026-09-03, corrected 2026-09-04** (`plan.md` §49.5): Clerk's own
    integration toggle turned out to inject a `role` claim only, never an
    `aud` claim at all — a gap that would have silently reintroduced the
    exact "Token is missing the 'aud' claim" failure the moment
    `CLERK_AUDIENCE=authenticated` took effect on a new deploy. Fixed by
    manually adding `aud` (plus `email`/`app_metadata`/`user_metadata`) to
    Clerk's Claims template for the Production session token, matching
    Supabase's own native GoTrue token shape.
  - **Verified live, 2026-09-04** (`plan.md` §49.5, §21): after both fixes
    — and after clearing one stale sentinel row left over from an earlier
    test run — the operator re-ran `scripts/verify_rls.py`: Layer 1's
    positive *and* negative cross-user checks passed on all three tables
    (`user_preferences`, `study_bookmarks`, `user_memories`), and Layer
    2's token diagnostic read `aud='authenticated'` (previously
    `aud=None`). Quoted directly: **"RLS is enforcing cross-user isolation
    on every checked table."** This closes `plan.md` §21 (🟢 RESOLVED
    2026-09-04) and `claude_code_prompts.md` Prompt 34 (✅ RESOLVED
    2026-09-04 — supersedes the "Not started" status this line previously
    carried; see also Prompt 63's write-up of §49.4/§49.5).
  - `docs/SECURITY.md` §2 carries the same empirically-confirmed result
    (refreshed in the same pass as this line).
- **"Pen-test/security-review complete" — not done as an external
  review.** `docs/SECURITY.md` says outright in its own header that it "is
  an engineering document, not a substitute for the penetration test /
  external security review §8.C.7 also calls for."

### 5. "Privacy ops: DSR + account-deletion flow working, DPAs executed, retention job running, breach plan documented."

**Status: ⚠️ Partial.**

Done: `GET /api/user/data-export` and `POST /api/user/delete-account`
(`backend/routes_privacy.py:114,196`) both work and are tested
(`tests/test_routes_privacy.py`, 30 tests as of this session, including a
new one added this session pinning that a missing `CLERK_SECRET_KEY` now
reaches `_capture_backend_error` instead of failing silently). The breach
response plan is written ([`docs/PRIVACY_OPERATIONS.md`](PRIVACY_OPERATIONS.md)
§6, GDPR 72h timeline + notification templates).

**Two sub-items are not done:**

- **"DPAs executed" — not done, and `docs/PRIVACY_OPERATIONS.md` §3 says
  so directly.** Every processor row in its DPA checklist (Clerk,
  Supabase, Vercel, Google, Anthropic, Sentry) is marked "⏸ Not yet
  executed — action item." This is explicitly a repo-owner action, not an
  engineering task.
- **"Retention job running" — ✅ `CRON_SECRET` confirmed set in production
  (operator-attested, 2026-09-06).** `GET /api/devtools/retention-enforce`
  (`backend/routes_privacy.py:272`) enforces 90-day windows on
  `ask_history` and `ai_usage_log`, and `vercel.json` (tracked in git,
  though it currently carries uncommitted local edits alongside these
  entries) has a `"0 14 * * *"` cron pointed at it. `CRON_SECRET` ships
  **empty** in `.env.example` by design (it's a placeholder file, not the
  real config) — the operator has confirmed the real value is set in the
  live Vercel environment, so the deployed cron authenticates. This
  confirmation is operator-attested, not independently verifiable from
  this repo (no live dashboard/console access from an engineering pass).

Two caveats already documented (not gaps): both DSR endpoints deliberately
bypass RLS via the service-role client — `docs/PRIVACY_OPERATIONS.md`
notes this is the same finding as line 4's RLS item, not a separate bug —
and consent records (`legal_accepted`/version columns) are deleted along
with the account on deletion, by design.

### 6. "Observability: Sentry + structured logs + cost metering + budget cap + uptime alerts live; circuit breakers wired; fallback path verified."

**Status: ⚠️ Partial — uptime alerts are the one remaining open item;
budget cap, the multi-account decision, and the WAF/global breaker have
since shipped (see "Update 2026-09-06" below).**

Done: Sentry initialized (backend + browser SDK), structured request-ID
logging across Flask + asyncio, cost metering on every model call with
correct per-model pricing (`claude_code_prompts.md` Prompt 33a), an atomic
per-user budget ceiling that actually blocks at the database layer
(`scripts/sql/check_and_reserve_user_budget.sql`, Prompt 33b — a
20-concurrent-caller test went from 20/20 allowed pre-fix to 1/20
allowed post-fix). Circuit breakers: `sefaria`/`web`/`translate_*` were
already wired (Phase 3); this session's B2 track closed the long-standing
`hebcal` gap across all four call sites
(`backend/zmanim_engine.py`, `backend/search.py`,
`backend/calendar_service.py`, `backend/routes_calendar.py`), with 16 new
tests plus the fallback-path degradation test named in line 2.

**One sub-item is not done; the other three were open as of 2026-08-20 and
have since closed:**

- **Uptime alerts — still not configured.** [`docs/RUNBOOKS.md`](RUNBOOKS.md)
  ("Uptime monitoring") specifies the intended two-tier check (a 5-minute
  static-asset check + a 15–30 minute `/api/health` check, with the
  caveat that `/api/health` is Clerk-gated so an anonymous monitor must be
  configured to treat `401` as healthy) but states outright: "**Status:
  not yet configured by the operator.**" No monitor is wired up. This
  requires creating a third-party monitoring account, which is an operator
  action, not an engineering one.
- **Global budget cap — ✅ done, live since 2026-09-01.** `DAILY_BUDGET_USD`
  is set to `10.00` in `.env.example` and was confirmed live in production
  Vercel (`vercel env pull --environment=production`, re-confirmed
  2026-09-04) — see `plan.md` §20. The `budget-check` cron
  (`vercel.json`, `"0 13 * * *"`) now actually sums the day's
  `ai_usage_log` spend and alerts through `_capture_backend_error` when it
  reaches the cap; it is no longer a no-op. **2026-09-06 hardening:** the
  no-op code path (for a future misconfiguration, e.g. the env var getting
  unset again) is no longer silent — `backend/cost_meter.py` now logs a
  `logger.warning` at both no-op sites, and `/api/stack/health`'s
  `security.cost_breaker` field reports `configured: false` with an
  explanatory note, mirroring the existing `limiter_store` gap-visibility
  pattern (this is exactly the failure mode that let the var ship empty
  undetected the first time).
- **The multi-account budget-bypass question — ✅ decided 2026-08-23.**
  `plan.md` §20.2 PHASE 20c / `claude_code_prompts.md` Prompt 33c's Option
  A ("accept and document") was chosen. That decision was conditional on
  the WAF + global cost-breaker work below shipping first — it has (see
  next item), so the decision is no longer aspirational.
- **The edge WAF + global cost breaker — ✅ shipped.** The global cost
  breaker (`backend/cost_meter.py::is_global_cost_breaker_tripped()`) is
  wired into the live `/ask` path (`asgi.py`, imported and invoked inside
  `ask_async()`), serving a cached answer or a calm paused-state payload
  when tripped — shipped 2026-08-22 (`plan.md` §16 Phase 9b,
  `claude_code_prompts.md` Prompt 29b). Vercel WAF L1 rules (3 custom
  firewall rules, including the `/ask` rate-limit rule) were entered into
  the dashboard by the operator on 2026-08-26 (`docs/SECURITY.md` §7).

The B2-track note previously here — that `health.is_healthy('claude')`/
`('gemini')` is never consulted before the *primary* `/ask` AI call — was
itself stale: see the "Update 2026-09-06" note on line 2 above. Both the
primary and fallback call sites have been circuit-breaker-gated since
2026-08-20 (Prompt 39).

### 7. "Quality: test coverage gate passing, WCAG AA audited, accessibility statement published."

**Status: ✅ Done.**

The coverage gate is enforced in CI at `--cov-fail-under=85`
(`pytest.ini`), with actual coverage at 91.39–91.43% depending on the
run. The accessibility statement is published
([`templates/accessibility.html`](../templates/accessibility.html)). The
automated WCAG 2.1 AA gate now covers **both** themes: CI's "Accessibility
scan" step (`.github/workflows/ci.yml`) runs `npm run test:a11y`, which
chains `test:a11y:light` (`pa11y-ci --config .pa11yci.json`, against all
8 registered pages) and `test:a11y:dark` (`node scripts/a11y_dark_scan.js`,
`plan.md` §26.2). The dark-theme half seeds localStorage's `Sh'elahPrefs`
key via Puppeteer's `page.evaluateOnNewDocument` before navigation,
asserts `document.documentElement.getAttribute('data-theme') === 'dark'`,
then runs the same `urls`/`defaults` from `.pa11yci.json` through pa11y's
Node API directly — necessary because pa11y-ci's own `actions` grammar
has no JS-evaluation step to seed a theme preference declaratively, so a
second `.pa11yci.json`-style config file could not do this on its own.
16 checks total (8 pages × 2 themes); see
[`docs/ACCESSIBILITY_AUDIT.md`](ACCESSIBILITY_AUDIT.md#automated-coverage-ci)
for the full mechanism and fix history (`plan.md` §26.2). A separate
manual color-contrast audit in the same document covers both themes'
CSS tokens directly.

**Commit-status caveat (2026-09-06):** `.pa11yci.json`,
`scripts/a11y_dark_scan.js`, and `docs/ACCESSIBILITY_AUDIT.md` are
committed. The `package.json` `test:a11y*` scripts and
`.github/workflows/ci.yml`'s "Accessibility scan" step that actually
invoke them are still uncommitted, entangled with a larger unrelated
CI-hardening pass (Node test-runner wiring, `pip-audit`, hash-pinned
dependency installs) awaiting its own commit — so the gate described
above is proven correct locally but not yet running in CI on `main`.

### 8. "Business: entity + insurance in place, trademark cleared."

**Status: ⚠️ Decided, not "in place" — entity and insurance are both
operator decisions now made, not open questions.**

No engineering pass could resolve this line — it's entirely outside what
code, tests, or documentation can close — but the operator has since made
both calls: **entity — registering under the parent as an individual, not
an LLC** (2026-09-04, too complex to stand up for a non-revenue site), and
**insurance — declined** (2026-09-06, too costly and complex for a free,
non-revenue site). Neither line item is something a policy or a
certificate of formation now makes "in place"; both are accepted-risk
decisions. Trademark clearance remains untouched. See "Business &
compliance — needs human counsel" below (plan.md §8.G item 1) for the
full reasoning and its consequences.

### 9. "Backups + restore tested; rollback runbook validated on a preview deploy."

**Status: ⚠️ Partial — documented, not executed.**

`docs/RUNBOOKS.md` documents both procedures in concrete detail:
"Backups & recovery" (manual `pg_dump`/`supabase db dump` backup and
restore commands) and "Rollback procedure" (Vercel dashboard
Promote-to-Production as the lead path, `vercel rollback`/`promote` CLI
as fallback — chosen because this repo has no `.vercel/` link and CLI
availability in the deploy environment isn't guaranteed).

**What "tested"/"validated" would require, and hasn't happened:**

- **Backup restore has not actually been performed once.** Separately,
  Supabase's own backup/PITR guarantee for this project is now confirmed
  (2026-09-01, Akiva) — **Free plan, no PITR (a paid Pro-plan add-on) and
  no automated backups at all.** See `docs/RUNBOOKS.md`'s "Backups &
  recovery" section: the only backup this project has is a manual
  `pg_dump`, run on no schedule today. This is a real gap, not just an
  unconfirmed one.
  `scripts/migrate_customs_to_supabase.py` (the one Supabase-writing
  script whose safety matters for backup strategy) has been verified
  *re-runnable* (deterministic sha256 row IDs + upsert), but that is not
  the same claim as "a restore was tested."
- **The rollback runbook has not been validated on an actual preview
  deploy.** `claude_code_prompts.md` Prompt 24's status is explicit about
  why: "CLI not authenticated in this environment, so the actual
  preview-deploy verification hasn't run." The runbook's steps are
  written from Vercel's documented dashboard behavior, not from having
  exercised them against this project's real deployment.

---

## Business & compliance — needs human counsel (plan.md §8.G)

These five items are structural to launching a real product and cannot be
resolved by an engineering pass — they need a human decision-maker
(attorney, accountant, insurance broker), not more code. This section
explains **why each one matters**, matching `plan.md` §8.G's own
ordering; it is documentation only, not legal or business advice, and
none of it should be read as a recommendation of what to actually decide.

### 1. Entity & insurance

**Why it matters:** right now, Sh'elah appears to be operated by an
individual, not a liability-shielded entity (LLC/corporation). Every
contractual disclaimer in `templates/terms.html` (assumption of risk,
"AS IS," damages cap) is a *contractual* limitation — it reduces what a
user can successfully sue over, but it does not by itself protect the
operator's personal assets the way operating through an entity does.
Tech E&O, general liability, and cyber insurance are the practical
backstop behind those same disclaimers if a claim gets through anyway.
`plan.md` §8.G.1 calls this "arguably the single most effective 'don't
get sued into personal bankruptcy' step" and says to discuss it with
counsel — that framing is `plan.md`'s own, reproduced here rather than
restated as independent advice.

**Operator decision, 2026-09-06 (Akiva): declining insurance.** Tech
E&O / general liability / cyber insurance is not being purchased — too
costly and complex to justify for a free, non-revenue site, mirroring the
2026-09-04 decision to skip an LLC. This means the disclaimers in
`terms.html` (assumption of risk, AS-IS, damages cap) are now the *only*
backstop — there is neither an entity shield nor an insurance policy
behind them. This is a documented accepted-risk decision, not an
oversight; it has not been reviewed by counsel (attorney review is
declined project-wide, per `akiva_tasks.md` T14).

### 2. Trademark/name clearance

**Why it matters:** "Sh'elah" and its logo have not been cleared against
existing trademarks, and the domain/brand identity built around that name
hasn't been legally confirmed as available to use. Building further brand
equity (marketing, App Store listings, press) before this is cleared
raises the cost of a forced rename later.

### 3. Accessibility/consumer-protection posture for launch markets

**Why it matters:** WCAG 2.1 AA (line 7 above) is the *technical*
accessibility standard this project targets, but the *legal* posture
needed for specific launch markets — US ADA applicability to a
web/PWA product, the EU's Accessibility Act (in force from 2025) if
serving EU users — is a separate question with jurisdiction-specific
answers that a technical audit doesn't resolve on its own.

### 4. AI-specific regulatory regimes

**Why it matters:** the EU AI Act's transparency obligations (users must
know they're interacting with an AI system, roughly what `ai-disclosure.html`
already aims at) and any emerging US state-level AI-disclosure laws are a
moving regulatory target. `docs/AGE_AND_SAFETY_POLICY.md` and
`templates/ai-disclosure.html` implement the product-level disclosure
`plan.md` currently specifies, but "does this satisfy every applicable
regime in every market this app is used from" is a legal question that
needs periodic re-review as those laws change, not a one-time engineering
checkbox.

### 5. Payment/commerce triggers

**Why it matters:** Sh'elah does not currently monetize — no payments,
subscriptions, or donations flow through the app. `plan.md` §8.G.5 flags
that the moment that changes, it triggers an entirely separate
compliance stack: a refund policy, billing terms, PCI-DSS scope (even if
narrowed by using a compliant processor), and sales-tax/VAT handling.
None of that exists today, correctly, because nothing in the product
needs it yet — but it should not be added informally the day monetization
is first discussed; it needs the same counsel/accountant involvement as
the other four items here.

---

## Cross-references

- Full prompt-by-prompt evidence trail: `claude_code_prompts.md`, rows
  for Prompts 12–19, 29a/29b/29c, 33a/33b/33c, 34–37.
- Design source for every line above: `plan.md` §8 (A–H), §16 (rate
  limiting/WAF), §20 (cost-ledger integrity, §20c multi-account bypass),
  §21 (RLS verification).
- Related docs cited throughout: [`docs/SECURITY.md`](SECURITY.md),
  [`docs/PRIVACY_OPERATIONS.md`](PRIVACY_OPERATIONS.md),
  [`docs/DPIA.md`](DPIA.md), [`docs/RUNBOOKS.md`](RUNBOOKS.md),
  [`docs/AGE_AND_SAFETY_POLICY.md`](AGE_AND_SAFETY_POLICY.md),
  [`docs/CONTENT_QA.md`](CONTENT_QA.md),
  [`docs/ACCESSIBILITY_AUDIT.md`](ACCESSIBILITY_AUDIT.md).
