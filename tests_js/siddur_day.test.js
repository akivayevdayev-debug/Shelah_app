/**
 * static/js/siddur-day.js: which Hebrew day it is, Israel/Diaspora, the day
 * client, and the Today card's reminders (Prayers redesign; research brief
 * (a)4). The day answers themselves are the server's
 * (tests/test_siddur_day.py); these fixtures mirror its shape.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

let mod;
async function day() {
    if (!mod) mod = (await loadEsmModule('static/js/siddur-day.js', {})).namespace;
    return mod;
}

const t = (en) => en;
const tHe = (_en, he) => he;
const escapeHtml = (s) => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

function dayFixture(overrides = {}) {
    return {
        date: '2026-10-20', il: false, weekday: 3,
        hebrew: { year: 5787, month: 8, day: 9, monthName: 'Cheshvan', he: 'ט׳ חשון תשפ״ז' },
        occasions: [],
        tachanun: { shacharit: true, mincha: true },
        hallel: { kind: 'none', beracha: false },
        gevurot: { arbit: 'mashiv', shacharit: 'mashiv', musaf: 'mashiv', mincha: 'mashiv' },
        birkatHashanim: 'barchenu',
        yaalehVeyavo: false, alHanissim: false, alHanissimJerusalem: false,
        aseretYemeiTeshuva: false, musaf: false, aneinu: false,
        omer: { today: null, tonight: null },
        ...overrides,
    };
}

const keys = (items) => items.map((i) => i.key);

test('civilDate and addDays use local dates and roll months and years', async () => {
    const d = await day();
    assert.equal(d.civilDate(new Date(2026, 8, 7)), '2026-09-07');
    assert.equal(d.addDays('2026-12-31', 1), '2027-01-01');
    assert.equal(d.addDays('2026-03-01', -1), '2026-02-28');
});

test('hebrewDayFor rolls to tomorrow after a known sunset today', async () => {
    const d = await day();
    const sunset = new Date(2026, 9, 20, 18, 5);
    assert.deepEqual(d.hebrewDayFor(new Date(2026, 9, 20, 17, 0), sunset), { date: '2026-10-20', afterSunset: false });
    assert.deepEqual(d.hebrewDayFor(new Date(2026, 9, 20, 18, 30), sunset), { date: '2026-10-21', afterSunset: true });
});

test('hebrewDayFor ignores a sunset for another day, a bad one, or none', async () => {
    const d = await day();
    const now = new Date(2026, 9, 20, 21, 0);
    assert.deepEqual(d.hebrewDayFor(now, new Date(2026, 9, 19, 18, 5)), { date: '2026-10-20', afterSunset: null });
    assert.deepEqual(d.hebrewDayFor(now, new Date(NaN)), { date: '2026-10-20', afterSunset: null });
    assert.deepEqual(d.hebrewDayFor(now), { date: '2026-10-20', afterSunset: null });
});

test('the manual after-sunset switch wins over the sunset', async () => {
    const d = await day();
    const sunset = new Date(2026, 9, 20, 18, 5);
    assert.deepEqual(d.hebrewDayFor(new Date(2026, 9, 20, 12, 0), sunset, true), { date: '2026-10-21', afterSunset: true });
    assert.deepEqual(d.hebrewDayFor(new Date(2026, 9, 20, 20, 0), sunset, false), { date: '2026-10-20', afterSunset: false });
});

test('guessIsrael from a location', async () => {
    const d = await day();
    assert.equal(d.guessIsrael({ lat: 31.78, lon: 35.22 }), true);   // Jerusalem
    assert.equal(d.guessIsrael({ lat: 40.71, lon: -74.0 }), false);  // New York
    assert.equal(d.guessIsrael(null), false);
    assert.equal(d.guessIsrael({ lat: 'x', lon: 35 }), false);
});

test('the Israel choice round-trips through storage, and storage failures read as unset', async () => {
    const d = await day();
    const store = new Map();
    const storage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) };
    assert.equal(d.readIsrael(storage), null);
    d.writeIsrael(true, storage);
    assert.equal(d.readIsrael(storage), true);
    d.writeIsrael(false, storage);
    assert.equal(d.readIsrael(storage), false);
    const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
    assert.equal(d.readIsrael(broken), null);
    assert.doesNotThrow(() => d.writeIsrael(true, broken));
});

test('the day client asks once per day and Israel flag, and forgets a failure', async () => {
    const d = await day();
    const calls = [];
    let fail = true;
    const fetchImpl = async (url) => {
        calls.push(url);
        if (fail) return { ok: false, status: 503 };
        return { ok: true, json: async () => ({ date: '2026-10-20' }) };
    };
    const client = d.createDayClient(fetchImpl);
    await assert.rejects(client.get('2026-10-20', false), /503/);
    fail = false;
    const first = await client.get('2026-10-20', false);
    await client.get('2026-10-20', false);
    await client.get('2026-10-20', true);
    assert.deepEqual(first, { date: '2026-10-20' });
    assert.deepEqual(calls, [
        '/api/siddur/v2/day?date=2026-10-20&il=0',
        '/api/siddur/v2/day?date=2026-10-20&il=0',
        '/api/siddur/v2/day?date=2026-10-20&il=1',
    ]);
});

test('serviceKind and activeTags', async () => {
    const d = await day();
    assert.equal(d.serviceKind('shacharit-shabbat'), 'shacharit');
    assert.equal(d.serviceKind('musaf-shabbat'), 'musaf');
    assert.equal(d.serviceKind('birkat-hamazon'), null);
    assert.deepEqual([...d.activeTags(dayFixture({ occasions: ['rosh-chodesh'], birkatHashanim: 'barech-alenu' }))], ['rosh-chodesh', 'barech-alenu']);
    assert.deepEqual([...d.activeTags(null)], []);
});

test('an ordinary weekday: the Amidah season, Birkat HaShanim and Tachanun', async () => {
    const d = await day();
    const items = d.reminders(dayFixture(), { t });
    assert.deepEqual(keys(items), ['gevurot', 'birkat-hashanim', 'tachanun']);
    assert.deepEqual(items.map((i) => i.text), ['In the Amidah: Mashiv HaRuach', 'Barechenu (the summer blessing)', 'Tachanun is said']);
});

test('a switch day says when the Amidah season changes', async () => {
    const d = await day();
    const [gevurot] = d.reminders(dayFixture({
        occasions: ['shabbat', 'shemini-atzeret'], tachanun: { shacharit: false, mincha: false, tzidkatcha: false },
        gevurot: { arbit: 'morid', shacharit: 'morid', musaf: 'mashiv', mincha: 'mashiv' },
    }), { t });
    assert.equal(gevurot.key, 'gevurot');
    assert.equal(gevurot.text, 'Morid HaTal until Musaf, then Mashiv HaRuach');
    // For one service, its own form.
    const musaf = d.reminders(dayFixture({ gevurot: { arbit: 'morid', shacharit: 'morid', musaf: 'mashiv', mincha: 'mashiv' } }), { kind: 'musaf', t });
    assert.equal(musaf.find((i) => i.key === 'gevurot').text, 'In the Amidah: Mashiv HaRuach');
});

test('the Ten Days come first, and Hallel shows for Shacharit only', async () => {
    const d = await day();
    const rc = dayFixture({
        occasions: ['rosh-chodesh'], aseretYemeiTeshuva: true, yaalehVeyavo: true, musaf: true,
        hallel: { kind: 'half', beracha: false }, tachanun: { shacharit: false, mincha: false },
    });
    const all = d.reminders(rc, { t });
    assert.deepEqual(keys(all), ['ayt', 'hallel', 'yaaleh', 'gevurot', 'birkat-hashanim', 'tachanun', 'musaf']);
    assert.equal(all[1].text, 'Half Hallel, without a beracha (Sephardic custom)');
    assert.ok(!keys(d.reminders(rc, { kind: 'mincha', t })).includes('hallel'));
    assert.ok(!keys(d.reminders(rc, { kind: 'mincha', t })).includes('musaf'));
    assert.equal(d.reminders(dayFixture({ hallel: { kind: 'full', beracha: true } }), { kind: 'shacharit', t })[0].text, 'Full Hallel, with a beracha');
});

test('Tachanun per service, and split between Shacharit and Mincha', async () => {
    const d = await day();
    const split = dayFixture({ tachanun: { shacharit: true, mincha: false } });
    assert.equal(d.reminders(split, { t }).find((i) => i.key === 'tachanun').text, 'Tachanun at Shacharit, not at Mincha');
    assert.equal(d.reminders(split, { kind: 'mincha', t }).find((i) => i.key === 'tachanun').text, 'No Tachanun today');
    const reverse = dayFixture({ tachanun: { shacharit: false, mincha: true } });
    assert.equal(d.reminders(reverse, { t }).find((i) => i.key === 'tachanun').text, 'No Tachanun at Shacharit; said at Mincha');
    const none = dayFixture({ tachanun: { shacharit: false, mincha: false } });
    assert.equal(d.reminders(none, { t }).find((i) => i.key === 'tachanun').text, 'No Tachanun today');
    assert.ok(!keys(d.reminders(split, { kind: 'arbit', t })).includes('tachanun'));
});

test('Shabbat: Tzidkatcha at Mincha, and no weekday Birkat HaShanim', async () => {
    const d = await day();
    const shabbat = dayFixture({ occasions: ['shabbat'], tachanun: { shacharit: false, mincha: false, tzidkatcha: true }, musaf: true });
    const items = d.reminders(shabbat, { t });
    assert.equal(items.find((i) => i.key === 'tachanun').text, 'Tzidkatcha at Mincha');
    assert.ok(!keys(items).includes('birkat-hashanim'));
    assert.ok(!keys(d.reminders(shabbat, { kind: 'shacharit', t })).includes('tachanun'));
    const noTz = dayFixture({ occasions: ['shabbat'], tachanun: { shacharit: false, mincha: false, tzidkatcha: false } });
    assert.equal(d.reminders(noTz, { kind: 'mincha', t }).find((i) => i.key === 'tachanun').text, 'No Tzidkatcha at Mincha');
});

test('Yom Tov days skip the weekday Amidah reminders; Chol HaMoed keeps them', async () => {
    const d = await day();
    for (const occasions of [['pesach'], ['sukkot'], ['shavuot'], ['rosh-hashana']]) {
        const items = d.reminders(dayFixture({ occasions, tachanun: { shacharit: false, mincha: false } }), { t });
        assert.ok(!keys(items).includes('birkat-hashanim'), occasions.join());
    }
    const cholHamoed = d.reminders(dayFixture({ occasions: ['pesach', 'chol-hamoed-pesach'] }), { t });
    assert.ok(keys(cholHamoed).includes('birkat-hashanim'));
});

test('Al HaNissim, Shushan Purim in Jerusalem, Aneinu, and the winter blessing', async () => {
    const d = await day();
    const items = d.reminders(dayFixture({ alHanissim: true, aneinu: true, birkatHashanim: 'barech-alenu' }), { t });
    assert.deepEqual(keys(items), ['al-hanissim', 'gevurot', 'birkat-hashanim', 'tachanun', 'aneinu']);
    assert.equal(items[2].text, 'Barech Alenu (the winter blessing for rain)');
    const shushan = d.reminders(dayFixture({ alHanissimJerusalem: true }), { t });
    assert.equal(shushan[0].text, 'In Jerusalem: Al HaNissim (Shushan Purim)');
    assert.ok(!keys(d.reminders(dayFixture({ aneinu: true }), { kind: 'musaf', t })).includes('aneinu'));
});

test('the Omer count: tonight before sunset, tonight after it', async () => {
    const d = await day();
    const omer = dayFixture({ omer: { today: 16, tonight: 17 } });
    assert.equal(d.reminders(omer, { t }).at(-1).text, 'Tonight after nightfall: day 17 of the Omer');
    assert.equal(d.reminders(omer, { afterSunset: true, t }).at(-1).text, 'Tonight: day 16 of the Omer');
    assert.ok(!keys(d.reminders(omer, { kind: 'shacharit', t })).includes('omer'));
    assert.deepEqual(d.reminders(null, { t }), []);
});

test('Hebrew text for Hebrew readers', async () => {
    const d = await day();
    assert.deepEqual(d.reminders(dayFixture(), { t: tHe }).map((i) => i.text), ['בעמידה: משיב הרוח', 'ברכנו (נוסח הקיץ)', 'אומרים תחנון']);
    assert.equal(d.hebrewDateText(dayFixture(), true), 'ט׳ חשון תשפ״ז');
    assert.equal(d.hebrewDateText(dayFixture(), false), '9 Cheshvan 5787');
    assert.equal(d.hebrewDateText({}, false), '');
});

test('the Today card: reminders, switches and their pressed state, escaped', async () => {
    const d = await day();
    const html = d.todayCardMarkup(dayFixture(), { kind: 'shacharit', il: true, afterSunset: true, t, escapeHtml });
    assert.match(html, /<section class="siddur-today" aria-labelledby="siddurTodayTitle">/);
    assert.match(html, /<li data-reminder="tachanun">Tachanun is said<\/li>/);
    assert.match(html, /data-siddur-action="toggle-israel" aria-pressed="true">\s*In Israel/);
    assert.match(html, /data-siddur-action="toggle-evening" aria-pressed="true">\s*After sunset/);
    assert.match(html, /siddur-today-note">Days the calendar can't know/);
    const outside = d.todayCardMarkup(dayFixture(), { t, escapeHtml });
    assert.match(outside, /aria-pressed="false">\s*Outside Israel/);
    assert.match(outside, /aria-pressed="false">\s*Before sunset/);
    const before = d.todayCardMarkup(dayFixture(), { afterSunset: false, t, escapeHtml });
    assert.match(before, /aria-pressed="false">\s*Before sunset/);
    // A day with nothing to remind still says so, and text is escaped.
    const quiet = d.todayCardMarkup(dayFixture({ gevurot: null, birkatHashanim: null, tachanun: null }), { t: (en) => `${en} <x>`, escapeHtml });
    assert.match(quiet, /siddur-today-empty">No seasonal changes today. &lt;x&gt;/);
    assert.doesNotMatch(quiet, /siddur-today-note/);
});
