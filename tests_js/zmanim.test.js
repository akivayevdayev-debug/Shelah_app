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
        setTimeout: timer.setTimeout,
        clearTimeout: timer.clearTimeout,
        URLSearchParams,
    });
    return { mod, document, localStorage, window, timer };
}

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
    assert.equal(timer.setTimeout.calls.length, 0, 'no countdown is started for an errored response');
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
