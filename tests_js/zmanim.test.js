/**
 * plan.md §19 Phase 2 — the zman-clock rendering that used to live entirely
 * inline in templates/index.html (initZmanim/fetchZmanimAPI/startCountdown
 * and friends) now lives in static/js/zmanim.js, taking the seven
 * classic-script i18n/formatting globals (t, isHebrewMode,
 * translateHolidayName, formatOmerLabel, formatWeeklyShabbatLabel,
 * translateShabbatWarning, escapeHtml) as an explicit `deps` object instead
 * of reading them off `window` (§19.9 constraint 3).
 *
 * Unlike tests_js/ai_service.test.js's DOM-free module, these functions
 * read and write `document` directly, so this file also exercises
 * tests_js/helpers/fake_dom.js — a hand-built, narrowly-scoped DOM fake
 * (getElementById auto-vivifies elements; querySelectorAll supports only
 * the one attribute-presence selector zmanim.js actually uses).
 *
 * IMPORTANT CAVEAT (§19.9 constraint 6): these are fake-DOM unit tests of
 * the extracted logic (row visibility, label text, countdown math, badge
 * markup). They are NOT a substitute for a real-browser check that the
 * next-zman highlight and countdown badge actually render correctly against
 * the live templates/index.html markup — that still needs manual or
 * browser-automation verification.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');
const {
    addFakeChild,
    createFakeDocument,
    createFakeLocalStorage,
    createRecordingTimer,
} = require('./helpers/fake_dom');

const ZMANIM_PATH = 'static/js/zmanim.js';

function makeJsonResponse(body) {
    return { ok: true, json: async () => body };
}

function makeSequenceFetch(sequence) {
    const calls = [];
    let i = 0;
    const fetchFn = async (url) => {
        calls.push(url);
        const entry = sequence[Math.min(i, sequence.length - 1)];
        i += 1;
        return entry;
    };
    fetchFn.calls = calls;
    return fetchFn;
}

function makeDeps(overrides = {}) {
    const deps = {
        isHebrewMode: () => false,
        translateHolidayName: (name) => name,
        formatOmerLabel: () => 'Omer Day 1',
        formatWeeklyShabbatLabel: () => 'Parashat Test',
        translateShabbatWarning: (warning) => warning,
        escapeHtml: (value) => value,
        ...overrides,
    };
    deps.t = overrides.t || ((en, he) => (deps.isHebrewMode() ? he : en));
    return deps;
}

async function loadZmanim(overrides = {}) {
    const document = overrides.document || createFakeDocument();
    const localStorage = overrides.localStorage || createFakeLocalStorage();
    const window = overrides.window || {};
    const timer = overrides.timer || createRecordingTimer();
    const mod = await loadEsmModule(ZMANIM_PATH, {
        window,
        document,
        localStorage,
        fetch: overrides.fetch,
        ...(overrides.Date ? { Date: overrides.Date } : {}),
        setTimeout: timer.setTimeout,
        clearTimeout: timer.clearTimeout,
        URLSearchParams,
    });
    return { mod, document, localStorage, window, timer };
}

function makeRoutingFetch(routes) {
    const calls = [];
    const fetchFn = async (url) => {
        calls.push(url);
        for (const [matches, response] of routes) {
            if (matches(url)) {
                return typeof response === 'function' ? response(url) : response;
            }
        }
        throw new Error(`makeRoutingFetch: no route matched ${url}`);
    };
    fetchFn.calls = calls;
    return fetchFn;
}

test('prewarmDailyStudy collects refs from arbitrary payload fields via collectRefsFromPayload\'s generic fallback, not just the four named keys', async () => {
    // Top-level strings (hebrew_date) and .title labels are not refs.
    // /api/daily-study's payload shape isn't fixed to {daf_yomi, rambam,
    // parasha, parasha_ref} -- collectRefsFromPayload() also walks every
    // OTHER field generically (addRefsFromValue/addRefsFromArrayItem) so a
    // payload can carry extra study refs under any key. Only the four named
    // keys were previously exercised by any test.
    const dailyStudyPayload = {
        daf_yomi: { ref: 'Berakhot 2a' },
        hebrew_date: '14 Tishrei 5787',
        extra_string_field: 'Extra Ref 1:1',
        extra_object_field: { ref: 'Extra Ref 2:1', title: 'ignored: object branch only reads .ref' },
        extra_array_field: [
            'Extra Ref 3:1',
            { ref: 'Extra Ref 4:1' },
            { title: 'Extra Ref 5:1' },
            42,
        ],
        extra_null_field: null,
    };
    const fetchFn = makeRoutingFetch([
        [(url) => url === '/api/daily-study', makeJsonResponse(dailyStudyPayload)],
        [() => true, makeJsonResponse({})],
    ]);

    const { mod } = await loadZmanim({ fetch: fetchFn });
    const refs = await mod.namespace.prewarmDailyStudy();

    assert.deepEqual(refs, [
        'Berakhot 2a',
        'Extra Ref 2:1',
        'Extra Ref 3:1',
        'Extra Ref 4:1',
    ]);
    const textFetches = fetchFn.calls.filter((url) => String(url).startsWith('/api/text/'));
    assert.ok(!textFetches.some((url) => url.includes('Tishrei')), 'the Hebrew date is never fetched as a text');
});

test('hasRealZman rejects empty/N/A/placeholder values and accepts real times', async () => {
    const { mod } = await loadZmanim();
    assert.equal(mod.namespace.hasRealZman(''), false);
    assert.equal(mod.namespace.hasRealZman('N/A'), false);
    assert.equal(mod.namespace.hasRealZman('--:--'), false);
    assert.equal(mod.namespace.hasRealZman('7:12 PM'), true);
});

test('setZmanRowVisibility toggles the hidden class based on the visible flag', async () => {
    const { mod, document } = await loadZmanim();
    mod.namespace.setZmanRowVisibility('zRowMusaf', false);
    assert.equal(document.getElementById('zRowMusaf').classList.contains('hidden'), true);
    mod.namespace.setZmanRowVisibility('zRowMusaf', true);
    assert.equal(document.getElementById('zRowMusaf').classList.contains('hidden'), false);
});

test('applyOptionalZmanRows hides Musaf/Candles/Havdalah when absent and shows them when present', async () => {
    const { mod, document } = await loadZmanim();
    mod.namespace.applyOptionalZmanRows({
        'Latest Musaf': 'N/A',
        'Candle Lighting': '7:12 PM',
        'Havdalah': '--:--',
    });
    assert.equal(document.getElementById('zRowMusaf').classList.contains('hidden'), true);
    assert.equal(document.getElementById('zRowCandles').classList.contains('hidden'), false);
    assert.equal(document.getElementById('zRowHavdalah').classList.contains('hidden'), true);
});

test('formatCountdownDuration renders English "Xh Ym"/"Ym Zs" and Hebrew equivalents', async () => {
    const { mod } = await loadZmanim();
    const englishDeps = makeDeps({ isHebrewMode: () => false });
    const hebrewDeps = makeDeps({ isHebrewMode: () => true });

    assert.equal(mod.namespace.formatCountdownDuration(3661 * 1000, englishDeps), '1h 1m');
    assert.equal(mod.namespace.formatCountdownDuration(65 * 1000, englishDeps), '1m 5s');
    assert.equal(mod.namespace.formatCountdownDuration(3661 * 1000, hebrewDeps), '1 ש׳ 1 ד׳');
    assert.equal(mod.namespace.formatCountdownDuration(65 * 1000, hebrewDeps), '1 ד׳ 5 ש׳');
});

test('formatZmanClockDisplay only localizes the AM/PM meridiem in Hebrew mode', async () => {
    const { mod } = await loadZmanim();
    const englishDeps = makeDeps({ isHebrewMode: () => false });
    const hebrewDeps = makeDeps({ isHebrewMode: () => true });

    assert.equal(mod.namespace.formatZmanClockDisplay('7:05 PM', englishDeps), '7:05 PM');
    assert.equal(mod.namespace.formatZmanClockDisplay('7:05 PM', hebrewDeps), '7:05 אחה"צ');
    assert.equal(mod.namespace.formatZmanClockDisplay('N/A', hebrewDeps), 'N/A');
    assert.equal(mod.namespace.formatZmanClockDisplay('', hebrewDeps), 'N/A');
});

test('initZmanim with a valid cached location fetches zmanim for that lat/lon', async () => {
    const localStorage = createFakeLocalStorage();
    localStorage.setItem("Sh'elahLastLocation", JSON.stringify({ lat: 40.1, lon: -73.9 }));
    const fetchFn = makeSequenceFetch([makeJsonResponse({ zmanim: {}, metadata: {} })]);
    const { mod } = await loadZmanim({ localStorage, fetch: fetchFn });

    await mod.namespace.initZmanim(makeDeps());

    assert.equal(fetchFn.calls.length, 1);
    const url = new URL(fetchFn.calls[0], 'http://example.test');
    assert.equal(url.pathname, '/api/zmanim');
    assert.equal(url.searchParams.get('lat'), '40.1');
    assert.equal(url.searchParams.get('lon'), '-73.9');
});

test('initZmanim with malformed cached location JSON falls back to no location', async () => {
    const localStorage = createFakeLocalStorage();
    localStorage.setItem("Sh'elahLastLocation", 'not-json{');
    const fetchFn = makeSequenceFetch([makeJsonResponse({ zmanim: {}, metadata: {} })]);
    const { mod } = await loadZmanim({ localStorage, fetch: fetchFn });

    await mod.namespace.initZmanim(makeDeps());

    assert.equal(fetchFn.calls.length, 1);
    assert.equal(fetchFn.calls[0], '/api/zmanim');
});

test('initZmanim uses a saved location label as a fallback when the API reply has none', async () => {
    const localStorage = createFakeLocalStorage();
    localStorage.setItem('ShelahZmanimLocationLabel', 'Brooklyn, NY');
    const fetchFn = makeSequenceFetch([makeJsonResponse({ zmanim: {}, metadata: {} })]);
    const { mod, document } = await loadZmanim({ localStorage, fetch: fetchFn });

    await mod.namespace.initZmanim(makeDeps());

    assert.equal(document.getElementById('zmanLoc').innerText, 'Brooklyn, NY');
});

test('fetchZmanimAPI success renders zman fields, the location label, and starts the countdown', async () => {
    const nowMs = Date.now();
    const data = {
        zmanim: {
            'Sunrise': '6:00 AM',
            'Sunset': '7:45 PM',
        },
        metadata: {
            location_label: 'Test City',
            timezone: 'America/New_York',
            zmanim_iso: {
                Sunrise: new Date(nowMs - 3600 * 1000).toISOString(),
                Sunset: new Date(nowMs + 3600 * 1000).toISOString(),
            },
        },
    };
    const fetchFn = makeSequenceFetch([makeJsonResponse(data)]);
    const { mod, document, timer } = await loadZmanim({ fetch: fetchFn });

    await mod.namespace.fetchZmanimAPI(null, makeDeps());

    assert.equal(document.getElementById('zmanLoc').innerText, 'Test City');
    assert.equal(document.getElementById('zSunrise').innerText, '6:00 AM');
    assert.equal(document.getElementById('zSunset').innerText, '7:45 PM');
    assert.equal(timer.setTimeout.calls.length, 1, 'startCountdown schedules its next tick');
    assert.equal(timer.setTimeout.calls[0].delay, 1000);
});

test('fetchZmanimAPI does not render when the API reports an error', async () => {
    const fetchFn = makeSequenceFetch([makeJsonResponse({ error: 'no data' })]);
    const { mod, document, timer } = await loadZmanim({ fetch: fetchFn });

    await mod.namespace.fetchZmanimAPI(null, makeDeps());

    assert.equal(document.getElementById('zmanLoc').innerText, '', 'label is untouched when the fetch reports an error');
    assert.equal(document.getElementById('zmanimWarning').dataset.zmanimLoadError, '1', 'the error banner is shown instead of a silent no-op');
    assert.equal(timer.setTimeout.calls.length, 1, 'a retry is scheduled after an errored response, not a countdown tick');
    assert.equal(timer.setTimeout.calls[0].delay, 3000, 'the first retry uses the shortest configured backoff');
});

test('startCountdown highlights the soonest upcoming zman row and fills the badge text', async () => {
    const nowMs = Date.now();
    const data = {
        zmanim: {
            'Sunrise': '6:00 AM',
            'Sunset': '7:45 PM',
        },
        metadata: {
            zmanim_iso: {
                Sunrise: new Date(nowMs - 3600 * 1000).toISOString(),
                Sunset: new Date(nowMs + 3600 * 1000).toISOString(),
            },
        },
    };
    const fetchFn = makeSequenceFetch([makeJsonResponse(data)]);
    const { mod, document } = await loadZmanim({ fetch: fetchFn });
    addFakeChild(document.getElementById('zRowSunset'), 'span', { textContent: 'Sunset' });

    await mod.namespace.fetchZmanimAPI(null, makeDeps());

    assert.equal(document.getElementById('zRowSunset').classList.contains('zman-next-highlight'), true);
    assert.equal(document.getElementById('zRowSunrise').classList.contains('zman-next-highlight'), false);
    const badge = document.getElementById('nextZmanBadge');
    assert.equal(badge.classList.contains('hidden'), false);
    assert.match(badge.innerHTML, /Sunset/);
});

// A Date whose zero-argument constructor is pinned to `instant`, so the
// legacy "h:mm AM/PM" fallback (payloads without metadata.zmanim_iso, which
// builds today's date from the clock string) is testable at any wall-clock time.
function pinnedDate(instant) {
    return class PinnedDate extends Date {
        constructor(...args) {
            if (args.length === 0) super(instant.getTime());
            else super(...args);
        }

        static now() {
            return instant.getTime();
        }
    };
}

test('startCountdown converts legacy 12-hour clock strings correctly (no ISO timestamps)', async () => {
    const pinned = pinnedDate(new Date(2026, 2, 4, 10, 0, 0));
    const cases = [
        // [clock string, expected badge countdown, or null when nothing is upcoming]
        ['3:30 PM', 'in 5h 30m'],
        ['11:15 AM', 'in 1h 15m'],
        ['12:00 PM', 'in 2h 0m'],
        ['07:45 PM', 'in 9h 45m'],
        ['12:30 AM', null],
        ['9:59 AM', null],
        ['soon', null],
    ];

    for (const [clock, expected] of cases) {
        const fetchFn = makeSequenceFetch([
            makeJsonResponse({ zmanim: { Sunset: clock }, metadata: {} }),
        ]);
        const { mod, document } = await loadZmanim({ fetch: fetchFn, Date: pinned });
        addFakeChild(document.getElementById('zRowSunset'), 'span', { textContent: 'Sunset' });

        await mod.namespace.fetchZmanimAPI(null, makeDeps());

        const badge = document.getElementById('nextZmanBadge');
        if (expected === null) {
            assert.equal(badge.classList.contains('hidden'), true, `${clock} is not upcoming`);
        } else {
            assert.equal(badge.classList.contains('hidden'), false, `${clock} is upcoming`);
            assert.match(badge.innerHTML, new RegExp(`Sunset <span[^>]*>${expected}</span>`), clock);
        }
    }
});

test('startCountdown hides the next-zman badge when nothing is upcoming', async () => {
    const nowMs = Date.now();
    const data = {
        zmanim: { 'Sunrise': '6:00 AM' },
        metadata: {
            zmanim_iso: { Sunrise: new Date(nowMs - 3600 * 1000).toISOString() },
        },
    };
    const fetchFn = makeSequenceFetch([makeJsonResponse(data)]);
    const { mod, document, timer } = await loadZmanim({ fetch: fetchFn });
    document.getElementById('nextZmanBadge').classList.remove('hidden');
    document.getElementById('nextZmanBadge').textContent = 'stale';

    await mod.namespace.fetchZmanimAPI(null, makeDeps());

    const badge = document.getElementById('nextZmanBadge');
    assert.equal(badge.classList.contains('hidden'), true);
    assert.equal(badge.textContent, '');
    assert.equal(timer.setTimeout.calls.length, 1, 'still reschedules to check again later');
});

test('refreshZmanimDisplay combines the GRA/BHT Shema rows when times match, splits them when they differ', async () => {
    const dataSame = {
        zmanim: {
            'Latest Shema (GRA)': '9:00 AM',
            'Latest Shema (Baal HaTanya)': '9:00 AM',
        },
        metadata: {},
    };
    const fetchFnSame = makeSequenceFetch([makeJsonResponse(dataSame)]);
    const { mod: modSame, document: docSame } = await loadZmanim({ fetch: fetchFnSame });
    await modSame.namespace.fetchZmanimAPI(null, makeDeps());
    assert.equal(docSame.getElementById('shemaBhtRow').classList.contains('hidden'), true);
    assert.equal(docSame.getElementById('shemaGraLabel').innerText, 'Latest Shema (GRA / Baal HaTanya)');

    const dataDiff = {
        zmanim: {
            'Latest Shema (GRA)': '9:00 AM',
            'Latest Shema (Baal HaTanya)': '9:20 AM',
        },
        metadata: {},
    };
    const fetchFnDiff = makeSequenceFetch([makeJsonResponse(dataDiff)]);
    const { mod: modDiff, document: docDiff } = await loadZmanim({ fetch: fetchFnDiff });
    await modDiff.namespace.fetchZmanimAPI(null, makeDeps());
    assert.equal(docDiff.getElementById('shemaBhtRow').classList.contains('hidden'), false);
    assert.equal(docDiff.getElementById('shemaGraLabel').innerText, 'Latest Shema (GRA)');
});

// ---------------------------------------------------------------------------
// refreshZmanimDisplay's per-section renderers (holiday/Shabbat names, omer,
// warning, GRA/BHT rows). Each test drives the public entry point
// (fetchZmanimAPI -> refreshZmanimDisplay) and inspects the fake DOM.
// ---------------------------------------------------------------------------

async function renderSequence(payloads, depsOverrides = {}) {
    const fetchFn = makeSequenceFetch(payloads.map(makeJsonResponse));
    const { mod, document } = await loadZmanim({ fetch: fetchFn });
    const deps = makeDeps(depsOverrides);
    const renderNext = () => mod.namespace.fetchZmanimAPI(null, deps);
    return { document, renderNext };
}

test('refreshZmanimDisplay combines the GRA/BHT Shacharit rows only when both times match', async () => {
    const { document, renderNext } = await renderSequence([
        { zmanim: { 'Latest Shacharit (GRA)': '10:00 AM', 'Latest Shacharit (Baal HaTanya)': '10:00 AM' }, metadata: {} },
        { zmanim: { 'Latest Shacharit (GRA)': '10:00 AM', 'Latest Shacharit (Baal HaTanya)': '10:30 AM' }, metadata: {} },
        { zmanim: { 'Latest Shacharit (GRA)': 'N/A', 'Latest Shacharit (Baal HaTanya)': 'N/A' }, metadata: {} },
    ]);

    await renderNext();
    assert.equal(document.getElementById('shacharitBhtRow').classList.contains('hidden'), true);
    assert.equal(document.getElementById('shacharitGraLabel').innerText, 'Latest Shacharit (GRA / Baal HaTanya)');

    await renderNext();
    assert.equal(document.getElementById('shacharitBhtRow').classList.contains('hidden'), false);
    assert.equal(document.getElementById('shacharitGraLabel').innerText, 'Latest Shacharit (GRA)');

    // Two "N/A" values are equal but not a real time: never combine them.
    await renderNext();
    assert.equal(document.getElementById('shacharitBhtRow').classList.contains('hidden'), false);
    assert.equal(document.getElementById('shacharitGraLabel').innerText, 'Latest Shacharit (GRA)');
});

test('refreshZmanimDisplay uses the Hebrew GRA/BHT labels in Hebrew mode', async () => {
    const { document, renderNext } = await renderSequence([
        {
            zmanim: {
                'Latest Shema (GRA)': '9:00 AM', 'Latest Shema (Baal HaTanya)': '9:00 AM',
                'Latest Shacharit (GRA)': '10:00 AM', 'Latest Shacharit (Baal HaTanya)': '10:20 AM',
            },
            metadata: {},
        },
    ], { isHebrewMode: () => true });

    await renderNext();

    assert.equal(document.getElementById('shemaGraLabel').innerText, 'סוף זמן שמע (גר״א / בעל התניא)');
    assert.equal(document.getElementById('shacharitGraLabel').innerText, 'סוף זמן תפילת שחרית (גר״א)');
});

test('refreshZmanimDisplay styles the holiday name as RTL Hebrew only in Hebrew mode for a real holiday', async () => {
    const holiday = { zmanim: {}, metadata: { holiday: 'Rosh Hashana' } };
    const regular = { zmanim: {}, metadata: {} };

    const english = await renderSequence([holiday]);
    await english.renderNext();
    const englishEl = english.document.getElementById('holidayName');
    assert.equal(englishEl.innerText, 'Rosh Hashana');
    assert.equal(englishEl.classList.contains('font-hebrew'), false);
    assert.equal(englishEl.hasAttribute('dir'), false);

    const hebrew = await renderSequence([holiday, regular], { isHebrewMode: () => true });
    await hebrew.renderNext();
    const hebrewEl = hebrew.document.getElementById('holidayName');
    assert.equal(hebrewEl.classList.contains('font-hebrew'), true);
    assert.equal(hebrewEl.getAttribute('dir'), 'rtl');

    // A later render for an ordinary day must clear the styling again.
    await hebrew.renderNext();
    assert.equal(hebrewEl.innerText, 'Regular Day');
    assert.equal(hebrewEl.classList.contains('font-hebrew'), false);
    assert.equal(hebrewEl.hasAttribute('dir'), false);
});

test('refreshZmanimDisplay styles the Shabbat week label as RTL only when it contains שבת in Hebrew mode', async () => {
    const payload = { zmanim: {}, metadata: {} };

    const withShabbat = await renderSequence([payload], {
        isHebrewMode: () => true,
        formatWeeklyShabbatLabel: () => 'פרשת שבת נחמו',
    });
    await withShabbat.renderNext();
    const shabbatEl = withShabbat.document.getElementById('shabbatWeekName');
    assert.equal(shabbatEl.innerText, 'פרשת שבת נחמו');
    assert.equal(shabbatEl.classList.contains('font-hebrew'), true);
    assert.equal(shabbatEl.getAttribute('dir'), 'rtl');

    const withoutShabbat = await renderSequence([payload], {
        isHebrewMode: () => true,
        formatWeeklyShabbatLabel: () => 'פרשת נחמו',
    });
    await withoutShabbat.renderNext();
    assert.equal(withoutShabbat.document.getElementById('shabbatWeekName').classList.contains('font-hebrew'), false);

    const english = await renderSequence([payload], { formatWeeklyShabbatLabel: () => 'Shabbat Nachamu' });
    await english.renderNext();
    assert.equal(english.document.getElementById('shabbatWeekName').hasAttribute('dir'), false);
});

test('refreshZmanimDisplay shows the omer row on an omer day and the hint otherwise', async () => {
    const { document, renderNext } = await renderSequence([
        { zmanim: {}, metadata: { omer_day: 12 } },
        { zmanim: {}, metadata: {} },
        { zmanim: {}, metadata: { omer_day: 13 } },
    ], { formatOmerLabel: (meta) => `Omer ${meta.omer_day}` });

    await renderNext();
    assert.equal(document.getElementById('omerCount').innerText, 'Omer 12');
    assert.equal(document.getElementById('omerRow').classList.contains('hidden'), false);
    assert.equal(document.getElementById('omerHint').classList.contains('hidden'), true);

    await renderNext();
    assert.equal(document.getElementById('omerRow').classList.contains('hidden'), true);
    assert.equal(document.getElementById('omerHint').classList.contains('hidden'), false);

    await renderNext();
    assert.equal(document.getElementById('omerRow').classList.contains('hidden'), false);
    assert.equal(document.getElementById('omerHint').classList.contains('hidden'), true);
});

test('refreshZmanimDisplay shows the translated Shabbat warning only while the API reports one', async () => {
    const { document, renderNext } = await renderSequence([
        { zmanim: {}, metadata: {} },
        { zmanim: {}, metadata: { shabbat_warning: 'Shabbat begins soon' } },
        { zmanim: {}, metadata: {} },
    ], { translateShabbatWarning: (text) => `T:${text}` });
    const warning = () => document.getElementById('zmanimWarning');

    await renderNext();
    assert.equal(warning().classList.contains('hidden'), true);

    await renderNext();
    assert.equal(warning().innerText, 'T:Shabbat begins soon');
    assert.equal(warning().classList.contains('hidden'), false);

    await renderNext();
    assert.equal(warning().classList.contains('hidden'), true);
});

test('timezoneLabel names a zone in the reader\'s language instead of its IANA ID', async () => {
    const { mod } = await loadZmanim();
    const { timezoneLabel } = mod.namespace;
    assert.equal(timezoneLabel('Asia/Jerusalem', 'en'), 'Israel Time');
    assert.equal(timezoneLabel('Asia/Jerusalem', 'he'), 'שעון ישראל');
    assert.equal(timezoneLabel('Not/A_Real_Zone', 'en'), 'A Real Zone');
    assert.equal(timezoneLabel('', 'en'), '');
});

test('refreshZmanimDisplay hands the location\'s Hebrew date to the header, and skips an empty one', async () => {
    const seen = [];
    const { renderNext } = await renderSequence([
        { zmanim: {}, metadata: { hebrew_date: '15 Tishrei 5787', timezone: 'Asia/Jerusalem' } },
        { zmanim: {}, metadata: { timezone: 'Asia/Jerusalem' } },
    ], { setHebrewDate: (value) => seen.push(value) });
    await renderNext();
    await renderNext();
    assert.deepEqual(seen, ['15 Tishrei 5787']);
});

test('after the timed retries run out, zmanim re-fetch once the network is back', async () => {
    const error = makeJsonResponse({ error: 'no data' });
    const ok = makeJsonResponse({ zmanim: {}, metadata: { location_label: 'Back Again' } });
    const fetchFn = makeSequenceFetch([error, error, error, ok]);
    const listeners = new Map();
    const on = (type, fn) => listeners.set(type, fn);
    const off = (type, fn) => { if (listeners.get(type) === fn) listeners.delete(type); };
    const window = { addEventListener: on, removeEventListener: off };
    const document = createFakeDocument();
    document.addEventListener = on;
    document.removeEventListener = off;
    const { mod, timer } = await loadZmanim({ fetch: fetchFn, window, document });
    const flush = () => new Promise((resolve) => setImmediate(resolve));

    await mod.namespace.fetchZmanimAPI(null, makeDeps());
    for (let i = 0; i < 2; i += 1) {
        timer.setTimeout.calls.shift().fn();
        await flush();
    }
    assert.equal(fetchFn.calls.length, 3, 'the initial fetch plus two timed retries');
    assert.equal(timer.setTimeout.calls.length, 0, 'no third timed retry');
    assert.equal(typeof listeners.get('online'), 'function');
    assert.equal(typeof listeners.get('visibilitychange'), 'function');

    listeners.get('online')();
    await flush();
    assert.equal(fetchFn.calls.length, 4);
    assert.equal(document.getElementById('zmanLoc').innerText, 'Back Again');
    assert.equal(document.getElementById('zmanimWarning').dataset.zmanimLoadError, undefined);
    assert.equal(listeners.has('online'), false, 'the listeners are one-shot');
    assert.equal(listeners.has('visibilitychange'), false);
});
