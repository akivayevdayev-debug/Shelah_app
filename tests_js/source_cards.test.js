/**
 * static/js/source-cards.js -- the source-card string builders shared by the
 * single-answer modal and the conversation panel: classification (with
 * transliteration variants), badges in both languages, the external links per
 * kind of source, and the bilingual preview.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

async function load() {
    const mod = await loadEsmModule('static/js/source-cards.js', { window: {} });
    return mod.namespace;
}

test('sourceKind classifies each corpus, tolerating kh/ch spellings', async () => {
    const c = await load();
    assert.equal(c.sourceKind('Genesis 1:1'), 'tanakh');
    assert.equal(c.sourceKind('Berachot 2a'), 'talmud');
    assert.equal(c.sourceKind('Shulchan Arukh, Orach Chayim 261:2'), 'halacha');
    assert.equal(c.sourceKind('Igrot Moshe, Orach Chaim 4:40'), 'responsa');
    assert.equal(c.sourceKind('Mishnah Berakhot 1:1'), 'mishnah');
    assert.equal(c.sourceKind('Some Unknown Work 3'), 'other');
    assert.equal(c.sourceKind(''), 'other');
    assert.equal(c.normalizeRefForMatch('  Arukh '), 'Aruch');
});

test('sourceBadgeHtml localizes the label and keeps the kind class', async () => {
    const c = await load();
    assert.equal(c.sourceBadgeHtml('Genesis 1:1'), '<span class="ai-src-badge ai-src-badge--tanakh">Tanakh</span>');
    assert.match(c.sourceBadgeHtml('Genesis 1:1', 'he'), /תנ״ך/);
    assert.match(c.sourceBadgeHtml('Foo', 'he'), /ai-src-badge--other">מקור</);
});

test('externalLinks: Talmud gets Dicta + AlHaTorah + Sefaria', async () => {
    const c = await load();
    const labels = c.externalLinks('Berachot 2a').map((l) => l.label);
    assert.deepEqual(labels, ['Dicta', 'AlHaTorah', 'Sefaria']);
    assert.match(c.externalLinks('Berachot 2a')[0].href, /dicta\.org\.il\/talmudsearch\?search=Berachot%202a/);
});

test('externalLinks: Tanakh, practical halacha, responsa, empty', async () => {
    const c = await load();
    assert.deepEqual(c.externalLinks('Genesis 1:1').map((l) => l.label), ['AlHaTorah', 'Sefaria']);
    const sa = c.externalLinks('Shulchan Aruch, Orach Chayim 261:2');
    assert.deepEqual(sa.map((l) => l.label), ['Sefaria', 'Halachipedia']);
    // Halachipedia searches the work, not the siman number.
    assert.match(sa[1].href, /search=Shulchan%20Aruch%2C%20Orach%20Chayim$/);
    const responsa = c.externalLinks('Chatam Sofer, Orach Chaim 1');
    assert.equal(responsa[0].label, 'HebrewBooks');
    assert.match(responsa[0].href, /site%3Ahebrewbooks\.org%20Chatam%20Sofer$/);
    assert.deepEqual(c.externalLinks(''), []);
});

test('externalLinksHtml escapes and opens in a new tab safely', async () => {
    const c = await load();
    const html = c.externalLinksHtml('Genesis 1:1');
    assert.match(html, /target="_blank" rel="noopener noreferrer" class="src-ext-link">AlHaTorah ↗<\/a>/);
    assert.equal(c.escapeHtml(`<a href="x">'&`), '&lt;a href=&quot;x&quot;&gt;&#39;&amp;');
});

test('previewHtml renders up to three bilingual lines, stripped and escaped', async () => {
    const c = await load();
    const lines = [
        { he: '<b>בראשית</b>', en: 'In the <i>beginning</i>' },
        { he: 'ב', en: 'two' },
        { he: 'ג', en: 'three' },
        { he: 'ד', en: 'four' },
    ];
    const html = c.previewHtml(lines);
    assert.match(html, /^<div class="ai-src-box-body"><div class="ai-src-box-he" dir="rtl"><p>בראשית<\/p>/);
    assert.match(html, /<p>In the beginning<\/p>/);
    assert.doesNotMatch(html, /four/);
    assert.equal(c.previewHtml([{ he: '', en: '' }]), '');
    assert.equal(c.previewHtml(null), '');
    assert.match(c.previewHtml(['plain & english']), /<div class="ai-src-box-en"><p>plain &amp; english<\/p><\/div>/);
});
