# Third-Party Licenses & Attribution

Sh'elah's own original code is licensed under the [MIT License](LICENSE). This
document covers the third-party content and libraries the Service displays or
depends on — **none of it is relicensed by Sh'elah's MIT license; each item
below remains under its own license.**

## Content sources

### Sefaria
Torah, Talmud, halachic, and commentary texts are retrieved live from the
[Sefaria](https://www.sefaria.org) API. Sefaria's own corpus is aggregated
from many sources and is **not uniformly licensed** — individual texts and
translations carry a mix of CC0, CC-BY, CC-BY-NC, and public-domain terms,
and **licensing varies per individual text/translation**; some carry more
restrictive terms than others. Sh'elah displays Sefaria content under
Sefaria's own terms of use and does not relicense it; verify the license of
any specific text directly on Sefaria before reuse outside this app. See
[Sefaria's licensing page](https://www.sefaria.org/about) for the
authoritative, per-text breakdown.

### Hebcal
Jewish calendar, holiday, and zmanim (halachic times) data is retrieved from
the [Hebcal](https://www.hebcal.com) API. Hebcal's own license terms apply to
that data; see [Hebcal's developer documentation](https://www.hebcal.com/home/developer-apis)
for details.

### Wikipedia
Used only as a last-resort general-knowledge source when Judaic texts and
computed calendar data cannot answer a question. Wikipedia content is
licensed **CC-BY-SA**, which carries a **share-alike** obligation — any
downstream reuse of Wikipedia-sourced material retrieved through this app
must itself be licensed CC-BY-SA and attributed to Wikipedia. See
[Wikipedia's reuse guide](https://en.wikipedia.org/wiki/Wikipedia:Reusing_Wikipedia_content)
for the full terms.

### Halachipedia / HebrewBooks
Whitelisted external halachic sources used as tier-2 evidence (after Sefaria,
before general web). Each carries its own site terms of use; Sh'elah links to
and quotes brief excerpts from these sources under fair-use / attribution
norms, not a blanket relicense.

## Fonts

### SILEOT
`static/fonts/SILEOT.woff` / `SILEOT.woff2` — filename and file identify this
as **Ezra SIL** (also distributed as "SIL Ezra"), a Hebrew biblical OpenType
typeface published by [SIL International](https://software.sil.org/ezra/)
(the upstream distribution's own TrueType file is itself named
`SILEOT.ttf`, matching this repo's asset name). Ezra SIL's font software is
licensed under the **SIL Open Font License (OFL) 1.1**, Copyright (c)
1997–2007 SIL International, with Reserved Font Names "SIL" and "Ezra"; the
OFL permits embedding, use, redistribution, and modification (including in a
web/`@font-face` context) provided the font is not sold on its own and any
modified versions are renamed away from the reserved names. A separate
Hebrew-layout-intelligence component of the original release is
Copyright (c) 2003 & 2007 Ralph Hancock and John Hudson under the MIT/X11
License. **Residual caveat:** this identification is based on the shipped
filename matching SIL's own canonical distribution name and on SIL's public
licensing terms for the typeface family, not on decoding the compressed
WOFF/WOFF2 `name` table byte-for-byte — a final binary-level check (e.g.
`fonttools ttx` against the decompressed font) is still recommended before
public launch, per `docs/LAUNCH_CHECKLIST.md`, but the OFL 1.1 attribution
above should be treated as materially correct rather than a placeholder.

## Open-source libraries — frontend (JavaScript/CSS)

The following libraries are used in the Service's own frontend code (distinct
from the content-source licenses above). Versions below are the ones actually
pinned/loaded in this repo, confirmed against `package-lock.json` (npm
devDependency) or the CDN `<script>`/`<link>` tags in `templates/index.html`
(loaded at runtime, not npm-managed):

- **Tailwind CSS** `3.4.19` (npm devDependency, `package-lock.json`) — MIT —
  https://github.com/tailwindlabs/tailwindcss
- **DaisyUI** `4.12.24` (CDN, `templates/index.html`) — MIT —
  https://github.com/saadeghi/daisyui
- **marked** `15.0.12` (CDN, `templates/index.html`) — MIT —
  https://github.com/markedjs/marked

A typical MIT notice (see each project for its exact copyright line):

```
MIT License

Copyright (c) <year> <copyright holders>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### DOMPurify (not MIT — dual-licensed)
- **DOMPurify** `3.4.9` (CDN, `templates/index.html`) —
  https://github.com/cure53/DOMPurify

DOMPurify is **not** MIT-licensed. Its own `package.json` declares
`"license": "(MPL-2.0 OR Apache-2.0)"` — a dual license under the **Mozilla
Public License 2.0** or, at the recipient's option, the **Apache License
2.0**. Both permit the use made of it here (bundling a minified,
unmodified build via CDN into a proprietary web app); MPL-2.0's
file-level copyleft only attaches if DOMPurify's own source files are
modified and redistributed, which this project does not do. See DOMPurify's
repository for the full `LICENSE` text of both options.

## Backend (Python) dependencies

Direct dependencies pinned in `requirements.txt`, confirmed against each
installed package's distribution metadata (`pip show`) in this repo's
`.venv`. All are permissively licensed (MIT / BSD-3-Clause / Apache-2.0, or
a permissive dual choice of those) **except one flagged below**:

- **Permissive (MIT / BSD-3-Clause / Apache-2.0):** Flask `3.1.3`
  (BSD-3-Clause), FastAPI `0.141.1` (MIT), Starlette `1.3.1`
  (BSD-3-Clause), Uvicorn `0.35.0` (BSD-3-Clause), Flask-Limiter `3.8.0`
  (MIT), python-dotenv `1.2.2` (BSD-3-Clause), requests `2.33.1`
  (Apache-2.0), httpx `0.28.1` (BSD-3-Clause), pyluach `2.3.0` (MIT),
  tzdata `2026.2` (Apache-2.0), timezonefinder `8.2.2` (MIT), anthropic
  SDK `0.89.0` (MIT), google-genai `2.6.0` (Apache-2.0), tenacity `9.1.2`
  (Apache-2.0), PyJWT `2.13.0` (MIT), cryptography `50.0.0`
  (Apache-2.0 OR BSD-3-Clause, dual), supabase `2.28.3` (MIT), python-docx
  `1.1.2` (MIT), reportlab `4.2.2` (BSD-style license, per its own
  `license.txt`), sentry-sdk `2.63.0` (MIT).

- **⚠️ `zmanim` `0.3.1`** (`backend/zmanim_engine.py`) — **NOT** a simple
  permissive license. Its PyPI/installed metadata classifier reads
  `License :: OSI Approved :: GNU Lesser General Public License v2 or
  later (LGPLv2+)`. LGPLv2+ is a "weak copyleft" library license: this
  project's own code may use the library and remain under its own MIT
  license (dynamic use/import doesn't trigger the copyleft), but
  redistributing the `zmanim` package itself (e.g. bundling it into a
  distributed artifact) carries LGPL obligations — including making the
  LGPL-covered source available and, per §6 of LGPLv2, allowing the
  library to be replaced/relinked with a modified version. No modifications
  to `zmanim`'s own source are made in this repo. Flagged here rather than
  silently grouped with the permissive set above, per this document's own
  "not simply MIT/BSD/Apache-2.0" standard.

## AI model providers

Anthropic (Claude) and Google (Gemini) provide the AI synthesis models used
by the Service under their respective commercial API terms — not open-source
licenses. See `docs/AI_TOOLS.md` and `templates/ai-disclosure.html` for how
these are used.

---

See also: [LICENSE](LICENSE) (Sh'elah's own MIT license), [/licenses](templates/licenses.html)
(the in-app attribution page), `templates/terms.html` §9 (Intellectual
Property; Third-Party Content & Attribution).
