/**
 * static/js/conversation-entry.js -- decision A1: signed-in users get the
 * conversation UI, signed-out users keep the legacy AI modal with a sign-in
 * hint. Sign-in detection must match templates/index.html's own test
 * (Clerk configured + session + user), so a half-loaded Clerk never routes a
 * user into a UI whose API calls would all 401.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

const CONFIGURED = { publishableKey: 'pk_test_x' };
const SESSION = { getToken: async () => 't' };
const USER = { id: 'user_1' };

async function loadEntry(window = {}) {
    const mod = await loadEsmModule('static/js/conversation-entry.js', { window });
    return { entry: mod.namespace, window };
}

test('isSignedIn needs Clerk configured, a session, and a user', async () => {
    const cases = [
        [{ APP_CLERK: CONFIGURED, Clerk: { session: SESSION, user: USER } }, true],
        [{ APP_CLERK: CONFIGURED, Clerk: { session: null, user: USER } }, false],
        [{ APP_CLERK: CONFIGURED, Clerk: { session: SESSION, user: null } }, false],
        [{ APP_CLERK: CONFIGURED }, false],                                   // SDK not loaded yet
        [{ APP_CLERK: { publishableKey: '' }, Clerk: { session: SESSION, user: USER } }, false],
        [{ Clerk: { session: SESSION, user: USER } }, false],                 // Clerk not configured
        [{}, false],
    ];
    for (const [window, expected] of cases) {
        const { entry } = await loadEntry(window);
        assert.equal(entry.isSignedIn(), expected, JSON.stringify(window));
    }
});

test('isSignedIn reads the live Clerk state, not a snapshot from load time', async () => {
    const { entry, window } = await loadEntry({ APP_CLERK: CONFIGURED, Clerk: { session: null, user: null } });
    assert.equal(entry.isSignedIn(), false);
    window.Clerk.session = SESSION;
    window.Clerk.user = USER;
    assert.equal(entry.isSignedIn(), true);
});

test('chooseAskSurface: signed in -> conversation, signed out -> legacy', async () => {
    const { entry } = await loadEntry();
    assert.equal(entry.chooseAskSurface({ signedIn: true }), 'conversation');
    assert.equal(entry.chooseAskSurface({ signedIn: false }), 'legacy');
    assert.equal(entry.ASK_SURFACE.CONVERSATION, 'conversation');
    assert.equal(entry.ASK_SURFACE.LEGACY, 'legacy');
});

test('chooseAskSurface defaults to the live sign-in state', async () => {
    let { entry } = await loadEntry({ APP_CLERK: CONFIGURED, Clerk: { session: SESSION, user: USER } });
    assert.equal(entry.chooseAskSurface(), 'conversation');
    assert.equal(entry.chooseAskSurface({}), 'conversation');
    ({ entry } = await loadEntry({}));
    assert.equal(entry.chooseAskSurface(), 'legacy');
});

test('SIGN_IN_HINT is short plain copy', async () => {
    const { entry } = await loadEntry();
    assert.equal(typeof entry.SIGN_IN_HINT, 'string');
    assert.match(entry.SIGN_IN_HINT, /sign in/i);
    assert.ok(entry.SIGN_IN_HINT.length <= 60);
});

test('exposed on window.ShelahConversationEntry like window.ShelahRouter', async () => {
    const { entry, window } = await loadEntry();
    assert.equal(window.ShelahConversationEntry.chooseAskSurface, entry.chooseAskSurface);
    assert.equal(window.ShelahConversationEntry.isSignedIn, entry.isSignedIn);
    assert.equal(window.ShelahConversationEntry.SIGN_IN_HINT, entry.SIGN_IN_HINT);
    assert.equal(window.ShelahConversationEntry.ASK_SURFACE, entry.ASK_SURFACE);
});
