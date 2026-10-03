# Launch Checklist

**Status:** living checklist, last re-verified against the repository on
2026-10-02. It has nine launch-gate lines, each with the file, test or
document that proves it, or the honest gap where it isn't proven. It is
an engineering-authored status snapshot, not a legal or business sign-off;
the "Business & compliance" section at the end covers the items that no
engineering pass can close.

**How to read the status column:**

| Status | Meaning |
|---|---|
| ✅ Done | Implemented, tested, and verified against the current repo state |
| ✅ Done — operator-attested | Engineering work is complete; the remaining fact is a dashboard or account setting only the operator can see, recorded with its date |
| ⚠️ Partial | Some of the line is done; the rest is named specifically |
| ❌ Not started | No work has begun on this line |

Re-verify before relying on this table for a real launch decision; it ages
as soon as the next commit lands.

**Snapshot of the repository at the time of writing:**

| Check | Result |
|---|---|
| Backend tests (`pytest`) | 4,700 passed |
| Backend coverage (`--cov=backend`) | 99.8%, gate at `--cov-fail-under=85` (`pytest.ini`) |
| Frontend tests (`npm test`, `node --test tests_js/*.test.js`) | 31 files, 628 tests |
| Accessibility scan (`npm run test:a11y`) | 8 pages × 2 themes = 16 checks, runs in CI |
| Vercel deployment | single region `iad1`, 2 daily crons (`budget-check` 13:00 UTC, `retention-enforce` 14:00 UTC) |

---

## The checklist

### 1. Legal documents: drafted, versioned, dated, notice given on every page

**Status: ✅ Done.**

All seven documents exist, are dated and versioned, and are cross-linked
from a persistent footer notice on every page:
[`templates/terms.html`](../templates/terms.html),
[`templates/privacy.html`](../templates/privacy.html),
[`templates/ai-disclosure.html`](../templates/ai-disclosure.html),
[`templates/acceptable-use.html`](../templates/acceptable-use.html),
[`templates/dmca.html`](../templates/dmca.html),
[`templates/accessibility.html`](../templates/accessibility.html),
[`templates/licenses.html`](../templates/licenses.html), plus
[`THIRD_PARTY_LICENSES.md`](../THIRD_PARTY_LICENSES.md). They are served by
`backend/routes_legal.py`; cross-link coverage is enforced by
`tests/test_routes_legal.py`. Remaining `[Placeholder]` markers sit on
clauses that are genuinely undecided (damages-cap figure, arbitration/venue
choice, transfer-mechanism confirmation, EU representative/DPO
determination).

**Operator decision, 2026-09-05 (Akiva): attorney review is declined
outright**, not deferred. It is too complex and costly to justify for a
free, non-revenue site. The "DRAFT — pending attorney review" banners were
removed from all five gated pages (commit `054b7d5`). The DMCA
designated-agent registration is a separate administrative filing the
operator is still handling with their parent; it is not a substantive legal
review.

**Notice mechanism: browsewrap, decided 2026-09-04.** A persistent footer,
not a click-through gate. `/api/accept-legal` and the version-tracking
database columns still exist and are tested, but nothing in the UI calls
them; they stay dormant by design.

### 2. Persistent AI-answer disclaimer live; safety routing verified

**Status: ✅ Done.**

`meta.safety_class` and `meta.rabbinic_disclaimer` are returned on every
`/ask` response shape on both the Flask and FastAPI transports.
`templates/index.html` renders a persistent, non-dismissible
`#aiDisclaimerBanner` / `#readerDisclaimerBanner` on every answer, with a
visually distinct variant (icon and colour, never colour alone) for the
`medical`, `mental_health_or_self_harm`, `abuse_or_minor_safety` and
`dangerous_or_illegal` classes. `classify_safety()` in `backend/claude.py`
and the referral text set route those classes to a referral, never a
"ruling." Tests: `tests/test_ask.py::TestSafetyClassMetaPropagation`
(meta parity across both transports for ok, medical and domain-refusal) and
`tests/test_rag.py::TestStoreAskHistory`.

Degradation paths are tested end to end. With every circuit breaker forced
open, `POST /ask` still returns a well-formed 200 with a non-empty answer,
sources and `meta.fallback=true` on both transports
(`tests/test_ask.py::TestAskDegradationPath`). The primary AI call is
gated too, not just the fallback: `backend/claude.py` consults
`is_healthy()` before all four provider entry points (sync and async Gemini
primary, shared Anthropic fallback, agentic Anthropic turn), shipped
2026-08-20 and pinned by `tests/test_ask.py::TestAskPrimaryAiCircuitBreaker`
(8 tests).

