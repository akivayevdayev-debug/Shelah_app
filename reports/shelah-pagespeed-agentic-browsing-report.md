# Sh'elah PageSpeed and Agentic Browsing Remediation Report

**Site audited:** https://shelah.org  
**PageSpeed report:** October 9, 2026, 3:34 AM UTC  
**Scope:** Every reported performance and Agentic Browsing issue in the supplied mobile and desktop Lighthouse runs, plus verification of the live AI-facing endpoints.

## Scorecard

- **Mobile:** Performance **60**; FCP **5.7 s**; LCP **10.7 s**; TBT **10 ms**; CLS **0**; Speed Index **6.4 s**.
- **Desktop:** Performance **77**; FCP **1.0 s**; LCP **1.1 s**; TBT **380 ms**; CLS **0.094**; Speed Index **1.2 s**.
- **Agentic Browsing:** **2/3 on both mobile and desktop**.
- **Field data:** No Chrome User Experience Report data is available for this page, so these are lab results from a single initial-load session, not a field-user percentile. [1] [2]

The main risk is mobile first render. The same application that is acceptable on desktop is shipping a large, highly dynamic shell to a simulated Moto G Power over slow 4G. The LCP element is ordinary library-description text, but it experiences approximately **2.48 seconds of element render delay**. The report also identifies a **3.633-second maximum critical request path** and approximately **1.564 MiB** of total transfer. [1]

## P0 — Fix the Agentic Browsing failure on both devices

### 1. `llms.txt` does not meet the report’s discoverability recommendation

**PageSpeed finding:** `llms.txt does not follow recommendations`; the report’s specific error is: **“File does not appear to contain any links.”** This is the only failed Agentic Browsing audit in both runs. [1] [2]

**Live verification:** `https://shelah.org/llms.txt` currently returns `200 text/plain` and contains an H1 plus links, but every listed page points to the old `https://shelah-app.vercel.app/` host rather than the canonical `https://shelah.org/` host. That stale host reference is an agent-discoverability and canonicalization defect even though the current plain-text response appears to contain Markdown links. [3]

**Fix for Claude Code:**

- Rewrite every URL in `llms.txt` to the canonical `https://shelah.org/...` origin.
- Keep valid Markdown links with absolute HTTPS URLs.
- Include the canonical homepage, primary library/text routes, Ask Sh'elah route, legal and policy pages, accessibility statement, AI disclosure, glossary, help, and sitemap.
- Ensure the file is served directly as `text/plain; charset=utf-8`, with no HTML fallback, redirect, login wall, or content transformation.
- Add a deployment test that fetches `/llms.txt`, checks for an H1, checks for at least one Markdown link, rejects `shelah-app.vercel.app` links, and verifies every URL has a successful canonical response.
- Re-run PageSpeed after deployment. If the audit still says no links, inspect the exact response received by Lighthouse rather than relying on the browser-rendered page.

### 2. `/ai-catalog.json` is missing in production

PageSpeed reports the `ai-catalog.json` schema as valid, but its audit is marked **Not applicable**, not passed. Direct verification of `https://shelah.org/ai-catalog.json` returns **404 HTML**, not JSON. [1] [2] [4]

This is not the reported failed audit, but it means the site does not currently expose a usable machine-readable catalog at the conventional path. It should be treated as an agentic-browsing gap.

**Fix for Claude Code:**

- Publish a real `/ai-catalog.json` with `Content-Type: application/json`.
- Use the schema expected by the project’s AI/ARD integration and include canonical URLs, human-readable names, descriptions, supported actions, required inputs, authentication requirements, safety limitations, and whether an action is read-only or mutating.
- Do not advertise an action unless it works for an unauthenticated agent or clearly declares its authentication requirement.
- Include the catalog URL from `llms.txt` and link it from the site’s agent documentation.
- Add CI validation for JSON syntax, schema, canonical-host URLs, and HTTP `200` production delivery.

### 3. Keep the two already-passing agentic checks intact

The report says the **accessibility tree is well formed** and **CLS is 0 on mobile**. WebMCP form coverage, WebMCP tool registration, and catalog validation were not applicable or had no reported error in this run. Do not regress these while adding the catalog or annotations. [1] [2]

## P1 — Mobile performance blockers

### 4. Render-blocking requests: estimated savings **3,370 ms**

