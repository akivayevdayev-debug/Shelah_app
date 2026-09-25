/**
 * calendar-detail.js — Apple-style detail card for calendar days and events.
 *
 * Tapping a day or event opens ONE <dialog id="calDetail"> that is
 *   • desktop (>= 768px): an anchored popover — arrow, placed on the best side of the
 *     tapped element, growing out of it and folding back into it on close;
 *   • phone   (<  768px): a bottom sheet — grabber, drag-to-dismiss with velocity
 *     projection and rubber-banding, slides up from / down to the same edge.
 *
 * Content is what Hebcal shows on its holiday pages: description, when it begins and
 * ends (sundown / nightfall / dawn), Torah + Haftarah, Hebrew name and date.
 *
 * Motion (ENGINEERING_RULES.md): springs via window.ShelahMotion (motion.dev) drive
 * transform only; opacity is a CSS transition, which is also the reduced-motion
 * cross-fade.  Everything is interruptible: a touch mid-animation reads the live
 * position and carries its velocity into the next spring.
 *
 * Public API: window.ShelahCalendarDetail = { open, close, isOpen }.
 */

const SHEET_QUERY = '(max-width: 767px)';
const ARROW = 8;              // how far the arrow protrudes past the panel edge (px)
const GAP = 10;               // panel ↔ anchor distance: arrow plus breathing room
const EDGE = 12;              // minimum distance to the viewport edge
const ARROW_INSET = 24;       // keep the arrow clear of the rounded corners
const DECELERATION = 0.998;   // UIScrollView-style momentum projection
const HYSTERESIS = 6;         // px of travel before a press becomes a drag
const FADE_MS = 170;          // matches the CSS opacity transition on the panel

const HEBCAL_HOSTS = new Set(['hebcal.com', 'www.hebcal.com']);

const STR = {
    en: {
        close: 'Close', back: 'Back', date: 'Date',
        begins: 'Begins', ends: 'Ends', torah: 'Torah', haftarah: 'Haftarah', maftir: 'Maftir',
        sundown: 'Sundown', nightfall: 'Nightfall', dawn: 'Dawn',
        // Names for a moment that has a clock time next to it.
        at: { candles: 'Candle lighting', sunset: 'Sunset', havdalah: 'Havdalah', nightfall: 'Nightfall', dawn: 'Dawn' },
        timesFor: 'Times for', candleLighting: 'Candle lighting', read: 'Read',
        learn: 'Learn more on Hebcal', credit: 'Calendar data:', empty: 'No holidays or special days.',
        kind: {
            holiday: 'Holiday', minor: 'Minor holiday', fast: 'Fast day', shabbat: 'Shabbat',
            specialShabbat: 'Special Shabbat', roshchodesh: 'Rosh Chodesh', modern: 'Modern holiday',
            special: 'Special day', day: 'Day',
        },
    },
    he: {
        close: 'סגור', back: 'חזרה', date: 'תאריך',
        begins: 'מתחיל', ends: 'מסתיים', torah: 'תורה', haftarah: 'הפטרה', maftir: 'מפטיר',
        sundown: 'בשקיעה', nightfall: 'בצאת הכוכבים', dawn: 'בעלות השחר',
        at: { candles: 'הדלקת נרות', sunset: 'שקיעה', havdalah: 'הבדלה', nightfall: 'צאת הכוכבים', dawn: 'עלות השחר' },
        timesFor: 'זמנים עבור', candleLighting: 'הדלקת נרות', read: 'קרא',
        learn: 'למידע נוסף ב-Hebcal', credit: 'נתוני לוח שנה:', empty: 'אין חגים או ימים מיוחדים.',
        kind: {
            holiday: 'חג', minor: 'חג משני', fast: 'יום צום', shabbat: 'שבת',
            specialShabbat: 'שבת מיוחדת', roshchodesh: 'ראש חודש', modern: 'יום מודרני',
            special: 'יום מיוחד', day: 'יום',
        },
    },
};

// Phosphor "caret" glyphs (regular weight), inline so the module needs no icon macro.
const CARET_RIGHT = '<svg viewBox="0 0 256 256" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M181.66,133.66l-80,80a8,8,0,0,1-11.32-11.32L164.69,128,90.34,53.66a8,8,0,0,1,11.32-11.32l80,80A8,8,0,0,1,181.66,133.66Z"/></svg>';
const CARET_LEFT = '<svg viewBox="0 0 256 256" width="16" height="16" fill="currentColor" aria-hidden="true"><path d="M165.66,202.34a8,8,0,0,1-11.32,11.32l-80-80a8,8,0,0,1,0-11.32l80-80a8,8,0,0,1,11.32,11.32L91.31,128Z"/></svg>';

