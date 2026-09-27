/**
 * The esm_harness coverage flush: V8 coverage of a harness-loaded module copy
 * must describe the file on disk (offsets without the injected preamble, URL
 * without the `?esmHarnessCtx=<n>` query), or `node --test
 * --experimental-test-coverage` shifts every hit several lines late and
 * writes one lcov record per copy. See tests_js/helpers/esm_harness.js.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { pathToFileURL } = require('node:url');
const { rebaseCoverageResult } = require('./helpers/esm_harness');

const REPO_ROOT = path.resolve(__dirname, '..');
const STATE_JS = path.join(REPO_ROOT, 'static', 'js', 'state.js');
const STATE_URL = pathToFileURL(STATE_JS).href;

function script(url, ranges) {
    return { scriptId: '1', url, functions: [{ functionName: '', isBlockCoverage: true, ranges }] };
}

test('rebaseCoverageResult strips the ctx query and shifts offsets back by that ctx\'s preamble', () => {
    const result = [
        script(`${STATE_URL}?esmHarnessCtx=3`, [
            { startOffset: 0, endOffset: 150, count: 1 },
            { startOffset: 70, endOffset: 90, count: 0 },
        ]),
    ];

    const [rebased] = rebaseCoverageResult(result, new Map([['3', 50]]));

    assert.equal(rebased.url, STATE_URL);
    assert.deepEqual(rebased.functions[0].ranges, [
        { startOffset: 0, endOffset: 100, count: 1 },
        { startOffset: 20, endOffset: 40, count: 0 },
    ]);
    // The input is left alone: the caller decides what to write.
    assert.equal(result[0].functions[0].ranges[0].endOffset, 150);
});

test('rebaseCoverageResult leaves scripts the harness did not load unchanged', () => {
    const plain = script(STATE_URL, [{ startOffset: 5, endOffset: 9, count: 1 }]);
    const builtin = script('node:internal/foo', [{ startOffset: 5, endOffset: 9, count: 1 }]);

    const rebased = rebaseCoverageResult([plain, builtin], new Map([['0', 50]]));

    assert.equal(rebased[0], plain);
    assert.equal(rebased[1], builtin);
});

test('rebaseCoverageResult refuses a copy whose preamble length it never recorded', () => {
    assert.throws(
        () => rebaseCoverageResult([script(`${STATE_URL}?esmHarnessCtx=9`, [])], new Map()),
        /no preamble length recorded/,
    );
});

test('a process that loads a module twice writes one coverage script per file, on disk offsets', () => {
    const coverageDir = fs.mkdtempSync(path.join(os.tmpdir(), 'esm-harness-cov-'));
    try {
        // Two copies whose preambles differ in length (different globals), so
        // an unshifted or half-shifted range would not line up below.
        const child = `
            const { loadEsmModule } = require(${JSON.stringify(path.join(__dirname, 'helpers', 'esm_harness.js'))});
            (async () => {
                const a = (await loadEsmModule('static/js/state.js', { window: {} })).namespace;
                const b = (await loadEsmModule('static/js/state.js', { window: {}, document: {}, navigator: {} })).namespace;
                a.setState({ x: 1 });
                b.getState();
            })();
        `;
        execFileSync(process.execPath, ['-e', child], {
            env: { ...process.env, NODE_V8_COVERAGE: coverageDir },
            stdio: 'pipe',
        });

        const files = fs.readdirSync(coverageDir).filter((name) => name.endsWith('.json'));
        assert.equal(files.length, 1, 'the flush replaces the exit-time coverage write');
        const { result } = JSON.parse(fs.readFileSync(path.join(coverageDir, files[0]), 'utf8'));

        const copies = result.filter((entry) => entry.url.startsWith(pathToFileURL(STATE_JS).href));
        assert.equal(copies.length, 2);
        const source = fs.readFileSync(STATE_JS, 'utf8');
        for (const copy of copies) {
            assert.equal(copy.url, STATE_URL);
            const topLevel = copy.functions.find((fn) => fn.functionName === '');
            assert.deepEqual(
                [topLevel.ranges[0].startOffset, topLevel.ranges[0].endOffset],
                [0, source.length],
            );
            const setState = copy.functions.find((fn) => fn.functionName === 'setState');
            assert.equal(setState.ranges[0].startOffset, source.indexOf('function setState'));
        }
    } finally {
        fs.rmSync(coverageDir, { recursive: true, force: true });
    }
});