Requests block the initial render and delay LCP. The mobile run identifies the page’s broad stylesheet and many feature-specific styles/scripts in the critical path, including `style.css`, `tokens.css`, `reader.css`, `conversation.css`, `ai.css`, `sidebar.css`, `calendar.css`, `siddur.css`, `tailwind.css`, `load-region.js`, `hebrew-ref.js`, `actions.js`, Sentry initialization, Clerk appearance, and other feature modules. [1]

**Fix:** Split the homepage critical CSS from reader, siddur, calendar, history, and conversation CSS. Inline only the small above-the-fold shell styles; load feature CSS on route or component activation. Mark non-critical scripts `defer` or dynamically import them after first paint. Do not defer code required for the first usable search/Ask Sh'elah control until after the control is interactive.

### 5. Inefficient cache lifetimes: estimated savings **388 KiB**

Many versioned first-party assets are served with approximately **5-minute** cache lifetimes, including fonts, CSS, and JavaScript. [1]

**Fix:** For content-hashed or immutable versioned assets, send `Cache-Control: public, max-age=31536000, immutable`. Keep HTML and API responses separately cache-controlled. If query-string versions remain instead of content hashes, make the deployment process guarantee that each version is immutable and purge only HTML references.

### 6. Excessive unused CSS: estimated savings **181 KiB**

The largest single item is the JSDelivr `full.min.css`, with approximately **136.6 KiB** estimated unused. First-party `style.css` contributes about **33.3 KiB** unused and `conversation.css` about **11.0 KiB** unused. [1]

**Fix:** Remove the global CDN stylesheet from the homepage critical path. Replace it with a route/component build that includes only the rules used by the home shell. Purge unused utility classes only after accounting for dynamically generated class names. Load reader, conversation, calendar, and siddur styles only when those surfaces are opened.

### 7. Excessive unused JavaScript: estimated savings **247 KiB**

The report attributes approximately **160.1 KiB** to JSDelivr modules and **86.6 KiB** to first-party initial-page JavaScript. Major candidates include FullCalendar, Clerk, Motion, and the large inline initial-route/hydration block. [1]

**Fix:** Use route-level dynamic imports. Do not initialize FullCalendar, Siddur, reader, commentary preload, history, or calendar detail on the home route. Load Clerk only when the profile/sign-in surface is opened, unless a specific authentication requirement makes early loading unavoidable. Split the inline hydration gate into a minimal bootstrap and deferred feature modules.

### 8. Minification gaps: estimated savings **42 KiB CSS** and **107 KiB JavaScript**

The CSS savings are concentrated in `style.css`, `tokens.css`, `ai.css`, `conversation.css`, `calendar.css`, `sidebar.css`, `siddur.css`, and `loading.css`. The JavaScript savings include the inline route gate and modules such as `conversation-ui.js`, `router.js`, `calendar-detail.js`, `motion.js`, `conversation-store.js`, `zmanim.js`, `conversation-api.js`, and related modules. [1]

**Fix:** Make production minification and compression mandatory in the build. Prefer Brotli for text assets, verify that the deployed response—not only the local artifact—is minified, and avoid shipping large formatted inline scripts.

### 9. Forced reflow

The mobile trace reports forced layout work. The top reported call is Sentry’s bundle at `/10.69.0/bundle.min.js:3:59313` with **36 ms** reflow time. First-party sources include an approximately **61 ms** aggregate entry at one page source location and **29 ms** from `conversation-ui.js:747:32`. [1]

**Fix:** Batch DOM writes before reads, avoid reading `offsetWidth`/`offsetHeight` after class or DOM mutations, use `requestAnimationFrame` for visual updates, and isolate conversation/readers from global layout recalculation. Confirm whether Sentry instrumentation is contributing and move nonessential initialization after the first interaction.

### 10. Large DOM: **3,255 elements**, depth **13**, most children **46**

The library tree is rendered into the initial DOM even though much of it is below the fold or not yet needed. The report identifies a Hoshea button in the deep tree and a `.library-tree-children` container with 46 children. [1]

**Fix:** Render only the visible library branch initially; virtualize or progressively disclose the tree; avoid shipping all catalog branches in the initial HTML; and mount prayer, calendar, and conversation panels on demand. This should reduce style calculation, layout, memory, and accessibility-tree work.

### 11. Large network payload: **1,564 KiB**

