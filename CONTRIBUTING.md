# Contributing to Sh'elah

Thanks for your interest in Sh'elah. This is currently a **solo/small project** — there's no dedicated maintainer team, no SLA on review turnaround, and no formal RFC process. That said, bug reports, small fixes, and focused pull requests are genuinely welcome; just keep expectations calibrated to a project run by one person in their spare time.

## Before you start

- For anything beyond a small, obvious fix (typo, broken link, small bug), please **open an issue first** describing what you want to change and why, so we can agree on the approach before you invest time in a PR.
- This is a Jewish learning/halacha application. Sh'elah is explicitly **educational only, not a posek** — see the disclaimer in [README.md](README.md) and [docs/AGE_AND_SAFETY_POLICY.md](docs/AGE_AND_SAFETY_POLICY.md). Changes that touch AI prompts, source citation logic, or the safety/disclaimer layer (`backend/claude.py`) get extra scrutiny, since they affect what users are told about halacha.

## Development setup

```bash
git clone <repo-url>
cd Sh\'elah_app
python3 -m venv .venv            # the Python version is pinned in .python-version (3.14)
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
npm ci                           # only needed for the JS tests and the accessibility scan
pre-commit install               # secret and security scan on every commit
cp .env.example .env             # fill in what you need
```

CI installs from the hash-locked `requirements.lock.txt` and `requirements-dev.lock.txt`. If you change `requirements.txt` or `requirements-dev.txt`, regenerate both lock files in the same PR.

See the "Running it locally" section of [README.md](README.md) for the full setup, [docs/DEVELOPER_NOTES.md](docs/DEVELOPER_NOTES.md) for a map of the code and the dev-server commands, and [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) for what each variable actually does (reconciled against the live code, not just `.env.example`'s prose).

## Running tests

```bash
.venv/bin/python -m pytest      # about 4,700 tests
npm test                        # about 630 Node tests for static/js, the service worker and the relay
```

- The Python suite runs **fully offline** — `tests/conftest.py` sets mock credentials and disables auth enforcement, so no live Clerk/Supabase/Sefaria/Hebcal/Gemini/Anthropic access is required.
- Coverage is gated at `--cov-fail-under=85` against `backend/` (see `pytest.ini`). A PR that drops coverage below the gate will fail CI.
- If you're moving or refactoring existing behavior (not just adding new code), follow this repo's **golden-master rule**: write a characterization test pinning the *current* behavior first, confirm it passes, then make your change — the same test should still pass afterward unless your PR's entire point is to fix that exact behavior, in which case flip it deliberately and say so in the PR description. This is how the `app.py` → `backend/` module extraction shipped without regressions.
- The Node tests need no browser or network either (`node --test`, with fakes for the DOM and `fetch`).
- Frontend/accessibility changes: `npm run test:a11y` runs the same WCAG 2.1 AA scan CI runs, in both the light and the dark theme. A change that only passes in one theme is not finished.

## Coding standards

This repo has one canonical rules document — **[.agents/ENGINEERING_RULES.md](.agents/ENGINEERING_RULES.md)** — covering:

- Responsive layout, accessibility (WCAG 2.1 AA), and motion/animation conventions for any UI change.
- Async safety: no blocking I/O on the FastAPI event loop (`asyncio.to_thread` or async `httpx` only).
- New routes belong in `backend/` blueprint modules, not `app.py`.
- AI request resilience rules (timeout/retry budgets, source-integrity handling) for any `/ask` or model-call change.

Read it before making a non-trivial change; PRs that visibly conflict with it (e.g. a new blocking `requests.get()` on the async path, or a new route added directly to `app.py`) will be asked to fix that before merge.

Linting: `ruff check .` runs in CI (currently non-blocking, but please keep new code clean). Secret and security scanning (`pre-commit run --all-files`: gitleaks and bandit) **is** blocking in CI, so the hook you installed above catches the same things locally. The dependency audit (`pip-audit`) reports in CI without blocking. SonarCloud also analyses every push; its quality gate needs to stay green.

## Pull request process

1. Fork/branch, make your change, keep the diff focused on one thing.
2. Make sure `pytest` and `npm test` are green locally and coverage hasn't dropped.
3. Update relevant docs in the same PR if you changed behavior (README, `docs/`, or code comments) — stale docs are worse than no docs. The reference documents are [docs/API.md](docs/API.md) for routes, [docs/FRONTEND.md](docs/FRONTEND.md) for the browser side, [docs/SERVICE_ARCHITECTURE.md](docs/SERVICE_ARCHITECTURE.md) for the backend, [docs/DATABASE.md](docs/DATABASE.md) for the schema, and [docs/ENVIRONMENT.md](docs/ENVIRONMENT.md) for configuration. Add a line to [CHANGELOG.md](CHANGELOG.md) under `[Unreleased]` for anything a user or operator would notice.
4. Open the PR with a short description of *why*, not just *what*. Link the issue it addresses if there is one.
5. Since this is a solo-maintained project, review may take a while — a ping after a week or two with no response is completely fine, not rude.

Commits and pull requests here carry a single author. Please don't add `Co-Authored-By:` trailers or tool-attribution lines (for example from an AI coding assistant) to commit messages or PR descriptions.

## Reporting a security issue

**Do not open a public issue for a security vulnerability.** See [docs/SECURITY.md](docs/SECURITY.md#reporting-a-vulnerability) for how to report one privately.

## Questions

Open an issue, or see [README.md](README.md) for links to the fuller documentation set under `docs/`.