// ── Small helpers ──────────────────────────────────────────────────────────

const pad = (n) => String(n).padStart(2, '0');
const keyOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const dateOf = (key) => { const [y, m, d] = key.split('-').map(Number); return new Date(y, m - 1, d); };
const addDays = (key, n) => { const d = dateOf(key); d.setDate(d.getDate() + n); return keyOf(d); };
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function h(tag, props = {}, ...kids) {
    const el = document.createElement(tag);
    for (const [name, value] of Object.entries(props)) {
        if (value == null || value === false) continue;
        if (name === 'class') el.className = value;
        else if (name === 'text') el.textContent = value;
        else if (name.startsWith('on')) el.addEventListener(name.slice(2), value);
        else el.setAttribute(name, value === true ? '' : value);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid);
    return el;
}

// Leading emoji/symbols are chip decoration; the card sets the name in type.
const cleanTitle = (title) => String(title || '').replace(/^[^\p{L}\p{N}]+/u, '').trim();

// Hebcal's `link` is third-party data rendered as an href: re-check it here as well
// as on the server.
function safeHebcalUrl(value) {
    try {
        const url = new URL(String(value || ''));
        return url.protocol === 'https:' && HEBCAL_HOSTS.has(url.hostname) ? url.href : null;
    } catch (_) {
        return null;
    }
}

// Spring parameters, in Apple's duration/bounce vocabulary (see motion.js).
const spring = (duration, bounce = 0) => window.ShelahMotion?.appleSpring?.(duration, bounce)
    ?? { stiffness: 200, damping: 26, mass: 1 };

// ── State ──────────────────────────────────────────────────────────────────

const state = {
    open: false,
    closing: false,
    token: 0,          // invalidates awaits that outlive a close/reopen
    mode: 'popover',
    anchor: null,
    prevFocus: null,
    ctx: null,         // { he, T, events, allEvents, dateKey, location, openText, bookName }
    view: null,        // 'day' | 'event'
    event: null,       // the event the 'event' view is showing
    y: 0,              // sheet translateY (px): the single source of truth for its position
    motion: null,      // running spring controls
    drag: null,
};

let dialog; let panel; let scrim; let scroll; let arrow; let kindEl; let dotEl; let backBtn; let closeBtn; let gripEl;

function bind() {
    dialog = document.getElementById('calDetail');
    if (!dialog || dialog.dataset.bound) return Boolean(dialog);
    dialog.dataset.bound = '1';
    panel = dialog.querySelector('.cal-detail__panel');
    scrim = dialog.querySelector('.cal-detail__scrim');
    scroll = dialog.querySelector('.cal-detail__scroll');
    arrow = dialog.querySelector('.cal-detail__arrow');
    kindEl = dialog.querySelector('.cal-detail__kind');
    dotEl = dialog.querySelector('.cal-detail__dot');
    backBtn = dialog.querySelector('.cal-detail__back');
    closeBtn = dialog.querySelector('.cal-detail__close');
    gripEl = dialog.querySelector('.cal-detail__drag');

    dialog.addEventListener('cancel', (e) => { e.preventDefault(); close(); });      // Escape
    dialog.addEventListener('click', (e) => { if (e.target === dialog || e.target === scrim) close(); });
    closeBtn.addEventListener('click', () => close());
    backBtn.addEventListener('click', () => showDay({ swap: true }));
    window.addEventListener('resize', () => { if (state.open && state.mode === 'popover') placePopover(); });
    window.matchMedia(SHEET_QUERY).addEventListener('change', () => close({ immediate: true }));
    attachDrag();
    return true;
}

// ── Time-of-day semantics (what Hebcal's holiday pages show) ───────────────
//
// A yom tov begins at sundown the evening before and ends at nightfall on its last
// day; Shabbat likewise; a minor fast runs dawn → nightfall, while Yom Kippur (a yom
// tov) and Tish'a B'Av begin at sundown.  Everything else is an all-day date.

// Consecutive yom tov days of one holiday (Rosh Hashana I–II).  Only yom tov days
// count: Erev and Chol HaMoed share the holiday's Hebcal link but are not the
// observance itself.
function runOfSameHoliday(ev, allEvents) {
    const link = ev.detail?.link;
    if (!link) return { first: ev.key, last: ev.key };
    const keys = [...new Set(allEvents.filter((e) => e.detail?.link === link && e.detail?.yomtov).map((e) => e.key))].sort();
    let i = keys.indexOf(ev.key);
    if (i < 0) return { first: ev.key, last: ev.key };
    let lo = i; let hi = i;
    while (lo > 0 && keys[lo - 1] === addDays(keys[lo], -1)) lo -= 1;
    while (hi < keys.length - 1 && keys[hi + 1] === addDays(keys[hi], 1)) hi += 1;
    return { first: keys[lo], last: keys[hi] };
}

