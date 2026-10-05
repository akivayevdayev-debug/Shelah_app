/**
 * static/js/community-labels.js -- Hebrew names for the community-customs
 * data's categories, topics, sources and origin lines.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const labels = require('../static/js/community-labels.js');

test('he() names categories, topics and sources regardless of case and spacing', () => {
    assert.equal(labels.he('shabbat'), 'שבת');
    assert.equal(labels.he('Family Law'), 'דיני משפחה');
    assert.equal(labels.he('  candle   lighting '), 'הדלקת נרות');
    assert.equal(labels.he('Mishnah Berurah'), 'משנה ברורה');
    assert.equal(labels.he('Rema'), 'רמ״א');
    assert.equal(labels.he('something new'), null);
    assert.equal(labels.he(''), null);
    assert.equal(labels.he(null), null);
});

test('he() matches the Ottoman origin line despite its non-breaking hyphen', () => {
    assert.match(
        labels.he('Ottoman Empire Jewish communities, especially Turkish‑based Sephardi settlements after 1492'),
        /^קהילות היהודים באימפריה העות׳מאנית/,
    );
});

test('label() is Hebrew only in the Hebrew UI and falls back to the English', () => {
    assert.equal(labels.label('kashrut', 'he'), 'כשרות');
    assert.equal(labels.label('kashrut', 'en'), 'kashrut');
    assert.equal(labels.label('unknown topic', 'he'), 'unknown topic');
    assert.equal(labels.label(undefined, 'he'), '');
});

test('sourceList() names each comma-separated source, keeping unknown ones', () => {
    assert.equal(
        labels.sourceList('Rema, Mishnah Berurah, Aruch HaShulchan, Chayei Adam', 'he'),
        'רמ״א, משנה ברורה, ערוך השולחן, חיי אדם',
    );
    assert.equal(labels.sourceList('Shulchan Aruch, Some Local Rabbi', 'he'), 'שולחן ערוך, Some Local Rabbi');
    assert.equal(labels.sourceList('Rema, Rif', 'en'), 'Rema, Rif');
    assert.equal(labels.sourceList('', 'he'), '');
});

test('every category in customs/*.json has a Hebrew name', () => {
    // Topic titles are free-form research headings and fall back to the English;
    // the categories are the fixed set the page groups and titles by.
    const dir = path.join(__dirname, '..', 'customs');
    const missing = new Set();
    for (const file of fs.readdirSync(dir)) {
        if (!file.endsWith('.json') || file === 'schema.json' || file === 'customs_db.json') continue;
        const data = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'));
        for (const item of data.halacha_index || []) {
            if (item.category && !labels.he(item.category)) missing.add(item.category);
        }
    }
    assert.deepEqual([...missing], []);
});

test('an unknown topic shows as written in the Hebrew UI', () => {
    assert.equal(labels.label('Hazarat HaShatz', 'he'), 'Hazarat HaShatz');
    assert.equal(labels.sourceList("Magen Avot, 'Half Kaddish'", 'he'), "Magen Avot, 'Half Kaddish'");
});

test('confidence() gives a chip label and hint for entries to weigh, in either language', () => {
    assert.deepEqual(labels.confidence('disputed', 'en'), {
        key: 'disputed',
        label: 'Disputed',
        hint: 'Rabbis and sources differ on this.',
        note: 'Rabbis differ on this. Each rabbi gives their own answer, so ask your own rabbi.',
    });
    assert.equal(labels.confidence('disputed', 'he').label, 'שנוי במחלוקת');
    assert.equal(labels.confidence('regional', 'he').label, 'מקומי');
    assert.equal(labels.confidence('needs-review', 'en').label, 'Needs review');
    assert.equal(labels.confidence('Needs_Review', 'en').key, 'needs-review');
    assert.equal(labels.confidence('needs-review', 'fr').label, 'Needs review');
});

test('every confidence note sends the reader to their own rabbi, in either language', () => {
    for (const key of ['disputed', 'regional', 'needs-review']) {
        assert.match(labels.confidence(key, 'en').note, /your own rabbi/, key);
        assert.match(labels.confidence(key, 'he').note, /שאלו את הרב שלכם/, key);
    }
});

test('confidence() shows no chip for well-attested or unrecognised values', () => {
    for (const value of ['well-attested', '', null, undefined, 'constructor', '__proto__', 'bogus']) {
        assert.equal(labels.confidence(value, 'en'), null, String(value));
    }
});

test('attaches to window when loaded as a classic script', () => {
    const vm = require('node:vm');
    const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'community-labels.js'), 'utf8');
    const window = {};
    vm.runInNewContext(src, { window });
    assert.equal(window.ShelahCommunityLabels.he('purim'), 'פורים');
});
