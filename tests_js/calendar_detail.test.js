/**
 * static/js/calendar-detail.js — the calendar's holiday detail card.
 *
 * The module's only surface is window.ShelahCalendarDetail = { open, close, isOpen },
 * so these tests drive it the way templates/index.html does: build the #calDetail
 * dialog markup, call open() with Hebcal-shaped events, and read the card it renders.
 *
 * The DOM below is a hand-built fake covering just what the module touches (class /
 * attribute / child selectors, append / replaceChildren / after / replaceWith, events).
 * Like tests_js/zmanim.test.js's fake, it checks the card's content and behavior, not
 * layout or motion, which still need a real browser.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const MODULE_PATH = 'static/js/calendar-detail.js';
const SHEET_QUERY = '(max-width: 767px)';

// ── Fake DOM ────────────────────────────────────────────────────────────────

function matchesSimple(el, sel) {
    if (sel === ':first-child') return Boolean(el.parent) && el.parent.elements()[0] === el;
    if (sel.startsWith('.')) return el.classList.contains(sel.slice(1));
    const attr = /^\[([\w-]+)="([^"]*)"\]$/.exec(sel);
    if (attr) {
        const [, name, value] = attr;
        if (name.startsWith('data-')) return el.dataset[name.slice(5)] === value;
        return el.getAttribute(name) === value;
    }
    return el.tagName === sel.toUpperCase();
}

function matches(el, selector) {
    const [parentSel, childSel] = selector.split('>').map((s) => s.trim());
    if (childSel === undefined) return matchesSimple(el, parentSel);
    return matchesSimple(el, childSel) && Boolean(el.parent) && matchesSimple(el.parent, parentSel);
}

class FakeElement {
    constructor(tag, doc) {
        this.tagName = tag.toUpperCase();
        this.ownerDocument = doc;
        this.children = [];
        this.parent = null;
        this.attrs = {};
        this.dataset = {};
        this.style = {};
        this.listeners = {};
        this.className = '';
        this.hidden = false;
        this.scrollTop = 0;
        this.offsetWidth = 320;
        this.offsetHeight = 480;
        this.ownText = '';
        this.html = '';
        this.open = false;
    }

    get classList() {
        const names = () => this.className.split(/\s+/).filter(Boolean);
        return {
            add: (...add) => { this.className = [...new Set([...names(), ...add])].join(' '); },
            remove: (...drop) => { this.className = names().filter((n) => !drop.includes(n)).join(' '); },
            contains: (name) => names().includes(name),
        };
    }

    elements() { return this.children.filter((c) => c instanceof FakeElement); }

    setAttribute(name, value) { this.attrs[name] = String(value); if (name === 'id') this.id = String(value); }
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; }

    addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
    fire(type, extra = {}) {
        for (const fn of this.listeners[type] || []) fn({ target: this, preventDefault() {}, ...extra });
    }
    click() { this.fire('click'); }

    append(...kids) {
        for (const kid of kids) {
            if (kid instanceof FakeElement) {
                kid.remove();
                kid.parent = this;
                this.children.push(kid);
            } else {
                this.children.push(String(kid));
            }
        }
    }
    remove() {
        if (!this.parent) return;
        this.parent.children = this.parent.children.filter((c) => c !== this);
        this.parent = null;
    }
    replaceChildren(...kids) {
        for (const c of this.elements()) c.parent = null;
        this.children = [];
        this.ownText = '';
        this.append(...kids);
    }
    after(node) {
        const p = this.parent;
        node.remove();
        node.parent = p;
        p.children.splice(p.children.indexOf(this) + 1, 0, node);
    }
    replaceWith(node) {
        const p = this.parent;
        node.remove();
        node.parent = p;
        p.children[p.children.indexOf(this)] = node;
        this.parent = null;
    }
    insertAdjacentHTML(_where, html) { this.html += html; }
    set innerHTML(html) { this.html = html; }
    get innerHTML() { return this.html; }

    set textContent(text) { this.replaceChildren(); this.ownText = String(text); }
    get textContent() {
        return this.ownText + this.children.map((c) => (c instanceof FakeElement ? c.textContent : c)).join('');
    }

    get isConnected() {
        let node = this;
        while (node.parent) node = node.parent;
        return node === this.ownerDocument.body;
    }

    querySelectorAll(selector) {
        const found = [];
        const walk = (el) => {
            for (const child of el.elements()) {
                if (matches(child, selector)) found.push(child);
                walk(child);
            }
        };
        walk(this);
        return found;
    }
    querySelector(selector) { return this.querySelectorAll(selector)[0] ?? null; }
    closest(selector) {
        for (let node = this; node; node = node.parent) if (matchesSimple(node, selector)) return node;
        return null;
    }

    getBoundingClientRect() { return { left: 100, top: 100, right: 180, bottom: 124, width: 80, height: 24 }; }
    focus() { this.ownerDocument.activeElement = this; }
    showModal() { this.open = true; }
    close() { this.open = false; }
}

function createDocument() {
    const doc = { activeElement: null };
    doc.createElement = (tag) => new FakeElement(tag, doc);
    doc.body = new FakeElement('body', doc);
    doc.getElementById = (id) => doc.body.querySelectorAll('div').concat(doc.body.querySelectorAll('dialog'))
        .find((el) => el.id === id) ?? null;

    // Same structure as the #calDetail markup in templates/index.html.
    const el = (tag, cls, ...kids) => {
        const node = doc.createElement(tag);
        if (cls) node.className = cls;
        node.append(...kids);
        return node;
    };
    const dialog = el('dialog', 'cal-detail',
        el('div', 'cal-detail__scrim'),
        el('div', 'cal-detail__panel',
            el('span', 'cal-detail__arrow'),
            el('div', 'cal-detail__drag',
                el('span', 'cal-detail__grabber'),
                el('div', 'cal-detail__bar',
                    el('button', 'cal-detail__back', 'Back'),
                    el('span', 'cal-detail__kicker', el('span', 'cal-detail__dot'), el('span', 'cal-detail__kind')),
                    el('button', 'cal-detail__close'))),
            el('div', 'cal-detail__scroll')));
    dialog.setAttribute('id', 'calDetail');
    doc.body.append(dialog);
    return doc;
}

// ── Harness ─────────────────────────────────────────────────────────────────

const tick = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

async function loadCard({ sheet = false, fetchImpl = null, hebrewRef = null } = {}) {
    const document = createDocument();
    const fetchCalls = [];
    const window = {
        innerWidth: 1280,
        innerHeight: 800,
        addEventListener() {},
        matchMedia: (query) => ({ matches: query === SHEET_QUERY ? sheet : false, addEventListener() {} }),
        ShelahHebrewRef: hebrewRef,
    };
    const fetch = async (url) => {
        fetchCalls.push(url);
        if (!fetchImpl) throw new Error('no network in this test');
        return fetchImpl(url);
    };
    await loadEsmModule(MODULE_PATH, {
        window,
        document,
        fetch,
        requestAnimationFrame: () => 1,
        cancelAnimationFrame() {},
    });
    const scroll = document.body.querySelector('.cal-detail__scroll');
    const rowText = (slot) => scroll.querySelector(`[data-slot="${slot}"]`)?.textContent ?? null;
    return { api: window.ShelahCalendarDetail, document, scroll, fetchCalls, rowText };
}

const LINK = 'https://www.hebcal.com/holidays/rosh-hashana-2026';

// Rosh Hashana 5787: Sat–Sun 12–13 Sep 2026 (yom tov), plus its Erev on the 11th.
const ROSH_HASHANA = [
    { key: '2026-09-13', title: 'Rosh Hashana II', category: 'major', detail: { link: LINK, yomtov: true, subcat: 'major' } },
    { key: '2026-09-11', title: 'Erev Rosh Hashana', category: 'major', detail: { link: LINK, subcat: 'major' } },
    {
        key: '2026-09-12', title: '🍎 Rosh Hashana 5787', category: 'major',
        detail: {
            link: LINK, yomtov: true, subcat: 'major', hdate: '1 Tishrei 5787', hebrew: 'ראש השנה',
            memo: 'The Jewish New Year',
            leyning: {
                torah: 'Genesis 21:1-34; Numbers 29:1-6',
                haftarah: 'I Samuel 1:1-2:10 | Rosh Hashana I',
                maftir: 'see the note in the siddur',
            },
        },
    },
];

const openRoshHashana = (api, extra = {}) => api.open({
    anchor: null, dateKey: '2026-09-12', events: [ROSH_HASHANA[2]], allEvents: ROSH_HASHANA, ...extra,
});

// ── Readings ────────────────────────────────────────────────────────────────

test('each Torah / Haftarah reference becomes its own button that opens the reader', async () => {
    const { api, scroll } = await loadCard();
    const opened = [];
    openRoshHashana(api, { openText: (ref) => opened.push(ref) });

    const refs = scroll.querySelectorAll('.cal-detail__ref');
    assert.deepEqual(refs.map((b) => b.getAttribute('aria-label')), [
        'Read: Genesis 21:1-34',
        'Read: Numbers 29:1-6',
        'Read: I Samuel 1:1-2:10',
    ]);
    refs[2].click();
    assert.deepEqual(opened, ['I Samuel 1:1-2:10']);
});

test('a trailing "| label" on a reading is shown as a note, not linked', async () => {
    const { api, scroll } = await loadCard();
    openRoshHashana(api, { openText: () => {} });

    const haftarah = scroll.querySelectorAll('.cal-detail__row--refs')[1];
    assert.equal(haftarah.querySelector('small').textContent, 'Rosh Hashana I');
});

test('a reading that does not parse stays plain text instead of a wrong link', async () => {
    const { api, scroll } = await loadCard();
    openRoshHashana(api, { openText: () => {} });

    const rows = scroll.querySelectorAll('.cal-detail__row');
    const maftir = rows.find((r) => r.querySelector('dt')?.textContent === 'Maftir');
    assert.ok(maftir);
    assert.equal(maftir.querySelectorAll('.cal-detail__ref').length, 0);
    assert.match(maftir.textContent, /see the note in the siddur/);
});

test('without an openText callback every reading is plain text', async () => {
    const { api, scroll } = await loadCard();
    openRoshHashana(api);

    assert.equal(scroll.querySelectorAll('.cal-detail__ref').length, 0);
    assert.match(scroll.textContent, /Genesis 21:1-34; Numbers 29:1-6/);
});

test('a same-book continuation keeps the previous book; chapter-spanning ranges keep both chapters', async () => {
    const { api, scroll } = await loadCard();
    const ev = {
        key: '2026-02-14', title: 'Shabbat Shekalim', category: 'holiday',
        detail: { subcat: 'shabbat', leyning: { torah: 'Exodus 21:1-24:18, 30:11-16', maftir: 'Exodus 30:11-11' } },
    };
    api.open({ anchor: null, dateKey: ev.key, events: [ev], openText: () => {} });

    assert.deepEqual(scroll.querySelectorAll('.cal-detail__ref').map((b) => b.getAttribute('aria-label')), [
        'Read: Exodus 21:1-24:18',
        'Read: Exodus 30:11-16',
        'Read: Exodus 30:11',
    ]);
});

test('the Hebrew UI names the book in Hebrew and writes the numbers as Hebrew numerals', async () => {
    const { api, scroll } = await loadCard({ hebrewRef: { hebrewNumeral: (n) => `[${n}]` } });
    openRoshHashana(api, { he: true, openText: () => {}, bookName: (book) => `${book}-he` });

    assert.equal(scroll.querySelector('.cal-detail__ref').getAttribute('aria-label'), 'קרא: Genesis-he [21]:[1]-[34]');
});

// ── Begins / Ends ───────────────────────────────────────────────────────────

test('a two-day yom tov runs from sundown before the first day to nightfall on the last', async () => {
    const { api, rowText } = await loadCard();
    openRoshHashana(api);

    // No location: the card names the moments instead of giving clock times.
    assert.match(rowText('begins'), /^BeginsSundownFriday, September 11$/);
    assert.match(rowText('ends'), /^EndsNightfallSunday, September 13$/);
});

test('a minor fast runs from dawn; Tisha B\'Av from the sundown before', async () => {
    const { api, rowText } = await loadCard();
    const gedalia = { key: '2026-09-14', title: 'Tzom Gedaliah', category: 'holiday', detail: { subcat: 'fast' } };
    api.open({ anchor: null, dateKey: gedalia.key, events: [gedalia] });
    assert.match(rowText('begins'), /^BeginsDawn/);

    const tishaBav = { key: '2026-07-23', title: "Tish'a B'Av", category: 'holiday', detail: { subcat: 'fast' } };
    api.open({ anchor: null, dateKey: tishaBav.key, events: [tishaBav] });
    assert.match(rowText('begins'), /^BeginsSundownWednesday, July 22$/);
});

test('with a location, the words are swapped for clock times at that place', async () => {
    const fetchImpl = async () => ({
        ok: true,
        json: async () => ({
            timezone: 'America/New_York',
            days: {
                '2026-09-11': { candles: '2026-09-11T18:48:00-04:00', sunset: '2026-09-11T19:06:00-04:00' },
                '2026-09-13': { nightfall: '2026-09-13T19:50:00-04:00', havdalah: '2026-09-13T19:55:00-04:00' },
            },
        }),
    });
    const { api, rowText, fetchCalls } = await loadCard({ fetchImpl });
    const location = { lat: 40.7, lon: -74, label: 'Brooklyn, NY' };
    openRoshHashana(api, { location });
    await tick(5);

    assert.equal(fetchCalls.length, 1);
    assert.match(fetchCalls[0], /dates=2026-09-11%2C2026-09-13/);
    // The zone name is added only when it differs from the viewer's own (e.g. on a UTC runner).
    assert.match(rowText('begins'), /^Begins6:48\sPM(?: EDT)?Candle lighting · Friday, September 11$/);
    assert.match(rowText('ends'), /^Ends7:55\sPM(?: EDT)?Havdalah · Sunday, September 13$/);
    assert.equal(rowText('where'), 'Times for Brooklyn, NY');
});

test('when the times request fails the card keeps its words', async () => {
    const { api, rowText, fetchCalls } = await loadCard(); // fetch rejects
    openRoshHashana(api, { location: { lat: 40.7, lon: -74 } });
    await tick(5);

    assert.equal(fetchCalls.length, 1);
    assert.match(rowText('begins'), /^BeginsSundown/);
    assert.equal(rowText('where'), null);
});

// ── Date row, footer, links ─────────────────────────────────────────────────

test('the date row shows the civil date with the Hebrew-calendar date under it', async () => {
    const { api, scroll } = await loadCard();
    openRoshHashana(api);

    const date = scroll.querySelector('.cal-detail__row');
    assert.equal(date.querySelector('dt').textContent, 'Date');
    assert.match(date.querySelector('dd').textContent, /^Saturday, September 12, 2026/);
    assert.equal(date.querySelector('small').textContent, '1 Tishrei 5787');
});

test('only an https Hebcal link is linked; the Hebcal credit is always shown', async () => {
    const { api, scroll } = await loadCard();
    openRoshHashana(api);
    assert.equal(scroll.querySelector('.cal-detail__link').getAttribute('href'), LINK);

    for (const link of ['not a url', 'http://www.hebcal.com/x', 'https://evil.example/hebcal']) {
        const ev = { key: '2026-09-12', title: 'Day', category: 'holiday', detail: { link } };
        api.open({ anchor: null, dateKey: ev.key, events: [ev] });
        assert.equal(scroll.querySelector('.cal-detail__link'), null, link);
        assert.match(scroll.querySelector('.cal-detail__credit').textContent, /Hebcal\.com/);
    }
});

test('the title drops the leading emoji Hebcal uses as chip decoration', async () => {
    const { api, scroll } = await loadCard();
    openRoshHashana(api);
    assert.equal(scroll.querySelector('.cal-detail__title').textContent, 'Rosh Hashana 5787');
});

// ── Day view, back, close ───────────────────────────────────────────────────

test('a day with several events lists them; picking one shows it with a Back button', async () => {
    const { api, document, scroll } = await loadCard();
    api.open({ anchor: null, dateKey: '2026-09-12', events: ROSH_HASHANA.slice(1), allEvents: ROSH_HASHANA });

    const items = scroll.querySelectorAll('.cal-detail__item');
    assert.equal(items.length, 2);
    items[1].click();
    await tick(200);

    const back = document.body.querySelector('.cal-detail__back');
    assert.equal(back.hidden, false);
    assert.equal(scroll.querySelector('.cal-detail__title').textContent, 'Rosh Hashana 5787');
    back.click();
    await tick(200);
    assert.equal(scroll.querySelectorAll('.cal-detail__item').length, 2);
});

test('closing returns focus to what had it before the card opened', async () => {
    const { api, document } = await loadCard();
    const chip = document.createElement('button');
    document.body.append(chip);
    chip.focus();

    openRoshHashana(api, { anchor: chip });
    assert.equal(api.isOpen(), true);
    assert.notEqual(document.activeElement, chip);

    await api.close();
    assert.equal(api.isOpen(), false);
    assert.equal(document.activeElement, chip);
});

test('an immediate close with a detached anchor does not try to refocus it', async () => {
    const { api, document } = await loadCard();
    const detached = document.createElement('button');
    document.activeElement = detached;

    openRoshHashana(api);
    await api.close({ immediate: true });
    assert.equal(api.isOpen(), false);
    assert.notEqual(document.activeElement, detached);
});

test('on a phone, a tap on the sheet header without a drag leaves the sheet open', async () => {
    const { api, document } = await loadCard({ sheet: true });
    openRoshHashana(api);

    const grip = document.body.querySelector('.cal-detail__drag');
    grip.fire('pointerdown', { button: 0, clientY: 50, pointerId: 1 });
    grip.fire('pointerup');
    assert.equal(api.isOpen(), true);
    assert.equal(document.body.querySelector('.cal-detail__scrim').style.transition, '');
});
