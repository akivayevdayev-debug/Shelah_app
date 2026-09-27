/**
 * static/js/siddur.js: the siddur view's controller -- loading the toc and
 * services (the real checked-in data under data/siddur/, served by a fake
 * fetch), drawing a page, the Today card and its switches, today's marks,
 * re-rendering, and the menus.
 *
 * The page DOM is a small fake: the container keeps the HTML it's given and
 * hands back a root that answers the few queries the controller makes, so
 * the markup itself is checked as a string (tests_js/siddur_reader.test.js
 * covers the markup builders in detail).
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { loadEsmModule } = require('./helpers/esm_harness');

const DATA = path.join(__dirname, '..', 'data', 'siddur', 'edot-hamizrach');
const TOC = JSON.parse(fs.readFileSync(path.join(DATA, 'toc.json'), 'utf8'));
const serviceJson = (slug) => JSON.parse(fs.readFileSync(path.join(DATA, 'services', `${slug}.json`), 'utf8'));

function dayAnswer(overrides = {}) {
    return {
        date: '2026-10-20', il: false, weekday: 3,
        hebrew: { year: 5787, month: 8, day: 9, monthName: 'Cheshvan', he: 'ט׳ חשון תשפ״ז' },
        occasions: [], tachanun: { shacharit: true, mincha: true }, hallel: { kind: 'none', beracha: false },
        gevurot: { arbit: 'mashiv', shacharit: 'mashiv', musaf: 'mashiv', mincha: 'mashiv' },
        birkatHashanim: 'barech-alenu', yaalehVeyavo: false, alHanissim: false, alHanissimJerusalem: false,
        aseretYemeiTeshuva: false, musaf: false, aneinu: false, omer: { today: null, tonight: null },
        ...overrides,
    };
}

function makeFetch({ day = () => dayAnswer(), fail = {} } = {}) {
    const calls = [];
    const fetchImpl = async (url) => {
        calls.push(url);
        const respond = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
        for (const [prefix, status] of Object.entries(fail)) {
            if (url.startsWith(prefix)) return respond(status, { error: 'x' });
        }
        let m = /^\/api\/siddur\/v2\/toc\/([a-z-]+)$/.exec(url);
        if (m) return m[1] === 'edot-hamizrach' ? respond(200, TOC) : respond(404, { error: 'no rite' });
        m = /^\/api\/siddur\/v2\/service\/edot-hamizrach\/([a-z-]+)\?v=/.exec(url);
        if (m) return fs.existsSync(path.join(DATA, 'services', `${m[1]}.json`)) ? respond(200, serviceJson(m[1])) : respond(404, {});
        m = /^\/api\/siddur\/v2\/day\?date=([\d-]+)&il=([01])$/.exec(url);
        if (m) {
            const answer = day(m[1], m[2] === '1');
            return answer ? respond(200, answer) : respond(503, {});
        }
        return respond(404, {});
    };
    return { fetchImpl, calls };
}

function fakeNode(attrs = {}) {
    const attributes = new Map(Object.entries(attrs));
    return {
        hidden: attrs.hidden !== undefined,
        innerHTML: '',
        textContent: '',
        href: '',
        dataset: {},
        setAttribute: (k, v) => attributes.set(k, String(v)),
        getAttribute: (k) => (attributes.has(k) ? attributes.get(k) : null),
    };
}

// A root over the HTML the container was given: the Today slot, the wake
// button, the continue link and the day-tagged lines are live fakes.
function fakeRoot(html) {
    const clicks = [];
    const slot = fakeNode();
    const wake = fakeNode({ hidden: '' });
    const cont = fakeNode({ hidden: '' });
    const lines = [...html.matchAll(/<(?:p|div|h3) class="siddur-line[^"]*" id="([^"]+)"[^>]*data-when="([^"]+)"([^>]*)>/g)].map(([, id, when, rest]) => {
        const line = { id, dataset: { when }, today: /data-today/.test(rest), badge: /data-today/.test(rest) };
        line.toggleAttribute = (name, on) => { if (name === 'data-today') line.today = Boolean(on); };
        line.querySelector = () => (line.badge ? { remove: () => { line.badge = false; } } : null);
        line.insertAdjacentHTML = (_where, markup) => { if (/siddur-today-badge/.test(markup)) line.badge = true; };
        return line;
    });
    const root = {
        html, slot, wake, cont, lines, clicks,
        isConnected: true,
        offsetParent: {},
        currentChip: null,
        addEventListener: (type, fn) => { if (type === 'click') clicks.push(fn); },
        querySelector(sel) {
            if (sel === '[data-siddur-today]') return /data-siddur-today/.test(html) ? slot : null;
            if (sel === '[data-siddur-action="wake"]') return /data-siddur-action="wake"/.test(html) ? wake : null;
            if (sel === '[data-siddur-continue]') return /data-siddur-continue/.test(html) ? cont : null;
            if (sel === '[data-siddur-section][aria-current="location"]') return root.currentChip;
            return null;
        },
        querySelectorAll(sel) {
            if (sel === '[data-when]') return lines;
            return [];
        },
        click(target) {
            const event = {
                target: { closest: (sel) => target(sel) },
                button: 0, metaKey: false, ctrlKey: false, shiftKey: false, altKey: false,
                prevented: false,
                preventDefault() { this.prevented = true; },
            };
            clicks.forEach((fn) => fn(event));
            return event;
        },
    };
    return root;
}

function fakeContainer() {
    const container = { html: '', firstElementChild: null };
    Object.defineProperty(container, 'innerHTML', {
        get: () => container.html,
        set: (value) => { container.html = value; container.firstElementChild = fakeRoot(value); },
    });
    return container;
}

function memoryStorage() {
    const store = new Map();
    return { store, getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)) };
}

const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

async function makeSiddur({ fetch = makeFetch(), storage = memoryStorage(), deps = {}, nav = {}, win } = {}) {
    const mod = (await loadEsmModule('static/js/siddur.js', {})).namespace;
    const routes = [];
    const siddur = mod.createSiddur({
        fetchImpl: fetch.fetchImpl,
        storage,
        nav,
        doc: { visibilityState: 'visible', addEventListener() {}, removeEventListener() {} },
        win: win || { innerHeight: 900, addEventListener() {}, removeEventListener() {}, requestAnimationFrame: () => 0, cancelAnimationFrame() {} },
        deps: {
            t: (en) => en,
            isHebrewMode: () => false,
            escapeHtml,
            applyHebrewDisplaySettings: (s) => s,
            getPrefs: () => ({ readerLayout: 'bilingual' }),
            getSunset: () => null,
            getLocation: () => null,
            reduceMotion: () => true,
            replaceRoute: (value) => routes.push(value),
            ...deps,
        },
    });
    return { mod, siddur, routes, fetch, storage };
}

test('open draws a service with its Today card, fetched once per data version', async () => {
    const { siddur, fetch } = await makeSiddur();
    const container = fakeContainer();
    const result = await siddur.open('edot-hamizrach/arbit', { container });
    assert.equal(result.label, 'Arbit');
    assert.equal(result.section, null);
    assert.match(container.html, /data-siddur-service="arbit"/);
    assert.match(container.html, /data-siddur-section-body="amidah"/);
    // The Today card went into its slot in the same pass, for Arbit.
    const slot = container.firstElementChild.slot;
    assert.match(slot.innerHTML, /Barech Alenu \(the winter blessing for rain\)/);
    assert.doesNotMatch(slot.innerHTML, /Tachanun/, 'no Tachanun reminder at Arbit, so no Tachanun footnote either');
    // Today's rubric is marked in the first paint.
    assert.match(container.html, /data-when="barech-alenu" data-today>/);
    await siddur.open('edot-hamizrach/arbit', { container: fakeContainer() });
    assert.equal(fetch.calls.filter((u) => u.startsWith('/api/siddur/v2/service/')).length, 1);
    assert.equal(fetch.calls.filter((u) => u.startsWith('/api/siddur/v2/toc/')).length, 1);
    assert.match(fetch.calls.find((u) => u.startsWith('/api/siddur/v2/service/')), new RegExp(`\\?v=${TOC.version}$`));
});

test('the rite page draws the contents, with no service fetch', async () => {
    const { siddur, fetch } = await makeSiddur();
    const container = fakeContainer();
    const result = await siddur.open('edot-hamizrach', { container });
    assert.equal(result.label, 'Siddur · Sephardi (Edot HaMizrach)');
    assert.match(container.html, /siddur-landing/);
    assert.equal(fetch.calls.some((u) => u.includes('/service/')), false);
    assert.match(container.firstElementChild.slot.innerHTML, /Today in the siddur/);
});

test('pages the siddur lacks reject as SiddurNotFound', async () => {
    const { siddur, mod } = await makeSiddur();
    for (const value of ['Edot', 'edot-hamizrach/nope', 'edot-hamizrach/shacharit/nope', 'edot-hamizrach/birkat-hamazon/birkat-hamazon', 'ashkenaz/shacharit']) {
        await assert.rejects(siddur.prepare(value), (err) => err.name === 'SiddurNotFound' && err instanceof mod.SiddurNotFound, value);
    }
});

test('a server error is retryable: it is not cached', async () => {
    const fetch = makeFetch({ fail: { '/api/siddur/v2/toc/': 503 } });
    const { siddur } = await makeSiddur({ fetch });
    await assert.rejects(siddur.prepare('edot-hamizrach/mincha'), (err) => err.name !== 'SiddurNotFound');
    const again = makeFetch();
    fetch.fetchImpl = again.fetchImpl;
    // A new siddur isn't needed: the failed toc load was dropped.
    const { siddur: fresh } = await makeSiddur({ fetch: again });
    assert.equal((await fresh.prepare('edot-hamizrach/mincha')).where.service.slug, 'mincha');
});

test('prepare draws nothing; mount draws it and jumps to the section', async () => {
    const { siddur } = await makeSiddur();
    const prepared = await siddur.prepare('edot-hamizrach/shacharit/amida');
    assert.equal(prepared.where.section.slug, 'amida');
    assert.ok(prepared.early, 'the day answer arrived within the wait');
    const container = fakeContainer();
    const mounted = siddur.mount(container, prepared);
    assert.deepEqual(mounted, { label: 'Amida · Shacharit', section: 'amida' });
    assert.equal(siddur.isShowing('edot-hamizrach/shacharit/keriat-shema'), true);
    assert.equal(siddur.isShowing('edot-hamizrach/mincha'), false);
    assert.equal(siddur.isShowing('junk'), false);
});

test('a slow day answer lands after the page, marking today then', async () => {
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    const fetch = makeFetch();
    const inner = fetch.fetchImpl;
    fetch.fetchImpl = async (url) => {
        if (url.includes('/day?')) await gate;
        return inner(url);
    };
    const mod = (await loadEsmModule('static/js/siddur.js', {})).namespace;
    const { siddur } = await makeSiddur({ fetch });
    const container = fakeContainer();
    // Shorten the wait: race resolves undefined after DAY_WAIT_MS; don't sit through it.
    const prepared = await siddur.prepare('edot-hamizrach/mincha');
    assert.equal(prepared.early, null);
    siddur.mount(container, prepared);
    const root = container.firstElementChild;
    assert.equal(root.lines.find((l) => l.dataset.when === 'barech-alenu').today, false);
    release();
    await new Promise((resolve) => setTimeout(resolve, 20));
    assert.equal(root.lines.find((l) => l.dataset.when === 'barech-alenu').today, true);
    assert.equal(root.lines.find((l) => l.dataset.when === 'barech-alenu').badge, true);
    assert.match(root.slot.innerHTML, /Barech Alenu/);
    assert.ok(mod.DEFAULT_RITE);
});

test('the Israel and after-sunset switches re-ask for the right day', async () => {
    const seen = [];
    const fetch = makeFetch({ day: (date, il) => { seen.push([date, il]); return dayAnswer({ il, birkatHashanim: il ? 'barech-alenu' : 'barchenu' }); } });
    const storage = memoryStorage();
    const { siddur } = await makeSiddur({ fetch, storage });
    const container = fakeContainer();
    await siddur.open('edot-hamizrach/mincha', { container });
    const root = container.firstElementChild;
    root.click((sel) => (sel === '[data-siddur-action]' ? { dataset: { siddurAction: 'toggle-israel' } } : null));
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(storage.getItem('shelah.siddur.il'), '1');
    assert.equal(seen.at(-1)[1], true);
    assert.match(root.slot.innerHTML, /In Israel/);
    root.click((sel) => (sel === '[data-siddur-action]' ? { dataset: { siddurAction: 'toggle-evening' } } : null));
    await new Promise((resolve) => setTimeout(resolve, 10));
    // After sunset: the next civil day.
    assert.notEqual(seen.at(-1)[0], seen[0][0]);
    assert.match(root.slot.innerHTML, /aria-pressed="true">\s*After sunset/);
});

test('a day answer that fails leaves the page without a card', async () => {
    const fetch = makeFetch({ day: () => null });
    const { siddur } = await makeSiddur({ fetch });
    const container = fakeContainer();
    await siddur.open('edot-hamizrach/mincha', { container });
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(container.firstElementChild.slot.innerHTML, '');
});

test('a section chip scrolls in place; the wake switch shows only where supported', async () => {
    const granted = [];
    const nav = { wakeLock: { request: async () => { granted.push(1); return { addEventListener() {}, release: async () => {} }; } } };
    const { siddur } = await makeSiddur({ nav });
    const container = fakeContainer();
    await siddur.open('edot-hamizrach/shacharit', { container });
    const root = container.firstElementChild;
    assert.equal(root.wake.hidden, false);
    assert.equal(root.wake.getAttribute('aria-pressed'), 'false');
    const event = root.click((sel) => (sel === '[data-siddur-section]' ? { dataset: { siddurSection: 'amida' } } : null));
    assert.equal(event.prevented, true);
    root.click((sel) => (sel === '[data-siddur-action]' ? root.wake : null));
    root.wake.dataset.siddurAction = 'wake';
    root.click((sel) => (sel === '[data-siddur-action]' ? Object.assign(root.wake, { dataset: { siddurAction: 'wake' } }) : null));
    await new Promise((resolve) => setTimeout(resolve, 10));
    assert.equal(granted.length, 1);
    assert.equal(root.wake.getAttribute('aria-pressed'), 'true');
    siddur.close();
    assert.equal(siddur.isShowing('edot-hamizrach/shacharit'), false);

    const plain = await makeSiddur();
    const other = fakeContainer();
    await plain.siddur.open('edot-hamizrach/shacharit', { container: other });
    assert.equal(other.firstElementChild.wake.hidden, true);
});

test('Continue offers the saved section; the first section or none offers nothing', async () => {
    const storage = memoryStorage();
    storage.setItem('shelah.siddur.position', JSON.stringify({ 'edot-hamizrach/shacharit': 'amida' }));
    const { siddur } = await makeSiddur({ storage });
    const container = fakeContainer();
    await siddur.open('edot-hamizrach/shacharit', { container });
    const cont = container.firstElementChild.cont;
    assert.equal(cont.hidden, false);
    assert.equal(cont.href, '/siddur/edot-hamizrach/shacharit/amida');
    assert.equal(cont.textContent, 'Continue: Amida');
    // Opening at a named section doesn't offer it.
    const named = fakeContainer();
    await siddur.open('edot-hamizrach/shacharit/alenu', { container: named });
    assert.equal(named.firstElementChild.cont.hidden, true);
    storage.setItem('shelah.siddur.position', JSON.stringify({ 'edot-hamizrach/shacharit': 'petichat-eliyahu' }));
    const first = fakeContainer();
    await siddur.open('edot-hamizrach/shacharit', { container: first });
    assert.equal(first.firstElementChild.cont.hidden, true);
});

test('rerender redraws the page on screen at the section being read', async () => {
    let layout = 'bilingual';
    const { siddur } = await makeSiddur({ deps: { getPrefs: () => ({ readerLayout: layout }) } });
    assert.equal(siddur.rerender(), null);
    const container = fakeContainer();
    await siddur.open('edot-hamizrach/mincha', { container });
    assert.match(container.html, /siddur-en/);
    container.firstElementChild.currentChip = { dataset: { siddurSection: 'alenu' } };
    layout = 'hebrew';
    const again = siddur.rerender();
    assert.deepEqual(again, { label: 'Mincha', section: 'alenu' });
    assert.doesNotMatch(container.html, /class="siddur-en"/);
});

test('scrollTo only moves within the page on screen', async () => {
    const { siddur } = await makeSiddur();
    assert.equal(siddur.scrollTo('edot-hamizrach/mincha/amida'), false);
    const container = fakeContainer();
    await siddur.open('edot-hamizrach/mincha', { container });
    assert.equal(siddur.scrollTo('edot-hamizrach/mincha'), false);
});

test('renderToc fills a menu and clears its busy state, also on failure', async () => {
    const { siddur } = await makeSiddur();
    const mount = fakeNode();
    await siddur.renderToc(mount, { compact: true, current: 'mincha' });
    assert.equal(mount.getAttribute('aria-busy'), 'false');
    assert.match(mount.innerHTML, /siddur-toc-compact/);
    assert.match(mount.innerHTML, /data-siddur-path="edot-hamizrach\/mincha" aria-current="page"/);
    await siddur.renderToc(null);

    const broken = await makeSiddur({ fetch: makeFetch({ fail: { '/api/siddur/v2/toc/': 500 } }) });
    const failed = fakeNode();
    await assert.rejects(broken.siddur.renderToc(failed));
    assert.equal(failed.getAttribute('aria-busy'), 'false');
});
