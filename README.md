# Sh'elah

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.14](https://img.shields.io/badge/python-3.14-blue.svg)](.github/workflows/ci.yml)
[![Deployed on Vercel](https://img.shields.io/badge/deployed%20on-Vercel-black?logo=vercel&logoColor=white)](https://vercel.com)
[![CI](https://github.com/akivayevdayev-debug/Shelah_app/actions/workflows/ci.yml/badge.svg)](https://github.com/akivayevdayev-debug/Shelah_app/actions/workflows/ci.yml)
[![Quality gate status](https://sonarcloud.io/api/project_badges/measure?project=akivayevdayev-debug_Shelah_app&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=akivayevdayev-debug_Shelah_app)

**[shelah.org](https://shelah.org)** — the Torah Encyclopedia (literally, that's kind of the whole idea lol)

Sh'elah (שאלה, "question") is a Jewish learning website I built by myself. It does three things:

1. A searchable library of Torah and Judaic texts and commentary (all powered by Sefaria's texts, but made to actually feel easy to use), with customs broken out by community: Ashkenazi, Sefardi, Teimani, and a bunch of others.
2. An AI assistant that answers questions on Jewish law (halacha) by pulling from primary sources like the Torah and other Judaic texts instead of just guessing.
3. The practical stuff people actually need every day: live prayer times (zmanim) and candle-lighting times based on where you are, and a Hebrew calendar with the weekly Torah portion built in.

Quick but important thing before anything else: **Sh'elah is for learning, not for rulings.** The AI isn't a rabbi and doesn't pretend to be one. Every answer ends by telling you to take real questions to your own rabbi, and I mean that. (Same goes for legal stuff: ask an actual lawyer, not my website lol.)

## What it does right now

- Answers halacha questions with the actual sources it used cited, and always points you back to your own rabbi for anything that's a real ruling.
- Refuses to touch anything medical, mental-health, or abuse-related. Instead of the AI trying to answer, it sends you to real help. Halacha touches sensitive stuff, and a chatbot is the wrong thing to be answering those questions.
- Has safeguards against weird off-topic questions and prompt injection (because we don't want the site getting hacked lol).
- A Torah/Judaic text library with commentary and word translations, shown in English and Hebrew side by side (with proper right-to-left layout for the Hebrew).
- Community-aware answers and customs for 14 traditions: Ashkenaz, Sefardic, Yemenite (Teimani), Moroccan, Persian, Syrian, Bukharian, Iraqi, Ethiopian, Georgian, Greek/Romaniote, Mountain Jewish (Kavkazi), Turkish/Ottoman Sefardic, and more as I add them.
- Prayer services (Shacharit, Mincha, Maariv) that know about your community's nusach.
- Daily study stuff like Daf Yomi and Mishna Yomit, pulled live from Hebcal.
- Bookmarks and saved preferences once you sign in.
- You can install it like an app (it's a PWA), and the core reading stuff works offline.
- Live zmanim and candle-lighting times based on your real location, including fast start/end times and the right times on holidays.
- A Hebrew calendar with the parashah, plus holiday cards with real clock times and links straight into the text.
- Works in English and Hebrew, light and dark mode, and on desktop, tablets, and phones.
- Every page has a real link. A specific text, a prayer, a calendar day, an AI answer, even your settings. Send someone the link and it opens exactly where you were.
- You can export or delete your own account and data yourself, no emailing me required.
- Keyboard and screen-reader support, bot protection, and a per-user daily AI spending cap so the site can stay free without me going broke °~°

## What was hard

Getting the AI to have safeguards, not hallucinate, follow Judaic law and texts, and all set up took way longer than I originally expected. It's easy to make a chatbot that sounds confident; it's much harder to make one that cites where an answer actually comes from, doesn't quietly make things up, and knows when to deflect a question or a malicious prompt.

I also spent a LOT of time on things nobody will ever see directly: locking down who can access what in the database, making sure a runaway request can't rack up a huge AI bill (because this site is made for the public to be free, and not to make me go broke °~°), and keeping the whole thing fast and not-broken across English and Hebrew, light and dark mode, and phone-sized screens.

And the reader page jumping around while you scroll. That one bug haunted me for literal months. More on that below :|

## How it came together

I posted devlogs the whole way through, so here's the (slightly cleaned up) version of how this thing went from "basic Q&A" to an actual shipped site.

### The early build

When I first put Sh'elah up it could already answer questions using primary sources, search the library with customs split out by community, and calculate live zmanim and a Hebrew calendar. It worked, but it was early, and I was pretty upfront that a lot still needed doing:

- The AI only knew whatever got stuffed into the prompt ahead of time. No real tool use yet, so it couldn't go look anything up.
- The safety layer was still in progress. I wanted anything medical, mental-health, or abuse-related to go to real professional resources instead of an AI ruling, and I didn't want a younger user getting graphic content when the real answer should just be "ask your rabbi for the details."
- One backend file (the AI question-answering one) had gotten way too big to maintain.
- The caching layer needed to handle a bunch of requests at once without breaking, and I needed circuit breakers so one third-party outage couldn't take the whole site down.

### Devlog 2: the "please don't bankrupt me" update

- Removed a leftover routing rule in Vercel that was returning a 404 on the production site.
- Added a server-side daily AI budget per user, so if someone goes way past normal usage they get rate limited and can't spam requests. Also fixed a bug where AI usage was being logged as $0 when it definitely wasn't.
- Fixed a license mismatch and added a CONTRIBUTING.md and code of conduct.
- Old logs and history get deleted on a schedule now, so trash doesn't pile up.
- Built a dark-mode accessibility checker and fixed the contrast problems it found. Added circuit breakers so an external service failing doesn't crash the site.
- Added About/Help/glossary pages, a sitemap, a feedback widget, and analytics you can opt into.
- Added CI checks and Vercel Speed Insights.
- Swapped the GPS permission popup for zmanim over to cookie/IP-based location instead.

### Devlog 3: cleanup

- Fixed a bug where a malformed cache config could crash the entire app on startup. Fun.
- Split that giant AI question-answering file into smaller pieces, and added tests to make sure the Gemini version and the Claude version always answer the same way with the same guardrails.
- Added a shared caching layer so the library, prayers, texts, daily study, and calendar pages stop hitting the database on every single request.
- Moved rate limiting into one place for the whole site instead of having it scattered everywhere.
- Cleaned up files the code was referencing that were never committed (which were causing boot errors).
- Simplified some functions that had gotten too complicated to safely touch.
- Sped up cold starts by only loading the AI libraries when they're actually needed.

### Devlog 4: security (which was a pain :<)

- Fixed row-level security in Supabase so one user can't see another user's personal info, plus some security issues with Vercel and Clerk. Which was a pain to fix :<
- Found out my login system and database weren't even linked to each other correctly, which was breaking every signed-in read and write. Fixed.
- Added an AI usage tracker and fixed the old usage records so the spending log is actually correct.
- My webhook (discord error bot) was malfunctioning (and still kinda is in clerk :<) and no longer is! YAY MORE ERRORS FOR ME! (at least I'll know why they happen :|)
- Fixed a bug where the site was incorrectly caching files in browsers, making things slower AND more expensive at the same time (yay less money to spend!!!!!)
- Ran accessibility tests and fixed what they found.
- Fixed some legal doc issues (Privacy Policy and ToS) so I don't get sued!!!

### Devlog 5: the AI can finally go look things up

- Built real tool use for the AI, so it can fetch live prayer times, calendar dates, and texts when it needs them instead of having everything pre-loaded into every single request. Judaic texts always come first, and web search is strictly a last resort. (It's behind an on/off switch, `AI_AGENTIC_TOOLS`, since every tool call is another paid model call and I want the rate limiting fully hardened before it's on for everyone.)
- Filed the site for DMCA copyright protection.
- Added self-serve privacy controls so you can export or delete your own account and data.
- Added a feedback button on AI answers so people can flag bad ones directly.
- Added keyboard/screen-reader support with tests for it in CI.
- Fixed a security hole where a malicious link cited by the AI could run code, and put a bot check (Turnstile) in front of the AI endpoint.
- Fixed the browser caching bug from last time. For real this time. Pages actually get served from cache now.
- Fixed a bunch of small annoying bugs: the page jumping back while scrolling, the wrong section getting highlighted, calendar color glitches, animations not loading.
- Fixed a security-check script that had been quietly failing every single scheduled run for over a week. Nobody told me. Not even the script.
- Spent a lot of time simplifying complicated functions across the codebase.

### Devlog 6: tests, tests, and more tests

- Fixed dark mode colors that didn't match the rest of the site.
- Added a star-shaped AI icon and animations on the search bar while the AI is thinking, plus fixed spacing issues in AI answers.
- Added a commentary button to the reader's top bar so people actually know the sidebar exists.
- Fixed zmanim on holidays and fast days, added fast start and end times, and gave the AI Hebrew-date math tools.
- Finalized the legal pages with real operator info, registered a DMCA agent, and added an age confirmation.
- Went through every single SonarCloud finding. The quality gate passes now, with security and reliability both rated A.
- Wrote a huge batch of new tests. Coverage went from 60% to about 84%, and all 3,517 tests pass (yes ik it's a lot of tests). (Yes, somehow it was 60% before when I had explicitly added tests for 93% coverage · ~ ·)
- Rotated a leaked Gemini key and cleaned it out of the repo history (correctly this time).
- Merged two copies of the code that had drifted apart, so shared stuff like the AI answer builder only lives in one place now.
- Rewrote the docs, added a changelog, documented every environment variable, and regenerated the database docs from the live schema.

### Devlog 7: the big punch list

- Gave every page its own real address. Any text, prayer, calendar day, or AI answer has a link that opens straight to it, even on refresh. Old-style links still redirect.
- Made AI answers shareable with a Copy Link button. Private ones stay out of search engines.
- Knocked out a 15-item punch list: infinite scroll no longer jumps around, the commentary sidebar loads way faster and lighter, calendar bugs fixed, parashah and commentary names are accurate and ranked properly, community pages have full Hebrew translations, the header date follows your actual location, loading spinners recover on their own if your connection drops, and Hebrew bold text isn't invisible anymore.
- Completely redid EVERYTHING about the AI UI. The AI window grows out of the Ask button on desktop instead of just popping in, and rises up as a sheet on phones. Moved the Ask button onto the phone's top bar instead of hiding it behind a search tap. New small window, new fullscreen window, and chat share links that legitimately work.
- Polished motion across the whole site: screens fade up, buttons feel right on hover/press/focus, and every emoji became a matching icon. (The calendar close button used to get *lighter* on hover. Now it darkens like everything else.)
- Built a real holiday detail card for the calendar with accurate times and links into the reader.
- Fixed prayer and reader text losing bold and italic formatting when it came in from the source library.
- Locked down AI spending again: turns out the plain (non-agentic) chat route was quietly skipping the budget check, and a bug in the model-calling code could leak the API key. Both fixed, every path respects the daily budget now.
- Added a circuit breaker to the rate limiter so database lag can't take the whole site down with it.
- Cleared SonarCloud's backlog. Again.
- Made Turnstile (the invisible spam check) actually invisible instead of showing its widget.
- Merged a pile of parallel branches (calendar icons, translations, reader fixes, security fixes) back together cleanly.

### Devlog 8: WE SHIP!!!!!

- Redesigned parts of the AI look again so it doesn't look like AI slop :)
- Gave the whole site real links and web addresses for everything, including chats and settings, so if you send someone a link they land exactly where you are. Honestly the single biggest fix, because before this you couldn't share what you were actually looking at.
- Added `/signin` and `/profile` as real pages that layer on top of whatever you're looking at, so signing in from, say, a help page brings you right back to it.
- Found out why the AI sometimes gave no answer at all: the sign-in check between the site and the database was rejecting valid logins, which quietly broke saving your conversations and preferences. That explained an on-and-off "no answer" bug. (YAY MORE BUGS FIXED)
- Sped up word/translation lookups. A word Sefaria didn't recognize used to try several spellings against the dictionary and then several more against translation, one at a time. Now they run at the same time and it takes about a second (maybe 2 if it needs more sources).
- Fixed animation and fading issues when switching reading layout, switching commentary verses, and jumping to a cited passage (which, it turned out, didn't actually highlight anything yet).
- Fixed the reader losing its place. Scrolling to load more text, or loading a previous chapter, could silently jump the page's address to the wrong chapter. Now it tracks what you're actually reading, not whichever paragraph happened to load first. (I'VE BEEN TRYNA FIX THAT ISSUE FOR MONTHS!)
- Cleaned up account and privacy stuff: a smaller profile menu, a delete-account warning that isn't half-pink in dark mode, and your data export actually includes your AI chats now.
- Fixed a database error that had been showing up in the logs for a week from a migrations table that never existed. It exists now, with the right access rules.

## What I'm proud of

That it actually exists as a real product instead of just an idea living inside someone's head.

The commentary and translations are genuinely fast now (they were super buggy and slow before), the calendar had a huge glow-up, and every page has a real link that opens back up exactly the way you left it.

The whole thing was built by me, end to end. Yes, ofc I used AI to help, and without it this would've taken years. But it has a real test suite, real security practices, accessibility support, full legal docs and a copyright license. It's not a prototype held together by hopes and prayers.

## Want to try it?

Go to [shelah.org](https://shelah.org) and:

- Ask it an actual halacha question and see if you get a real answer with sources instead of something vague and wrong. (If you're not Jewish, ask it something you've wondered about the Old Testament or Judaism in general!)
- Open a Torah portion and look at the commentaries, or tap a word to see its translation.
- Switch the site to Hebrew and see if anything breaks.
- Try it on your phone.
- Check the prayer times for where you are, and poke around the calendar.
- Go bananas!

If anything at all is broken, email me at **akiva.yevda@gmail.com** and I'll try my best to fix it as soon as I can.

## How the AI works (and how it stays in its lane)

The `/ask` endpoint runs a retrieval pipeline: before the model sees your question at all, `backend/rag.py` pulls in live Sefaria results, the 14-community customs data, and your own saved context (if you're signed in). **Gemini is the main model, and Claude is the automatic backup** if Gemini errors out or is down (`backend/claude.py`). Both get the exact same guardrails, and there are tests that make sure that stays true.

On top of that there's a tool-use layer (`backend/ai_tools.py`, 22 tools) so the AI can go fetch texts, zmanim, calendar dates, and Hebrew-date math when it actually needs them, instead of everything getting shoved into the prompt every time. It's switched on with `AI_AGENTIC_TOOLS=true` and is off by default. With it off, the AI still gets the Sefaria results, customs, and your saved context up front, just not the ability to go fetch more mid-answer. Why it's a switch: one question can turn into several tool rounds, which means several paid model calls, so it shouldn't be on until the rate limiter uses a shared store (`RATE_LIMIT_REDIS_URL`). Details in [docs/AI_TOOLS.md](docs/AI_TOOLS.md).

**Where answers are allowed to come from**, in order (this is baked into the system prompt, `CORE_SYSTEM_PROMPT` in `backend/claude.py`):

1. Direct, chapter-level Sefaria citations (Talmud, Tanakh, Shulchan Aruch, Mishneh Torah, the major commentaries)
2. Broader keyword evidence from Sefaria, HebrewBooks, and Halachipedia
3. Acharonim and modern poskim (19th–21st century responsa)
4. The model's own knowledge, ONLY when 1–3 come up empty or clearly contradict each other

General web search (like Wikipedia) is a last resort for when the texts and the calendar math genuinely can't answer something, and it's never allowed to be the only basis for a halachic answer.

**The guardrails:**

- Every answer carries a disclaimer you can't dismiss ("Please consult with your local Rabbi for a final ruling"), both in the AI window and in the reader. The AI is never allowed to claim it's a rabbi or that anything it says is a binding ruling (`NO_IMPERSONATION_DIRECTIVE`).
- A safety classifier (`classify_safety()` in `backend/claude.py`) catches medical, mental-health/self-harm, abuse/minor-safety, and dangerous/illegal stuff, and swaps the answer for a much more prominent referral to real help.
- Minimum age is 13+ (16+ in the EU/UK), with an age confirmation. There's also an age-appropriate output layer, so sensitive topics (like niddah) stay clinical and educational instead of explicit, without dumbing down the sourcing. The whole policy is in [docs/AGE_AND_SAFETY_POLICY.md](docs/AGE_AND_SAFETY_POLICY.md).
- Prompt injection defenses, Turnstile in front of the AI endpoint, sanitized links in answers (after the malicious-link thing from devlog 5 :|), sitewide rate limiting, and the per-user daily budget.
- The full "here's what AI we use and how we handle your data" disclosure lives at [/ai-disclosure](https://shelah.org/ai-disclosure).

## The stack

- **Backend:** Flask + FastAPI together. FastAPI handles the async AI `/ask` pipeline and Flask handles everything else (it's mounted inside the FastAPI app). New routes live in `backend/` as their own blueprints, not in `app.py`, which is still bigger than I'd like.
- **Hosting:** Vercel, as one serverless function.
- **Database:** Supabase (Postgres), with row-level security so users can only see their own stuff.
- **Auth:** Clerk.
- **AI:** Gemini first, Claude as backup, with optional tool use (see above).
- **Texts and calendar data:** Sefaria for texts, Hebcal for the calendar and zmanim, MyMemory / Google Translate for the translation fallback, plus the 14 community customs datasets in `customs/`.
- **Frontend:** plain HTML/CSS/JS (ES modules), Tailwind + DaisyUI, marked + DOMPurify for rendering AI answers safely.
- **Keeping it alive:** Turnstile for bot checks, Sentry and a Discord webhook for errors, circuit breakers on every external service, SonarCloud for code quality, and a lot of CI.

### How it's wired together

```
Browser
  |
  v
Vercel (catch-all route → asgi.py)
  |
  v
asgi.py  (FastAPI app)
  |-- async /ask pipeline (auth → rate limit → budget check → RAG / tools → AI → response)
  |-- WSGIMiddleware → Flask app (app.py)
        |-- HTML pages + /api/* endpoints
        |-- backend/ blueprints for each area of the site
              |
              |-- Supabase    (preferences, bookmarks, conversations, ask history,
              |                community knowledge, ai_usage_log)
              |-- Sefaria API (texts, search, library tree)
              |-- Hebcal API  (calendar, zmanim, parashah)
              |-- Gemini      (main AI)
              |-- Claude      (backup AI)
              |-- MyMemory / Google Translate (translation fallback)
```

The full breakdown is in [docs/SERVICE_ARCHITECTURE.md](docs/SERVICE_ARCHITECTURE.md). The important pieces:

| File | What it does |
|---|---|
| `app.py` | The Flask app: page routes, middleware, and wiring up the blueprints |
| `asgi.py` | FastAPI wrapper that owns the async `/ask` pipeline and mounts Flask |
| `backend/ask_pipeline.py` | The `/ask` flow itself, including the agentic tool loop |
| `backend/ai_tools.py` | The AI's tool registry |
| `backend/claude.py` | Model calls (Gemini first, Claude backup), prompts, safety classifier |
| `backend/rag.py` | Builds the context the AI gets before answering |
| `backend/auth.py` | Clerk JWT verification |
| `backend/sefaria.py`, `backend/sefaria_library.py` | Sefaria client, library tree, text browsing |
| `backend/calendar_service.py`, `backend/zmanim_engine.py` | Calendar, Daf Yomi, and zmanim math |
| `backend/customs.py` | Loads and matches community customs |
| `backend/cost_meter.py`, `backend/cost_gates.py` | AI spend tracking and the daily budget checks |
| `backend/rate_limit.py` | The sitewide rate limiter (with its own circuit breaker) |
| `backend/health_check.py` | Circuit breakers for external APIs |
| `backend/cache.py`, `backend/cache_policy.py` | The shared caching layer and browser cache headers |
| `backend/turnstile.py` | Bot check |
| `backend/routes_*.py` | Blueprints: library, calendar, community, prayers, user, privacy, conversations, answer sharing, feedback, legal, webhooks, devtools, and the deep-link paths |

## Running it locally

You'll need Python 3.14 (that's what CI runs and what the lockfiles are built against, pinned in `.python-version`; 3.12+ might work but I haven't checked), plus your own Clerk project, Supabase project, and a Gemini and/or Anthropic API key.

```bash
git clone https://github.com/akivayevdayev-debug/Shelah_app.git
cd Shelah_app

python3 -m venv .venv
source .venv/bin/activate      # on Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env           # then fill in your keys
```

Every environment variable (required vs optional, defaults, what each one does) is documented in [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md). The ones you actually need to get going: `FLASK_SECRET_KEY`, `GEMINI_API_KEY` and/or `ANTHROPIC_API_KEY`, `CLERK_PUBLISHABLE_KEY`, `CLERK_JWT_ISSUER`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `SUPABASE_SECRET_KEY`.

Heads up: `.env.example` is set up for local dev (`CLERK_ENFORCE_AUTH=false`), so don't ship it to production as-is. Production turns auth enforcement on automatically anyway, but set `DAILY_BUDGET_USD` and `CRON_SECRET` yourself before a real deploy. And never commit real keys (ask me how I know °~°).

Then run it:

```bash
uvicorn asgi:fastapi_app --reload     # same setup as Vercel, runs on :8000
# or
python3 app.py                        # plain Flask, no async /ask, runs on :5001
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

- **Fully offline.** `tests/conftest.py` sets fake credentials and turns off auth enforcement, so everything external (Sefaria, Hebcal, Clerk, Supabase, Gemini, Anthropic) is mocked and you don't need any real keys.
- **Coverage gate.** `pytest.ini` fails the build if `backend/` coverage drops under 85%. That number only goes up, never down.
- **Golden-master rule.** Before I move or refactor anything, I write a test that pins down what it does *right now*, make sure it passes on the old code, then make the change and make sure the same test still passes. (Unless the whole point of the change is fixing that exact behavior, in which case the test gets flipped on purpose.) That's how I pulled a ton of code out of `app.py` into `backend/` without breaking stuff. More in `plan.md` §6.2.
- **CI** (`.github/workflows/ci.yml`) runs the whole suite with coverage, plus `ruff` (non-blocking for now), `pre-commit` with gitleaks + bandit for secrets and security (blocking), `pip-audit` for vulnerable dependencies (non-blocking for now), JS tests, a pa11y WCAG 2.1 AA scan of the main and legal pages in both light and dark mode, and SonarCloud. There's also a scheduled RLS check (`rls-verify.yml`) that makes sure the database access rules are still doing their job.

## Deploying

### Vercel (what shelah.org runs on)

`vercel.json` sends every request to `asgi.py`. All the env vars go in the Vercel dashboard (Settings → Environment Variables). Vercel Cron also hits the budget-check and data-retention jobs on a schedule (that's what `CRON_SECRET` is for).

```bash
vercel link       # first time only
vercel deploy     # preview
vercel --prod     # production
```

### Hosting it yourself

```bash
uvicorn asgi:fastapi_app --host 0.0.0.0 --port 8000 --workers 4    # ASGI
# or
gunicorn app:app --bind 0.0.0.0:5001 --workers 4                   # WSGI (no async /ask)
```

## Where everything lives

```
.
├── app.py                  Flask app: pages, middleware, blueprint wiring
├── asgi.py                 FastAPI wrapper, owns the async /ask pipeline
├── vercel.json             Vercel routing + cron jobs
├── requirements*.txt       Python deps (+ hash-locked *.lock.txt versions)
│
├── backend/                Everything server-side: AI, auth, caching, rate limits,
│   │                       Sefaria, calendar, customs, and all the routes_*.py blueprints
│   └── utils/              text_engine.py + search_provider.py (pulled out of app.py)
│
├── templates/              index.html (the main app) + about, help, glossary,
│                           legal pages, 404
├── static/
│   ├── js/                 ES modules: router, reader, AI/conversation UI, zmanim, calendar...
│   ├── css/                tokens.css (design tokens) + per-feature stylesheets
│   ├── service-worker.js   Offline support
│   └── manifest.webmanifest
│
├── customs/                The 14 community customs datasets (+ schema.json)
├── docs/                   All the deep documentation (see below)
├── scripts/                Utility scripts + sql/ (Supabase schema and RLS policies)
├── tests/                  Python tests
└── tests_js/               JS tests
```

## Docs

| Doc | What's in it |
|---|---|
| [SERVICE_ARCHITECTURE.md](docs/SERVICE_ARCHITECTURE.md) | How the modules fit together and how a request flows through them |
| [API.md](docs/API.md) | Every route: method, path, params, auth, response |
| [ENVIRONMENT.md](docs/ENVIRONMENT.md) | Every environment variable |
| [AI_TOOLS.md](docs/AI_TOOLS.md) | The AI's tool-use layer and all 22 tools |
| [OBSERVABILITY.md](docs/OBSERVABILITY.md) | Logging, request IDs, Sentry, cost tracking, circuit breakers |
| [FRONTEND.md](docs/FRONTEND.md) | Templates, theming, motion rules, JS module map |
| [DATABASE.md](docs/DATABASE.md) | The Supabase schema (generated from the live database) |
| [DEVELOPER_NOTES.md](docs/DEVELOPER_NOTES.md) | Dev notes and conventions |
| [SECURITY.md](docs/SECURITY.md) | Security findings: secrets, RLS, CSP, dependencies, input validation, auth, and how to report a vulnerability |
| [PRIVACY_OPERATIONS.md](docs/PRIVACY_OPERATIONS.md) | Account deletion flow, data retention, GDPR records, breach response |
| [DPIA.md](docs/DPIA.md) | Data Protection Impact Assessment for the AI |
| [RUNBOOKS.md](docs/RUNBOOKS.md) | Deploying, rolling back, incidents, backups, spend guardrails |
| [AGE_AND_SAFETY_POLICY.md](docs/AGE_AND_SAFETY_POLICY.md) | Age policy and the age-appropriate AI output layer |
| [LAUNCH_CHECKLIST.md](docs/LAUNCH_CHECKLIST.md) | The launch checklist, checked off against real evidence |
| [ACCESSIBILITY_AUDIT.md](docs/ACCESSIBILITY_AUDIT.md) | WCAG 2.1 AA color-contrast audit, light and dark |
| [CONTENT_QA.md](docs/CONTENT_QA.md) | How religious accuracy gets reviewed, and the "not rabbinically supervised" disclosure |

Old docs from earlier versions of the project are in [docs/archive/](docs/archive/). I kept them instead of deleting them. There's also a [CHANGELOG.md](CHANGELOG.md) if you want the version-by-version history.

## Contributing

It's just me right now, so there isn't a big contribution process, but issues, bug reports, and small PRs are totally welcome! [CONTRIBUTING.md](CONTRIBUTING.md) has the dev setup, how to run the tests, and the coding rules this repo follows (`.agents/ENGINEERING_RULES.md`). Everyone's expected to follow the [code of conduct](CODE_OF_CONDUCT.md) (adapted from the Contributor Covenant v2.1). Basically, be nice.

## Found a security issue?

Please **don't** open a public issue for it. Email the address in [docs/SECURITY.md](docs/SECURITY.md) (under "Reporting a vulnerability") with the details and, if you can, how to reproduce it. That file also shows up in this repo's Security tab.

## License and credits

- **My code:** [MIT licensed](LICENSE).
- **The website's content:** all the text, branding, and media on the Sh'elah site are © 2026, all rights reserved.

Sh'elah uses and shows a lot of third-party content that keeps its own license, and my MIT license **doesn't** relicense any of it: Sefaria (a mix of CC0, CC-BY, CC-BY-NC, and public domain, depending on the text), Hebcal, Wikipedia (CC-BY-SA), Halachipedia and HebrewBooks, the SILEOT font, and MIT-licensed libraries like Tailwind CSS, DaisyUI, marked, and DOMPurify. Full attributions are in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).

Huge thanks to Sefaria especially. None of this would exist without them.
