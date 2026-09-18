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
 * Because the files execute at their real URLs, `node --test
 * --experimental-test-coverage` attributes their coverage to static/js/*.js.
 *
 * Fidelity note: only the names in `globals` are shadowed. A module that
 * touches some other browser-only global gets Node's real global of that name
 * (or a ReferenceError if Node has none) — pass every browser global the
 * module needs, as the existing tests do.
 *
 * Requires Node >= 22.15 (module.registerHooks); no experimental flag needed.
 */
'use strict';

const path = require('node:path');
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

let hooksRegistered = false;
let nextContextId = 0;

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
            return { ...loaded, format: 'module', source: preamble + String(loaded.source) };
        },
    });
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

module.exports = { loadEsmModule };
