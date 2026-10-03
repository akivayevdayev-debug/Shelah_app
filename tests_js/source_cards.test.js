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

test('externalLinks: Talmud links the daf on AlHaTorah and nothing else', async () => {
    const c = await load();
    const links = c.externalLinks('Berakhot 2a');
    assert.deepEqual(links.map((l) => l.label), ['AlHaTorah']);
    assert.equal(links[0].href, 'https://shas.alhatorah.org/Full/Berakhot/2a');
    // AlHaTorah wants underscores for the spaces of a multi-word name.
    assert.equal(c.externalLinks('Bava Metzia 10b')[0].href, 'https://shas.alhatorah.org/Full/Bava_Metzia/10b');
    assert.equal(c.externalLinks('Rosh Hashanah 16a')[0].href, 'https://shas.alhatorah.org/Full/Rosh_Hashanah/16a');
});

test('alhatorahTractate puts the common spellings in the one the site reads', async () => {
    const c = await load();
    const cases = {
        Berachot: 'Berakhot', Brachos: 'Berakhot', Berakhot: 'Berakhot',
        Shabbos: 'Shabbat', Shabbat: 'Shabbat', 'Baba Kama': 'Bava Kamma', 'Bava Kamma': 'Bava Kamma',
        'Baba Metzia': 'Bava Metzia', 'Avoda Zara': 'Avodah Zarah', Kesubos: 'Ketubot', "Ta'anis": 'Taanit',
        Bechorot: 'Bekhorot', Arachin: 'Arakhin', 'Rosh Hashana': 'Rosh Hashanah', Yoma: 'Yoma',
        'Talmud Bavli Berakhot': 'Berakhot', 'Babylonian Talmud Shabbat': 'Shabbat',
    };
    for (const [spelled, canonical] of Object.entries(cases)) {
        assert.equal(c.alhatorahTractate(spelled), canonical, spelled);
    }
    // A name it does not know is passed on as written, never guessed.
    assert.equal(c.alhatorahTractate('Moonshine'), 'Moonshine');
    assert.equal(c.externalLinks('Brachos 2a')[0].href, 'https://shas.alhatorah.org/Full/Berakhot/2a');
    assert.equal(c.externalLinks('Shabbos 31a')[0].href, 'https://shas.alhatorah.org/Full/Shabbat/31a');
});

test('readerRef puts a daf in the tractate spelling the reader resolves, and leaves everything else alone', async () => {
    const c = await load();
    assert.equal(c.readerRef('Brachos 2a'), 'Berakhot 2a');
    assert.equal(c.readerRef('Talmud Bavli Berakhot 2a'), 'Berakhot 2a');
    assert.equal(c.readerRef('Baba Kama 60b:3'), 'Bava Kamma 60b:3');
    assert.equal(c.readerRef('Shabbat 73b'), 'Shabbat 73b');
    assert.equal(c.readerRef('Talmud Yerushalmi Berakhot 2a'), 'Talmud Yerushalmi Berakhot 2a');
    assert.equal(c.readerRef('Genesis 1:1'), 'Genesis 1:1');
    assert.equal(c.readerRef('Moonshine 3a'), 'Moonshine 3a');
    assert.equal(c.readerRef(''), '');
});

test('externalLinks: the Yerushalmi is not sent to the Bavli', async () => {
    const c = await load();
    assert.deepEqual(c.externalLinks('Talmud Yerushalmi Berakhot 2a'), []);
    assert.deepEqual(c.externalLinks('Jerusalem Talmud Shabbat 1a'), []);
});

test('externalLinks: Tanakh links the verse on AlHaTorah, with no Sefaria link', async () => {
    const c = await load();
    assert.equal(c.externalLinks('Genesis 1:1')[0].href, 'https://mg.alhatorah.org/Full/Genesis/1.1');
    assert.equal(c.externalLinks('Exodus 20:8-11')[0].href, 'https://mg.alhatorah.org/Full/Exodus/20.8-11');
    assert.equal(c.externalLinks('Psalms 23')[0].href, 'https://mg.alhatorah.org/Full/Psalms/23');
    assert.equal(c.externalLinks('I Samuel 1:1')[0].href, 'https://mg.alhatorah.org/Full/I_Samuel/1.1');
    // Numbered books are written 1/2 as often as I/II; the site reads Roman numerals.
    assert.equal(c.externalLinks('1 Samuel 3:4')[0].href, 'https://mg.alhatorah.org/Full/I_Samuel/3.4');
    assert.equal(c.externalLinks('2 Kings 4:1')[0].href, 'https://mg.alhatorah.org/Full/II_Kings/4.1');
    assert.equal(c.externalLinks('II Chronicles 1:1')[0].href, 'https://mg.alhatorah.org/Full/II_Chronicles/1.1');
    assert.equal(c.externalLinks('Song of Songs 1:1')[0].href, 'https://mg.alhatorah.org/Full/Song_of_Songs/1.1');
    for (const ref of ['Genesis 1:1', 'Berakhot 2a', 'Shulchan Aruch, Orach Chayim 261:2']) {
        assert.ok(c.externalLinks(ref).every((l) => !/sefaria/i.test(l.label + l.href)), ref);
    }
});

test('externalLinks: the old alhatorah.org, Dicta and Halachipedia addresses are gone', async () => {
    const c = await load();
    const all = ['Berakhot 2a', 'Genesis 1:1', 'Shulchan Aruch, Orach Chayim 261:2', 'Chatam Sofer, Orach Chaim 1']
        .flatMap((ref) => c.externalLinks(ref));
    for (const link of all) {
        assert.doesNotMatch(link.href, /^https:\/\/alhatorah\.org|dicta\.org|halachipedia/);
    }
});

