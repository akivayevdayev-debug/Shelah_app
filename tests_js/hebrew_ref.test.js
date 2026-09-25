/**
 * static/js/hebrew-ref.js -- Hebrew numerals and the verse-accurate weekly
 * portion for a Chumash reference (boundaries from Sefaria's Parasha alt
 * structure).
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const ref = require('../static/js/hebrew-ref.js');

test('hebrewNumeral writes gematria with geresh / gershayim', () => {
    assert.equal(ref.hebrewNumeral(1), 'א׳');
    assert.equal(ref.hebrewNumeral(5), 'ה׳');
    assert.equal(ref.hebrewNumeral(10), 'י׳');
    assert.equal(ref.hebrewNumeral(11), 'י״א');
    assert.equal(ref.hebrewNumeral(15), 'ט״ו');
    assert.equal(ref.hebrewNumeral(16), 'ט״ז');
    assert.equal(ref.hebrewNumeral(21), 'כ״א');
    assert.equal(ref.hebrewNumeral(89), 'פ״ט');
    assert.equal(ref.hebrewNumeral(100), 'ק׳');
    assert.equal(ref.hebrewNumeral(115), 'קט״ו');
    assert.equal(ref.hebrewNumeral(150), 'ק״נ');
    assert.equal(ref.hebrewNumeral(176), 'קע״ו');
    assert.equal(ref.hebrewNumeral(345), 'שמ״ה');
    assert.equal(ref.hebrewNumeral(500), 'ת״ק');
    assert.equal(ref.hebrewNumeral(900), 'תת״ק');
});

test('hebrewNumeral drops thousands for years and passes junk through', () => {
    assert.equal(ref.hebrewNumeral(5787), 'תשפ״ז');
    assert.equal(ref.hebrewNumeral(5786), 'תשפ״ו');
    assert.equal(ref.hebrewNumeral(12, { punctuate: false }), 'יב');
    assert.equal(ref.hebrewNumeral(0), '0');
    assert.equal(ref.hebrewNumeral(-3), '-3');
    assert.equal(ref.hebrewNumeral(2.5), '2.5');
    assert.equal(ref.hebrewNumeral('abc'), 'abc');
    assert.equal(ref.hebrewNumeral(null), '');
    assert.equal(ref.hebrewNumeral(1000), '1000');
});

test('parseTorahRef reads space, dot and range forms, and rejects other books', () => {
    assert.deepEqual(ref.parseTorahRef('Numbers 5'), { book: 'Numbers', chapter: 5, verse: null });
    assert.deepEqual(ref.parseTorahRef('Numbers 4:21'), { book: 'Numbers', chapter: 4, verse: 21 });
    assert.deepEqual(ref.parseTorahRef('Numbers.4.21'), { book: 'Numbers', chapter: 4, verse: 21 });
    assert.deepEqual(ref.parseTorahRef('numbers 4:21-7:89'), { book: 'Numbers', chapter: 4, verse: 21 });
    assert.equal(ref.parseTorahRef('Isaiah 40'), null);
    assert.equal(ref.parseTorahRef('Berakhot 2a'), null);
    assert.equal(ref.parseTorahRef(''), null);
});

test('the table has all 54 portions in reading order', () => {
    assert.equal(ref.PARASHOT.length, 54);
    const books = ['Genesis', 'Exodus', 'Leviticus', 'Numbers', 'Deuteronomy'];
    for (let i = 1; i < ref.PARASHOT.length; i += 1) {
        const a = ref.PARASHOT[i - 1];
        const b = ref.PARASHOT[i];
        const order = books.indexOf(b.book) - books.indexOf(a.book);
        assert.ok(order > 0 || (order === 0 && (b.chapter > a.chapter || (b.chapter === a.chapter && b.verse > a.verse))),
            `${a.name} -> ${b.name} out of order`);
    }
});

test('every boundary: a portion\'s first verse is its own, the verse before is the previous one', () => {
    for (let i = 1; i < ref.PARASHOT.length; i += 1) {
        const prev = ref.PARASHOT[i - 1];
        const cur = ref.PARASHOT[i];
        assert.equal(ref.parashaAt(cur.book, cur.chapter, cur.verse), cur, `${cur.name} start`);
        if (prev.book !== cur.book) continue;
        const before = cur.verse > 1
            ? ref.parashaAt(cur.book, cur.chapter, cur.verse - 1)
            : ref.parashaAt(cur.book, cur.chapter - 1, 200);
        assert.equal(before, prev, `verse before ${cur.name} belongs to ${prev.name}`);
    }
});

test('named boundaries the old chapter-only table got wrong', () => {
    assert.equal(ref.parashaAt('Numbers', 4, 20).name, 'Bamidbar');
    assert.equal(ref.parashaAt('Numbers', 4, 21).name, 'Naso');
    assert.equal(ref.parashaAt('Genesis', 6, 8).name, 'Bereshit');
    assert.equal(ref.parashaAt('Genesis', 6, 9).name, 'Noach');
    assert.equal(ref.parashaAt('Genesis', 44, 17).name, 'Miketz');
    assert.equal(ref.parashaAt('Genesis', 44, 18).name, 'Vayigash');
    assert.equal(ref.parashaAt('Exodus', 6, 1).name, 'Shemot');
    assert.equal(ref.parashaAt('Leviticus', 26, 2).name, 'Behar');
    assert.equal(ref.parashaAt('Leviticus', 26, 3).name, 'Bechukotai');
    assert.equal(ref.parashaAt('Numbers', 22, 1).name, 'Chukat');
    assert.equal(ref.parashaAt('Numbers', 30, 1).name, 'Pinchas');
    assert.equal(ref.parashaAt('Deuteronomy', 3, 22).name, 'Devarim');
    assert.equal(ref.parashaAt('Deuteronomy', 34, 12).name, 'Vezot Haberakhah');
    assert.equal(ref.parashaAt('Isaiah', 1, 1), null);
});

test('parashotInChapter lists every portion that touches the chapter', () => {
    assert.deepEqual(ref.parashotInChapter('Numbers', 4).map((e) => e.name), ['Bamidbar', 'Naso']);
    assert.deepEqual(ref.parashotInChapter('Numbers', 5).map((e) => e.name), ['Naso']);
    assert.deepEqual(ref.parashotInChapter('Genesis', 1).map((e) => e.name), ['Bereshit']);
    assert.deepEqual(ref.parashotInChapter('Leviticus', 26).map((e) => e.name), ['Behar', 'Bechukotai']);
});

test('parashaLabel names the portion in English and Hebrew', () => {
    assert.equal(ref.parashaLabel('Numbers 5'), 'Parashat Naso');
    assert.equal(ref.parashaLabel('Numbers 5', 'he'), 'פרשת נשא');
    assert.equal(ref.parashaLabel('Numbers 4:20', 'he'), 'פרשת במדבר');
    assert.equal(ref.parashaLabel('Numbers 4'), 'Parashot Bamidbar–Naso');
    assert.equal(ref.parashaLabel('Numbers 4', 'he'), 'פרשות במדבר–נשא');
    assert.equal(ref.parashotForRef('Genesis 1:1')[0].name, 'Bereshit');
    assert.equal(ref.parashaLabel('Psalms 23'), '');
    assert.deepEqual(ref.parashotForRef('Psalms 23'), []);
});

test('parashaStartLabel and parashotByBook feed the chapter grid', () => {
    const naso = ref.parashaAt('Numbers', 5, 1);
    assert.equal(ref.parashaStartLabel(naso), '4:21');
    assert.equal(ref.parashaStartLabel(naso, 'he'), 'ד׳:כ״א');
    assert.equal(ref.parashaStartLabel(null), '');
    const byBook = ref.parashotByBook();
    assert.deepEqual(Object.keys(byBook), ['Genesis', 'Exodus', 'Leviticus', 'Numbers', 'Deuteronomy']);
    assert.equal(byBook.Numbers.length, 10);
    assert.deepEqual(byBook.Numbers[1], { chapter: 4, verse: 21, name: 'Naso' });
});

test('attaches to window when loaded as a classic script', () => {
    const fs = require('node:fs');
    const path = require('node:path');
    const vm = require('node:vm');
    const src = fs.readFileSync(path.join(__dirname, '..', 'static', 'js', 'hebrew-ref.js'), 'utf8');
    const window = {};
    vm.runInNewContext(src, { window });
    assert.equal(window.ShelahHebrewRef.hebrewNumeral(5), 'ה׳');
});