First-party transfer is approximately **695.3 KiB**, including `/library/index` at **387.3 KiB**, the HTML at **185.5 KiB**, `SILEOT.woff2` at **48.5 KiB**, and `style.css` at **40.7 KiB**. CDN transfer is approximately **352.6 KiB**, led by `full.min.css` at **137.9 KiB**, Clerk at **86.6 KiB**, FullCalendar at **80.4 KiB**, and Motion at **47.7 KiB**. [1]

**Fix:** Do not fetch the full library index on initial load. Return a compact summary and fetch branches/search results on demand. Remove duplicate CDN and first-party libraries, subset or defer fonts, and use compressed, cacheable assets.

### 12. LCP render delay and critical request chain

The LCP element is the library description text. The breakdown reports **50 ms TTFB** and approximately **2,480 ms element render delay**. The maximum critical-path latency is **3,633 ms**. The dependency chain includes Google Fonts, many first-party stylesheets, Sentry, `/library/index`, `/api/holidays?year=2026`, and multiple feature scripts. [1]

**Fix:** Render the home shell and library heading immediately from server HTML or a minimal static payload. Do not gate that text on hydration, library-index completion, holiday data, fonts, Sentry, or feature initialization. Self-host or preload only the font actually required above the fold, and use `font-display: swap` or `optional` with metric overrides.

### 13. Five long main-thread tasks

The mobile trace records five long tasks: first-party page work around **125 ms**, **112 ms**, and **95 ms**; `conversation-ui.js` around **59 ms**; and Clerk around **51 ms**. The mobile main-thread breakdown is approximately **1.2 s** total: Other **456 ms**, Style/Layout **319 ms**, Script Evaluation **232 ms**, Script Parsing/Compilation **98 ms**, HTML/CSS parsing **91 ms**, Rendering **13 ms**, and GC **2 ms**. [1]

**Fix:** Break initialization into idle-time chunks, yield between library rendering and conversation boot, defer Clerk/Sentry, and avoid constructing hidden panels during initial load.

### 14. JavaScript execution and main-thread work

The mobile run reports approximately **0.2 s JavaScript execution time** in the dedicated diagnostic and **1.2 s** main-thread work overall. The issue is not only raw execution; style/layout and initialization sequencing are also expensive. [1]

**Fix:** Measure route boot with User Timing marks, then set budgets for initial JS, DOM nodes, style recalculation, and long tasks. Track home, search, text reader, and Ask Sh'elah as separate journeys.

### 15. Non-composited animation

One animated element is reported as not composited. Non-composited animation can increase jank and CLS even though the mobile CLS score happened to be zero in this run. [1]

**Fix:** Animate `transform` and `opacity` instead of layout-affecting properties, promote only the required element with `will-change`, and honor `prefers-reduced-motion`.

### 16. Additional mobile recommendations shown by Lighthouse

These are unscored but are still actionable findings shown in the report: duplicated JavaScript modules; possible image-delivery optimization; LCP request discovery; legacy JavaScript/polyfills; no User Timing instrumentation; and the report’s font-display recommendation. The mobile viewport audit itself passes because the page has `width=device-width, initial-scale=1.0, viewport-fit=cover`. [1]

**Fix:** Consolidate duplicate modules, ensure any LCP image is discoverable and not accidentally lazy-loaded, ship modern ES modules to modern browsers, add `performance.mark()`/`measure()` around route boot and first usable interaction, and use explicit font loading strategy.

## P1 — Desktop performance issues

### 17. Render-blocking requests: estimated savings **510 ms**

Desktop has the same architectural issue but a smaller simulated cost. The report lists `load-region.js`, `hebrew-ref.js`, Clerk appearance, Tailwind, AI, topbar, sidebar, the global stylesheet, typography, prayer, calendar, siddur, halacha, history, loading, Sentry, community labels, conversation, tokens, reader, and other feature assets in the render-blocking set. [2]

Apply the same critical-CSS and route-level JavaScript split described for mobile. Do not optimize desktop separately in a way that leaves mobile with the same global bundle.

### 18. Inefficient cache lifetimes: estimated savings **387 KiB**

The desktop report shows the same approximately **5-minute** TTL pattern for versioned first-party assets. [2]

Apply immutable caching to versioned static assets and separate cache policy for HTML/API responses.

### 19. Main-thread work: **2.6 s**

Desktop’s main-thread breakdown is approximately Other **1,120 ms**, Style/Layout **704 ms**, Script Evaluation **372 ms**, Script Parsing/Compilation **189 ms**, HTML/CSS parsing **160 ms**, and Rendering **79 ms**. [2]

