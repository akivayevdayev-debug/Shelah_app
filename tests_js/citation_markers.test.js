/**
 * static/js/citation-markers.js -- the numbered source markers in an AI answer:
 * parsing "[1][2]" groups, turning them into chips in rendered HTML (never
 * inside links or code), and dropping the answer's own trailing Sources list,
 * which the sources area under the answer replaces.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

async function load() {
    const mod = await loadEsmModule('static/js/citation-markers.js', { window: {} });
    return mod.namespace;
}

const CITES = [{ ref: 'Genesis 1:1' }, { ref: 'Shabbat 73b' }, { ref: 'Mishneh Torah, Sabbath 12' }];

test('markerNumbers reads lists and ranges, ascending and without repeats', async () => {
    const c = await load();
    assert.deepEqual(c.markerNumbers('[1]'), [1]);
    assert.deepEqual(c.markerNumbers('[3][1]'), [1, 3]);
    assert.deepEqual(c.markerNumbers('[1, 2]'), [1, 2]);
    assert.deepEqual(c.markerNumbers('[1,3]'), [1, 3]);
    assert.deepEqual(c.markerNumbers('[1-3]'), [1, 2, 3]);
    assert.deepEqual(c.markerNumbers('[1–3]'), [1, 2, 3]);
    assert.deepEqual(c.markerNumbers('[2][2]'), [2]);
    // A backwards or absurd range is the number it starts with, not a flood of chips.
    assert.deepEqual(c.markerNumbers('[3-1]'), [3]);
    assert.deepEqual(c.markerNumbers('[1-40]'), [1]);
});

test('placeMarkers marks a run of sentences on one source once, at its last sentence', async () => {
    const c = await load();
    assert.equal(c.placeMarkers('A.[1] B.[1] C.[1]'), 'A. B. C.[1]');
    assert.equal(c.placeMarkers('A.[1] B.[1] C.[1][2] D.[2]'), 'A. B. C.[1] D.[2]');
    assert.equal(
        c.placeMarkers('Kindling is forbidden.[1] Cooking too.[1][2] And more.[1]'),
        'Kindling is forbidden. Cooking too.[2] And more.[1]',
    );
});

test('placeMarkers keeps each marker where its excerpt ends, not at the paragraph end', async () => {
    const c = await load();
    assert.equal(
        c.placeMarkers('Kindling is forbidden.[1] Cooking is too.[2] Plain.'),
        'Kindling is forbidden.[1] Cooking is too.[2] Plain.',
    );
    assert.equal(c.placeMarkers('Some say X[1] while others say Y.[2]'), 'Some say X[1] while others say Y.[2]');
});

test('placeMarkers treats each paragraph and list item as its own segment', async () => {
    const c = await load();
    assert.equal(
        c.placeMarkers('One.[1] Again.[1]\n\nTwo.[2]\n- item.[3] item.[3]\n1. step.[3][1]'),
        'One. Again.[1]\n\nTwo.[2]\n- item. item.[3]\n1. step.[1][3]',
    );
    assert.equal(c.placeMarkers('A.[1]\n\nB.[1]'), 'A.[1]\n\nB.[1]');
});

test('placeMarkers lists the sources ascending and expands lists and ranges', async () => {
    const c = await load();
    assert.equal(c.placeMarkers('A.[3] B.[1, 2] C.[2]'), 'A.[3] B.[1] C.[2]');
    assert.equal(c.placeMarkers('A.[1-3]'), 'A.[1][2][3]');
    assert.equal(c.placeMarkers('A.[3][1]'), 'A.[1][3]');
});

test('placeMarkers does not read an abbreviation as the end of a sentence', async () => {
    const c = await load();
    assert.equal(c.placeMarkers('Dr. Smith said so.[1] Next.[1]'), 'Dr. Smith said so. Next.[1]');
    assert.equal(c.placeMarkers('R. Yochanan taught it.[1] Then more.[1]'), 'R. Yochanan taught it. Then more.[1]');
});

test('placeMarkers is idempotent and leaves marker-free text and non-markers alone', async () => {
    const c = await load();
    const once = c.placeMarkers('A.[2] B.[1]  [2]\n\nC.[3] D.[3]');
    assert.equal(once, 'A. B.[1][2]\n\nC. D.[3]');
    assert.equal(c.placeMarkers(once), once);
    assert.equal(c.placeMarkers('No markers here.'), 'No markers here.');
    assert.equal(c.placeMarkers(''), '');
    assert.equal(c.placeMarkers(null), '');
    assert.equal(c.placeMarkers('See [2a] and [the Rema].[1] Yes.[1]'), 'See [2a] and [the Rema]. Yes.[1]');
});

test('placeMarkers leaves a code fence alone', async () => {
    const c = await load();
    assert.equal(c.placeMarkers('```\nx[1] y[1]\n```\nz[1] w[1]'), '```\nx[1] y[1]\n```\nz w[1]');
});

test('placed markers render as one chip per source for a run of sentences', async () => {
    const c = await load();
    const html = c.injectMarkers(
        '<p>' + c.placeMarkers('Kindling is forbidden.[1] Cooking too.[1][2]') + '</p>',
        { citations: CITES, idPrefix: 'conv-cite-m1' },
    );
    assert.equal((html.match(/data-mark="1"/g) || []).length, 1);
    assert.equal((html.match(/data-mark="2"/g) || []).length, 1);
    assert.equal((html.match(/class="conv-marks"/g) || []).length, 1);
});

test('splitMarkers separates text from marker groups and takes the spaces before a group', async () => {
    const c = await load();
    assert.deepEqual(c.splitMarkers('Forbidden.[1][2] Next [3].'), [
        { text: 'Forbidden.' },
        { numbers: [1, 2] },
        { text: ' Next' },
        { numbers: [3] },
        { text: '.' },
    ]);
    assert.deepEqual(c.splitMarkers('no markers'), [{ text: 'no markers' }]);
    assert.deepEqual(c.splitMarkers(''), []);
});

test('splitMarkers leaves brackets that are not source numbers alone', async () => {
    const c = await load();
    for (const text of ['see [2a] and [the Rema]', 'a [] b', 'a [ ] b', 'a [123] b', 'range [x-1]']) {
        assert.deepEqual(c.splitMarkers(text), [{ text }], text);
    }
});

test('injectMarkers turns markers into one chip group per run, each pointing at its list row', async () => {
    const c = await load();
    const html = c.injectMarkers('<p>Kindling is forbidden.[1][3] Cooking too.[2]</p>', { citations: CITES, idPrefix: 'conv-cite-m1' });
    assert.equal((html.match(/class="conv-marks"/g) || []).length, 2);
    assert.match(html, /<button type="button" class="conv-mark" data-mark="1" data-mark-target="conv-cite-m1-1"[^>]*aria-label="Source 1: Genesis 1:1">1<\/button>/);
    assert.match(html, /data-mark="3" data-mark-target="conv-cite-m1-3"/);
    assert.match(html, /data-mark="2" data-mark-target="conv-cite-m1-2"/);
    // The chip sits against the sentence: no space left before the group.
    assert.match(html, /forbidden\.<span class="conv-marks">/);
    assert.doesNotMatch(html, /\[\d\]/);
});

test('injectMarkers: a chip is a labelled popover button for assistive tech', async () => {
    const c = await load();
    const html = c.injectMarkers('<p>x[1]</p>', {
        citations: CITES,
        label: (n, cite) => `מקור ${n}: ${cite.ref}`,
    });
    assert.match(html, /aria-haspopup="dialog" aria-expanded="false" aria-label="מקור 1: Genesis 1:1"/);
});

test('injectMarkers never touches links, code or buttons', async () => {
    const c = await load();
    const input = '<p><a href="/x">see [1]</a> <code>a[1]</code> <button>b[1]</button></p><pre>c[1]</pre><p>real[1]</p>';
    const html = c.injectMarkers(input, { citations: CITES });
    assert.match(html, /<a href="\/x">see \[1\]<\/a>/);
    assert.match(html, /<code>a\[1\]<\/code>/);
    assert.match(html, /<button>b\[1\]<\/button>/);
    assert.match(html, /<pre>c\[1\]<\/pre>/);
    assert.equal((html.match(/class="conv-mark"/g) || []).length, 1);
    assert.match(html, /real<span class="conv-marks">/);
});

test('injectMarkers keeps attribute values and entities intact', async () => {
    const c = await load();
    const input = '<p title="a>[1]b" data-x=\'[2]\'>Tom &amp; Jerry[1]</p><img alt="[1]" src="x">';
    const html = c.injectMarkers(input, { citations: CITES });
    assert.ok(html.includes('title="a>[1]b"'));
    assert.ok(html.includes("data-x='[2]'"));
    assert.ok(html.includes('alt="[1]"'));
    assert.match(html, /Tom &amp; Jerry<span class="conv-marks">/);
    assert.equal((html.match(/class="conv-mark"/g) || []).length, 1);
});

test('injectMarkers drops a marker with no source behind it instead of a dead chip', async () => {
    const c = await load();
    assert.equal(c.injectMarkers('<p>Ok.[9]</p>', { citations: CITES }), '<p>Ok.</p>');
    assert.equal(c.injectMarkers('<p>Ok.[1]</p>', { citations: [] }), '<p>Ok.</p>');
    // A source row with no ref cannot be opened or shown, so it gets no chip.
    assert.equal(c.injectMarkers('<p>Ok.[1]</p>', { citations: [{ ref: '' }] }), '<p>Ok.</p>');
    // [9] dropped but [1] kept in the same run.
    const mixed = c.injectMarkers('<p>Ok.[1][9]</p>', { citations: CITES });
    assert.equal((mixed.match(/class="conv-mark"/g) || []).length, 1);
});

test('injectMarkers escapes anything that comes from a ref', async () => {
    const c = await load();
    const html = c.injectMarkers('<p>x[1]</p>', {
        citations: [{ ref: '"><img src=x onerror=alert(1)>' }],
        idPrefix: 'a"b',
    });
    assert.doesNotMatch(html, /<img/);
    assert.match(html, /data-mark-target="a&quot;b-1"/);
});

test('injectMarkers returns html with no brackets untouched', async () => {
    const c = await load();
    const html = '<p>Nothing to mark.</p>';
    assert.equal(c.injectMarkers(html, { citations: CITES }), html);
    assert.equal(c.injectMarkers('', { citations: CITES }), '');
});

test('stripSourcesBlock removes the trailing label and its list, in English and Hebrew', async () => {
    const c = await load();
    const en = '## Direct Answer\n\nForbidden.[1]\n\n**Sources**\n\n- Shabbat 73b — labors\n- Genesis 2:2 — rest\n';
    assert.equal(c.stripSourcesBlock(en), '## Direct Answer\n\nForbidden.[1]');
    const he = '## תשובה ישירה\n\nאסור.[1]\n\n**מקורות**\n\n- שבת עג ב — מלאכות\n';
    assert.equal(c.stripSourcesBlock(he), '## תשובה ישירה\n\nאסור.[1]');
    assert.equal(c.stripSourcesBlock('Short.\n\n## Sources\n\n1. A 1\n2) B 2'), 'Short.');
});

test('stripSourcesBlock leaves an answer alone unless the list is last', async () => {
    const c = await load();
    const noBlock = '## Direct Answer\n\nForbidden.';
    assert.equal(c.stripSourcesBlock(noBlock), noBlock);
    const trailing = '**Sources**\n\n- A 1\n\nMore prose after the list.';
    assert.equal(c.stripSourcesBlock(trailing), trailing);
    const word = 'The Sources are many.';
    assert.equal(c.stripSourcesBlock(word), word);
    assert.equal(c.stripSourcesBlock(''), '');
    assert.equal(c.stripSourcesBlock(null), '');
});

test('citeIdPrefix makes any message id safe to use in an id attribute', async () => {
    const c = await load();
    assert.equal(c.citeIdPrefix('3f2a-91'), 'conv-cite-3f2a-91');
    assert.equal(c.citeIdPrefix('local:7 x'), 'conv-cite-local7x');
    assert.equal(c.citeIdPrefix(''), 'conv-cite-answer');
    assert.equal(c.citeIdPrefix(null), 'conv-cite-answer');
});
