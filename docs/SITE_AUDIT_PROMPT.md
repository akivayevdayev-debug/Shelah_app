# Whole-site audit prompt (design · security · reliability)

A standing prompt for a **local Claude Code session** (on the developer's own
machine, with a full checkout) to run a full-site pass — not a PR diff —
across design, security and reliability. Paste the block below into a fresh
session when you want this done. Re-run it periodically or before a release;
it is written to stand alone, with no memory of any prior session.

It complements, not replaces, per-PR review: `/security-review` still runs
per-PR in CI/agent workflows. This prompt is for the sweep across the parts
of the site a single PR's diff never touches.

---

## The prompt

```
You are auditing the whole Sh'elah site for design, security and
reliability issues — not reviewing a diff. Load these three skills first
and follow them together for the rest of this task:
  - design-excellence
  - security-review
  - apex-craft (skip its git-diff-specific steps; everything else applies)

Ground rules (from this repo's own conventions — do not skip):
  - Read-only audit unless told otherwise: report findings, do not push
    fixes without asking, and never merge or delete anything.
  - Use `graphify query "<question>"` / `graphify explain "<concept>"` for
    navigation before grepping raw files (CLAUDE.md) — it is faster and
    keeps you oriented in a codebase this size. Do NOT run
    `graphify update .` during a read-only audit.
  - Never edit a stray "foo 2.js"-style iCloud conflict-copy file if you
    find one; flag it instead.
  - Never kill or reuse port 5002 if a dev server is already on it; pick a
    free port for any server you start.
  - Follow `.agents/ENGINEERING_RULES.md` for the house UI/motion/a11y/
    backend rules — they are the bar `security-review`/`design-excellence`
    findings get checked against, not just general best practice.
  - Cite specific `file:line` for every finding. No finding without a
    concrete reproduction or a specific line — vague "could be improved"
    notes are noise, not findings.

Scope: the whole site, in this order (stop and report between phases
rather than running all three unattended — each phase's findings can
change how deep the next one needs to go):

## Phase 1 — Security
security-review's own workflow is diff-based; adapt it to a full sweep:
1. Enumerate every user-input entry point: every Flask/FastAPI route in
   `app.py`/`asgi.py`/`backend/routes_*.py` that reads
   `request.args`/`.form`/`.json`/`.cookies`/path parameters; every place a
   route parameter reaches a file path, a subprocess, a template, a raw
   SQL/Supabase query, or another outbound URL; every `innerHTML`/
   `dangerouslySetInnerHTML`-equivalent assignment in `static/js/*.js` and
   `templates/index.html`; the service worker's message handlers.
2. Apply security-review's categories (injection, path traversal, auth/
   session, secrets, XSS, SSRF-by-host-or-protocol, deserialization) to
   each. Use its severity/confidence bar: only report ≥0.8 confidence,
   skip its listed exclusions (DOS, rate limiting, log-spoofing,
   documentation, etc. — see the skill for the full list).
3. Check `docs/SECURITY.md` and `docs/AI_SECURITY_REVIEW.md` first — don't
   re-report what's already a documented, accepted tradeoff there; do
   flag anything that contradicts what those docs claim is true.
4. Report in security-review's own markdown format (file, line, severity,
   category, description, exploit scenario, fix, confidence).

## Phase 2 — Design
Apply design-excellence's 10-second self-review and craft.md checklist to
every distinct page/view template and its states (loading, empty, error,
dense, RTL, dark mode): the home screen, the AI answer view, the library
reader, prayers/siddur, calendar, community pages, settings, auth. For
each: hierarchy, spacing scale adherence (tokens, not magic numbers),
touch targets ≥44×44, contrast (AA), focus rings, one accent color,
consistency with `static/css/tokens.css`, and the anti-checklist (mixed
button styles, borders-and-shadows-on-everything, layout shift, missing
microstates). If a real browser is available, verify at 320/768/1280/
1920px in both themes rather than reading CSS alone — rendering and
reading the CSS agree less often than they should.

## Phase 3 — Reliability
For each backend module and frontend module touched by recent history
(`git log --stat -20` as a starting point, not a limit):
  - Unhandled exceptions on the request path (bare `except:`, an
    un-caught `.json()`/`JSON.parse`, an async call with no `.catch` and
    no enclosing try/catch, a promise chain that can reject unobserved).
  - Race conditions in shared/cached state (module-level caches, the
    service worker's caches, anything read-then-written without a lock
    where two requests can interleave).
  - Backward compatibility: a changed API shape, cache key, or storage
    schema that breaks an already-deployed client still in the wild.
  - Run `mypy --ignore-missing-imports` over `backend/`, `ruff check .`,
    the full `pytest` suite and `npm run test:coverage`; report anything
    red. A clean run is itself a finding worth stating, not silence.

## Output
One markdown report per phase (or one combined report with three
sections), most severe first, each finding actionable on its own. End
with a one-paragraph summary: what's solid, what needs a human decision,
and what you'd fix first if given one hour.
```

---

## Notes for whoever runs this

- This is deliberately **not** wired to fire automatically — a full-site
  sweep is expensive (tokens and wall-clock) and most sessions only need
  the per-PR `/security-review`. Run it deliberately.
- If the findings warrant code changes, do those as their own follow-up
  task/PR per finding (or a small batch of related ones) — not as one
  giant diff bolted onto the audit itself. `apex-craft`'s Law 3 (scope
  before you plan) applies to the fix work the same way it applies to
  everything else.