### 3. Age handling and output safety

**Status: ⚠️ Partial — the age-appropriate output layer is done; an age *gate* does not exist.**

Output safety is solid. `AGE_APPROPRIATE_DIRECTIVE` and
`NO_IMPERSONATION_DIRECTIVE` are hoisted into both `CORE_SYSTEM_PROMPT` and
`SIMPLE_SYSTEM_PROMPT` in `backend/claude.py`, so every answer on every
model carries them. The safety classifier and post-generation checks are
the mechanism verified in line 2. Behaviour in English and Hebrew is
covered by the safety-classifier regression fixtures. See
[`docs/AGE_AND_SAFETY_POLICY.md`](AGE_AND_SAFETY_POLICY.md).

**The gap, stated plainly:** there is no age gate. The app ships a
persistent, non-blocking footer notice ("By using this site you confirm you
are at least 13 years old…") with no checkbox, no modal, and nothing that
blocks interaction pending agreement. **No age or date of birth is
collected and no attestation is persisted.** This is a deliberate product
decision, made with the browsewrap decision in line 1. If a real age gate
is required before launch, that is a product/legal decision, not an
unfinished engineering task.

### 4. Security: secrets, RLS, CSP, supply chain, auth

**Status: ⚠️ Partial — the engineering items are done; an external penetration test has not been done.**

The full engineering review is [`docs/SECURITY.md`](SECURITY.md). Summary:

- **Secrets — ✅ rotated, history rewritten.** A Gemini API key committed in
  three early commits was removed from the working tree and rotated
  (operator-confirmed 2026-09-02, Google Cloud Console), and the repository
  history was rewritten on 2026-09-18 to purge it. Two residual exposures
  remain open and are listed in `docs/SECURITY.md` §13: GitHub's hidden
  `refs/pull/*/head` refs and by-hash serving of old commits, both closable
  only through a GitHub Support request.
- **Strict RLS — ✅ Done, empirically confirmed live 2026-09-04** and
  reconfirmed 2026-09-16. `STRICT_SUPABASE_RLS` is a hardcoded `True`,
  `/api/devtools/rls-audit` covers every user-scoped table, and a
  regression test pins `strict_rls: True`. The confirmation took three dated
  fixes: Supabase-side Third-Party Auth for Clerk (2026-08-24); the
  Clerk-side Supabase integration toggle, found disabled on Production
  (2026-09-03); and a manually added `aud` claim in Clerk's session-token
  template, because the integration toggle injects only `role`
  (2026-09-03, corrected 2026-09-04). After those, `scripts/verify_rls.py`
  passed its positive and negative cross-user checks on `user_preferences`,
  `study_bookmarks` and `user_memories`, and the token diagnostic read
  `aud='authenticated'`.
- **CSP, SRI, pinned dependencies, secret scanning — ✅ Done.** See
  `docs/SECURITY.md` §3 and §4. `'unsafe-inline'` remains in the script and
  style policies (inline `onclick` and `style=` attributes) and is tracked
  as an open item.
- **Authentication and authorization review — ✅ Done**
  (`docs/SECURITY.md` §6). `CLERK_AUDIENCE` is set in production.
- **Auth enforcement on the live site — ⚠️ operator check.**
  `CLERK_ENFORCE_AUTH` defaults to `true` on Vercel, so the "Optional" routes
  (including `/ask`) require sign-in there, but the deployed value is not
  recorded in the repository and `.env.example` ships `false` for local use.
  The one-command check is in `docs/SECURITY.md` §13.
- **Penetration test / external security review — not done.** The engineering
  review says outright that it is not a substitute for one.

### 5. Privacy operations: DSR, account deletion, DPAs, retention, breach plan

**Status: ✅ Done.**

- `GET /api/user/data-export` and `POST /api/user/delete-account`
  (`backend/routes_privacy.py`) both work and are tested
  (`tests/test_routes_privacy.py`).
- The breach response plan is written, with a 72-hour GDPR timeline and
  notification templates
  ([`docs/PRIVACY_OPERATIONS.md`](PRIVACY_OPERATIONS.md) §6).
- **DPAs — ✅ executed, operator-confirmed 2026-09-16** for all six
  processors (Clerk, Supabase, Vercel, Google, Anthropic, Sentry);
  see `docs/PRIVACY_OPERATIONS.md` §3.
- **Retention job — ✅ running, `CRON_SECRET` confirmed set in production
  (operator-attested, 2026-09-06).** `GET /api/devtools/retention-enforce`
  enforces 90-day windows on `ask_history` and `ai_usage_log`, and
  `vercel.json` schedules it daily at 14:00 UTC. `CRON_SECRET` ships empty
  in `.env.example` by design.

Two documented caveats, not gaps: both DSR endpoints deliberately use the
service-role client and bypass RLS (they authenticate the user themselves),
and consent records are deleted with the account.

### 6. Observability: Sentry, structured logs, cost metering, budget cap, uptime alerts, circuit breakers

**Status: ⚠️ Partial — uptime alerts are the one remaining open item.**

Done:

- Sentry is initialised in the backend and the browser, and structured
  request-ID logging runs across Flask and asyncio
  ([`docs/OBSERVABILITY.md`](OBSERVABILITY.md)).
- Cost metering runs on every model call with per-model pricing, and an
  atomic per-user daily budget blocks at the database layer
  (`scripts/sql/check_and_reserve_user_budget.sql`; a 20-concurrent-caller
  test went from 20 of 20 allowed before the fix to 1 of 20 after).
- **Global budget cap — ✅ live since 2026-09-01.** `DAILY_BUDGET_USD` is
  `10.00` in production (reconfirmed 2026-09-04). The `budget-check` cron
  sums the day's `ai_usage_log` spend and alerts when it reaches the cap.
  A misconfigured or unset value is no longer silent:
  `backend/cost_meter.py` logs a warning at both no-op sites and
  `/api/stack/health` reports `security.cost_breaker.configured: false`.
- **Global cost breaker — ✅ shipped 2026-08-22.**
  `is_global_cost_breaker_tripped()` is consulted inside `ask_async()` and
  serves a cached answer or a calm paused-state payload when tripped.
- **Multi-account budget bypass — decided 2026-08-23:** accept and document,
  conditional on the WAF and global breaker above (both shipped).
- **Edge WAF — ✅ entered 2026-08-26** (3 custom rules including the `/ask`
  rate limit). The rate-limit rule is in Enforce mode at 100 requests per
  minute per IP+JA4, confirmed live 2026-09-16 (`docs/SECURITY.md` §7).
- **Circuit breakers** are wired for Sefaria, web fetches, translation and
  Hebcal (all four Hebcal call sites), and for both AI providers.

**Not done — uptime alerts.** No monitor is configured.
[`docs/RUNBOOKS.md`](RUNBOOKS.md) ("Uptime monitoring") specifies the
intended two-tier check: a 5-minute check on a static asset, and a
15–30 minute check on a function-backed endpoint, with the caveat that
`/api/health` is Clerk-gated so an anonymous monitor must treat `401` as
healthy. Creating the monitoring account is an operator action.

### 7. Quality: coverage gate, WCAG AA audit, accessibility statement

**Status: ✅ Done.**

- **Coverage:** CI enforces `--cov-fail-under=85`; actual backend coverage is
  99.8% (4,700 tests).
- **Accessibility statement:** published at
  [`templates/accessibility.html`](../templates/accessibility.html).
- **Automated WCAG 2.1 AA gate, both themes:** CI's "Accessibility scan" step
  (`.github/workflows/ci.yml`) runs `npm run test:a11y`, which chains
  `test:a11y:light` (`pa11y-ci --config .pa11yci.json`) and
  `test:a11y:dark` (`node scripts/a11y_dark_scan.js`) against all 8
  registered pages: 16 checks. The dark half seeds the theme preference
  through Puppeteer before navigation and asserts
  `data-theme="dark"` before scanning, because pa11y-ci's own `actions`
  grammar cannot evaluate JavaScript. See
  [`docs/ACCESSIBILITY_AUDIT.md`](ACCESSIBILITY_AUDIT.md) for the mechanism,
  the manual colour-contrast audit of both themes' CSS tokens, and the fix
  history.

### 8. Business: entity, insurance, trademark

**Status: ⚠️ Decided, not "in place" — all three are accepted-risk decisions.**

No engineering pass can resolve this line. The operator has made all three
calls: **entity — operating as an individual, not an LLC** (2026-09-04),
**insurance — declined** (2026-09-06), and **trademark/name clearance —
declined** (2026-09-16), each because the cost and complexity are
disproportionate for a free, non-revenue site. See "Business & compliance"
below for the consequences of each.

### 9. Backups, restore, rollback

**Status: ⚠️ Partial — documented, not executed.**

[`docs/RUNBOOKS.md`](RUNBOOKS.md) documents both procedures: "Backups &
recovery" (manual `pg_dump` / `supabase db dump` and restore commands) and
"Rollback procedure" (Vercel dashboard Promote-to-Production first,
`vercel rollback` / `promote` CLI as the fallback).

What "tested" would require, and hasn't happened:

- **No backup restore has ever been performed.** The Supabase project is on
  the Free plan: the Database → Backups tabs (Scheduled backups, Point in
  Time, Restore to new project) are all gated behind a Pro upgrade,
  confirmed 2026-09-01 and re-confirmed 2026-09-16. There are no automated
  backups, no PITR, and so no restore point to test against. The only
  backup is a manual `pg_dump` run on no schedule; a dry-run of that path
  against a scratch database has not been done either.
- **The rollback runbook has not been validated end to end.** A read-only
  look at the live Vercel dashboard on 2026-09-16 confirmed the mechanism
  is real: every past deployment's menu offers Instant Rollback and
  Promote, and the history shows earlier redeploys. Actually rolling back
  or promoting a production deployment was deliberately not triggered
  without the operator's explicit go-ahead.

---

## Business & compliance — needs human counsel

These five items are structural to launching a real product and cannot be
resolved by an engineering pass. This section explains why each one
matters; it is documentation only, not legal or business advice.

### 1. Entity & insurance

**Why it matters:** Sh'elah is operated by an individual, not a
liability-shielded entity. Every contractual disclaimer in
`templates/terms.html` (assumption of risk, "AS IS," damages cap) reduces
what a user can successfully sue over, but does not by itself protect the
operator's personal assets the way an entity does. Tech E&O, general
liability and cyber insurance are the practical backstop behind those
disclaimers.

**Operator decisions:** no LLC (2026-09-04) and no insurance (2026-09-06).
The disclaimers are therefore the *only* backstop. This is a documented
accepted-risk decision, not an oversight, and it has not been reviewed by
counsel (attorney review is declined project-wide; see line 1).

### 2. Trademark and name clearance

**Why it matters:** "Sh'elah" and its logo have not been cleared against
existing trademarks. Building brand equity (marketing, app-store listings,
press) before clearance raises the cost of a forced rename later.

**Operator decision, 2026-09-16:** no formal clearance search and no
registration. The residual risk is a forced rename if a conflicting mark
surfaces after the fact.

### 3. Accessibility and consumer-protection posture for launch markets

**Why it matters:** WCAG 2.1 AA (line 7) is the technical standard this
project targets, but the legal posture for specific markets (US ADA
applicability to a web/PWA product, the EU Accessibility Act when serving EU
users) has jurisdiction-specific answers that a technical audit does not
resolve.

### 4. AI-specific regulatory regimes

**Why it matters:** the EU AI Act's transparency obligations and emerging US
state-level AI-disclosure laws are a moving target.
`docs/AGE_AND_SAFETY_POLICY.md` and `templates/ai-disclosure.html`
implement the product-level disclosure, but whether that satisfies every
applicable regime in every market is a legal question that needs periodic
re-review.

### 5. Payment and commerce triggers

**Why it matters:** Sh'elah does not monetise: no payments, subscriptions
or donations flow through the app. The moment that changes it triggers a
separate compliance stack (refund policy, billing terms, PCI-DSS scope,
sales-tax/VAT handling) that does not exist today because nothing needs it
yet. It should not be added informally.

---

## Related documents

[`docs/SECURITY.md`](SECURITY.md),
[`docs/PRIVACY_OPERATIONS.md`](PRIVACY_OPERATIONS.md),
[`docs/DPIA.md`](DPIA.md), [`docs/RUNBOOKS.md`](RUNBOOKS.md),
[`docs/OBSERVABILITY.md`](OBSERVABILITY.md),
[`docs/AGE_AND_SAFETY_POLICY.md`](AGE_AND_SAFETY_POLICY.md),
[`docs/CONTENT_QA.md`](CONTENT_QA.md),
[`docs/ACCESSIBILITY_AUDIT.md`](ACCESSIBILITY_AUDIT.md),
[`DECISIONS.md`](../DECISIONS.md).
