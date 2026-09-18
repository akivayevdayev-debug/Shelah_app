#!/usr/bin/env node
'use strict';

/**
 * Dark-theme half of the WCAG 2.1 AA gate (plan.md §26.2 / Prompt 39 STEP 2).
 *
 * `pa11y-ci --config .pa11yci.json` only ever exercises light theme: headless
 * Chrome resolves `prefers-color-scheme` to light in CI, and — verified, not
 * assumed — templates/index.html's boot code normalizes an *absent* stored
 * preference to 'light' outright (templates/index.html's
 * `normalizeThemePreference` returns 'light' as its fallback, and
 * hydratePreferences() re-applies it after the FOUC script runs), so even a
 * browser that *did* report `prefers-color-scheme: dark` would still render
 * the page light. The only thing that reliably selects dark theme is the
 * stored preference itself: localStorage key "Sh'elahPrefs", a JSON blob
 * whose `theme` field is read by templates/index.html's <head> script and by
 * templates/components/legal_scripts.html for all seven legal pages.
 *
 * pa11y has no action for evaluating JavaScript (its action grammar is
 * navigate / click / set field / clear field / check field / screen capture /
 * wait for …, confirmed by reading node_modules/pa11y/lib/action.js), so the
 * localStorage seed cannot be expressed in .pa11yci.json. pa11y *does*
 * support being handed an already-navigated page via its `browser` + `page` +
 * `ignoreUrl` options, which is what this script uses: seed localStorage
 * before first paint with page.evaluateOnNewDocument, navigate, assert the
 * page actually went dark, then let pa11y audit that page.
 *
 * .pa11yci.json stays the single source of truth for the URL list and every
 * scan option — this script reads them from there rather than restating them,
 * so a page added to the light scan is automatically covered by the dark one.
 *
 * Run via `npm run test:a11y` (which runs light then dark) against a live dev
 * server. See README.md / docs/ACCESSIBILITY_AUDIT.md.
 */

const fs = require('node:fs');
const path = require('node:path');

const REPO_ROOT = path.resolve(__dirname, '..');
const CONFIG_PATH = path.join(REPO_ROOT, '.pa11yci.json');

// Deliberately pa11y's OWN puppeteer rather than a second declared copy: this
// script launches the browser that pa11y then drives, and a version skew
// between the two would be a genuine hazard (pa11y 9 pins puppeteer ^24).
// Resolving through pa11y's module path also means this does not silently
// depend on npm having hoisted puppeteer to the top level.
const pa11y = require('pa11y');
const puppeteer = require(
    require.resolve('puppeteer', { paths: [path.dirname(require.resolve('pa11y'))] }));

// Must stay in sync with templates/index.html's APP_PREFS_KEY and
// templates/components/legal_scripts.html's APP_PREFS_KEY. The assertion in
// checkUrl() below is what makes a drift here fail loudly instead of silently
// re-running the light-theme scan a second time.
const THEME_PREFS_KEY = "Sh'elahPrefs";

function loadConfig() {
    const raw = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
    const defaults = { ...raw.defaults };
    // Comment keys are documentation for humans; pa11y rejects unknown ones.
    for (const key of Object.keys(defaults)) {
        if (key.startsWith('_')) {
            delete defaults[key];
        }
    }
    return { defaults, urls: raw.urls || [] };
}

async function checkUrl(browser, url, defaults) {
    const page = await browser.newPage();
    try {
        await page.evaluateOnNewDocument((key) => {
            // Merge rather than overwrite, mirroring how the app itself
            // writes this blob (legal_scripts.html's setThemePreference).
            let prefs = {};
            try {
                prefs = JSON.parse(window.localStorage.getItem(key) || '{}');
            } catch (_) { /* fall through to a fresh blob */ }
            prefs.theme = 'dark';
            window.localStorage.setItem(key, JSON.stringify(prefs));
        }, THEME_PREFS_KEY);

        await page.goto(url, {
            waitUntil: 'networkidle2',
            timeout: defaults.timeout || 30000,
        });

        const resolvedTheme = await page.evaluate(
            () => document.documentElement.dataset.theme);
        if (resolvedTheme !== 'dark') {
            throw new Error(
                `${url} did not render in dark theme (data-theme="${resolvedTheme}"). ` +
                `The stored-preference key this script seeds (${THEME_PREFS_KEY}) no longer ` +
                'matches what the page\'s theme script reads — fix scripts/a11y_dark_scan.js ' +
                'rather than deleting this check, or the dark scan silently becomes a second ' +
                'light-theme scan.');
        }

        return await pa11y(url, {
            ...defaults,
            browser,
            page,
            ignoreUrl: true,
        });
    } finally {
        await page.close();
    }
}

async function main() {
    const { defaults, urls } = loadConfig();
    if (!urls.length) {
        console.error('No URLs configured in .pa11yci.json');
        process.exitCode = 1;
        return;
    }

    const browser = await puppeteer.launch(defaults.chromeLaunchConfig || {});
    let failures = 0;

    try {
        console.log(`Running pa11y (dark theme) on ${urls.length} URLs:`);
        for (const url of urls) {
            const result = await checkUrl(browser, url, defaults);
            const issues = result.issues || [];
            if (issues.length) {
                failures += 1;
                console.log(` > ${url} - ${issues.length} errors`);
                for (const issue of issues) {
                    console.log(`   • ${issue.code}: ${issue.message}`);
                    console.log(`     (${issue.selector})`);
                }
            } else {
                console.log(` > ${url} - 0 errors`);
            }
        }
    } finally {
        await browser.close();
    }

    console.log('');
    if (failures) {
        console.log(`${urls.length - failures}/${urls.length} URLs passed (dark theme)`);
        process.exitCode = 1;
    } else {
        console.log(`${urls.length}/${urls.length} URLs passed (dark theme)`);
    }
}

main().catch((error) => {
    console.error(error);
    process.exitCode = 1;
});
