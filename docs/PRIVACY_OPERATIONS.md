# Privacy Operations (plan.md §8.D)

**Status:** privacy-operations pass, implemented 2026-08-17. This document
is the DSR procedure, DPA checklist, Records of Processing table, and
breach response plan that plan.md §8.D.1/§8.D.3/§8.D.4/§8.D.6 call for. The
basic DPIA plan.md §8.D.4 also calls for lives in `docs/DPIA.md`.

Sh'elah is operated by a solo developer (Akiva Yevdayev), not a company
with a dedicated privacy team — every "who does this" answer below is the
same person, contactable at **akiva.yevda@gmail.com**.

See [`docs/LAUNCH_CHECKLIST.md`](LAUNCH_CHECKLIST.md) for how this
document's items map onto the plan.md §8.H launch-gate checklist,
including the DPA-execution and retention-cron items that are still open.

## 1. Data-subject request (DSR) flow

### Self-serve (primary path)

Signed-in users can exercise the two highest-volume rights themselves,
without waiting on a manual request:

- **Access / portability** — "Privacy & Data" in the account menu →
  "Download my data" calls `GET /api/user/data-export`
  (`backend/routes_privacy.py`), which returns a JSON bundle of every row
  the user owns across `user_preferences`, `study_bookmarks`,
  `ask_history`, `user_memories`, `ai_usage_log`, and `answer_feedback`
  (added 2026-08-27, plan.md §39.1 — previously excluded, so a feedback
  row survived account deletion undiscoverable and unreachable). This
  satisfies GDPR Art. 15 (access) and Art. 20 (portability — the export is
  already machine-readable JSON, matching the "JSON/CSV" commitment in
  `privacy.html` §6) in one action.
- **Deletion ("right to be forgotten")** — the same panel's "Delete my
  account" calls `POST /api/user/delete-account`, gated behind typing the
  confirmation phrase `DELETE`. It deletes the user's rows from every table
  listed above, and only if *all* of those deletes succeed does it go on to
  delete the Clerk identity itself via Clerk's Backend API
  (`DELETE /v1/users/{id}`, requires `CLERK_SECRET_KEY` — see
  `.env.example`). If any table delete fails, the Clerk account is
  deliberately left intact (HTTP 207, `clerk_skipped` in the response) so
  the user keeps a working login and can retry rather than being locked out
  with data still undeleted. Both the per-table deletes and the Clerk call
  are individually idempotent, so a retried request after a partial failure
  is safe to resend as-is.
- **Completeness backstop for deletions this app doesn't initiate** (added
  2026-08-27, plan.md §39.2) — `POST /api/webhooks/clerk`
  (`backend/routes_webhooks.py`) listens for Clerk's `user.deleted`
  webhook and runs the same per-table cascade delete above, keyed on the
  event's `data.id`. This exists because `delete_account()` above is not
  the only way a Clerk identity can be removed: a user can also delete
  their own identity through Clerk's hosted `<UserProfile />` UI (embedded
  via `openClerkUserProfile()` in `templates/index.html`), or an operator
  can delete a user directly from the Clerk Dashboard (e.g. handling
  abuse/a takedown request) — neither path ever calls this app's own
  endpoint, so without this listener every table row for that user would
  be orphaned permanently with no Clerk JWT left to authenticate a retry.
  Requires `CLERK_WEBHOOK_SIGNING_SECRET` (see `.env.example`) to be
  configured, both in this app's environment and as the signing secret of
  a matching Endpoint created in Clerk Dashboard → Webhooks, subscribed to
  `user.deleted`, pointed at this route. **Confirmed enabled, 2026-09-02**
  (Akiva, Clerk Dashboard) — self-service account deletion is live for
  this app's Clerk instance. Carries no completeness risk: the webhook
  above already backstops this exact path. See `docs/SECURITY.md` §9 for
  the dated record (plan.md §39.2 STEP 3).

Neither endpoint reaches a table via the RLS-scoped client — see the
module docstring in `backend/routes_privacy.py` for why (the frontend's
Clerk bearer token isn't a Supabase-compatible JWT, so gating a compliance
endpoint behind that would silently fail for every real user). Each query
instead uses the service-role client with an explicit
`.eq("user_id", ...)` filter, which is also the only way to reach
`user_memories` at all — its RLS policy blocks all client-side access by
design (`scripts/sql/rag_identity_cache_setup.sql`).

