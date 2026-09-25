/**
 * Tests for scripts/a11y_dark_scan.js — the dark-theme half of the WCAG gate,
 * which had zero test coverage because it is a CLI script that used to call
 * main() unconditionally at module load, making it impossible to `require()`
 * without launching a real Chrome and hitting real dev-server URLs.
 *
 * scripts/a11y_dark_scan.js now guards that call with `require.main ===
 * module` and takes its puppeteer/pa11y/config-path dependencies as optional
 * parameters (defaulting to the real ones), so the CLI's own behaviour is
 * unchanged but the module can be `require()`d and driven with fakes here.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const a11yDarkScan = require('../scripts/a11y_dark_scan.js');

function makeFakePage({ theme = 'dark' } = {}) {
    const record = { evaluateOnNewDocumentCalls: [], gotoCalls: [], evaluateCalls: 0, closed: false };
    return {
        record,
        async evaluateOnNewDocument(fn, ...args) {
            record.evaluateOnNewDocumentCalls.push({ fn, args });
        },
        async goto(url, opts) {
            record.gotoCalls.push({ url, opts });
        },
        async evaluate() {
            record.evaluateCalls += 1;
            return theme;
        },
        async close() {
            record.closed = true;
        },
    };
}

function makeFakeBrowser(pages) {
    let i = 0;
    const record = { newPageCalls: 0, closed: false };
    return {
        record,
        async newPage() {
            record.newPageCalls += 1;
            return pages[Math.min(i++, pages.length - 1)];
        },
        async close() {
            record.closed = true;
        },
    };
}

function makeStubPa11y(resultOrFn) {
    const calls = [];
    const fn = async (url, opts) => {
        calls.push({ url, opts });
        return typeof resultOrFn === 'function' ? resultOrFn(url, opts) : resultOrFn;
    };
    fn.calls = calls;
    return fn;
}

function writeTempConfig(obj) {
    const file = path.join(os.tmpdir(), `a11y-dark-scan-test-${process.pid}-${crypto.randomUUID()}.json`);
    fs.writeFileSync(file, JSON.stringify(obj));
    return file;
}

// ── loadConfig ───────────────────────────────────────────────────────────

test('loadConfig reads the real .pa11yci.json and strips underscore-prefixed comment keys', () => {
    const { defaults, urls } = a11yDarkScan.loadConfig();
    assert.ok(urls.length > 0, 'the real config must list at least one URL');
    assert.equal(defaults.standard, 'WCAG2AA');
    for (const key of Object.keys(defaults)) {
        assert.ok(!key.startsWith('_'), `defaults must not leak comment key "${key}" through to pa11y`);
    }
});

test('loadConfig defaults to an empty URL list when "urls" is missing', () => {
    const file = writeTempConfig({ defaults: {} });
    try {
        const { urls } = a11yDarkScan.loadConfig(file);
        assert.deepEqual(urls, []);
    } finally {
        fs.unlinkSync(file);
    }
});

// ── checkUrl ─────────────────────────────────────────────────────────────

test('checkUrl seeds the theme key, navigates, and asserts the page actually rendered dark', async () => {
    const page = makeFakePage({ theme: 'dark' });
    const browser = makeFakeBrowser([page]);
    const pa11yFn = makeStubPa11y({ issues: [] });

    const result = await a11yDarkScan.checkUrl(browser, 'http://example.test/', { timeout: 1234 }, pa11yFn);

    assert.deepEqual(result, { issues: [] });
    assert.equal(page.record.gotoCalls[0].url, 'http://example.test/');
    assert.equal(page.record.gotoCalls[0].opts.timeout, 1234);
    assert.equal(page.record.evaluateOnNewDocumentCalls[0].args[0], a11yDarkScan.THEME_PREFS_KEY);
    assert.equal(pa11yFn.calls[0].url, 'http://example.test/');
    assert.equal(pa11yFn.calls[0].opts.browser, browser);
    assert.equal(pa11yFn.calls[0].opts.page, page);
    assert.equal(pa11yFn.calls[0].opts.ignoreUrl, true);
    assert.equal(page.record.closed, true, 'the page must always be closed');
});

test('checkUrl defaults the navigation timeout to 30000ms when none is configured', async () => {
    const page = makeFakePage({ theme: 'dark' });
    const browser = makeFakeBrowser([page]);
    await a11yDarkScan.checkUrl(browser, 'http://example.test/', {}, makeStubPa11y({ issues: [] }));
    assert.equal(page.record.gotoCalls[0].opts.timeout, 30000);
});

test('checkUrl throws a descriptive error (and still closes the page) when the page did not render dark', async () => {
    const page = makeFakePage({ theme: 'light' });
    const browser = makeFakeBrowser([page]);

    await assert.rejects(
        () => a11yDarkScan.checkUrl(browser, 'http://example.test/', {}, makeStubPa11y({ issues: [] })),
        (err) => {
            assert.match(err.message, /did not render in dark theme/);
            assert.match(err.message, /data-theme="light"/);
            return true;
        },
    );
    assert.equal(page.record.closed, true, 'the page must be closed even when the theme assertion fails');
});

test('checkUrl still closes the page when pa11y itself throws', async () => {
    const page = makeFakePage({ theme: 'dark' });
    const browser = makeFakeBrowser([page]);
    const throwingPa11y = async () => { throw new Error('pa11y boom'); };

    await assert.rejects(
        () => a11yDarkScan.checkUrl(browser, 'http://example.test/', {}, throwingPa11y),
        /pa11y boom/,
    );
    assert.equal(page.record.closed, true);
});

test('checkUrl seeds a dark preference that MERGES with, rather than overwrites, any existing stored prefs', async () => {
    const page = makeFakePage({ theme: 'dark' });
    const browser = makeFakeBrowser([page]);
    await a11yDarkScan.checkUrl(browser, 'http://example.test/', {}, makeStubPa11y({ issues: [] }));

    const { fn, args } = page.record.evaluateOnNewDocumentCalls[0];
    const key = args[0];
    const store = new Map([[key, JSON.stringify({ locale: 'en' })]]);
    const fakeWindow = {
        localStorage: {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, v),
        },
    };

    // evaluateOnNewDocument's real job is to ship `fn` into the PAGE's own
    // browser context (via CDP, as source text) where `window` is a global;
    // it never runs in this Node process. Running it here against a
    // temporary global.window is the only way to unit-test that inlined
    // logic (it can't call out to a helper -- Puppeteer serializes it alone).
    const savedWindow = global.window;
    global.window = fakeWindow;
    try {
        fn(key);
    } finally {
        if (savedWindow === undefined) delete global.window;
        else global.window = savedWindow;
    }

    assert.deepEqual(JSON.parse(store.get(key)), { locale: 'en', theme: 'dark' },
        "the existing 'locale' field must survive; only 'theme' should change");
});

test("checkUrl's seed function falls through to a fresh blob when the existing stored value is invalid JSON", async () => {
    const page = makeFakePage({ theme: 'dark' });
    const browser = makeFakeBrowser([page]);
    await a11yDarkScan.checkUrl(browser, 'http://example.test/', {}, makeStubPa11y({ issues: [] }));

    const { fn, args } = page.record.evaluateOnNewDocumentCalls[0];
    const key = args[0];
    const store = new Map([[key, 'not valid json{{']]);
    const fakeWindow = {
        localStorage: {
            getItem: (k) => (store.has(k) ? store.get(k) : null),
            setItem: (k, v) => store.set(k, v),
        },
    };
    const savedWindow = global.window;
    global.window = fakeWindow;
    try {
        assert.doesNotThrow(() => fn(key));
    } finally {
        if (savedWindow === undefined) delete global.window;
        else global.window = savedWindow;
    }
    assert.deepEqual(JSON.parse(store.get(key)), { theme: 'dark' });
});

// ── main ─────────────────────────────────────────────────────────────────

async function withExitCode(run) {
    const saved = process.exitCode;
    process.exitCode = undefined;
    try {
        await run();
        return process.exitCode;
    } finally {
        process.exitCode = saved;
    }
}

test('main() logs an error and sets a failing exit code when no URLs are configured', async () => {
    const emptyConfig = writeTempConfig({ defaults: {}, urls: [] });
    try {
        const puppeteerLib = { launch: async () => { throw new Error('must not launch a browser with no URLs'); } };
        const exitCode = await withExitCode(() => a11yDarkScan.main(puppeteerLib, makeStubPa11y({ issues: [] }), emptyConfig));
        assert.equal(exitCode, 1);
    } finally {
        fs.unlinkSync(emptyConfig);
    }
});

test('main() runs every configured URL, counts pa11y-reported failures, and sets exit code 1 when any URL has issues', async () => {
    const cfg = writeTempConfig({
        defaults: { standard: 'WCAG2AA' },
        urls: ['http://example.test/a', 'http://example.test/b'],
    });
    try {
        const pages = [makeFakePage({ theme: 'dark' }), makeFakePage({ theme: 'dark' })];
        const browser = makeFakeBrowser(pages);
        const puppeteerLib = { launch: async () => browser };
        const pa11yFn = makeStubPa11y((url) => ({
            issues: url.endsWith('/b') ? [{ code: 'X', message: 'bad contrast', selector: '.foo' }] : [],
        }));

        const exitCode = await withExitCode(() => a11yDarkScan.main(puppeteerLib, pa11yFn, cfg));

        assert.equal(pa11yFn.calls.length, 2);
        assert.equal(exitCode, 1, 'any URL with pa11y issues must fail the whole run');
        assert.equal(browser.record.closed, true);
    } finally {
        fs.unlinkSync(cfg);
    }
});

test('main() leaves the exit code untouched when every URL passes clean', async () => {
    const cfg = writeTempConfig({ defaults: {}, urls: ['http://example.test/a'] });
    try {
        const browser = makeFakeBrowser([makeFakePage({ theme: 'dark' })]);
        const puppeteerLib = { launch: async () => browser };
        const exitCode = await withExitCode(() => a11yDarkScan.main(puppeteerLib, makeStubPa11y({ issues: [] }), cfg));
        assert.equal(exitCode, undefined);
        assert.equal(browser.record.closed, true);
    } finally {
        fs.unlinkSync(cfg);
    }
});

test('main() still closes the browser when a URL throws (e.g. the dark-theme assertion fails) mid-run', async () => {
    const cfg = writeTempConfig({
        defaults: {},
        urls: ['http://example.test/a', 'http://example.test/b'],
    });
    try {
        const pages = [makeFakePage({ theme: 'light' }), makeFakePage({ theme: 'dark' })];
        const browser = makeFakeBrowser(pages);
        const puppeteerLib = { launch: async () => browser };

        await assert.rejects(
            () => a11yDarkScan.main(puppeteerLib, makeStubPa11y({ issues: [] }), cfg),
            /did not render in dark theme/,
        );
        assert.equal(browser.record.closed, true, 'the browser must still be closed when a URL throws mid-loop');
        assert.equal(browser.record.newPageCalls, 1, 'a throwing URL aborts the run rather than continuing to the next one');
    } finally {
        fs.unlinkSync(cfg);
    }
});

test('main() passes the configured chromeLaunchConfig through to puppeteer.launch', async () => {
    const cfg = writeTempConfig({
        defaults: { chromeLaunchConfig: { args: ['--no-sandbox'] } },
        urls: ['http://example.test/a'],
    });
    try {
        const launchCalls = [];
        const browser = makeFakeBrowser([makeFakePage({ theme: 'dark' })]);
        const puppeteerLib = { launch: async (opts) => { launchCalls.push(opts); return browser; } };
        await a11yDarkScan.main(puppeteerLib, makeStubPa11y({ issues: [] }), cfg);
        assert.deepEqual(launchCalls[0], { args: ['--no-sandbox'] });
    } finally {
        fs.unlinkSync(cfg);
    }
});
