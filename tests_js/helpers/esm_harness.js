/**
 * Loads a real static/js/*.js ES module (import/export syntax, written for
 * <script type="module"> in the browser) in Node, with caller-supplied
 * browser globals (window/document/fetch/...) bound to the module and to every
 * relative module it imports.
 *
 * Why this exists: static/js/*.js files use native ESM syntax, but the
 * project's package.json is "type": "commonjs" (required so
 * static/js/sentry-init.js — deliberately written with no import/export
 * syntax so it can load as a plain, synchronous, non-module <script> and
 * still be `require()`d directly by tests_js/sentry_init.test.js — keeps
 * working). Adding a directory-wide static/js/package.json with
 * "type": "module" would make sentry-init.js unrequireable (Node parses it
 * as ESM before running it, and it has no `export` statement) and would
 * mean giving it one, which breaks its documented non-module-script
 * requirement. So instead of changing either file's on-disk module system,
 * a `module.registerHooks()` customization (below) makes Node's own ESM
 * loader treat the requested files as ES modules.
 *
 * How (no eval / new Function / vm): the real file is loaded with a native
 * `import()` at its real `file:` URL plus a unique `?esmHarnessCtx=<n>` query,
 * which gives every harness load its own fresh instance of the whole import
 * graph (module-level state such as state.js's store is never shared between
 * tests). The load hook prepends a one-line `const { window, document, ... }
 * = <this load's globals>;` to each tagged module, so the module's bare
 * references resolve to the test's fakes instead of the real Node globals —
 * per module instance, without touching globalThis. The preamble sits on the
 * FIRST line with no newline, so every later line keeps its line number.
 *
 * Coverage (`node --test --experimental-test-coverage`): V8 reports block
 * ranges as character offsets into the source it ran, which includes the
 * preamble, while Node maps those offsets onto lines using the file on disk.
 * Left alone, every range would land `preamble.length` characters late
 * (several lines, e.g. a function declared on line 10 reported on line 14),
 * and each `?esmHarnessCtx=<n>` copy would be its own script, which Node's
 * lcov reporter writes as a separate `SF:` record with record-local branch
 * ids that cannot be merged afterwards. So, when coverage is on, this process
 * flushes its V8 coverage at exit (`rebaseCoverageResult` below): it shifts
 * each copy's offsets back by its preamble length and drops the query, and
 * Node's own offset-exact merge then folds every copy into one script per
 * static/js/*.js file, exactly as it merges a module imported by two test
 * files. This relies on the default per-file process isolation of `node --test`.
 *
 * Fidelity note: only the names in `globals` are shadowed. A module that
 * touches some other browser-only global gets Node's real global of that name
 * (or a ReferenceError if Node has none) — pass every browser global the
 * module needs, as the existing tests do.
 *
 * Requires Node >= 22.15 (module.registerHooks); no experimental flag needed.
 */
'use strict';

const fs = require('node:fs');
const path = require('node:path');
const v8 = require('node:v8');
const { registerHooks } = require('node:module');
const { pathToFileURL } = require('node:url');

const CTX_PARAM = 'esmHarnessCtx';
const REGISTRY_KEY = Symbol.for('shelah.esmHarness.globals');
const IDENTIFIER_RE = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
const REPO_ROOT = path.resolve(__dirname, '..', '..');

// ctx id -> { globals, names }. Also published on globalThis so the preamble
// of a module being evaluated can reach its own load's globals.
const contexts = new Map();
globalThis[REGISTRY_KEY] = { get: (ctx) => contexts.get(ctx).globals };

// ctx id -> length of the preamble prepended to every module of that load.
// Unlike `contexts` it lives until exit: the coverage flush needs it.
const preambleLengths = new Map();

let hooksRegistered = false;
let nextContextId = 0;

/**
 * Map V8 coverage of harness-loaded module copies back onto the files on disk.
 *
 * @param {Array<{url: string, functions: Array<{ranges: Array<{startOffset: number, endOffset: number}>}>}>} result
 *   The `result` array of a NODE_V8_COVERAGE JSON file.
 * @param {Map<string, number>} lengths ctx id -> preamble length.
 * @returns A new array: each `?esmHarnessCtx=<n>` script gets the plain file
 *   URL and its offsets shifted back by that ctx's preamble length (clamped at
 *   0, so the module's own top-level range still starts at the file's first
 *   character). Other scripts are returned unchanged.
 */
