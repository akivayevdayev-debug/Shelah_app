/**
 * static/js/conversation-ui-helpers.js -- the pure pieces of the
 * conversation UI: size steps, list-row times, the minhag lock line, error
 * notices, mini-widget corner snapping (incl. RTL), citation excerpts, and
 * the transcript's change-detection key.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

async function load() {
    const mod = await loadEsmModule('static/js/conversation-ui-helpers.js', { window: {} });
    return mod.namespace;
}

test('sizes normalize to overlay and expand one step at a time', async () => {
    const h = await load();
    assert.equal(h.normalizeSize('full'), 'full');
    assert.equal(h.normalizeSize('huge'), 'overlay');
    assert.equal(h.normalizeSize(undefined), 'overlay');
    assert.equal(h.expandedSize('mini'), 'overlay');
    assert.equal(h.expandedSize('overlay'), 'full');
    assert.equal(h.expandedSize('full'), 'full');
    assert.equal(h.isModalSize('mini'), false);
    assert.equal(h.isModalSize('overlay'), true);
    assert.equal(h.isModalSize('full'), true);
});

test('relativeTime buckets into now / minutes / hours / yesterday / date', async () => {
    const h = await load();
    const now = Date.parse('2026-09-24T12:00:00Z');
    const ago = (s) => new Date(now - s * 1000).toISOString();
    assert.equal(h.relativeTime(ago(10), { now }), 'now');
    assert.equal(h.relativeTime(ago(5 * 60), { now }), '5m');
    assert.equal(h.relativeTime(ago(3 * 3600), { now }), '3h');
    assert.equal(h.relativeTime(ago(30 * 3600), { now }), 'Yesterday');
    assert.equal(h.relativeTime(ago(30 * 3600), { now, lang: 'he' }), 'אתמול');
    assert.match(h.relativeTime(ago(10 * 86400), { now }), /Sep/);
    assert.equal(h.relativeTime('not a date', { now }), '');
    assert.equal(h.relativeTime(null, { now }), '');
});

test('lock line names the community, localized, and handles All', async () => {
    const h = await load();
    const options = [{ value: 'Sefardic', en: 'Sefardic', he: 'ספרדי' }];
    assert.equal(h.lockLine('Sefardic', 'en', options), 'Answers in this conversation follow Sefardic practice.');
    assert.match(h.lockLine('Sefardic', 'he', options), /ספרדי/);
    assert.match(h.lockLine(null, 'en'), /all communities/);
    assert.match(h.lockLine('ALL', 'en'), /all communities/);
    assert.equal(h.communityName('Syrian', 'he', options), 'Syrian'); // unknown option: raw value
});

test('noticeFor maps store error codes to user-facing notices', async () => {
    const h = await load();
    assert.equal(h.noticeFor(null), null);
    assert.match(h.noticeFor({ code: 'budget_exhausted' }).text, /today's AI limit/);
    assert.match(h.noticeFor({ code: 'ai_paused', retryAfter: 90 }).text, /2 min/);
    assert.match(h.noticeFor({ code: 'ai_paused' }).text, /paused/);
    assert.match(h.noticeFor({ code: 'rate_limited', retryAfter: 12 }).text, /12s/);
    assert.equal(h.noticeFor({ code: 'unauthorized' }).action, 'sign-in');
    assert.equal(h.noticeFor({ code: 'not_found' }).action, 'new');
    assert.equal(h.noticeFor({ code: 'answer_incomplete' }), null);
    assert.match(h.noticeFor({ code: 'server' }).text, /Something went wrong/);
    assert.match(h.noticeFor({ code: 'network' }, 'he').text, /החיבור/);
});

test('snapCorner picks the nearest corner and logicalCorner mirrors for RTL', async () => {
    const h = await load();
    const vp = { width: 1000, height: 800 };
    assert.equal(h.snapCorner({ x: 900, y: 700 }, vp), 'br');
    assert.equal(h.snapCorner({ x: 100, y: 700 }, vp), 'bl');
    assert.equal(h.snapCorner({ x: 900, y: 100 }, vp), 'tr');
    assert.equal(h.snapCorner({ x: 100, y: 100 }, vp), 'tl');
    assert.equal(h.logicalCorner('br', false), 'br');
    assert.equal(h.logicalCorner('br', true), 'bl');
    assert.equal(h.logicalCorner('tl', true), 'tr');
    assert.equal(h.logicalCorner('bogus', false), 'br');
});

test('citationExcerpt prefers the reader language and falls back', async () => {
    const h = await load();
    const c = { excerptEn: 'english', excerptHe: 'עברית' };
    assert.equal(h.citationExcerpt(c, 'en'), 'english');
    assert.equal(h.citationExcerpt(c, 'he'), 'עברית');
    assert.equal(h.citationExcerpt({ excerptEn: '', excerptHe: 'עברית' }, 'en'), 'עברית');
    assert.equal(h.citationExcerpt(null), '');
});

test('turnSignature changes when status, content, or citations change', async () => {
    const h = await load();
    const base = { role: 'assistant', status: 'pending', content: '', citations: [] };
    const done = { ...base, status: 'complete', content: 'answer' };
    const cited = { ...done, citations: [{ ref: 'Berakhot 2a' }] };
    assert.notEqual(h.turnSignature(base), h.turnSignature(done));
    assert.notEqual(h.turnSignature(done), h.turnSignature(cited));
    assert.equal(h.turnSignature(cited), h.turnSignature({ ...cited }));
    assert.equal(h.turnSignature(null), '');
});