This is the main desktop weakness: desktop paints quickly, but the page remains expensive to initialize and interact with. Prioritize DOM reduction and hidden-feature deferral, not only paint timing.

### 20. Nine long main-thread tasks

Desktop records nine long tasks. The longest listed work includes first-party page tasks of approximately **125 ms**, **112 ms**, and **95 ms**, `conversation-ui.js` around **59 ms**, and Clerk around **51 ms**. [2]

Apply the same chunking, yielding, dynamic import, and third-party deferral plan as mobile. Validate interaction latency after the page appears visually complete.

### 21. Layout-shift culprits and one non-composited animation

Desktop reports a **CLS of 0.094** and exposes the layout-shift diagnostic. It also reports one non-composited animated element. [2]

Reserve space for async library, holiday, conversation, and font content; set dimensions on media; avoid injecting panels above existing content; and animate only composited properties. The explicit image width/height audit passes, so focus on dynamic DOM and font/layout changes rather than only image dimensions.

### 22. Desktop DOM and payload issues

Desktop also reports the same **3,255-element DOM**, DOM depth **13**, maximum child count **46**, duplicated JavaScript, font-display and image-delivery recommendations, and the **1,564 KiB** payload issue. [2]

Use the same shared remediation: progressive library rendering, route-level bundles, cached immutable assets, smaller initial HTML, and deferred third parties.

### 23. Desktop diagnostics that remain worth fixing

Desktop repeats the unused CSS (**182 KiB**), unused JavaScript (**247 KiB**), CSS minification (**42 KiB**), and JavaScript minification (**107 KiB**) findings. It also reports a **2.6-second** main-thread cost and the same agent discoverability failure. [2]

The small one-kilobyte difference in unused CSS between mobile and desktop is not a separate defect; it is the same global-bundle problem measured under two conditions.

## Recommended implementation order for Claude Code

1. **Fix `/llms.txt` and publish `/ai-catalog.json`** on the canonical host; add production endpoint tests.
2. **Stop loading `/library/index` and hidden feature bundles on initial home load.** Render a small library shell and fetch branches lazily.
3. **Split and defer feature CSS/JS**, removing the global CDN stylesheet from the critical path.
4. **Fix the LCP render gate:** make the home heading/description server-rendered or immediately paintable without waiting for hydration, fonts, holidays, Sentry, or library data.
5. **Set long immutable cache headers** for versioned assets; retain short or revalidated caching only for HTML and APIs.
6. **Reduce DOM and layout work:** progressive disclosure/virtualization, batched reads/writes, composited animation, reserved async-content space.
7. **Minify/compress and modernize builds**, then measure home, search, reader, and Ask Sh'elah journeys separately.
8. **Re-run PageSpeed in both modes and add regression budgets** for mobile LCP/FCP, total transfer, DOM count, long tasks, and Agentic Browsing.

## Acceptance targets

- Agentic Browsing: **3/3**, with `llms.txt` passing and a valid canonical `ai-catalog.json` available.
- Mobile LCP: target **≤ 2.5 s** under the same Lighthouse mobile profile; stretch target **≤ 1.8 s**.
- Mobile FCP: target **≤ 1.8 s**.
- Desktop TBT: target **< 200 ms** and fewer than two long tasks on the initial route.
- Initial transfer: target **< 800 KiB** for the home route before user opens reader/calendar/conversation features.
- Initial DOM: target **well below 3,255 elements**, with the library tree progressively rendered.
- Cache: versioned static assets should no longer be limited to 5-minute TTLs.
- Preserve the current good results: accessibility, best practices, SEO, mobile CLS, and the well-formed accessibility tree.

## References

[1]: https://pagespeed.web.dev/analysis/https-shelah-org/py3hhnynsr?form_factor=mobile "PageSpeed Insights mobile report for shelah.org"
[2]: https://pagespeed.web.dev/analysis/https-shelah-org/py3hhnynsr?form_factor=desktop "PageSpeed Insights desktop report for shelah.org"
[3]: https://shelah.org/llms.txt "Live llms.txt served by shelah.org"
[4]: https://shelah.org/ai-catalog.json "Live ai-catalog.json endpoint on shelah.org"
[5]: https://shelah.org/robots.txt "Live robots.txt served by shelah.org"
[6]: https://shelah.org/accessibility "Sh'elah accessibility statement"