function observance(ev, allEvents) {
    const d = ev.detail || {};
    if (d.yomtov) {
        const { first, last } = runOfSameHoliday(ev, allEvents);
        return { begins: { at: 'sundown', key: addDays(first, -1) }, ends: { at: 'nightfall', key: last } };
    }
    if (ev.category === 'parashat') {
        return { begins: { at: 'sundown', key: addDays(ev.key, -1) }, ends: { at: 'nightfall', key: ev.key } };
    }
    if (d.subcat === 'fast') {
        const sundown = /tish.?a b/i.test(ev.title);
        return {
            begins: { at: sundown ? 'sundown' : 'dawn', key: sundown ? addDays(ev.key, -1) : ev.key },
            ends: { at: 'nightfall', key: ev.key },
        };
    }
    return null;
}

function kindOf(ev) {
    const sub = ev.detail?.subcat;
    if (ev.category === 'parashat') return 'shabbat';
    if (ev.category === 'roshchodesh') return 'roshchodesh';
    if (sub === 'fast') return 'fast';
    if (sub === 'modern') return 'modern';
    if (sub === 'minor') return 'minor';
    if (sub === 'shabbat') return 'specialShabbat';
    if (ev.category === 'holiday' || ev.category === 'major' || sub === 'major') return 'holiday';
    return 'special';
}

// ── Content views ──────────────────────────────────────────────────────────

