/**
 * static/js/icons.js -- the Phosphor glyphs for JS-built markup. They must
 * stay decorative (aria-hidden), carry both weights, and fail closed (an
 * empty string, never a broken <svg>) for a name the set doesn't have.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

async function load() {
    const mod = await loadEsmModule('static/js/icons.js', {});
    return mod.namespace;
}

test('icon renders a decorative Phosphor svg with the requested size and weight', async () => {
    const { icon } = await load();
    const bold = icon('caret-right', { size: 12, className: 'x' });
    assert.match(bold, /^<svg [^>]*viewBox="0 0 256 256" width="12" height="12" fill="currentColor" class="x" aria-hidden="true"/);
    assert.ok(bold.includes('M184.49,136.49'));
    const regular = icon('caret-right', { weight: 'regular' });
    assert.ok(regular.includes('M181.66,133.66'));
    assert.notEqual(bold, regular);
});

test('icon returns nothing for an unknown glyph or weight instead of a broken svg', async () => {
    const { icon } = await load();
    assert.equal(icon('not-an-icon'), '');
    assert.equal(icon('check', { weight: 'thin' }), '');
});

test('every glyph ships both weights', async () => {
    const { icon, iconNames } = await load();
    assert.ok(iconNames.length > 0);
    for (const name of iconNames) {
        assert.ok(icon(name, { weight: 'bold' }).includes('<path'), `${name} bold`);
        assert.ok(icon(name, { weight: 'regular' }).includes('<path'), `${name} regular`);
    }
});
