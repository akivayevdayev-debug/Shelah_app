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

test('every category, topic and source in customs/*.json has a Hebrew name', () => {
    const dir = path.join(__dirname, '..', 'customs');
    const missing = new Set();
    for (const file of fs.readdirSync(dir)) {
        if (!file.endsWith('.json') || file === 'schema.json' || file === 'customs_db.json') continue;
        const data = JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8'));
        for (const item of data.halacha_index || []) {
            for (const text of [item.category, item.topic]) {
                if (text && !labels.he(text)) missing.add(text);
            }
            for (const part of String(item.source || '').split(',')) {
                if (part.trim() && !labels.he(part)) missing.add(part.trim());
            }
        }
    }
    assert.deepEqual([...missing], []);
});

test('attaches to window when loaded as a classic script', () => {
    const vm = require('node:vm');
    const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'community-labels.js'), 'utf8');
    const window = {};
    vm.runInNewContext(src, { window });
    assert.equal(window.ShelahCommunityLabels.he('purim'), 'פורים');
});