function formats(he) {
    const locale = he ? 'he-IL' : 'en-US';
    const long = new Intl.DateTimeFormat(locale, { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' });
    const short = new Intl.DateTimeFormat(locale, { weekday: 'long', month: 'long', day: 'numeric' });
    let hebrew = null;
    try {
        hebrew = new Intl.DateTimeFormat(he ? 'he-u-ca-hebrew' : 'en-u-ca-hebrew', { day: 'numeric', month: 'long', year: 'numeric' });
    } catch (_) { /* engine without the Hebrew calendar */ }
    return {
        long: (key) => long.format(dateOf(key)),
        short: (key) => short.format(dateOf(key)),
        hebrew: (key) => (hebrew ? hebrew.format(dateOf(key)) : ''),
    };
}

const row = (label, ...value) => h('div', { class: 'cal-detail__row' }, h('dt', { text: label }), h('dd', {}, ...value));

// A reading may wrap between words but never inside a verse range ("29:1-" / "6").
function readingNodes(text) {
    return String(text)
        .split(/(\d+(?::\d+)?(?:-\d+(?::\d+)?)?)/)
        .map((part, i) => (i % 2 ? h('span', { class: 'cal-detail__nowrap', text: part }) : part));
}

// Hebcal writes a reading as "Genesis 21:1-34; Numbers 29:1-6", sometimes with a same-book
// continuation ("Exodus 21:1-24:18, 30:11-16") or a trailing label ("… | Shabbat Zachor").
// Each piece becomes one reference the reader can open; anything that doesn't parse
// cleanly yields no references, so the text is shown plain rather than linked wrongly.
const PIECE = /^(?:(\D.*?)\s+)?(\d+):(\d+)(?:-(?:(\d+):)?(\d+))?$/;

function parseReading(text) {
    const [list, ...rest] = String(text).split(/\s+\|\s+/);
    const note = rest.join(' | ');
    const refs = [];
    let book = '';
    for (const piece of list.split(/\s*[;,]\s*/)) {
        const m = PIECE.exec(piece.trim());
        if (!m || !(m[1] || book)) return { refs: [], note };
        book = m[1] || book;
        const [, , chapter, verse, endChapter, endVerse] = m;
        let range = `${chapter}:${verse}`;
        if (endVerse !== undefined && (endChapter ?? chapter) !== chapter) range += `-${endChapter}:${endVerse}`;
        else if (endVerse !== undefined && endVerse !== verse) range += `-${endVerse}`;
        refs.push({ book, range, ref: `${book} ${range}` });
    }
    return { refs, note };
}

// A tapped reading opens in the reader. The app owns the reader and dismisses the
// calendar (this card with it) before showing the text.
function readRef(ref) {
    state.ctx?.openText?.(ref);
}

// Torah / Haftarah / Maftir: one tappable line per reference.
function readingRow(label, text) {
    const { refs, note } = parseReading(text);
    if (!refs.length || !state.ctx.openText) return row(label, readingNodes(text));
    const { he, T, bookName } = state.ctx;
    const el = row(label,
        refs.map(({ book, range, ref }) => {
            const name = he && bookName ? bookName(book) : book;
            return h('button', { type: 'button', class: 'cal-detail__ref', 'aria-label': `${T.read}: ${name} ${range}`, onclick: () => readRef(ref) },
                h('span', { class: 'cal-detail__ref-label' }, `${name} `, h('span', { dir: 'ltr', text: range })),
                icon(CARET_RIGHT));
        }),
        note ? h('small', { text: note }) : null);
    el.classList.add('cal-detail__row--refs');
    return el;
}

// ── Clock times at the user's zmanim location ──────────────────────────────
//
// The card first shows "Sundown / Nightfall / Dawn" (no location needed), then swaps in
// the real clock times once /api/zmanim/days answers.  With no location, or if the
// request fails, the words simply stay.

const timeCache = new Map(); // "lat,lon|YYYY-MM-DD" -> { tz, dawn, sunset, nightfall, candles, havdalah }
const timeId = (loc, key) => `${loc.lat},${loc.lon}|${key}`;

// The dates whose clock times an event's card can use.
function timeKeys(ev, allEvents) {
    const span = observance(ev, allEvents);
    const keys = new Set(span ? [span.begins.key, span.ends.key] : []);
    if (isErev(ev)) keys.add(ev.key);
    return [...keys];
}

const isErev = (ev) => /^erev\b/i.test(cleanTitle(ev.title));

async function loadTimes(loc, keys) {
    const missing = keys.filter((k) => !timeCache.has(timeId(loc, k)));
    if (!missing.length) return true;
    try {
        const query = new URLSearchParams({ lat: String(loc.lat), lon: String(loc.lon), dates: missing.join(',') });
        const res = await fetch(`/api/zmanim/days?${query}`);
        if (!res.ok) return false;
        const data = await res.json();
        for (const key of missing) {
            if (data.days?.[key]) timeCache.set(timeId(loc, key), { tz: data.timezone, ...data.days[key] });
        }
        return true;
    } catch (_) {
        return false; // offline or a bad response: the words stay
    }
}

const timesOf = (key) => {
    const loc = state.ctx?.location;
    return loc ? timeCache.get(timeId(loc, key)) : undefined;
};

// A clock time in the LOCATION's timezone, with the zone named only when the viewer isn't in it.
function clock(iso, tz, he) {
    const opts = { hour: 'numeric', minute: '2-digit', timeZone: tz || undefined };
    if (tz && tz !== Intl.DateTimeFormat().resolvedOptions().timeZone) opts.timeZoneName = 'short';
    return new Intl.DateTimeFormat(he ? 'he-IL' : 'en-US', opts).format(new Date(iso));
}

// What the evening / morning at `key` is measured by, if its time is known.
//   sundown  → candle lighting on an eve of Shabbat / yom tov, otherwise sunset
//   nightfall→ havdalah when Hebcal lists it, otherwise nightfall
function momentAt(at, key) {
    const t = timesOf(key);
    if (!t) return null;
    const pick = { sundown: [['candles', t.candles], ['sunset', t.sunset]], nightfall: [['havdalah', t.havdalah], ['nightfall', t.nightfall]], dawn: [['dawn', t.dawn]] }[at];
    const [name, iso] = pick.find(([, value]) => value) || [];
    return iso ? { name, iso, tz: t.tz } : null;
}

// Begins / Ends (and an eve's own candle lighting): a row per moment.
function timeRows(ev, allEvents) {
    const { T, he } = state.ctx;
    const f = formats(he);
    const rows = [];
    const span = observance(ev, allEvents);
    if (span) {
        for (const [slot, label, part] of [['begins', T.begins, span.begins], ['ends', T.ends, span.ends]]) {
            const m = momentAt(part.at, part.key);
            rows.push(slotRow(slot, label,
                m ? clock(m.iso, m.tz, he) : T[part.at],
                h('small', { text: m ? (part.key === ev.key ? T.at[m.name] : `${T.at[m.name]} · ${f.short(part.key)}`) : f.short(part.key) })));
        }
    }
    const eve = isErev(ev) ? timesOf(ev.key)?.candles : null;
    if (eve) rows.push(slotRow('candles', T.candleLighting, clock(eve, timesOf(ev.key).tz, he)));
    if (timeKeys(ev, allEvents).some((k) => timesOf(k))) rows.push(whereRow());
    return rows;
}

// "Times for Brooklyn, NY" -- which location the clock times above belong to.
function whereRow() {
    const { T, location } = state.ctx;
    const place = location.label || location.timezone || `${location.lat.toFixed(2)}, ${location.lon.toFixed(2)}`;
    const el = h('div', { class: 'cal-detail__row cal-detail__row--where' }, h('p', { class: 'cal-detail__where', text: `${T.timesFor} ${place}` }));
    el.dataset.slot = 'where';
    return el;
}

const slotRow = (slot, label, ...value) => {
    const el = row(label, ...value);
    el.dataset.slot = slot;
    return el;
};

// Swap real times into the card that's already on screen: only the time rows change,
// so nothing the reader is looking at is rebuilt.
async function upgradeToClockTimes(ev) {
    const { location, allEvents } = state.ctx;
    const keys = location ? timeKeys(ev, allEvents) : [];
    if (!keys.length) return;
    const token = state.token;
    if (!(await loadTimes(location, keys))) return;
    if (token !== state.token || !state.open || state.view !== 'event' || state.event !== ev) return;

    // Rows keep the order timeRows() gives them: replace a row that's already there,
    // otherwise slot the new one in right after the previous.
    let prev = scroll.querySelector('.cal-detail__group > :first-child'); // the Date row
    for (const next of timeRows(ev, allEvents)) {
        const old = scroll.querySelector(`[data-slot="${next.dataset.slot}"]`);
        if (old) old.replaceWith(next); else prev?.after(next);
        prev = next;
        next.classList.add('is-fresh');
        requestAnimationFrame(() => requestAnimationFrame(() => next.classList.remove('is-fresh')));
    }
    if (state.mode === 'popover') placePopover();
}

function eventNodes(ev) {
    const { T, he, allEvents } = state.ctx;
    const f = formats(he);
    const d = ev.detail || {};
    const rows = [];

    // Hebcal's own hdate is English ("1 Tishrei 5787"), which an RTL row would reorder:
    // the Hebrew UI shows the Hebrew-calendar date in Hebrew instead.
    const hdate = he ? (f.hebrew(ev.key) || d.hdate) : (d.hdate || f.hebrew(ev.key));
    rows.push(row(T.date, f.long(ev.key), hdate ? h('small', { text: hdate, dir: he && !f.hebrew(ev.key) ? 'ltr' : null }) : null));

    rows.push(...timeRows(ev, allEvents));
    if (d.leyning) {
        for (const key of ['torah', 'haftarah', 'maftir']) {
            if (d.leyning[key]) rows.push(readingRow(T[key], d.leyning[key]));
        }
    }

    const nodes = [
        h('div', { class: 'cal-detail__head' },
            h('h2', { class: 'cal-detail__title', id: 'calDetailTitle', text: cleanTitle(ev.title) }),
            d.hebrew ? h('p', { class: 'cal-detail__hebrew', lang: 'he', text: d.hebrew }) : null,
            // What the day is comes first, as on Hebcal's own pages: the times below are
            // reference, and never push this out of the first screenful.
            d.memo ? h('p', { class: 'cal-detail__about', text: d.memo }) : null),
        h('dl', { class: 'cal-detail__group' }, rows),
    ];

    const url = safeHebcalUrl(d.link);
    if (url || Object.keys(d).length) {
        nodes.push(h('div', { class: 'cal-detail__foot' },
            url ? h('a', { class: 'cal-detail__link', href: url, target: '_blank', rel: 'noopener noreferrer' }, T.learn, h('span', { 'aria-hidden': 'true', text: '↗' })) : null,
            h('p', { class: 'cal-detail__credit' }, `${T.credit} `,
                h('a', { href: 'https://www.hebcal.com/', target: '_blank', rel: 'noopener noreferrer', text: 'Hebcal.com' }), ' (CC BY 4.0)')));
    }
    return nodes;
}

// Static, trusted SVG markup only (never data from Hebcal).
function icon(svg) {
    const span = h('span', { 'aria-hidden': 'true' });
    span.innerHTML = svg;
    return span;
}

function dayNodes() {
    const { T, he, events, dateKey } = state.ctx;
    const f = formats(he);
    const list = events.length
        ? h('ul', { class: 'cal-detail__group' }, events.map((ev) => h('li', {},
            h('button', { type: 'button', class: 'cal-detail__item', onclick: () => showEvent(ev, { swap: true }) },
                h('span', { class: `cal-detail__dot cal-cat-${ev.category || 'default'}`, 'aria-hidden': 'true' }),
                h('span', { class: 'cal-detail__item-title', text: cleanTitle(ev.title) }),
                icon(CARET_RIGHT)))))
        : h('div', { class: 'cal-detail__group' }, h('p', { class: 'cal-detail__empty', text: T.empty }));
    return [
        h('div', { class: 'cal-detail__head' },
            h('h2', { class: 'cal-detail__title', id: 'calDetailTitle', text: f.short(dateKey) }),
            // The Hebrew-calendar date is in the UI language, so it must not claim `he`.
            f.hebrew(dateKey) ? h('p', { class: 'cal-detail__hebrew', lang: he ? 'he' : 'en', text: f.hebrew(dateKey) }) : null),
        list,
    ];
}

function setChrome({ kind, cat, back }) {
    kindEl.textContent = kind;
    dotEl.className = `cal-detail__dot cal-cat-${cat}`;
    backBtn.hidden = !back;
    if (back) {
        backBtn.replaceChildren();
        backBtn.insertAdjacentHTML('afterbegin', CARET_LEFT);
        backBtn.append(state.ctx.T.back);
        backBtn.setAttribute('aria-label', `${state.ctx.T.back}: ${formats(state.ctx.he).short(state.ctx.dateKey)}`);
    }
}

async function swapContent(build, { swap }) {
    const token = state.token;
    if (swap) {
        scroll.classList.add('is-swapping');
        await sleep(reducedMotion() ? 0 : 150);
        if (token !== state.token || !state.open) return;
    }
    build();
    scroll.scrollTop = 0;
    scroll.classList.remove('is-swapping');
    if (state.open && state.mode === 'popover') placePopover(); // (open() places it after showModal)
}

function showEvent(ev, { swap = false } = {}) {
    state.view = 'event';
    state.event = ev;
    const canGoBack = state.ctx.events.length > 1;
    return swapContent(() => {
        setChrome({ kind: state.ctx.T.kind[kindOf(ev)], cat: ev.category || 'default', back: canGoBack });
        scroll.replaceChildren(...eventNodes(ev));
        dialog.setAttribute('aria-labelledby', 'calDetailTitle');
    }, { swap }).then(() => upgradeToClockTimes(ev));
}

function showDay({ swap = false } = {}) {
    state.view = 'day';
    state.event = null;
    return swapContent(() => {
        setChrome({ kind: state.ctx.T.kind.day, cat: 'default', back: false });
        scroll.replaceChildren(...dayNodes());
        dialog.setAttribute('aria-labelledby', 'calDetailTitle');
    }, { swap });
}

// ── Popover placement ──────────────────────────────────────────────────────

let tip = { x: 0, y: 0 }; // arrow tip in panel coordinates: the point the popover grows from

function placePopover() {
    if (!state.anchor?.isConnected) return;
    const a = state.anchor.getBoundingClientRect();
    const w = panel.offsetWidth; const hgt = panel.offsetHeight;
    const vw = window.innerWidth; const vh = window.innerHeight;
    const fits = {
        right: a.right + GAP + w <= vw - EDGE,
        left: a.left - GAP - w >= EDGE,
        bottom: a.bottom + GAP + hgt <= vh - EDGE,
        top: a.top - GAP - hgt >= EDGE,
    };
    // Calendar puts details beside the event when it can, then below/above.
    const placement = ['right', 'left', 'bottom', 'top'].find((p) => fits[p]) ?? 'bottom';
    const side = placement === 'right' || placement === 'left';

    let left; let top;
    if (side) {
        left = placement === 'right' ? a.right + GAP : a.left - GAP - w;
        top = clamp(a.top + a.height / 2 - hgt / 2, EDGE, Math.max(EDGE, vh - hgt - EDGE));
    } else {
        top = placement === 'bottom' ? a.bottom + GAP : a.top - GAP - hgt;
        left = clamp(a.left + a.width / 2 - w / 2, EDGE, Math.max(EDGE, vw - w - EDGE));
    }
    panel.style.left = `${Math.round(left)}px`;
    panel.style.top = `${Math.round(top)}px`;
    panel.dataset.placement = placement;

    arrow.style.top = ''; arrow.style.left = '';
    if (side) {
        const ay = clamp(a.top + a.height / 2 - top, ARROW_INSET, hgt - ARROW_INSET);
        arrow.style.top = `${Math.round(ay - 7)}px`;
        tip = { x: placement === 'right' ? -ARROW : w + ARROW, y: ay };
    } else {
        const ax = clamp(a.left + a.width / 2 - left, ARROW_INSET, w - ARROW_INSET);
        arrow.style.left = `${Math.round(ax - 7)}px`;
        tip = { x: ax, y: placement === 'bottom' ? -ARROW : hgt + ARROW };
    }
}

// The anchor lives in FullCalendar's grid, which re-flows when the modal or window
// resizes and swaps its nodes on re-render.  While a popover is open, compare the
// anchor's box once per frame so the popover stays attached to its chip through any of
// that, and close it if the chip is gone for good.
let followRaf = 0;

function followAnchor() {
    stopFollowingAnchor();
    let last = '';
    const tick = () => {
        followRaf = 0;
        if (!state.open || state.closing || state.mode !== 'popover') return;
        if (!state.anchor?.isConnected) { close({ immediate: true }); return; }
        const r = state.anchor.getBoundingClientRect();
        const box = `${r.left}|${r.top}|${r.width}|${r.height}|${window.innerWidth}|${window.innerHeight}`;
        if (last && box !== last) placePopover();
        last = box;
        followRaf = requestAnimationFrame(tick);
    };
    followRaf = requestAnimationFrame(tick);
}

function stopFollowingAnchor() {
    cancelAnimationFrame(followRaf);
    followRaf = 0;
}

// ── Motion ─────────────────────────────────────────────────────────────────

const setY =(y) => {
    state.y = y;
    panel.style.transform = y ? `translateY(${y}px)` : '';
};

const stopMotion = () => { state.motion?.stop?.(); state.motion = null; };

function animateIn() {
    // Start from the collapsed pose in the same frame the panel becomes visible.
    if (state.mode === 'sheet') {
        const height = panel.offsetHeight;
        setY(height);
    } else {
        panel.style.transformOrigin = `${tip.x}px ${tip.y}px`;
        panel.style.transform = 'scale(0.5)';
    }
    void panel.offsetWidth; // commit the start pose before opacity/motion begin
    panel.classList.add('is-shown');
    scrim.classList.add('is-shown');

    const M = window.ShelahMotion;
    if (state.mode === 'sheet') {
        state.motion = M?.springValue?.(state.y, 0, spring(0.42, 0), {
            onUpdate: setY,
            onComplete: () => setY(0),
        }) ?? null;
        if (!state.motion) setY(0);
    } else {
        state.motion = M?.springAnimate?.(panel, { transform: ['scale(0.5)', 'scale(1)'] }, spring(0.38, 0.08)) ?? null;
        Promise.resolve(state.motion).then(() => { if (state.open && !state.closing) panel.style.transform = ''; }, () => {});
        if (!state.motion) panel.style.transform = '';
    }
}

function finish() {
    stopMotion();
    state.open = false;
    state.closing = false;
    state.drag = null;
    if (dialog.open) dialog.close();
    panel.classList.remove('is-shown');
    scrim.classList.remove('is-shown');
    scrim.style.opacity = '';
    scrim.style.transition = '';
    panel.style.transform = '';
    panel.style.transformOrigin = '';
    state.y = 0;
    stopFollowingAnchor();
    const back = state.prevFocus;
    state.prevFocus = null;
    if (back && back.isConnected && typeof back.focus === 'function') back.focus({ preventScroll: true });
}

async function close({ immediate = false, velocity = 0 } = {}) {
    if (!state.open || state.closing) return;
    state.closing = true;
    const token = ++state.token;
    stopMotion();
    if (immediate) { finish(); return; }

    scrim.classList.remove('is-shown');
    scrim.style.opacity = '';
    scrim.style.transition = '';
    const M = window.ShelahMotion;

    if (state.mode === 'sheet' && !reducedMotion()) {
        // Leave along the path it arrived on, at the finger's speed if it was thrown.
        const height = panel.offsetHeight;
        await new Promise((resolve) => {
            state.motion = M?.springValue?.(state.y, height, spring(0.34, 0), {
                velocity, onUpdate: setY, onComplete: resolve,
            }) ?? null;
            if (!state.motion) resolve();
            setTimeout(resolve, 700);
        });
    } else {
        // Popover: fold back into the tapped element while it fades.
        panel.classList.remove('is-shown');
        if (state.mode === 'popover') {
            panel.style.transformOrigin = `${tip.x}px ${tip.y}px`;
            state.motion = M?.springAnimate?.(panel, { transform: ['scale(1)', 'scale(0.5)'] }, spring(0.28, 0)) ?? null;
        }
        await sleep(FADE_MS);
    }
    if (token !== state.token) return;
    finish();
}

// ── Sheet drag (Pointer Events on the header, touch events for pull-down in the body) ──

function rubberband(overshoot, dimension, c = 0.55) {
    return (overshoot * dimension * c) / (dimension + c * overshoot);
}

function dragBegin(y) {
    if (state.mode !== 'sheet' || !state.open) return null;
    stopMotion(); // grab it wherever it is; state.y already holds the live position
    scrim.style.transition = 'none';
    state.drag = { y0: y, base: state.y, samples: [{ t: performance.now(), y }], active: false };
    return state.drag;
}

function dragMove(y) {
    const d = state.drag;
    if (!d) return false;
    if (!d.active) {
        if (Math.abs(y - d.y0) < HYSTERESIS) return false;
        d.active = true;
        d.y0 = y; // engage without a jump
    }
    let target = d.base + (y - d.y0);
    if (target < 0) target = -rubberband(-target, 300); // soft ceiling
    setY(target);
    const height = panel.offsetHeight || 1;
    scrim.style.opacity = String(clamp(1 - Math.max(target, 0) / height, 0, 1));
    const now = performance.now();
    d.samples.push({ t: now, y });
    while (d.samples.length > 2 && now - d.samples[0].t > 120) d.samples.shift();
    return true;
}

function dragEnd() {
    const d = state.drag;
    state.drag = null;
    if (!d || !d.active) { scrim.style.transition = ''; return; }
    const first = d.samples[0]; const last = d.samples[d.samples.length - 1];
    const dt = Math.max(last.t - first.t, 1);
    const velocity = ((last.y - first.y) / dt) * 1000; // px/s, + is downward
    const height = panel.offsetHeight || 1;
    // Where the gesture is *going*, not where the finger let go.
    const projected = state.y + (velocity / 1000) * DECELERATION / (1 - DECELERATION);
    if (projected > height * 0.5) {
        close({ velocity });
        return;
    }
    scrim.style.transition = '';
    scrim.style.opacity = '';
    const M = window.ShelahMotion;
    state.motion = M?.springValue?.(state.y, 0, spring(0.4, 0.12), {
        velocity, onUpdate: setY, onComplete: () => setY(0),
    }) ?? null;
    if (!state.motion) setY(0);
}

function attachDrag() {
    gripEl.addEventListener('pointerdown', (e) => {
        if (e.button > 0 || e.target.closest('button')) return;
        if (dragBegin(e.clientY)) gripEl.setPointerCapture(e.pointerId);
    });
    gripEl.addEventListener('pointermove', (e) => { if (state.drag) dragMove(e.clientY); });
    const release = () => { if (state.drag) dragEnd(); };
    gripEl.addEventListener('pointerup', release);
    gripEl.addEventListener('pointercancel', release);

    // A pull-down from the top of the content also dismisses, like iOS sheets; the
    // browser would otherwise claim that gesture as scroll, so it needs touch events.
    scroll.addEventListener('touchstart', (e) => {
        if (state.mode !== 'sheet' || scroll.scrollTop > 0 || e.touches.length !== 1) return;
        dragBegin(e.touches[0].clientY);
    }, { passive: true });
    scroll.addEventListener('touchmove', (e) => {
        const d = state.drag;
        if (!d) return;
        const y = e.touches[0].clientY;
        if (!d.active && (scroll.scrollTop > 0 || y - d.y0 < -HYSTERESIS)) { // scrolling content, not a pull
            state.drag = null;
            scrim.style.transition = '';
            return;
        }
        if (dragMove(y) && e.cancelable) e.preventDefault();
    }, { passive: false });
    scroll.addEventListener('touchend', release);
    scroll.addEventListener('touchcancel', release);
}

// ── Public API ─────────────────────────────────────────────────────────────

/**
 * @param {object} o
 * @param {Element} o.anchor      the tapped chip / day cell / agenda row (the arrow points here)
 * @param {string}  o.dateKey     'YYYY-MM-DD' of the tapped day
 * @param {object[]} o.events     that day's events: { key, title, category, detail }
 * @param {object[]} o.allEvents  every loaded event (used to find a holiday's full span)
 * @param {object}  [o.event]     open straight to this event (an event chip was tapped)
 * @param {boolean} [o.he]        Hebrew UI
 * @param {{lat:number, lon:number, label?:string, timezone?:string}|null} [o.location]
 *        the zmanim location: with it the card shows clock times, without it just "Sundown" etc.
 * @param {(ref: string) => void} [o.openText]  open a reading ("Numbers 29:1-6") in the app's reader;
 *        without it the readings are plain text
 * @param {(book: string) => string} [o.bookName]  a book's name in Hebrew, for the Hebrew UI
 */
function open({ anchor, dateKey, events = [], allEvents = events, event = null, he = false, location = null, openText = null, bookName = null }) {
    if (!bind()) return;
    if (state.open) { state.token += 1; finish(); }
    state.token += 1;

    state.mode = window.matchMedia(SHEET_QUERY).matches ? 'sheet' : 'popover';
    state.anchor = anchor;
    state.prevFocus = document.activeElement;
    state.ctx = { he, T: STR[he ? 'he' : 'en'], events, allEvents, dateKey, location, openText, bookName };
    dialog.dataset.mode = state.mode;
    panel.dir = he ? 'rtl' : 'ltr';
    panel.lang = he ? 'he' : 'en';
    closeBtn.setAttribute('aria-label', state.ctx.T.close);
    panel.style.left = ''; panel.style.top = '';

    const start = event ?? (events.length === 1 ? events[0] : null);
    if (start) showEvent(start); else showDay();

    state.open = true;
    state.closing = false;
    dialog.showModal();
    if (state.mode === 'popover') { placePopover(); followAnchor(); }
    animateIn();
    panel.focus({ preventScroll: true });
}

window.ShelahCalendarDetail = { open, close, isOpen: () => state.open };
