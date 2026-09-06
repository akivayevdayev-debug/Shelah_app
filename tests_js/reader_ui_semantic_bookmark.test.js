/**
 * plan.md §19 Phase 0 — the semantic-bookmark double-bind tripwire.
 *
 * static/js/reader-ui.js's installSemanticBookmarking() must skip binding
 * its own click listener whenever templates/index.html's inline copy has
 * already bound one — checked via an explicit `window.__shelahLegacyBookmarkBound`
 * flag the inline block sets, NOT via `typeof window.saveSemanticBookmark ===
 * "function"` (the old guard), which only worked because the inline copy
 * happened to be a bare top-level classic-script function declaration.
 * Wrapping that block in an IIFE or converting it to a module — exactly what
 * every later §19 extraction phase does — silently breaks the old guard and
 * double-binds the button: two POSTs, two billed AI summaries, two alerts.
 *
 * Run with: node --experimental-vm-modules --test tests_js/
 * (wired into `npm test` in package.json; see tests_js/helpers/esm_harness.js
 * for why the flag is needed to load this real ES module in Node.)
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const READER_UI_PATH = 'static/js/reader-ui.js';

function makeFakeButton() {
    const listeners = [];
    return {
        addEventListener(type, handler) {
            if (type === 'click') listeners.push(handler);
        },
        async dispatchClick() {
            await Promise.all(listeners.map((handler) => handler()));
        },
        get clickListenerCount() {
            return listeners.length;
        },
        dataset: {},
        classList: { add() {}, remove() {} },
        textContent: '',
        disabled: false,
    };
}

function makeFetchMock(jsonBody) {
    const calls = [];
    const fetchFn = async (url, opts) => {
        calls.push({ url, opts });
        return { status: 200, ok: true, json: async () => jsonBody };
    };
    fetchFn.calls = calls;
    return fetchFn;
}

async function withReaderUi(globalsOverrides, run) {
    const button = makeFakeButton();
    const readerSources = { innerText: '' };
    const window = {
        __shelahLegacyBookmarkBound: undefined,
        getSelection: () => ({ toString: () => '' }),
        setTimeout: (fn) => fn(),
        location: { href: 'https://shelah.org/reader' },
        ...globalsOverrides.window,
    };
    const document = {
        getElementById: (id) => {
            if (id === 'semanticBookmarkBtn') return button;
            if (id === 'readerSources') return readerSources;
            return null;
        },
    };
    const fetch = globalsOverrides.fetch || makeFetchMock({ ok: true, item: {} });
    const mod = await loadEsmModule(READER_UI_PATH, { window, document, fetch });
    return run({ mod, button, window, document, fetch });
}

test('installSemanticBookmarking skips binding when the inline block has already bound (flag set) — scope model: global function present', async () => {
    await withReaderUi(
        { window: { saveSemanticBookmark: function inlineStub() {}, __shelahLegacyBookmarkBound: true } },
        async ({ mod, button }) => {
            // Simulate the inline block's own binding, present in every scope model.
            button.addEventListener('click', () => {});
            mod.namespace.installSemanticBookmarking();
            assert.equal(button.clickListenerCount, 1, 'only the inline listener should remain bound');
        },
    );
});

test('installSemanticBookmarking skips binding when the inline block has already bound (flag set) — scope model: global function absent (post-IIFE-wrap)', async () => {
    await withReaderUi(
        { window: { __shelahLegacyBookmarkBound: true } }, // no window.saveSemanticBookmark at all
        async ({ mod, button }) => {
            button.addEventListener('click', () => {});
            mod.namespace.installSemanticBookmarking();
            assert.equal(
                button.clickListenerCount,
                1,
                'the flag alone must be enough to skip module binding, independent of window.saveSemanticBookmark existing — ' +
                'this is the exact case the old typeof-based guard got wrong once the inline block stops being an implicit global',
            );
        },
    );
});

test('installSemanticBookmarking binds its own listener when the legacy flag is absent', async () => {
    await withReaderUi({ window: {} }, async ({ mod, button }) => {
        mod.namespace.installSemanticBookmarking();
        assert.equal(button.clickListenerCount, 1, 'module must still bind itself when nothing else has');
    });
});

test('a single click on the bound button issues exactly one POST /api/bookmarks/semantic', async () => {
    const fetchMock = makeFetchMock({ ok: true, item: { ai_summary: '' } });
    await withReaderUi(
        {
            window: {
                authHeaders: async (base) => ({ ...base, Authorization: 'Bearer test-token' }),
                getSelection: () => ({ toString: () => 'a selected passage to save as a note' }),
            },
            fetch: fetchMock,
        },
        async ({ mod, button }) => {
            mod.namespace.installSemanticBookmarking();
            await button.dispatchClick();

            const postCalls = fetchMock.calls.filter((c) => c.opts?.method === 'POST');
            assert.equal(postCalls.length, 1, 'exactly one POST per click');
            assert.equal(postCalls[0].url, '/api/bookmarks/semantic');
        },
    );
});
