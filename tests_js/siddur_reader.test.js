/**
 * static/js/siddur-reader.js: route values, the table of contents, the
 * landing and service markup (typed lines, "today" marks, layouts), the
 * section tracker, saved positions and the screen wake lock.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

let mod;
async function reader() {
    if (!mod) mod = (await loadEsmModule('static/js/siddur-reader.js', {})).namespace;
    return mod;
}

const t = (en) => en;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');

const TOC = {
    rite: { slug: 'edot-hamizrach', title: { en: 'Sephardi (Edot HaMizrach)', he: 'נוסח עדות המזרח' } },
    source: {
        index: 'Siddur Edot HaMizrach',
        he: { title: 'Shaliehsaboo Edition', license: 'CC0' },
        en: { title: 'Sefaria Community Translation', license: 'CC0' },
    },
    version: 'abc123',
    occasions: [
        { slug: 'weekday', title: { en: 'Weekdays', he: 'ימות החול' }, services: [
            { slug: 'shacharit', title: { en: 'Shacharit', he: 'שחרית לימי החול' }, gloss: 'Weekday morning', sections: [
                { slug: 'keriat-shema', title: { en: 'The Shema', he: 'ק"ש' }, ref: 'R, Shema', lines: 2, english: 1 },
                { slug: 'amida', title: { en: 'Amida', he: 'עמידה' }, ref: 'R, Amida', lines: 3, english: 0 },
            ] },
            { slug: 'mincha', title: { en: 'Mincha', he: 'מנחה' }, gloss: 'Weekday afternoon', sections: [
                { slug: 'amida', title: { en: 'Amida', he: 'עמידה' }, ref: 'R, M', lines: 1, english: 1 },
            ] },
        ] },
        { slug: 'berachot', title: { en: 'Blessings', he: 'ברכות' }, services: [
            { slug: 'birkat-hamazon', title: { en: 'Birkat Hamazon', he: 'ברכת המזון' }, gloss: 'Grace after meals', sections: [
                { slug: 'birkat-hamazon', title: { en: 'Post Meal Blessing', he: 'ברכת המזון' }, ref: 'R, BH', lines: 1, english: 1 },
            ] },
        ] },
    ],
};

const PAYLOAD = {
    sections: [
        { slug: 'keriat-shema', lines: [
            { t: 'heading', he: 'קריאת שמע', en: 'Shema', n: 1 },
            { t: 'prayer', he: 'שְׁמַע <b>יִשְׂרָאֵל</b><script>x</script>', en: 'Hear, Israel', n: 2 },
        ] },
        { slug: 'amida', lines: [
            { t: 'instruction', he: 'בקיץ:', when: ['barchenu'], n: 1 },
            { t: 'conditional', label: 'בראש חדש:', he: 'רֹאשׁ חֹדֶשׁ', when: ['rosh-chodesh'], n: 2 },
            { t: 'prayer', he: '', en: 'Only English', n: 3 },
        ] },
    ],
};

test('safeLineHtml keeps b/i/small/br and escapes every other tag', async () => {
    const r = await reader();
    assert.equal(r.safeLineHtml('<b>a</b><i>b</i><small>c</small><br><script>d</script><img src=x>'),
        '<b>a</b><i>b</i><small>c</small><br>&lt;script>d&lt;/script>&lt;img src=x>');
    assert.equal(r.safeLineHtml(null), '');
});

test('paths and route values', async () => {
    const r = await reader();
    assert.equal(r.sitePath('edot-hamizrach'), '/siddur/edot-hamizrach');
    assert.equal(r.sitePath('edot-hamizrach', 'shacharit', 'amida'), '/siddur/edot-hamizrach/shacharit/amida');
    assert.equal(r.routeValue('edot-hamizrach', 'shacharit', null), 'edot-hamizrach/shacharit');
    assert.deepEqual(r.parseValue('edot-hamizrach/shacharit/amida'), { rite: 'edot-hamizrach', service: 'shacharit', section: 'amida' });
    assert.deepEqual(r.parseValue('edot-hamizrach'), { rite: 'edot-hamizrach', service: null, section: null });
    for (const bad of ['', 'Edot', 'a/b/c/d', '../x', 'a//b', null]) assert.equal(r.parseValue(bad), null, String(bad));
});

test('locate: the rite page, a service, one of its sections, and misses', async () => {
    const r = await reader();
    assert.deepEqual(r.locate(TOC, { service: null }), { occasion: null, service: null, section: null, index: -1 });
    const service = r.locate(TOC, { service: 'shacharit' });
    assert.equal(service.service.slug, 'shacharit');
    assert.equal(service.section, null);
    assert.equal(service.index, 0);
    assert.equal(service.services.length, 3);
    assert.equal(r.locate(TOC, { service: 'shacharit', section: 'amida' }).section.slug, 'amida');
    assert.equal(r.locate(TOC, { service: 'mincha' }).section.slug, 'amida');  // a lone section
    assert.equal(r.locate(TOC, { service: 'mincha', section: 'amida' }), null);  // ...has no URL of its own
    assert.equal(r.locate(TOC, { service: 'shacharit', section: 'nope' }), null);
    assert.equal(r.locate(TOC, { service: 'nope' }), null);
});

test('viewLabel in both languages', async () => {
    const r = await reader();
    assert.equal(r.viewLabel(TOC, { service: null }, false), 'Siddur · Sephardi (Edot HaMizrach)');
    assert.equal(r.viewLabel(TOC, { service: null }, true), 'סידור · נוסח עדות המזרח');
    assert.equal(r.viewLabel(TOC, r.locate(TOC, { service: 'shacharit' }), false), 'Shacharit');
    assert.equal(r.viewLabel(TOC, r.locate(TOC, { service: 'shacharit', section: 'amida' }), false), 'Amida · Shacharit');
    assert.equal(r.viewLabel(TOC, r.locate(TOC, { service: 'mincha' }), true), 'מנחה');
});

test('tocMarkup: grouped links, the current service marked, compact groups', async () => {
    const r = await reader();
    const html = r.tocMarkup(TOC, { escapeHtml, current: 'mincha' });
    assert.match(html, /<h4 class="siddur-toc-occasion">Weekdays<\/h4>/);
    assert.match(html, /href="\/siddur\/edot-hamizrach\/mincha" data-siddur-path="edot-hamizrach\/mincha" aria-current="page"/);
    assert.match(html, /<span class="siddur-toc-alt" lang="he" dir="rtl">מנחה<\/span>/);
    const compact = r.tocMarkup(TOC, { escapeHtml, compact: true, current: 'birkat-hamazon', isHebrew: true });
    // The first group and the one holding the current service start open.
    assert.equal((compact.match(/<details class="siddur-toc-group" open>/g) || []).length, 2);
    assert.match(compact, /<summary class="siddur-toc-occasion">ברכות<\/summary>/);
    assert.match(compact, /siddur-toc-alt" lang="en" dir="ltr">Birkat Hamazon/);
});

test('landingMarkup: a card per service, a Today slot, attribution', async () => {
    const r = await reader();
    const html = r.landingMarkup(TOC, { t, escapeHtml });
    assert.equal((html.match(/class="siddur-card"/g) || []).length, 3);
    assert.match(html, /data-siddur-today/);
    assert.match(html, /Hebrew: Shaliehsaboo Edition \(CC0\)/);
});

test('serviceMarkup: typed lines, a picker, today marks, sanitized text', async () => {
    const r = await reader();
    const where = r.locate(TOC, { service: 'shacharit' });
    const html = r.serviceMarkup(TOC, where, PAYLOAD, {
        layout: 'bilingual', today: new Set(['rosh-chodesh']), t, escapeHtml,
        applyHebrew: (s) => s.replace(/ְ/g, ''),
    });
    assert.match(html, /<nav class="siddur-sections"/);
    assert.match(html, /data-siddur-section="keriat-shema" aria-current="location">The Shema/);
    assert.match(html, /<span data-siddur-progress>1<\/span> \/ 2/);
    assert.match(html, /<h3 class="siddur-line siddur-heading" id="siddur-keriat-shema-1" data-n="1">/);
    // Script escaped; applyHebrew ran (the sheva gone).
    assert.match(html, /<\/b>&lt;script>x&lt;\/script>/);
    assert.ok(!html.includes('\u05b0'), 'applyHebrew ran over every Hebrew line');
    // Instructions and conditionals keep their day tags; only today's is marked, with a word.
    assert.match(html, /<p class="siddur-line siddur-instruction" id="siddur-amida-1" data-n="1" data-when="barchenu">/);
    assert.match(html, /data-when="rosh-chodesh" data-today><span class="siddur-today-badge">Today<\/span><span class="siddur-label"/);
    // Amida has no English: said, not left blank.
    assert.match(html, /No English translation for this section yet\./);
    // English-only line and the next service.
    assert.match(html, /<span class="siddur-en" lang="en" dir="ltr">Only English<\/span>/);
    assert.match(html, /rel="next" href="\/siddur\/edot-hamizrach\/mincha"/);
    assert.doesNotMatch(html, /rel="prev"/);
});

test('serviceMarkup layouts: Hebrew only, English with a Hebrew fallback', async () => {
    const r = await reader();
    const where = r.locate(TOC, { service: 'shacharit' });
    const hebrew = r.serviceMarkup(TOC, where, PAYLOAD, { layout: 'hebrew', t, escapeHtml });
    assert.doesNotMatch(hebrew, /Hear, Israel/);
    assert.doesNotMatch(hebrew, /No English translation/);
    assert.match(hebrew, /Only English/);  // a line with nothing else still shows
    const english = r.serviceMarkup(TOC, where, PAYLOAD, { layout: 'english', t, escapeHtml });
    assert.match(english, /Hear, Israel/);
    assert.doesNotMatch(english, /siddur-he" lang="he" dir="rtl">שְׁמַע/);
    assert.match(english, /siddur-he" lang="he" dir="rtl">בקיץ:/);  // no English -> Hebrew
});

test('a one-section service has no picker; a middle service links both ways', async () => {
    const r = await reader();
    const where = r.locate(TOC, { service: 'mincha' });
    const html = r.serviceMarkup(TOC, where, { sections: [{ slug: 'amida', lines: [] }] }, { t, escapeHtml });
    assert.doesNotMatch(html, /siddur-sections/);
    assert.doesNotMatch(html, /siddur-section-title/);
    assert.match(html, /rel="prev" href="\/siddur\/edot-hamizrach\/shacharit"/);
    assert.match(html, /rel="next" href="\/siddur\/edot-hamizrach\/birkat-hamazon"/);
    // A section the payload lacks renders empty rather than throwing.
    const missing = r.serviceMarkup(TOC, r.locate(TOC, { service: 'shacharit' }), { sections: [] }, { t, escapeHtml });
    assert.match(missing, /data-siddur-section-body="amida"/);
});

// ── behavior ───────────────────────────────────────────────────────────────

function fakeEl(attrs = {}, rect = { top: 0 }) {
    const attributes = new Map(Object.entries(attrs));
    return {
        dataset: {},
        textContent: '',
        rect,
        scrolled: [],
        getBoundingClientRect() { return this.rect; },
        setAttribute: (k, v) => attributes.set(k, String(v)),
        getAttribute: (k) => (attributes.has(k) ? attributes.get(k) : null),
        removeAttribute: (k) => attributes.delete(k),
        scrollIntoView(opts) { this.scrolled.push(opts); },
    };
}

function fakeTrackRoot() {
    const bodies = ['a', 'b', 'c'].map((slug, i) => Object.assign(fakeEl({}, { top: i * 1000 }), { dataset: { siddurSectionBody: slug } }));
    const chips = ['a', 'b', 'c'].map((slug) => Object.assign(fakeEl(), { dataset: { siddurSection: slug } }));
    const progress = fakeEl();
    return {
        bodies, chips, progress,
        querySelectorAll(sel) {
            if (sel === '[data-siddur-section-body]') return bodies;
            if (sel === '[data-siddur-section]') return chips;
            return [];
        },
        querySelector(sel) {
            if (sel === '[data-siddur-progress]') return progress;
            const m = /data-siddur-section-body="([a-z-]+)"/.exec(sel);
            return m ? bodies.find((b) => b.dataset.siddurSectionBody === m[1]) || null : null;
        },
    };
}

function fakeWin() {
    const listeners = new Map();
    const frames = [];
    return {
        innerHeight: 900,
        listeners,
        addEventListener: (type, fn) => listeners.set(type, fn),
        removeEventListener: (type) => listeners.delete(type),
        requestAnimationFrame: (fn) => { frames.push(fn); return frames.length; },
        cancelAnimationFrame: () => {},
        flush() { while (frames.length) frames.shift()(); },
    };
}

test('trackSections follows the scroll, quietly on open', async () => {
    const r = await reader();
    const root = fakeTrackRoot();
    const win = fakeWin();
    const seen = [];
    const stop = r.trackSections(root, { win, onSection: (slug) => seen.push(slug) });
    assert.equal(root.chips[0].getAttribute('aria-current'), 'location');
    assert.deepEqual(seen, [], 'opening the page is not moving to a section');
    // Scroll: b passes the one-third line.
    root.bodies[1].rect = { top: 200 };
    win.listeners.get('scroll')();
    win.listeners.get('scroll')();  // coalesced into one frame
    win.flush();
    assert.deepEqual(seen, ['b']);
    assert.equal(root.chips[0].getAttribute('aria-current'), null);
    assert.equal(root.chips[1].getAttribute('aria-current'), 'location');
    assert.equal(root.progress.textContent, '2');
    assert.deepEqual(root.chips[1].scrolled, [{ block: 'nearest', inline: 'nearest' }]);
    // Same section again: no repeat.
    win.listeners.get('scroll')();
    win.flush();
    assert.deepEqual(seen, ['b']);
    stop();
    assert.equal(win.listeners.has('scroll'), false);
});

test('trackSections does nothing for a one-section page', async () => {
    const r = await reader();
    const stop = r.trackSections({ querySelectorAll: () => [], querySelector: () => null }, { win: fakeWin() });
    assert.equal(typeof stop, 'function');
    stop();
});

test('scrollToSection', async () => {
    const r = await reader();
    const root = fakeTrackRoot();
    assert.equal(r.scrollToSection(root, 'b', { reduceMotion: true }), true);
    assert.deepEqual(root.bodies[1].scrolled, [{ block: 'start', behavior: 'auto' }]);
    assert.equal(r.scrollToSection(root, 'c'), true);
    assert.deepEqual(root.bodies[2].scrolled, [{ block: 'start', behavior: 'smooth' }]);
    assert.equal(r.scrollToSection(root, 'zzz'), false);
    assert.equal(r.scrollToSection(root, '"]x'), false);
});

test('saved positions per service', async () => {
    const r = await reader();
    const store = new Map();
    const storage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) };
    assert.equal(r.readPosition('edot-hamizrach/shacharit', storage), null);
    r.writePosition('edot-hamizrach/shacharit', 'amida', storage);
    r.writePosition('edot-hamizrach/mincha', 'alenu', storage);
    assert.equal(r.readPosition('edot-hamizrach/shacharit', storage), 'amida');
    assert.equal(r.readPosition('edot-hamizrach/mincha', storage), 'alenu');
    store.set('shelah.siddur.position', '{not json');
    assert.equal(r.readPosition('edot-hamizrach/shacharit', storage), null);
    assert.doesNotThrow(() => r.writePosition('x', 'y', { getItem() { throw new Error('no'); } }));
});

function fakeWakeEnv({ grant = true } = {}) {
    const listeners = new Map();
    const requests = [];
    const doc = {
        visibilityState: 'visible',
        addEventListener: (type, fn) => listeners.set(type, fn),
        removeEventListener: (type) => listeners.delete(type),
    };
    const nav = {
        wakeLock: {
            async request(kind) {
                requests.push(kind);
                if (!grant) throw new Error('NotAllowedError');
                const sentinel = {
                    released: false,
                    onRelease: null,
                    addEventListener(type, fn) { if (type === 'release') this.onRelease = fn; },
                    async release() { this.released = true; this.onRelease?.(); },
                };
                requests.sentinels = [...(requests.sentinels || []), sentinel];
                return sentinel;
            },
        },
    };
    return { doc, nav, listeners, requests };
}

test('wake lock: on, re-taken when the page comes back, off', async () => {
    const r = await reader();
    const env = fakeWakeEnv();
    const lock = r.createWakeLock({ nav: env.nav, doc: env.doc });
    assert.equal(lock.supported, true);
    assert.equal(await lock.enable(), true);
    assert.equal(lock.active, true);
    // The browser drops it when the page hides; back to visible re-takes it.
    await env.requests.sentinels[0].release();
    env.listeners.get('visibilitychange')();
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(env.requests.slice(), ['screen', 'screen']);
    await lock.disable();
    assert.equal(lock.active, false);
    assert.equal(env.requests.sentinels[1].released, true);
    lock.destroy();
    assert.equal(env.listeners.has('visibilitychange'), false);
});

test('wake lock: a refused request leaves it off; unsupported browsers say so', async () => {
    const r = await reader();
    const env = fakeWakeEnv({ grant: false });
    const lock = r.createWakeLock({ nav: env.nav, doc: env.doc });
    assert.equal(await lock.enable(), false);
    assert.equal(lock.active, false);
    const hidden = fakeWakeEnv();
    hidden.doc.visibilityState = 'hidden';
    const later = r.createWakeLock({ nav: hidden.nav, doc: hidden.doc });
    assert.equal(await later.enable(), false);  // asked while hidden: wait for visible
    assert.equal(later.active, true);
    const none = r.createWakeLock({ nav: {}, doc: fakeWakeEnv().doc });
    assert.equal(none.supported, false);
    assert.equal(await none.enable(), false);
    await none.disable();
});

// ── keepOffline ───────────────────────────────────────────────────────────

function fakeKeepEnv({ controller = true, standalone = false, iosStandalone = false, persist } = {}) {
    const posted = [];
    const persisted = [];
    const nav = {
        // `ready` resolves to the registration; its active worker takes the message.
        serviceWorker: controller ? { ready: Promise.resolve({ active: { postMessage: (message) => posted.push(message) } }) } : {},
        storage: persist === null ? {} : { persist: persist || (async () => { persisted.push(true); return true; }) },
    };
    if (iosStandalone) nav.standalone = true;
    const win = { matchMedia: (query) => ({ matches: standalone && query === '(display-mode: standalone)' }) };
    return { nav, win, posted, persisted };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test('keepOffline asks the service worker to keep every service of this version', async () => {
    const r = await reader();
    const env = fakeKeepEnv();
    r.keepOffline(TOC, env);
    await settle();
    assert.deepEqual(env.posted, [{
        type: 'PRECACHE_SIDDUR', rite: 'edot-hamizrach', version: 'abc123',
        services: ['shacharit', 'mincha', 'birkat-hamazon'],
    }]);
});

test('keepOffline posts nothing without service workers, an active worker, or a versioned contents', async () => {
    const r = await reader();
    const none = fakeKeepEnv({ controller: false });
    r.keepOffline(TOC, none);
    await settle();
    assert.deepEqual(none.posted, []);

    const unversioned = fakeKeepEnv();
    r.keepOffline({ ...TOC, version: '' }, unversioned);
    r.keepOffline(null, unversioned);
    await settle();
    assert.deepEqual(unversioned.posted, []);
    assert.doesNotThrow(() => r.keepOffline(TOC, { nav: null, win: null }));

    // A registration with no active worker, or one whose `ready` rejects.
    for (const ready of [Promise.resolve({ active: null }), Promise.reject(new Error('no sw'))]) {
        assert.doesNotThrow(() => r.keepOffline(TOC, { nav: { serviceWorker: { ready } }, win: {} }));
    }
    await settle();
});

test('keepOffline reaches the worker on a first visit, before it controls the page', async () => {
    const r = await reader();
    const posted = [];
    let activate;
    const ready = new Promise((resolve) => { activate = resolve; });
    r.keepOffline(TOC, { nav: { serviceWorker: { controller: null, ready } }, win: {} });
    await settle();
    assert.deepEqual(posted, [], 'nothing is active yet');
    activate({ active: { postMessage: (message) => posted.push(message) } });
    await settle();
    assert.equal(posted.length, 1);
});

test('keepOffline asks for persistent storage only in the installed app', async () => {
    const r = await reader();
    const tab = fakeKeepEnv();
    r.keepOffline(TOC, tab);
    await new Promise((resolve) => setImmediate(resolve));
    assert.deepEqual(tab.persisted, [], 'in a tab Firefox would prompt; never asked there');

    for (const options of [{ standalone: true }, { iosStandalone: true }]) {
        const installed = fakeKeepEnv(options);
        r.keepOffline(TOC, installed);
        await new Promise((resolve) => setImmediate(resolve));
        assert.deepEqual(installed.persisted, [true], JSON.stringify(options));
    }

    const refused = fakeKeepEnv({ standalone: true, persist: async () => { throw new Error('denied'); } });
    r.keepOffline(TOC, refused);
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(refused.posted.length, 1, 'a refusal is swallowed; the precache still went out');

    const noApi = fakeKeepEnv({ standalone: true, persist: null });
    assert.doesNotThrow(() => r.keepOffline(TOC, noApi));
});

test('findPage resolves a search to a service or an exact section, never from mid-title', async () => {
    const r = await reader();
    assert.equal(r.findPage(TOC, 'Shacharit'), 'edot-hamizrach/shacharit');
    assert.equal(r.findPage(TOC, '  MINCHA! '), 'edot-hamizrach/mincha');
    assert.equal(r.findPage(TOC, 'ברכת המזון'), 'edot-hamizrach/birkat-hamazon');
    assert.equal(r.findPage(TOC, 'The Shema'), 'edot-hamizrach/shacharit/keriat-shema', 'a section of a multi-section service');
    assert.equal(r.findPage(TOC, 'birkat ham'), 'edot-hamizrach/birkat-hamazon', 'a long enough start of a service title');
    assert.equal(r.findPage(TOC, 'hamazon'), null, 'not from the middle of a title');
    assert.equal(r.findPage(TOC, 'amid'), null, 'sections only match exactly');
    assert.equal(r.findPage(TOC, 'Psalms'), null);
    assert.equal(r.findPage(TOC, ''), null);
    assert.equal(r.findPage(null, 'shacharit'), null);
});
