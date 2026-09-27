# Sh'elah

**[shelah.org](https://shelah.org)** — the Torah Encyclopedia (literally, that's kind of the whole idea lol)

Sh'elah (שאלה, "question") is a Jewish learning website I built by myself. It does three things:

1. A searchable library of Torah and Judaic texts and commentary (all powered by Sefaria's texts, but made to actually feel easy to use), with customs broken out by community: Ashkenazi, Sefardi, Teimani, and a bunch of others.
2. An AI assistant that answers questions on Jewish law (halacha) by pulling from primary sources like the Torah and other Judaic texts instead of just guessing.
3. The practical stuff people actually need every day: live prayer times (zmanim) and candle-lighting times based on where you are, and a Hebrew calendar with the weekly Torah portion built in.

Quick but important thing before anything else: **Sh'elah is for learning, not for rulings.** The AI isn't a rabbi and doesn't pretend to be one. Every answer ends by telling you to take real questions to your own rabbi, and I mean that.

## What it does right now

- Answers halacha questions with the actual sources it used cited, and always points you back to your own rabbi for anything that's a real ruling.
- Refuses to touch anything medical, mental-health, or abuse-related. Instead of the AI trying to answer, it sends you to real help. Halacha touches sensitive stuff, and a chatbot is the wrong thing to be answering those questions.
- Has safeguards against weird off-topic questions and prompt injection (because we don't want the site getting hacked lol).
- A Torah/Judaic text library with commentary, word translations, and community-specific customs.
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

- Gave the AI real tool use. It now fetches live prayer times, calendar dates, and texts when it needs them, instead of having everything pre-loaded into every single request. Judaic texts always come first, and web search is strictly a last resort.
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

## The stack

- **Backend:** Flask + FastAPI together. FastAPI handles the async AI `/ask` pipeline and Flask handles everything else (it's mounted inside the FastAPI app). New routes live in `backend/` as their own modules, not in `app.py`, which is already way too big.
- **Hosting:** Vercel, as a serverless function.
- **Database:** Supabase (Postgres), with row-level security so users can only see their own stuff.
- **Auth:** Clerk.
- **AI:** Gemini is the main model, and Claude is the backup if Gemini fails. Both go through the same guardrails (there are tests for that). The AI uses tools to fetch texts, zmanim, and calendar data when it needs them instead of getting everything shoved into the prompt.
- **Texts and calendar data:** Sefaria for texts, Hebcal for the calendar. Plus 16 community customs datasets in `customs/`.
- **Keeping it alive:** Turnstile for bot checks, Sentry and a Discord webhook for errors, SonarCloud for code quality, and a lot of CI.

If you want the deep technical details, they're in [`docs/`](docs/). [SERVICE_ARCHITECTURE.md](docs/SERVICE_ARCHITECTURE.md) is the best place to start, [AI_TOOLS.md](docs/AI_TOOLS.md) covers the AI's tools, and [AGE_AND_SAFETY_POLICY.md](docs/AGE_AND_SAFETY_POLICY.md) covers the safety stuff.

## Running it locally

You'll need Python 3.14 (that's what CI runs), plus your own Clerk project, Supabase project, and a Gemini and/or Anthropic API key.

```bash
git clone https://github.com/akivayevdayev-debug/Shelah_app.git
cd Shelah_app

python3 -m venv .venv
source .venv/bin/activate      # on Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env           # then fill in your keys
```

Every environment variable is documented in [`docs/ENVIRONMENT.md`](docs/ENVIRONMENT.md). Heads up: `.env.example` is set up for local dev (auth enforcement is off by default), so don't ship it to production as-is.

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

The tests run fully offline. Everything external (Sefaria, Hebcal, Clerk, Supabase, Gemini, Anthropic) is mocked, so you don't need any real keys. The build fails if backend coverage drops under 85%.

## Found a security issue?

Please don't open a public issue for it. The contact info and details are in [`docs/SECURITY.md`](docs/SECURITY.md).

## License and credits

My code is [MIT licensed](LICENSE). The website's own text, branding, and media are © 2026, all rights reserved.

The texts themselves come from Sefaria, and the calendar data comes from Hebcal. Those (and everything else Sh'elah uses, like Wikipedia, Halachipedia, HebrewBooks, and the fonts and libraries) keep their own licenses, which my MIT license doesn't change. All of that is listed in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). Huge thanks to Sefaria especially. None of this would exist without them.
