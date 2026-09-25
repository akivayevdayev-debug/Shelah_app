/**
 * static/js/answer-link.js -- "Copy link" for a stored AI answer (modal footer
 * and full-article view): id extraction (uuid only), the owner-only `/answer/<id>`
 * URL, the getLinkUrl hook for public links, clipboard copy with the
 * execCommand fallback, and the controller (hidden without an id, the
 * "Copied!" flash, manual-copy fallback, stale-click guard).
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const ID = '3f2b8c1e-9a4d-4e6f-8b1a-2c3d4e5f6a7b';
const OTHER_ID = '00000000-1111-4222-8333-444444444444';

async function load(globals = {}) {
    const mod = await loadEsmModule('static/js/answer-link.js', {
        window: { location: { origin: 'https://shelah.org' } },
        navigator: {},
        document: {},
        ...globals,
    });
    return mod.namespace;
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
        dataset: {},
        textContent: '',
        value: '',
        disabled: false,
        focused: 0,
        selected: 0,
        attrs,
        setAttribute(name, value) { attrs[name] = String(value); },
        addEventListener(type, fn) { listeners[type] = fn; },
        fire(type) { return listeners[type]?.(); },
        focus() { this.focused += 1; },
        select() { this.selected += 1; },
    };
}

function makeRoot() {
    const parts = {
        '.answer-link-btn': makeEl(),
        '.answer-link-btn__face--idle': makeEl(),
        '.answer-link-btn__face--copied': makeEl(),
        '.answer-link-status': makeEl(),
        '.answer-link-fallback': makeEl(['hidden']),
    };
    const root = makeEl(['hidden']);
    root.querySelector = (sel) => parts[sel];
    return {
        root,
        button: parts['.answer-link-btn'],
        idleFace: parts['.answer-link-btn__face--idle'],
        copiedFace: parts['.answer-link-btn__face--copied'],
        status: parts['.answer-link-status'],
        fallback: parts['.answer-link-fallback'],
    };
}

function fakeTimers() {
    const pending = new Map();
    let next = 1;
    return {
        setTimer(fn, ms) { const h = next++; pending.set(h, { fn, ms }); return h; },
        clearTimer(h) { pending.delete(h); },
        flush() { const fns = [...pending.values()]; pending.clear(); fns.forEach(({ fn }) => fn()); },
        pending,
    };
}

// ── pure helpers ────────────────────────────────────────────────────────

test('answerIdOf prefers history_id, falls back to id, accepts only uuids', async () => {
    const m = await load();
    assert.equal(m.answerIdOf({ history_id: ID, id: OTHER_ID }), ID);
    assert.equal(m.answerIdOf({ id: ` ${ID} ` }), ID);
    assert.equal(m.answerIdOf({ history_id: 'What is kiddush?' }), null);
    assert.equal(m.answerIdOf({ history_id: `${ID}&text=x` }), null);
    assert.equal(m.answerIdOf({}), null);
    assert.equal(m.answerIdOf(null), null);
});

test('answerLinkUrl builds the owner-only /answer/<id> deep link', async () => {
    const m = await load();
    assert.equal(m.answerLinkUrl(ID, 'https://shelah.org'), `https://shelah.org/answer/${ID}`);
});

test('copyText uses the Clipboard API when present', async () => {
    const m = await load();
    const written = [];
    const ok = await m.copyText('hello', { navigator: { clipboard: { writeText: async (t) => { written.push(t); } } }, document: {} });
    assert.equal(ok, true);
    assert.deepEqual(written, ['hello']);
});

function makeLegacyDocument(execResult) {
    const appended = [];
    const doc = {
        body: { appendChild: (el) => appended.push(el) },
        createElement: () => ({
            value: '', style: {}, removed: false,
            setAttribute() {}, select() {}, remove() { this.removed = true; },
        }),
        execCommand: (cmd) => { doc.lastCommand = cmd; if (execResult instanceof Error) throw execResult; return execResult; },
    };
    return { doc, appended };
}

test('copyText falls back to execCommand when the Clipboard API rejects, and cleans up', async () => {
    const m = await load();
    const { doc, appended } = makeLegacyDocument(true);
    const nav = { clipboard: { writeText: async () => { throw new Error('NotAllowedError'); } } };
    assert.equal(await m.copyText('x', { navigator: nav, document: doc }), true);
    assert.equal(doc.lastCommand, 'copy');
    assert.equal(appended[0].value, 'x');
    assert.equal(appended[0].removed, true);
});

test('copyText hands a pending link to clipboard.write via a ClipboardItem (Safari activation)', async () => {
    const m = await load();
    const writes = [];
    class FakeItem { constructor(items) { this.items = items; } }
    class FakeBlob { constructor(parts, opts) { this.text = parts.join(''); this.type = opts.type; } }
    const nav = { clipboard: { write: async (items) => { writes.push(items); await items[0].items['text/plain']; }, writeText: async () => { throw new Error('should not be needed'); } } };
    let release;
    const pending = new Promise((r) => { release = r; });
    const done = m.copyText(pending, { navigator: nav, document: {}, ClipboardItem: FakeItem, Blob: FakeBlob });
    assert.equal(writes.length, 1, 'the write starts before the link resolves');
    release('https://shelah.org/a/tok');
    assert.equal(await done, true);
    const blob = await writes[0][0].items['text/plain'];
    assert.equal(blob.text, 'https://shelah.org/a/tok');
    assert.equal(blob.type, 'text/plain');
});

test('copyText retries with writeText when a promise-valued ClipboardItem is refused', async () => {
    const m = await load();
    const written = [];
    const nav = { clipboard: { write: async () => { throw new Error('NotAllowedError'); }, writeText: async (t) => { written.push(t); } } };
    const ok = await m.copyText(Promise.resolve('late-link'), { navigator: nav, document: {}, ClipboardItem: class {}, Blob: class {} });
    assert.equal(ok, true);
    assert.deepEqual(written, ['late-link']);
});

test('copyText resolves false for a link that rejects or is empty', async () => {
    const m = await load();
    const nav = { clipboard: { writeText: async () => {} } };
    assert.equal(await m.copyText(Promise.reject(new Error('503')), { navigator: nav, document: {} }), false);
    assert.equal(await m.copyText(Promise.resolve(''), { navigator: nav, document: {} }), false);
    class FakeBlob { constructor(parts) { this.parts = parts; } }
    const writeNav = { clipboard: { write: async (items) => { await items[0].items['text/plain']; } } };
    assert.equal(await m.copyText(Promise.resolve(''), { navigator: writeNav, document: {}, ClipboardItem: class { constructor(i) { this.items = i; } }, Blob: FakeBlob }), false);
});

test('copyText resolves false when every path fails', async () => {
    const m = await load();
    assert.equal(await m.copyText('x', { navigator: {}, document: makeLegacyDocument(false).doc }), false);
    const threw = makeLegacyDocument(new Error('boom'));
    assert.equal(await m.copyText('x', { navigator: {}, document: threw.doc }), false);
    assert.equal(threw.appended[0].removed, true);
    assert.equal(await m.copyText('x', { navigator: {}, document: {} }), false);
});

// ── #aiAnswerLink controller ────────────────────────────────────────────

test('installAnswerLink with no root is a harmless no-op', async () => {
    const m = await load();
    const ctl = m.installAnswerLink(null);
    ctl.show({ history_id: ID });
    ctl.hide();
});

test('show() reveals the control only for answers with an id; hide() hides it', async () => {
    const m = await load();
    const { root } = makeRoot();
    const ctl = m.installAnswerLink(root, { copy: async () => true });
    ctl.show({ answer: 'signed-out answer' });
    assert.equal(root.classList.contains('hidden'), true);
    ctl.show({ history_id: ID });
    assert.equal(root.classList.contains('hidden'), false);
    ctl.hide();
    assert.equal(root.classList.contains('hidden'), true);
});

test('click copies the /answer/<id> URL, flips the button to "Copied!", then back', async () => {
    const m = await load();
    const { root, button, idleFace, copiedFace, status, fallback } = makeRoot();
    const timers = fakeTimers();
    const copied = [];
    const ctl = m.installAnswerLink(root, {
        getOrigin: () => 'https://shelah.org',
        translate: (en) => en,
        copy: async (url) => { copied.push(await url); return true; },
        ...timers,
    });
    ctl.show({ history_id: ID });
    assert.equal(button.dataset.state, 'idle');
    await button.fire('click');
    assert.deepEqual(copied, [`https://shelah.org/answer/${ID}`]);
    assert.equal(button.dataset.state, 'copied');
    assert.equal(copiedFace.attrs['aria-hidden'], 'false');
    assert.equal(idleFace.attrs['aria-hidden'], 'true');
    assert.equal(status.textContent, 'Link copied', 'announced for screen readers');
    assert.equal(status.classList.contains('is-visible'), false, 'the button itself shows the confirmation');
    assert.equal(fallback.classList.contains('hidden'), true);
    assert.equal(button.disabled, false);
    assert.deepEqual([...timers.pending.values()].map((t) => t.ms), [1600]);
    timers.flush();
    assert.equal(button.dataset.state, 'idle');
    assert.equal(idleFace.attrs['aria-hidden'], 'false');
});

test('a second copy restarts the "Copied!" timer instead of stacking timers', async () => {
    const m = await load();
    const { root, button } = makeRoot();
    const timers = fakeTimers();
    const ctl = m.installAnswerLink(root, { getOrigin: () => 'o', translate: (en) => en, copy: async () => true, ...timers });
    ctl.show({ id: ID });
    await button.fire('click');
    await button.fire('click');
    assert.equal(timers.pending.size, 1);
    assert.equal(button.dataset.state, 'copied');
});

test('getLinkUrl replaces the owner link (the hook public share links plug into)', async () => {
    const m = await load();
    const { root, button } = makeRoot();
    const copied = [];
    const ctl = m.installAnswerLink(root, {
        getLinkUrl: async (id) => `https://shelah.org/a/token-for-${id.slice(0, 4)}`,
        translate: (en) => en,
        copy: async (url) => { copied.push(await url); return true; },
        setTimer: () => 1,
        clearTimer: () => {},
    });
    ctl.show({ id: ID });
    await button.fire('click');
    assert.deepEqual(copied, ['https://shelah.org/a/token-for-3f2b']);
});

test('a getLinkUrl failure shows a visible retry message and copies nothing', async () => {
    const m = await load();
    const { root, button, status, fallback } = makeRoot();
    let copies = 0;
    const ctl = m.installAnswerLink(root, {
        getLinkUrl: async () => { throw new Error('503'); },
        translate: (en) => en,
        copy: async (url) => { await url; copies += 1; return true; },
    });
    ctl.show({ id: ID });
    await button.fire('click');
    assert.equal(copies, 0);
    assert.match(status.textContent, /Couldn't create a link/);
    assert.equal(status.classList.contains('is-visible'), true);
    assert.equal(fallback.classList.contains('hidden'), true);
    assert.equal(button.disabled, false);
    assert.equal(button.dataset.state, 'idle');
});

test('a failed copy shows the URL in the read-only field, selected, with a visible hint', async () => {
    const m = await load();
    const { root, button, status, fallback } = makeRoot();
    const timers = fakeTimers();
    const ctl = m.installAnswerLink(root, { getOrigin: () => 'https://x.test', translate: (en) => en, copy: async () => false, ...timers });
    ctl.show({ id: ID });
    await button.fire('click');
    assert.equal(fallback.value, `https://x.test/answer/${ID}`);
    assert.equal(fallback.classList.contains('hidden'), false);
    assert.equal(fallback.focused, 1);
    assert.ok(fallback.selected >= 1);
    assert.match(status.textContent, /Copy the link here/);
    assert.equal(status.classList.contains('is-visible'), true);
    assert.equal(button.dataset.state, 'idle', 'no "Copied!" when nothing was copied');

    fallback.fire('focus');
    assert.ok(fallback.selected >= 2, 'focusing the field selects the URL again');

    ctl.show({ id: OTHER_ID });
    assert.equal(fallback.classList.contains('hidden'), true, 'a new answer resets the fallback');
    assert.equal(fallback.value, '');
    assert.equal(status.textContent, '');
    assert.equal(status.classList.contains('is-visible'), false);
});

test('a copy that finishes after the answer changed does nothing', async () => {
    const m = await load();
    const { root, button, status } = makeRoot();
    let release;
    const ctl = m.installAnswerLink(root, { translate: (en) => en, copy: () => new Promise((r) => { release = r; }), getOrigin: () => 'o' });
    ctl.show({ id: ID });
    const pending = button.fire('click');
    assert.equal(button.disabled, true);
    await new Promise((r) => setImmediate(r)); // let the click reach copy()
    ctl.show({ id: OTHER_ID });
    release(true);
    await pending;
    assert.equal(status.textContent, '');
    assert.equal(button.dataset.state, 'idle');
});

test('clicks while hidden or already copying are ignored', async () => {
    const m = await load();
    const { root, button } = makeRoot();
    let calls = 0;
    const ctl = m.installAnswerLink(root, { copy: async () => { calls += 1; return true; }, getOrigin: () => 'o', translate: (en) => en });
    await button.fire('click');
    ctl.show({ id: ID });
    button.disabled = true;
    await button.fire('click');
    assert.equal(calls, 0);
});

test('default translate uses window.t when the classic script defined it', async () => {
    const m = await load({ window: { location: { origin: 'https://shelah.org' }, t: (_en, he) => he } });
    const { root, button, status } = makeRoot();
    const ctl = m.installAnswerLink(root, { copy: async () => true, setTimer: () => 1, clearTimer: () => {} });
    ctl.show({ id: ID });
    await button.fire('click');
    assert.equal(status.textContent, 'הקישור הועתק');
});

test('default link uses window.location.origin', async () => {
    const m = await load();
    const { root, button } = makeRoot();
    const copied = [];
    const ctl = m.installAnswerLink(root, { copy: async (u) => { copied.push(await u); return true; }, translate: (en) => en, setTimer: () => 1, clearTimer: () => {} });
    ctl.show({ id: ID });
    await button.fire('click');
    assert.deepEqual(copied, [`https://shelah.org/answer/${ID}`]);
});

test('getLinkUrl may resolve to { url, note }: the note is shown, not just announced', async () => {
    const m = await load();
    const { root, button, status } = makeRoot();
    const copied = [];
    const ctl = m.installAnswerLink(root, {
        getLinkUrl: async () => ({ url: 'https://shelah.org/answer/x', note: 'Copied a private link.' }),
        translate: (en) => en,
        copy: async (u) => { copied.push(await u); return true; },
        setTimer: () => 1,
        clearTimer: () => {},
    });
    ctl.show({ id: ID });
    await button.fire('click');
    assert.deepEqual(copied, ['https://shelah.org/answer/x']);
    assert.equal(button.dataset.state, 'copied');
    assert.equal(status.textContent, 'Copied a private link.');
    assert.equal(status.classList.contains('is-visible'), true);
});

test('a copy() that throws falls back to the manual-copy field', async () => {
    const m = await load();
    const { root, button, fallback } = makeRoot();
    const ctl = m.installAnswerLink(root, { getOrigin: () => 'https://x.test', translate: (en) => en, copy: async () => { throw new Error('boom'); } });
    ctl.show({ id: ID });
    await button.fire('click');
    assert.equal(fallback.value, `https://x.test/answer/${ID}`);
    assert.equal(fallback.classList.contains('hidden'), false);
});
