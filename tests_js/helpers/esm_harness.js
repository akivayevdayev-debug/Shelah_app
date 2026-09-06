/**
 * Loads a real static/js/*.js ES module (import/export syntax, written for
 * <script type="module"> in the browser) inside a Node vm context, with
 * caller-supplied browser globals (window/document/fetch/...).
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
 * requirement. vm.SourceTextModule lets the *other* static/js/ files — real
 * ES modules — be evaluated as ESM in an isolated context without touching
 * either file's on-disk module system. Requires the `--experimental-vm-modules`
 * Node flag (wired into `npm test` in package.json); does not affect any
 * other test in this directory.
 */
'use strict';

const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

/**
 * @param {string} entryPath Path to the entry module, relative to the repo root.
 * @param {object} globals Host globals the module (and its relative imports)
 *   may reference bare (e.g. { window, document, fetch }).
 * @returns {Promise<vm.SourceTextModule>} The linked, evaluated entry module.
 *   Read exports off `.namespace`.
 */
async function loadEsmModule(entryPath, globals) {
    const context = vm.createContext({ console, ...globals });
    const cache = new Map();

    async function link(specifier, referencingModule) {
        const baseDir = path.dirname(referencingModule.identifier);
        const resolved = path.resolve(baseDir, specifier);
        const cached = cache.get(resolved);
        if (cached) return cached;

        const source = fs.readFileSync(resolved, 'utf8');
        const mod = new vm.SourceTextModule(source, { context, identifier: resolved });
        cache.set(resolved, mod);
        await mod.link(link);
        return mod;
    }

    const repoRoot = path.resolve(__dirname, '..', '..');
    const entryAbsPath = path.resolve(repoRoot, entryPath);
    const entrySource = fs.readFileSync(entryAbsPath, 'utf8');
    const entryModule = new vm.SourceTextModule(entrySource, { context, identifier: entryAbsPath });
    cache.set(entryAbsPath, entryModule);
    await entryModule.link(link);
    await entryModule.evaluate();
    return entryModule;
}

module.exports = { loadEsmModule };
