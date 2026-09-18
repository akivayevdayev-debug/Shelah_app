# Content QA — Religious Accuracy

**Status:** engineering documentation of plan.md §8.F.3 ("Content QA for
religious accuracy"). This describes what the codebase actually does
today — not an aspirational editorial process. It should be reviewed by a
rabbinic advisor or counsel before public launch, same as the rest of the
§8.F/§8.G scaffolding (see `docs/LAUNCH_CHECKLIST.md`).

## Sh'elah is not rabbinically supervised

There is no rabbinic advisor, rabbinical board, hashgacha, or
hechsher-equivalent endorsement behind Sh'elah today. A repo-wide search
for that language (`rabbinic advisor`, `rabbinical board/supervision`,
`hechsher`, `hashgacha`, `rabbinic approval/endorsement/oversight`) turns
up nothing — the only hits are this plan item itself and an unrelated
"endorsement" disclaimer in `templates/terms.html` about links to
third-party sites, which is about outbound links, not Sh'elah's own
content.

This should be stated plainly to users, and already is:
`templates/ai-disclosure.html` §4 ("What Sh'elah's AI Does NOT Do") states
the app does not issue binding halachic rulings (p'sak halacha) and does
not replace a rabbi, posek, or other rabbinic authority, and §5 tells
users to always consult their rabbi before acting on an answer. If a
rabbinic advisor or hechsher-equivalent endorsement is obtained in the
future, its scope must be documented precisely here and in the
AI Disclosure page — overclaiming authority is itself a liability.

## What is NOT reviewed: individual AI answers

`docs/DPIA.md` §2 already states the ground truth for the `/ask` request
path: **"No human reviews individual questions or answers in the normal
path."** This document does not contradict that. A user's question is
retrieved against source text (Sefaria, the local customs corpus,
Halachipedia) and synthesized into an answer by an LLM (Google Gemini
primary, Anthropic Claude fallback per `docs/DPIA.md` §2 /
`templates/ai-disclosure.html` §1) with no human editorial step between
generation and delivery. Whatever QA exists today is upstream, on the
*source data* the retrieval step draws from and the *schema* of the
customs corpus — not on individual synthesized answers.

## What IS reviewed today: the customs corpus pipeline

The community-customs data under `customs/*.json` (`ashkenaz.json`,
`sefardic.json`, `yemenite.json`, etc.) is hand-authored/curated JSON, not
scraped or AI-generated. Per `customs/DEVELOPER_NOTES.md`, editing
guidance is to preserve valid JSON/UTF-8, keep topic/category fields
consistent for matcher quality, and include source metadata (halakhic
authorities, poskim, codes) where possible for transparency. This is a
data-curation and engineering discipline, not a rabbinic review process —
there is no sign-off step by a halakhic authority recorded anywhere in
the repo.

Two automated checks sit around that data:

1. **Schema validation at startup** —
   `validate_all_customs_at_startup()` in `backend/customs.py` walks every
   `customs/*.json` file (skipping the aggregated `customs_db.json` and
   `schema.json`) and checks two things only: that the file parses as
   valid JSON, and — for structured (v2.x) files — that the required
   top-level fields `heritage_id`, `name`, and `halacha_index` are present
   and non-empty. It logs an error per offending file and continues; it
   never raises, so one bad file cannot crash the app. **This is a
   structural/schema check, not a content-accuracy or halakhic-accuracy
   check** — it cannot detect a wrong citation, a misattributed ruling, or
   a factually incorrect summary, only a malformed or incomplete file. Per
   `docs/RUNBOOKS.md` ("Startup-cost gating"), it is a developer/CI safety
   net and is skipped by default on production cold starts (Vercel /
   `FLASK_ENV=production`) to avoid billing Active CPU on every request,
   overridable via `VALIDATE_CUSTOMS_AT_STARTUP=1`/`=0`; it always runs in
   CI.

2. **Migration to Supabase** — `scripts/migrate_customs_to_supabase.py`
   reads the same `customs/*.json` files (supporting both the modern
   `halacha_index` shape and a legacy flat `{community: {topic: entry}}`
   shape), normalizes each entry's text, derives a deterministic id via
   `sha256(community|topic|source)`, and upserts rows into Supabase's
   `community_knowledge` table (`--dry-run` and `--community <name>`
   filters are supported). This is an ETL/deterministic-upsert step, not
   a content-review step — it does not evaluate whether a ruling is
   correct, only reshapes and loads what is already in the JSON files.

A related but separate tool, `scripts/crawl_library_leaves.py`, crawls
the Sefaria API's library index (not the customs corpus) and probes each
leaf title to confirm its ref is actually loadable, producing a
machine-generated JSON report of suggested "fix" refs or "remove"
recommendations for broken/renamed Sefaria references. This validates
that the *links into* Sefaria's library resolve — it says nothing about
whether Sefaria's own text or Sh'elah's synthesis of it is halakhically
accurate.

## Summary: what exists vs. what does not

| | Exists today | Does not exist today |
|---|---|---|
| Per-answer human review | — | No human reviews individual AI-synthesized answers before they reach users (`docs/DPIA.md` §2) |
| Rabbinic endorsement | — | No rabbinic advisor, board, or hechsher-equivalent endorsement of the app or its content |
| Customs JSON schema check | `validate_all_customs_at_startup()` — required-fields + valid-JSON check at startup/CI | Does not check halakhic accuracy of the content |
| Customs data authorship | Hand-curated JSON files with source-metadata conventions (`customs/DEVELOPER_NOTES.md`) | No recorded halakhic sign-off step on that authorship |
| Sefaria library link health | `scripts/crawl_library_leaves.py` — machine-generated remove/fix report on ref loadability | Does not validate the substantive accuracy of Sefaria's text or Sh'elah's use of it |
| User-facing disclosure | `templates/ai-disclosure.html` §3/§4/§5 — hallucination risks, what the AI does not do, "always consult your rabbi" | — |

## Related documents

- `docs/DPIA.md` §2 — data-processing description of the `/ask` path,
  including the "no human review" statement this document defers to.
- `docs/RUNBOOKS.md` — "Startup-cost gating" section, for the
  `validate_all_customs_at_startup()` production skip/override behavior.
- `templates/ai-disclosure.html` — the user-facing AI disclosure page
  (limitations, hallucination risk, what the AI does not do, human-in-the-
  loop expectation).
- `customs/DEVELOPER_NOTES.md` — file-format and editing conventions for
  the customs corpus.
