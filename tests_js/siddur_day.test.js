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

// A slice of the real toc: the pages the day's additions live on.
function tocFixture() {
    const title = (en, he = en) => ({ en, he });
    const sections = (...slugs) => slugs.map((slug) => ({ slug, title: title(slug.replace(/-/g, ' ')) }));
    const service = (slug, ...secs) => ({ slug, title: title(slug), sections: sections(...(secs.length ? secs : [slug])) });
    return {
        rite: { slug: 'edot-hamizrach' },
        occasions: [
            { slug: 'shabbat', services: [service('musaf-shabbat', 'amida', 'alenu', 'incense-offering')] },
            {
                slug: 'festivals',
                services: [
                    service('rosh-chodesh', 'rosh-hodesh', 'hallel', 'mussaf'),
                    service('shalosh-regalim', 'song-for-passover', 'song-for-sukkot', 'amidah', 'mussaf'),
                    service('sefirat-haomer'),
                    service('chanukah', 'menorah-lighting', 'shacharit'),
                    service('purim', 'megillah-reading', 'purim-day'),
                    service('taaniyot', 'tenth-of-tevet', 'fast-of-gedalya', 'mourning', 'torah-reading-for-fast-days'),
                ],
            },
        ],
    };
}

test('linkFor sends a reminder to the page that holds it, and to nothing the toc lacks', async () => {
    const d = await day();
    const toc = tocFixture();
    const rc = dayFixture({ occasions: ['rosh-chodesh'] });
    assert.deepEqual(d.linkFor('hallel', rc, toc), {
        service: 'rosh-chodesh', section: 'hallel', value: 'edot-hamizrach/rosh-chodesh/hallel',
        href: '/siddur/edot-hamizrach/rosh-chodesh/hallel', title: { en: 'hallel', he: 'hallel' },
    });
    assert.equal(d.linkFor('yaaleh', rc, toc).value, 'edot-hamizrach/rosh-chodesh/rosh-hodesh');
    assert.equal(d.linkFor('musaf', rc, toc).value, 'edot-hamizrach/rosh-chodesh/mussaf');
    const pesach = dayFixture({ occasions: ['pesach', 'chol-hamoed-pesach'] });
    assert.equal(d.linkFor('yaaleh', pesach, toc).value, 'edot-hamizrach/shalosh-regalim/amidah');
    assert.equal(d.linkFor('musaf', dayFixture({ occasions: ['shabbat'] }), toc).value, 'edot-hamizrach/musaf-shabbat');
    // A one-section service has no section pages of its own.
    assert.equal(d.linkFor('omer', dayFixture(), toc).value, 'edot-hamizrach/sefirat-haomer');
    // Rosh Hashana has no siddur page here, nor does a key with no target.
    assert.equal(d.linkFor('yaaleh', dayFixture({ occasions: ['rosh-hashana'] }), toc), null);
    assert.equal(d.linkFor('tachanun', rc, toc), null);
    assert.equal(d.linkFor('hallel', rc, null), null);
    assert.equal(d.linkFor('hallel', null, toc), null);
    assert.equal(d.linkFor('hallel', rc, { rite: { slug: 'edot-hamizrach' }, occasions: [] }), null);
});

test('a fast day links its Selichot and Torah reading; Tisha BAv links the mourning page', async () => {
    const d = await day();
    const toc = tocFixture();
    const tevet = dayFixture({ fastDay: 'tenth-of-tevet', aneinu: true });
    assert.equal(d.linkFor('aneinu', tevet, toc).value, 'edot-hamizrach/taaniyot/tenth-of-tevet');
    assert.deepEqual(d.extraPages(tevet, toc).map((p) => p.value), [
        'edot-hamizrach/taaniyot/tenth-of-tevet',
        'edot-hamizrach/taaniyot/torah-reading-for-fast-days',
    ]);
    const av = dayFixture({ fastDay: 'tisha-beav', aneinu: true });
    assert.equal(d.linkFor('aneinu', av, toc).value, 'edot-hamizrach/taaniyot/mourning');
    assert.deepEqual(d.extraPages(av, toc).map((p) => p.value), ['edot-hamizrach/taaniyot/mourning']);
    // A fast the rite has no Selichot for still shows the reading.
    assert.deepEqual(d.extraPages(dayFixture({ fastDay: 'fast-of-esther' }), toc).map((p) => p.value), [
        'edot-hamizrach/taaniyot/torah-reading-for-fast-days',
    ]);
});

test('holidays add their own pages: menorah, Megillah, the festival song', async () => {
    const d = await day();
    const toc = tocFixture();
    assert.deepEqual(d.extraPages(dayFixture({ occasions: ['chanukah'] }), toc).map((p) => p.value), ['edot-hamizrach/chanukah/menorah-lighting']);
    assert.deepEqual(d.extraPages(dayFixture({ occasions: ['purim'] }), toc).map((p) => p.value), [
        'edot-hamizrach/purim/megillah-reading', 'edot-hamizrach/purim/purim-day',
    ]);
    assert.deepEqual(d.extraPages(dayFixture({ occasions: ['pesach', 'chol-hamoed-pesach'] }), toc).map((p) => p.value), ['edot-hamizrach/shalosh-regalim/song-for-passover']);
    // The toc has no song for Shavuot here, so nothing is offered.
    assert.deepEqual(d.extraPages(dayFixture({ occasions: ['shavuot'] }), toc), []);
    assert.deepEqual(d.extraPages(dayFixture(), toc), []);
    assert.deepEqual(d.extraPages(null, toc), []);
    assert.deepEqual(d.extraPages(dayFixture({ occasions: ['purim'] }), null), []);
});

test('the Today card links reminders and lists the holiday\'s other pages once', async () => {
    const d = await day();
    const toc = tocFixture();
    const tevet = dayFixture({ fastDay: 'tenth-of-tevet', aneinu: true, tachanun: { shacharit: true, mincha: true } });
    const html = d.todayCardMarkup(tevet, { t, escapeHtml, toc });
    // The reminder links Selichot; the extras row skips that page and keeps the reading.
    assert.match(html, /<li data-reminder="aneinu">Fast day: Aneinu <a class="siddur-today-link" href="\/siddur\/edot-hamizrach\/taaniyot\/tenth-of-tevet" data-siddur-path="edot-hamizrach\/taaniyot\/tenth-of-tevet" aria-label="Open: tenth of tevet">Open<\/a><\/li>/);
    assert.match(html, /Also in the siddur today<\/span><a class="siddur-today-link" href="[^"]*torah-reading-for-fast-days"[^>]*>torah reading for fast days<\/a><\/p>/);
    assert.equal((html.match(/tenth-of-tevet/g) || []).length, 2);
    // Without a toc the card is the plain reminders.
    const plain = d.todayCardMarkup(tevet, { t, escapeHtml });
    assert.doesNotMatch(plain, /siddur-today-link|Also in the siddur/);
    // One service's card (Shacharit) never adds the holiday row.
    assert.doesNotMatch(d.todayCardMarkup(dayFixture({ occasions: ['chanukah'] }), { kind: 'shacharit', t, escapeHtml, toc }), /Also in the siddur/);
    // Hebrew readers get Hebrew labels.
    const he = d.todayCardMarkup(dayFixture({ occasions: ['chanukah'] }), { t: tHe, escapeHtml, toc, isHebrew: true });
    assert.match(he, /עוד בסידור היום/);
});