**What self-serve does not cover** (still requires the manual path below):
correction of inaccurate data, objection/restriction requests, and any
request from someone who cannot sign in (lost access, or acting on behalf
of a minor per `privacy.html` §8).

**Known limitations:**

- **Concurrent-write race window.** `delete_account()` issues one
  `DELETE ... WHERE user_id = ?` per table with no transaction or lock
  against concurrent writers. A request already in flight when deletion
  runs (e.g. an `/api/ask` call finishing, or `cost_meter.py`'s
  fire-and-forget `ai_usage_log` write) can insert a new row for that
  `user_id` after that table's delete already executed. `ask_history` and
  `ai_usage_log` rows in this window are still swept by the 90-day
  retention job below; `user_preferences`, `study_bookmarks`, and
  `user_memories` have no further automated cleanup, so such a row would
  persist until a manual/second deletion request. In practice this
  requires the user to submit another write in the same instant as their
  deletion request, which is a narrow window.
- **Consent record is deleted along with everything else.** Because
  `legal_accepted`/`legal_accepted_at`/`age_attested` live in the same
  `user_preferences` row (see §2 below), deleting the account also erases
  proof that the user ever accepted the Terms/Privacy Policy or attested
  to the age requirement. Some GDPR/CCPA programs deliberately retain a
  minimal "accepted terms X at date Y" record after erasure under the Art.
  17(3)(e) legal-defense carve-out. This project has not implemented that
  carve-out — deletion here is total, matching the plain-language "delete
  my account and data" commitment in `privacy.html` §6 — but it's worth
  revisiting alongside counsel review if a dispute over consent timing
  ever becomes a real risk.

### Manual / email-intake procedure

For anything self-serve doesn't cover, or a user who can't or won't use
the in-app flow:

1. **Intake.** All requests arrive at akiva.yevda@gmail.com (the address
   published in `privacy.html` §6 and §12, and `terms.html`). Treat any
   email containing "delete my data", "access my data", "GDPR", "CCPA", or
   similar language as a DSR regardless of exact wording — data-protection
   law does not require the requester to use a magic phrase.
2. **Verify identity.** Ask the requester to confirm the email address
   associated with their account (Clerk is the source of truth for
   account ↔ email). Do not act on a request where the claimed identity
   can't be reasonably matched to an account — this protects other users
   from a social-engineered deletion/export of *their* data.