function rebaseCoverageResult(result, lengths) {
    return result.map((script) => {
        if (!script.url.startsWith('file:')) return script;
        const url = new URL(script.url);
        const ctx = url.searchParams.get(CTX_PARAM);
        if (ctx === null) return script;
        if (!lengths.has(ctx)) {
            // A copy this process did not load: shifting it by a guess would
            // silently misplace its coverage, so refuse instead.
            throw new Error(`esm_harness: no preamble length recorded for ${script.url}`);
        }
        const shift = lengths.get(ctx);
        url.searchParams.delete(CTX_PARAM);
        return {
            ...script,
            url: url.href,
            functions: script.functions.map((fn) => ({
                ...fn,
                ranges: fn.ranges.map((range) => ({
                    ...range,
                    startOffset: Math.max(0, range.startOffset - shift),
                    endOffset: Math.max(0, range.endOffset - shift),
                })),
            })),
        };
    });
}

/** Process-exit hook: write this process's V8 coverage, rebased. See the header. */
function flushRebasedCoverage() {
    const dir = process.env.NODE_V8_COVERAGE;
    if (!dir || preambleLengths.size === 0) return;

    const prefix = `coverage-${process.pid}-`;
    const before = new Set(fs.readdirSync(dir));
    v8.takeCoverage();
    // Nothing more is recorded or written at exit, so the file rewritten
    // below is this process's only coverage output. Node's own exit-time
    // collection then finds coverage already stopped and logs "Failed to get
    // 'result' from coverage profile response: Precise coverage has not been
    // started" once per test process: expected, harmless, nothing is lost.
    v8.stopCoverage();
    for (const name of fs.readdirSync(dir)) {
        if (before.has(name) || !name.startsWith(prefix) || !name.endsWith('.json')) continue;
        const file = path.join(dir, name);
        const coverage = JSON.parse(fs.readFileSync(file, 'utf8'));
        coverage.result = rebaseCoverageResult(coverage.result, preambleLengths);
        fs.writeFileSync(file, JSON.stringify(coverage));
    }
}

function ensureHooksRegistered() {
    if (hooksRegistered) return;
    registerHooks({
        resolve(specifier, context, nextResolve) {
            const resolved = nextResolve(specifier, context);
            if (!context.parentURL || !resolved.url.startsWith('file:')) return resolved;

            const ctx = new URL(context.parentURL).searchParams.get(CTX_PARAM);
            if (ctx === null) return resolved;

            const child = new URL(resolved.url);
            if (child.pathname.includes('/node_modules/') || child.searchParams.has(CTX_PARAM)) {
                return resolved;
            }
            child.searchParams.set(CTX_PARAM, ctx);
            return { ...resolved, url: child.href };
        },
        load(url, context, nextLoad) {
            const ctx = new URL(url).searchParams.get(CTX_PARAM);
            if (ctx === null) return nextLoad(url, context);

            const loaded = nextLoad(url, { ...context, format: 'module' });
            const { names } = contexts.get(ctx);
            const preamble = `const { ${names.join(', ')} } = globalThis[Symbol.for('shelah.esmHarness.globals')].get(${JSON.stringify(ctx)});`;
            preambleLengths.set(ctx, preamble.length);
            return { ...loaded, format: 'module', source: preamble + String(loaded.source) };
        },
    });
    process.once('exit', flushRebasedCoverage);
    hooksRegistered = true;
}

/**
 * @param {string} entryPath Path to the entry module, relative to the repo root.
 * @param {object} globals Host globals the module (and its relative imports)
 *   may reference bare (e.g. { window, document, fetch }).
 * @returns {Promise<{namespace: object}>} The evaluated entry module's
 *   namespace (its exports).
 */
async function loadEsmModule(entryPath, globals) {
    const merged = { console, ...globals };
    const names = Object.keys(merged);
    for (const name of names) {
        if (!IDENTIFIER_RE.test(name)) {
            throw new Error(`loadEsmModule: "${name}" is not a valid global identifier`);
        }
    }

    const entryAbsPath = path.resolve(REPO_ROOT, entryPath);
    const relative = path.relative(REPO_ROOT, entryAbsPath);
    if (relative.startsWith('..') || path.isAbsolute(relative)) {
        throw new Error(`loadEsmModule: "${entryPath}" is outside the repository`);
    }

    ensureHooksRegistered();

    const ctx = String(nextContextId++);
    contexts.set(ctx, { globals: merged, names });

    const entryUrl = pathToFileURL(entryAbsPath);
    entryUrl.searchParams.set(CTX_PARAM, ctx);

    try {
        const namespace = await import(entryUrl.href);
        return { namespace };
    } finally {
        // The preamble reads its globals once, while the graph evaluates
        // (inside the awaited import above); nothing needs them afterwards.
        contexts.delete(ctx);
    }
}

module.exports = { loadEsmModule, rebaseCoverageResult };