test('externalLinks: works the reader already holds link nowhere; responsa search HebrewBooks', async () => {
    const c = await load();
    assert.deepEqual(c.externalLinks('Shulchan Aruch, Orach Chayim 261:2'), []);
    assert.deepEqual(c.externalLinks('Mishneh Torah, Sabbath 2:1'), []);
    assert.deepEqual(c.externalLinks('Mishnah Berakhot 1:1'), []);
    const responsa = c.externalLinks('Chatam Sofer, Orach Chaim 1');
    assert.equal(responsa[0].label, 'HebrewBooks');
    assert.match(responsa[0].href, /site%3Ahebrewbooks\.org%20Chatam%20Sofer$/);
    assert.deepEqual(c.externalLinks(''), []);
});

test('externalLinks: a passage ref it cannot parse is not linked to a guessed address', async () => {
    const c = await load();
    assert.deepEqual(c.externalLinks('Talmud Bavli, Berakhot 2a'), []);
    assert.deepEqual(c.externalLinks('Genesis'), []);
});

test('externalLinksHtml escapes and opens in a new tab safely', async () => {
    const c = await load();
    const html = c.externalLinksHtml('Genesis 1:1');
    // The "leaves this page" mark is a Phosphor arrow-up-right, not a text glyph.
    assert.match(html, /target="_blank" rel="noopener noreferrer" class="src-ext-link">AlHaTorah<svg [^>]*class="src-ext-icon"[^>]*aria-hidden="true"[^>]*><path d="[^"]+"\/><\/svg><\/a>/);
    assert.ok(!html.includes('↗'));
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

test('externalLinks carries a Hebrew label for every site, and the Hebrew interface shows it', async () => {
    const c = await load();
    assert.deepEqual(c.externalLinks('Berakhot 2a').map((l) => l.labelHe), ['על התורה']);
    assert.equal(c.externalLinks('Chatam Sofer, Orach Chaim 1')[0].labelHe, 'היברובוקס');
    // Same links, same targets; only the visible name changes with the language.
    const he = c.externalLinksHtml('Genesis 1:1', 'he');
    assert.match(he, />על התורה<svg /);
    assert.doesNotMatch(he, /AlHaTorah/);
    assert.match(c.externalLinksHtml('Genesis 1:1', 'en'), />AlHaTorah<svg /);
    assert.equal(c.externalLinksHtml('Genesis 1:1'), c.externalLinksHtml('Genesis 1:1', 'en'));
    assert.equal(c.externalLinksHtml('Shulchan Aruch, Orach Chayim 1'), '');
});

test('previewHtml decodes entities and drops the section markers Sefaria leaves in its text', async () => {
    const c = await load();
    const html = c.previewHtml([{ he: 'הַשַּׁבָּֽת׃&nbsp;{פ}', en: 'the sabbath day.&nbsp;<span>Fish &amp; chips &#8217;s' }]);
    assert.doesNotMatch(html, /nbsp|\{|&amp;amp;/);
    assert.match(html, /הַשַּׁבָּֽת׃<\/p>/);
    assert.match(html, /Fish &amp; chips \u2019s/);
    // Decoded markup is still escaped on the way out, never rendered.
    assert.equal(c.previewHtml([{ en: '&lt;img src=x onerror=alert(1)&gt;' }]).includes('<img'), false);
});

test('previewHtml leaves out Sefaria footnotes, nested ones included', async () => {
    const c = await load();
    const en = 'A person who kindles a fire is liable,<sup class="footnote-marker">1</sup><i class="footnote">One of the 39 labors, see <i>Shabbat</i> 73b.</i> provided he needs the ash.<sup class="footnote-marker">2</sup><i class="footnote">Second note.</i> End.';
    const html = c.previewHtml([{ en }]);
    assert.match(html, /<p>A person who kindles a fire is liable, provided he needs the ash\. End\.<\/p>/);
    assert.doesNotMatch(html, /labors|Second note|footnote/);
    // A note that is never closed takes the rest of the line, not the whole page.
    assert.match(c.previewHtml([{ en: 'Before.<i class="footnote">never closed' }]), /<p>Before\.<\/p>/);
});

test('previewHtml in Hebrew shows the Hebrew text alone, falling back to English when there is none', async () => {
    const c = await load();
    const lines = [{ he: 'בראשית ברא', en: 'In the beginning' }];
    const he = c.previewHtml(lines, { lang: 'he' });
    assert.match(he, /<div class="ai-src-box-he" dir="rtl"><p>בראשית ברא<\/p><\/div>/);
    assert.doesNotMatch(he, /ai-src-box-en|In the beginning/);
    // English interface keeps both languages.
    assert.match(c.previewHtml(lines, { lang: 'en' }), /ai-src-box-he[\s\S]*ai-src-box-en/);
    // No Hebrew line to show: the English stays so the preview is never empty.
    const fallback = c.previewHtml([{ he: '', en: 'only english' }], { lang: 'he' });
    assert.match(fallback, /<div class="ai-src-box-en"><p>only english<\/p><\/div>/);
});

test('hebrewRefName reads Sefaria\'s heRef and is empty when it is missing', async () => {
    const c = await load();
    assert.equal(c.hebrewRefName({ heRef: '  ברכות ב׳ א ' }), 'ברכות ב׳ א');
    assert.equal(c.hebrewRefName({ heRef: '' }), '');
    assert.equal(c.hebrewRefName({ heTitle: 'ברכות' }), '');
    assert.equal(c.hebrewRefName(null), '');
});