3. **Fulfill, by request type:**
   - *Access/portability* — sign in as needed to trigger
     `GET /api/user/data-export` on the requester's behalf (or, once an
     admin/service-role tool exists, query Supabase directly by
     `user_id`), and send the JSON export as an attachment.
   - *Deletion* — trigger `POST /api/user/delete-account` the same way,
     or run the equivalent Supabase deletes + Clerk API call by hand if
     the requester's session isn't available.
   - *Correction* — no self-serve path exists yet (most stored data is
     preferences/bookmarks a user already edits directly, or AI-generated
     Q&A history that isn't "about" the user in the corrigible sense);
     handle by hand via direct Supabase update, scoped to the specific
     field named in the request.
   - *Objection/restriction* — since Sh'elah's only processing bases are
     consent, contract necessity, and legal obligation (`privacy.html`
     §2 table), an objection request is effectively a deletion request;
     handle it as one unless the requester specifies a narrower scope.
4. **Respond within 30 days** (the commitment in `privacy.html` §6), or
   sooner if a specific law requires it (e.g. CCPA's shorter windows for
   California residents).
5. **Log the request** (date, requester, type, outcome) — even a plain
   text/spreadsheet log is sufficient at this scale, but it's what
   demonstrates compliance if ever asked.

## 2. Consent records

`POST /api/accept-legal` (`backend/routes_user.py`) stores, per signed-in
user in `user_preferences`: `legal_accepted`, `legal_accepted_at`,
`legal_terms_version`, `legal_privacy_version`, and (plan.md §8.B-AGE.6)
`age_attested`/`age_attested_at`. The frontend's consent modal keys its
localStorage re-prompt flag off `LEGAL_TERMS_VERSION`/
`LEGAL_PRIVACY_VERSION` (`app.py`), so bumping either constant both
re-shows the modal to every user and produces a distinguishable acceptance
record for the new version — that's what makes "re-prompt on material
version change" (plan.md §8.D.2) real rather than aspirational.

**Bug fixed in this pass:** `accept_legal()` previously upserted against a
`clerk_id` column that does not exist anywhere in the schema (the table's
real primary key is `user_id`) — every consent-record write was silently
rejected by PostgREST and swallowed by a broad `except`. Consent records
were not actually being persisted until this fix. See
`backend/routes_user.py`'s `accept_legal()` and the regression test in
`tests/test_routes_user.py`.

## 3. Data Processing Agreements (DPA) checklist

Every processor below receives some category of personal or usage data
(full list and what each one receives: `privacy.html` §4). Most SaaS
vendors offer a click-through/standard DPA that a customer accepts once
(often bundled into the ToS or available in account settings) rather than
a negotiated document — confirm the current acceptance mechanism and URL
directly on each vendor's site before relying on the links below, since
legal-page URLs move.

| Processor | Data received | DPA mechanism | Status |
|---|---|---|---|
| Clerk | Name, email, auth metadata | Click-through DPA (Clerk dashboard → legal, or clerk.com/legal/dpa) | ⏸ Not yet executed — action item |
| Supabase | All app-stored user data (preferences, bookmarks, ask history, memories) | Click-through DPA (supabase.com/legal/dpa) | ⏸ Not yet executed — action item |
| Vercel | Request/hosting logs, aggregated analytics | Vercel DPA (vercel.com/legal/dpa) | ⏸ Not yet executed — action item |
| Google (Gemini API) | Question text + retrieved source excerpts (no account identifiers) | Google Cloud/API Data Processing Addendum (cloud.google.com/terms/data-processing-addendum) | ⏸ Not yet executed — action item |
| Anthropic (Claude API) | Question text + retrieved source excerpts (no account identifiers) | Anthropic commercial DPA (anthropic.com/legal — commercial terms) | ⏸ Not yet executed — action item |
| Sentry | Scrubbed error/crash reports (`docs/OBSERVABILITY.md`) | Click-through DPA (sentry.io/legal/dpa) | ⏸ Not yet executed — action item |
| Sefaria, Hebcal, MyMemory/Google Translate | Search/lookup queries derived from the question, or text snippets — not account identity | No formal DPA sought — public APIs, no account-identifying data shared, source-text/calendar/translation lookups only | Documented, not a gap |

**Action required (repo owner, not an engineering task):** click through
and retain a copy of each ⏸ row's DPA before launch. This is the item
plan.md §8.D.3 explicitly calls out as needing the account holder, not
code.

### 3.1 International transfer mechanism per sub-processor (researched 2026-09-04)

`privacy.html` §5 ("International Data Transfers") previously carried an
unresolved placeholder asking to "confirm each current sub-processor's
specific transfer mechanism." That's a factual lookup, not a legal
judgment call, so it was researched directly against each vendor's own
current public DPF/DPA documentation rather than left for counsel to
chase down:

| Processor | Mechanism | Source |
|---|---|---|
| Clerk | Self-certified under the EU-U.S. DPF (+ UK and Swiss extensions); SCCs (Modules 1-3) incorporated as fallback where DPF doesn't apply | [Clerk DPF Notice](https://clerk.com/legal/dpf), [Clerk DPA](https://clerk.com/legal/dpa), [Clerk changelog 2024-02-29](https://clerk.com/changelog/2024-02-29) |
| Vercel | Certified under the EU-U.S. DPF (+ UK and Swiss extensions) | [Vercel DPF changelog](https://vercel.com/changelog/vercel-is-now-certified-under-the-eu-us-data-privacy-framework-dpf), [Vercel DPA](https://vercel.com/legal/dpa) |
| Google (Gemini API / Cloud) | Participates in the EU-U.S./Swiss-U.S. DPF (+ UK extension); also relies on SCCs for transfers outside DPF's adequacy coverage | [Google data transfer frameworks](https://policies.google.com/privacy/frameworks?hl=en-US), [Google Cloud & GDPR](https://cloud.google.com/privacy/gdpr) |
| Anthropic | SCCs (EU Module 2 controller-to-processor, Module 3 processor-to-processor, plus UK and Swiss transfer addenda) as the primary documented mechanism; also participates in the EU-U.S. DPF as a secondary basis | Anthropic commercial DPA/Commercial Terms (anthropic.com/legal) |
| Supabase | No DPF self-certification found in Supabase's own documentation. SCCs only: Module 2 (controller-to-processor) and Module 3 (processor-to-processor), per DPA clause 12.1-12.2 and Schedule 2, with the UK ICO International Data Transfer Addendum (template B.1.0) and a Swiss addendum (Schedule 2 §§2-3) incorporated by reference on acceptance of the DPA | [Supabase DPA](https://supabase.com/legal/dpa), [Supabase subprocessor list](https://supabase.com/legal/customer-resources/subprocessor-list) |
| Sentry | Self-certified under the EU-U.S. DPF (confirmed on Sentry's own trust page and DPA Schedule 3 §1); DPA's "Data Privacy Framework" definition also covers the Swiss-U.S. DPF and UK Extension, though the trust page names only the EU-U.S. DPF by name. SCCs (Module 2 controller-to-processor / Module 3 processor-to-processor, DPA Schedule 3 §2, with UK Addendum at §2.2) apply as fallback if DPF is invalidated or doesn't otherwise apply | [Sentry DPA](https://sentry.io/legal/dpa/), [Sentry GDPR best practices](https://sentry.io/trust/privacy/gdpr-best-practices/), [Sentry subprocessor list](https://sentry.io/legal/subprocessors) |

**What this does and doesn't resolve:** it answers "what mechanism does
each vendor offer," which is a fact anyone can look up. It does **not**
answer whether that mechanism is legally sufficient for *this* Service's
actual data flows and EU/UK/Swiss exposure — that residual judgment call
is still for the attorney review in `plan.md` §8.H line 1 / `akiva_tasks.md`
T14, and vendor certifications/DPA terms can change without notice, so
re-verify before relying on this table for anything beyond drafting.

**Research note (2026-09-05):** Supabase and Sentry — the two remaining
sub-processors not covered in the 2026-09-04 pass — were researched
against their own current public DPA/trust-center documentation above.
Supabase's DPA makes no DPF claim anywhere, so its mechanism is SCCs
only; Supabase separately supports EU-region data residency (a project
provisioned entirely in an EU region), which is a data-location control
distinct from — and not a substitute for evaluating — the transfer
mechanism above for any data (backups, logs, sub-processors) that still
leaves the region. Sentry self-certifies under the DPF per its own trust
page and DPA, with SCCs as the documented fallback. Both rows are now
reflected in `privacy.html` §5.

## 4. Records of Processing Activities (GDPR Art. 30)

| Activity | Data subjects | Categories of data | Purpose | Recipients | Retention | Security measures |
|---|---|---|---|---|---|---|
| Account creation & auth | Registered users | Name, email, auth tokens | Provide the Service, secure login | Clerk | Until deletion or 1yr inactivity (`privacy.html` §3) | Clerk-managed password hashing; HTTPS/TLS 1.2+ |
| Halachic Q&A (ask pipeline) | Registered + anonymous users | Question text, AI answer, cited sources, community/mode/language settings | Generate and store the AI answer, per-user history | Google Gemini, Anthropic Claude, Sefaria, Supabase | 90 days, then deleted (automated — §5 below) | RLS-scoped table, encryption at rest (Supabase) |
| User preferences & study data | Registered users | UI prefs, bookmarks, study notes, AI summaries | Personalize reading experience | Supabase only | Until user deletes or account deletion | RLS-scoped table |
| Conversation memory | Registered users | Short AI-generated summaries of prior interactions | Personalize future answers | Supabase, (indirectly) Gemini/Claude at inference time | Until account deletion | RLS blocks all client access; service-role only |
| AI usage/cost metering | Registered + anonymous (IP-keyed) users | Model name, token counts, estimated cost, `user_id` or `client_key` | Enforce per-caller budget caps (plan.md §8.C.1/§16) | Supabase only | 90 days, then deleted (automated — §5 below) | Service-role only, not client-readable |
| Legal consent & age attestation | Registered users | ToS/Privacy version + timestamp, 13+/16+ attestation | Demonstrate consent, enforce minimum age | Supabase only | Duration of account + reasonable post-deletion period (defensibility) | RLS-scoped table |
| Error/crash telemetry | Registered + anonymous users | Scrubbed stack traces, request IDs — question/answer text excluded | Debug production incidents | Sentry | Sentry's own retention (project-configured) | Data scrubbing enabled (`docs/OBSERVABILITY.md`) |

## 5. Retention enforcement

`GET /api/devtools/retention-enforce` (`backend/routes_privacy.py`),
triggered daily by Vercel Cron (`vercel.json`, gated by `CRON_SECRET`
exactly like the existing `/api/devtools/budget-check` job), actually
deletes:

- `ask_history` rows older than 90 days
- `ai_usage_log` rows older than 90 days

This replaces the "an automated retention job is planned... until it
ships, deletion beyond user-initiated requests is manual" language that
previously sat in `privacy.html` §3 — that job now exists and runs daily.

**Deliberately out of scope for this job** (see `privacy.html` §3 for the
full retention table):

- **Account info / 1-year inactivity** — auto-deleting a whole account
  (Supabase rows *and* the Clerk identity) on an inactivity heuristic is a
  materially riskier operation than trimming log rows, and this app has no
  reliable "last active" signal synced locally today (Clerk tracks
  `last_sign_in_at` server-side, but nothing here reads it). Left as a
  manual/future job rather than shipped half-verified against live user
  accounts — the safer failure mode for an irreversible bulk-delete is "it
  doesn't run automatically yet," not "it runs against an untested
  heuristic."
- **User preferences & bookmarks** — retained "until deleted by user or
  account deletion" by design; there is no time-based window to enforce.
- **Legal consent & age-attestation records** — retained past account
  deletion intentionally, as a defensibility record; a job that deleted
  these would defeat their purpose.
- **Session cookies** — client-side, deleted on logout; not a server-side
  retention concern.
- **Security logs (IP, access logs) — 30 days** — these are Vercel's own
  platform request logs, not rows in this app's Supabase project; their
  retention is configured in the Vercel dashboard, not enforceable from
  application code.
- **Cached AI responses — 24 hours** — an in-memory/edge cache, not a
  Supabase table; expires on its own via the cache's TTL.

## 6. Breach response plan

**Roles.** Solo-operated project — one person (Akiva Yevdayev) holds
every role below until the project has more than one operator.

- **Incident lead / DPO-equivalent contact:** akiva.yevda@gmail.com
- **Technical responder:** same

**Detection sources:** Sentry alerts, Supabase dashboard alerts/logs,
Vercel deployment/runtime logs, a direct report (user, researcher, vendor).
`docs/SECURITY.md`'s "Reporting a vulnerability" section is the intake
path for the latter.

**Timeline (GDPR Art. 33 — 72 hours from *becoming aware*, not from the
breach itself):**

1. **Hour 0 — confirm & contain.** Verify the report is real. Rotate any
   exposed credential immediately (see `docs/SECURITY.md`'s key-rotation
   guidance). If a specific attack vector is active (e.g. an auth bypass),
   disable the affected route/feature via a deploy if that's faster than a
   full fix.
2. **Hour 0–24 — scope.** Determine what data was exposed, how many users
   are affected, and whether it meets the GDPR Art. 33 "risk to the rights
   and freedoms of natural persons" bar for notification. Given the app's
   halachic-question domain, treat exposure of question/answer content as
   higher-risk by default — plan.md §8.B's safety classifier exists
   because that content is routinely medical, marital, mental-health, and
   abuse-adjacent.
3. **Hour 24–72 — notify (if required).** If notification is required:
   - Fix the root cause first, or have a concrete remediation timeline.
   - Draft and send the user-facing notice (template below).
   - If an EU/UK/Swiss supervisory authority notification is required
     (Art. 33), that's a legal filing — get counsel involved before
     submitting one; `privacy.html` §10 already flags that this project
     hasn't yet determined whether it needs a formal EU representative.
4. **Post-incident.** Write up what happened, what was fixed, and what
   changes (code, process, or monitoring) prevent recurrence. Add the
   summary to this file or a dated entry in `docs/RUNBOOKS.md`.

**User-facing notification template:**

> Subject: Important security notice about your Sh'elah account
>
> We're writing to let you know about a security incident that may have
> affected your account. On [date], we discovered [what happened, in
> plain language]. The information involved may have included [specific
> data categories — be precise, not vague]. We [have fixed / are fixing]
> the issue by [remediation]. We recommend you [specific action, if any —
> e.g. nothing needed, or "sign out other sessions"]. If you have
> questions, reply to this email or contact akiva.yevda@gmail.com.

**Regulator notification template (Art. 33, only if counsel confirms it's
required):**

> Nature of the breach: [what happened, systems/data involved]
> Categories and approximate number of data subjects affected: [...]
> Categories and approximate number of records affected: [...]
> Likely consequences: [...]
> Measures taken or proposed: [...]
> Contact point: Akiva Yevdayev, akiva.yevda@gmail.com

## 7. DPIA

A basic Data Protection Impact Assessment covering the app's automated
religious-guidance processing lives in `docs/DPIA.md` (plan.md §8.D.4).
