/**
 * static/js/answer-share.js -- public share links for stored AI answers:
 * the API client (GET/POST/DELETE /api/user/history/<id>/share, the 503
 * share_unavailable path), the shared state store and its getLinkUrl hook
 * (idempotent public link, private /answer/<id> fallback with a note), and the
 * "Shared · anyone with the link can view" / "Stop sharing" controller.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const ID = '3f2b8c1e-9a4d-4e6f-8b1a-2c3d4e5f6a7b';
const OTHER_ID = '00000000-1111-4222-8333-444444444444';
// A share token's shape (16-64 URL-safe chars), plainly not a secret.
const TOKEN = 't'.repeat(22);
const ORIGIN = 'https://shelah.org';

async function load(windowExtra = {}) {
    const mod = await loadEsmModule('static/js/answer-share.js', {
        window: { location: { origin: ORIGIN }, ...windowExtra },
        navigator: {},
        document: {},
    });
    return mod.namespace;
}

function jsonResponse(status, body) {
    return { status, ok: status >= 200 && status < 300, json: async () => body };
}

function fakeApi(overrides = {}) {
    const calls = [];
    const api = {
        calls,
        get: async (id) => { calls.push(['get', id]); return { shared: false }; },
        create: async (id) => { calls.push(['create', id]); return { shared: true, token: TOKEN }; },
        revoke: async (id) => { calls.push(['revoke', id]); return { shared: false }; },
        ...overrides,
    };
    return api;
}

function makeClassList(initial = []) {
    const set = new Set(initial);
    return {
        add: (c) => set.add(c),
        remove: (c) => set.delete(c),
        contains: (c) => set.has(c),
        toggle(c, force) {
            const on = force === undefined ? !set.has(c) : !!force;
            if (on) set.add(c); else set.delete(c);
            return on;
        },
    };
}

function makeEl(classes = []) {
    const listeners = {};
    const attrs = {};
    return {
        classList: makeClassList(classes),
        textContent: '',
        disabled: false,
        focused: 0,
        attrs,
        setAttribute(name, value) { attrs[name] = String(value); },
        removeAttribute(name) { delete attrs[name]; },
        addEventListener(type, fn) { listeners[type] = fn; },
        fire(type) { return listeners[type]?.(); },
        focus() { this.focused += 1; },
    };
}

function makeRoot() {
    const revoke = makeEl();
    const line = makeEl(['hidden']);
    line.querySelector = (sel) => (sel === '.answer-share-revoke' ? revoke : null);
    const parts = {
        '.answer-share-state': line,
        '.answer-link-status': makeEl(),
        '.answer-link-btn': makeEl(),
    };
    const root = makeEl();
    root.querySelector = (sel) => parts[sel] || null;
    return { root, line, revoke, status: parts['.answer-link-status'], copyBtn: parts['.answer-link-btn'] };
}

const tick = () => new Promise((r) => setImmediate(r));

// ── helpers ─────────────────────────────────────────────────────────────

test('shareLinkUrl builds the public /a/<token> link', async () => {
    const m = await load();
    assert.equal(m.shareLinkUrl(TOKEN, ORIGIN), `${ORIGIN}/a/${TOKEN}`);
    assert.ok(m.SHARE_TOKEN_RE.test(TOKEN));
    assert.equal(m.SHARE_TOKEN_RE.test('short'), false);
});

// ── API client ──────────────────────────────────────────────────────────

test('createShareApi calls the share endpoint with auth headers and no cache', async () => {
    const m = await load();
    const requests = [];
    const api = m.createShareApi({
        fetchImpl: async (url, opts) => { requests.push([url, opts]); return jsonResponse(opts.method === 'POST' ? 201 : 200, { shared: opts.method !== 'DELETE', share_token: TOKEN }); },
        getHeaders: async () => ({ Authorization: 'Bearer t' }),
    });
    assert.deepEqual(await api.create(ID), { shared: true, token: TOKEN });
    assert.deepEqual(await api.get(ID), { shared: true, token: TOKEN });
    assert.deepEqual(await api.revoke(ID), { shared: false });
    assert.deepEqual(requests.map(([url, o]) => [url, o.method, o.cache, o.headers.Authorization]), [
        [`/api/user/history/${ID}/share`, 'POST', 'no-store', 'Bearer t'],
        [`/api/user/history/${ID}/share`, 'GET', 'no-store', 'Bearer t'],
        [`/api/user/history/${ID}/share`, 'DELETE', 'no-store', 'Bearer t'],
    ]);
});

test('createShareApi rejects a token that is not token-shaped', async () => {
    const m = await load();
    const api = m.createShareApi({ fetchImpl: async () => jsonResponse(200, { shared: true, share_token: '../../x' }), getHeaders: async () => ({}) });
    assert.deepEqual(await api.get(ID), { shared: false });
});

test('createShareApi maps 503 share_unavailable to ShareUnavailableError, other failures to Error', async () => {
    const m = await load();
    const unavailable = m.createShareApi({ fetchImpl: async () => jsonResponse(503, { code: 'share_unavailable' }), getHeaders: async () => ({}) });
    await assert.rejects(unavailable.create(ID), (err) => err instanceof m.ShareUnavailableError && err.name === 'ShareUnavailableError');

    const down = m.createShareApi({ fetchImpl: async () => jsonResponse(503, { error: 'Supabase not configured' }), getHeaders: async () => ({}) });
    await assert.rejects(down.create(ID), (err) => !(err instanceof m.ShareUnavailableError) && /503/.test(err.message));

    const notJson = m.createShareApi({
        fetchImpl: async () => ({ status: 500, ok: false, json: async () => { throw new SyntaxError('html'); } }),
        getHeaders: async () => ({}),
    });
    await assert.rejects(notJson.get(ID), /500/);
});

test('default headers come from window.authHeaders, and survive it throwing or being absent', async () => {
    const seen = [];
    const fetchImpl = async (_url, opts) => { seen.push(opts.headers); return jsonResponse(200, { shared: false }); };

    let m = await load({ authHeaders: async (base) => ({ ...base, Authorization: 'Bearer w' }) });
    await m.createShareApi({ fetchImpl }).get(ID);
    m = await load({ authHeaders: async () => { throw new Error('clerk'); } });
    await m.createShareApi({ fetchImpl }).get(ID);
    m = await load();
    await m.createShareApi({ fetchImpl }).get(ID);
    assert.deepEqual(seen, [{ Authorization: 'Bearer w' }, {}, {}]);
});

// ── store + getLinkUrl ──────────────────────────────────────────────────

test('linkFor creates the share once, then reuses the cached token', async () => {
    const m = await load();
    const api = fakeApi();
    const share = m.createAnswerShare({ api, getOrigin: () => ORIGIN, translate: (en) => en });
    assert.equal(await share.linkFor(ID), `${ORIGIN}/a/${TOKEN}`);
    assert.equal(await share.linkFor(ID), `${ORIGIN}/a/${TOKEN}`);
    assert.deepEqual(api.calls, [['create', ID]]);
    assert.deepEqual(share.peek(ID), { shared: true, token: TOKEN });
    assert.equal(share.peek(OTHER_ID), null);
});

test('linkFor falls back to the private /answer/<id> link with a note when sharing is unavailable', async () => {
    const m = await load();
    const api = fakeApi({ create: async (id) => { api.calls.push(['create', id]); throw new m.ShareUnavailableError(); } });
    const share = m.createAnswerShare({ api, getOrigin: () => ORIGIN, translate: (en) => en });
    const first = await share.linkFor(ID);
    assert.equal(first.url, `${ORIGIN}/answer/${ID}`);
    assert.match(first.note, /private link/);
    assert.deepEqual(await share.linkFor(ID), first, 'no second POST once known unavailable');
    assert.equal(api.calls.length, 1);
});

test('linkFor rethrows other failures and a create that did not share', async () => {
    const m = await load();
    const failing = m.createAnswerShare({ api: fakeApi({ create: async () => { throw new Error('500'); } }), getOrigin: () => ORIGIN });
    await assert.rejects(failing.linkFor(ID), /500/);
    const notShared = m.createAnswerShare({ api: fakeApi({ create: async () => ({ shared: false }) }), getOrigin: () => ORIGIN });
    await assert.rejects(notShared.linkFor(ID), /not created/);
});

test('load dedupes concurrent requests, records unavailability, and keeps state on other errors', async () => {
    const m = await load();
    let release;
    const api = fakeApi({ get: (id) => { api.calls.push(['get', id]); return new Promise((r) => { release = r; }); } });
    const share = m.createAnswerShare({ api, getOrigin: () => ORIGIN });
    assert.equal(await share.load(null), null);
    const a = share.load(ID);
    const b = share.load(ID);
    assert.equal(a, b);
    release({ shared: true, token: TOKEN });
    assert.deepEqual(await a, { shared: true, token: TOKEN });
    assert.equal(api.calls.length, 1);

    const unavailable = m.createAnswerShare({ api: fakeApi({ get: async () => { throw new m.ShareUnavailableError(); } }) });
    assert.deepEqual(await unavailable.load(ID), { shared: false, unavailable: true });

    const flaky = m.createAnswerShare({ api: fakeApi({ get: async () => { throw new Error('offline'); } }) });
    assert.equal(await flaky.load(ID), null);
});

test('revoke clears the state and notifies subscribers; unsubscribe stops notifications', async () => {
    const m = await load();
    const share = m.createAnswerShare({ api: fakeApi(), getOrigin: () => ORIGIN });
    const seen = [];
    const off = share.subscribe((id, state) => seen.push([id, state.shared]));
    await share.linkFor(ID);
    await share.revoke(ID);
    off();
    await share.linkFor(ID);
    assert.deepEqual(seen, [[ID, true], [ID, false]]);
    assert.deepEqual(share.peek(ID), { shared: true, token: TOKEN });
});

test('default origin and translate come from window', async () => {
    const m = await load({ t: (_en, he) => he });
    const share = m.createAnswerShare({ api: fakeApi({ create: async () => { throw new m.ShareUnavailableError(); } }) });
    const link = await share.linkFor(ID);
    assert.equal(link.url, `${ORIGIN}/answer/${ID}`);
    assert.match(link.note, /קישור פרטי/);
});

// ── Shared / Stop sharing controller ────────────────────────────────────

test('installShareState without its markup is a harmless no-op', async () => {
    const m = await load();
    const share = m.createAnswerShare({ api: fakeApi() });
    const ctl = m.installShareState(null, share);
    ctl.show(ID);
    ctl.hide();
    const empty = { querySelector: () => null };
    m.installShareState(empty, share).show(ID);
});

test('show() loads the state and reveals the Shared line only for a shared answer', async () => {
    const m = await load();
    const api = fakeApi({ get: async (id) => (id === ID ? { shared: true, token: TOKEN } : { shared: false }) });
    const share = m.createAnswerShare({ api, getOrigin: () => ORIGIN });
    const { root, line } = makeRoot();
    const ctl = m.installShareState(root, share, { translate: (en) => en });
    ctl.show(ID);
    assert.equal(line.classList.contains('hidden'), true, 'hidden until the state is known');
    await tick();
    assert.equal(line.classList.contains('hidden'), false);

    ctl.show(OTHER_ID);
    assert.equal(line.classList.contains('hidden'), true);
    await tick();
    assert.equal(line.classList.contains('hidden'), true);

    ctl.show(ID);
    assert.equal(line.classList.contains('hidden'), false, 'cached state renders at once');
    ctl.hide();
    assert.equal(line.classList.contains('hidden'), true);
    ctl.show(null);
    assert.equal(line.classList.contains('hidden'), true);
});

test('copying a link in one placement reveals the Shared line in the other', async () => {
    const m = await load();
    const share = m.createAnswerShare({ api: fakeApi(), getOrigin: () => ORIGIN });
    const modal = makeRoot();
    const article = makeRoot();
    m.installShareState(modal.root, share).show(ID);
    m.installShareState(article.root, share).show(ID);
    await tick();
    assert.equal(article.line.classList.contains('hidden'), true);
    await share.linkFor(ID);
    assert.equal(modal.line.classList.contains('hidden'), false);
    assert.equal(article.line.classList.contains('hidden'), false);
});

test('Stop sharing revokes, hides the line, keeps focus in the control, and announces it', async () => {
    const m = await load();
    const api = fakeApi({ get: async () => ({ shared: true, token: TOKEN }) });
    const share = m.createAnswerShare({ api, getOrigin: () => ORIGIN });
    const { root, line, revoke, status, copyBtn } = makeRoot();
    const ctl = m.installShareState(root, share, { translate: (en) => en });
    ctl.show(ID);
    await tick();
    const pending = revoke.fire('click');
    assert.equal(revoke.disabled, true);
    assert.equal(revoke.attrs['aria-busy'], 'true');
    await pending;
    assert.deepEqual(api.calls.at(-1), ['revoke', ID]);
    assert.equal(line.classList.contains('hidden'), true);
    assert.equal(copyBtn.focused, 1);
    assert.match(status.textContent, /Sharing stopped/);
    assert.equal(status.classList.contains('is-visible'), true);
    assert.equal(revoke.attrs['aria-busy'], undefined);
    assert.equal(revoke.disabled, false, 're-enabled for the next time it is shown');
});

test('a failed Stop sharing keeps the line and shows a retry message', async () => {
    const m = await load();
    const api = fakeApi({ get: async () => ({ shared: true, token: TOKEN }), revoke: async () => { throw new Error('500'); } });
    const share = m.createAnswerShare({ api, getOrigin: () => ORIGIN });
    const { root, line, revoke, status } = makeRoot();
    const ctl = m.installShareState(root, share, { translate: (en) => en });
    ctl.show(ID);
    await tick();
    await revoke.fire('click');
    assert.equal(line.classList.contains('hidden'), false);
    assert.equal(revoke.disabled, false);
    assert.match(status.textContent, /Couldn't stop sharing/);
});

test('Stop sharing is ignored while hidden or busy, and a stale result is not announced', async () => {
    const m = await load();
    let release;
    let revokes = 0;
    const api = fakeApi({
        get: async () => ({ shared: true, token: TOKEN }),
        revoke: () => { revokes += 1; return new Promise((r, reject) => { release = { r, reject }; }); },
    });
    const share = m.createAnswerShare({ api, getOrigin: () => ORIGIN });
    const { root, revoke, status } = makeRoot();
    const ctl = m.installShareState(root, share, { translate: (en) => en });
    await revoke.fire('click');
    assert.equal(revokes, 0, 'nothing shown');

    ctl.show(ID);
    await tick();
    const pending = revoke.fire('click');
    await revoke.fire('click');
    assert.equal(revokes, 1, 'busy');
    ctl.show(OTHER_ID);
    release.reject(new Error('late failure'));
    await pending;
    assert.equal(status.textContent, '');

    ctl.show(ID);
    await tick();
    const second = revoke.fire('click');
    ctl.hide();
    release.r({ shared: false });
    await second;
    assert.equal(status.textContent, '');
});

test('the default translate is window.t', async () => {
    const m = await load({ t: (_en, he) => he });
    const share = m.createAnswerShare({ api: fakeApi({ get: async () => ({ shared: true, token: TOKEN }) }) });
    const { root, revoke, status } = makeRoot();
    const ctl = m.installShareState(root, share);
    ctl.show(ID);
    await tick();
    await revoke.fire('click');
    assert.match(status.textContent, /השיתוף הופסק/);
});

test('a placement without a status region still revokes', async () => {
    const m = await load();
    const share = m.createAnswerShare({ api: fakeApi({ get: async () => ({ shared: true, token: TOKEN }) }) });
    const { root, line, revoke } = makeRoot();
    const inner = root.querySelector;
    root.querySelector = (sel) => (sel === '.answer-link-status' || sel === '.answer-link-btn' ? null : inner(sel));
    const ctl = m.installShareState(root, share, { translate: (en) => en });
    ctl.show(ID);
    await tick();
    await revoke.fire('click');
    assert.equal(line.classList.contains('hidden'), true);
});
