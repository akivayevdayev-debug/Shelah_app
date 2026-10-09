/**
 * A signed-out visitor's follow-ups and the guest limits (backend/guest_cap.py),
 * on the client side:
 *
 *   - conversation-store.js: askGuestFollowUp() continues the answers on screen
 *     (naming their ids to the server), keeps the guest counters, takes a
 *     refused question back out of the transcript, and signing in later
 *     seeds the account's conversation from the latest answer without
 *     duplicating the earlier turns.
 *   - conversation-ui-helpers.js: the dialog's words (English and Hebrew,
 *     per limit), the "N left" banner, and the notice a dismissed dialog
 *     leaves behind.
 *   - ai-service.js: history_ids on the request, and the refusal's reason and
 *     counters on the thrown error.
 *
 * Run with: node --test tests_js/*.test.js
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { loadEsmModule } = require('./helpers/esm_harness');

async function loadStore() {
    const mod = await loadEsmModule('static/js/conversation-store.js', {
        window: {}, fetch: async () => { throw new Error('real fetch must not be used'); },
        setTimeout, clearTimeout, AbortController,
    });
    return mod.namespace;
}

async function loadHelpers() {
    return (await loadEsmModule('static/js/conversation-ui-helpers.js', { window: {} })).namespace;
}

function makeApi(overrides = {}) {
    return {
        ERROR_CODES: { NETWORK: 'network', ANSWER_INCOMPLETE: 'answer_incomplete', UNAUTHORIZED: 'unauthorized' },
        createConversation: async ({ fromHistoryId }) => ({
            id: 'c1', title: 'T', minhag: 'All',
            messages: fromHistoryId ? [
                { id: 'm1', role: 'user', content: 'seeded question', status: 'complete' },
                { id: 'm2', role: 'assistant', content: 'seeded answer', status: 'complete' },
            ] : [],
        }),
        askInConversation: async (id, { question }) => ({
            user_message: { id: 'u2', role: 'user', content: question, status: 'complete' },
            assistant_message: { id: 'a2', role: 'assistant', content: 'Thread answer.', status: 'complete', citations: [] },
            conversation: { id, title: 'T', minhag: 'All' },
        }),
        listConversations: async () => [],
        ...overrides,
    };
}

function usage(overrides = {}) {
    return {
        questions_used: 1, questions_limit: 8, tokens_used: 400, tokens_limit: 24000,
        daily_used: 1, daily_limit: 24, remaining: 7, binding: 'thread', ...overrides,
    };
}

function payload(n, extra = {}) {
    return { answer: `Answer ${n}.`, history_id: `h-${n}`, ai_cited_sources: [], meta: { guest: usage({ questions_used: n, remaining: 8 - n }) }, ...extra };
}

// A store whose /ask client answers in sequence and records what it was asked.
async function guestStore(responses) {
    const { createConversationStore } = await loadStore();
    const asked = [];
    let i = 0;
    const askAnswer = async (question, request, extra) => {
        asked.push({ question, request, historyIds: extra.historyIds });
        const next = responses[Math.min(i, responses.length - 1)];
        i += 1;
        if (next instanceof Error) throw next;
        return next;
    };
    const store = createConversationStore({ api: makeApi(), askAnswer });
    return { store, asked };
}

function cap(reason = 'thread_questions', extra = {}) {
    return Object.assign(new Error('limit'), {
        status: 403, code: 'guest_cap_reached', reason, usage: usage({ questions_used: 8, remaining: 0 }), ...extra,
    });
}

// ── the store ──────────────────────────────────────────────────────────────

test('a guest follow-up names the answers on screen and appends its turns', async () => {
    const { store, asked } = await guestStore([payload(1), payload(2), payload(3)]);

    await store.askSearch('First?');
    assert.equal(store.canContinueAsGuest(), true);
    assert.deepEqual(store.guestHistoryIds(), ['h-1']);

    await store.askGuestFollowUp('Second?');
    await store.askGuestFollowUp('Third?');

    assert.deepEqual(asked.map((a) => a.historyIds), [undefined, ['h-1'], ['h-1', 'h-2']]);
    const state = store.getState();
    assert.deepEqual(state.messages.map((m) => m.content), [
        'First?', 'Answer 1.', 'Second?', 'Answer 2.', 'Third?', 'Answer 3.']);
    assert.equal(state.answerView.historyId, 'h-3');
    assert.equal(state.guestUsage.questions_used, 3);
    assert.equal(state.sending, false);
});

test('while the follow-up is in flight the earlier turns stay and a pending pair is added', async () => {
    const { createConversationStore, MESSAGE_STATUS } = await loadStore();
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    let call = 0;
    const store = createConversationStore({
        api: makeApi(),
        askAnswer: async () => { call += 1; if (call === 1) return payload(1); await gate; return payload(2); },
    });
    await store.askSearch('First?');

    const pending = store.askGuestFollowUp('Second?');
    const during = store.getState();
    assert.equal(during.sending, true);
    assert.deepEqual(during.messages.map((m) => [m.role, m.status]), [
        ['user', MESSAGE_STATUS.COMPLETE], ['assistant', MESSAGE_STATUS.COMPLETE],
        ['user', MESSAGE_STATUS.PENDING], ['assistant', MESSAGE_STATUS.PENDING]]);

    release();
    await pending;
    assert.equal(store.getState().messages.length, 4);
});

test('with nothing on screen a "follow-up" is just a new question', async () => {
    const { store, asked } = await guestStore([payload(1)]);
    assert.equal(store.canContinueAsGuest(), false);

    await store.askGuestFollowUp('Fresh?');

    assert.equal(asked[0].historyIds, undefined);
    assert.equal(store.getState().messages.length, 2);
});

test('a shared answer, or one that was never saved, cannot be continued', async () => {
    const { store } = await guestStore([payload(1, { history_id: undefined, id: undefined })]);
    await store.askSearch('Unsaved?');
    assert.equal(store.canContinueAsGuest(), false);

    store.showAnswer({ id: 'pub', answer: 'Shared.', question: 'Q?', public: true });
    assert.equal(store.canContinueAsGuest(), false);
});

test('a stored answer shown from history can be followed up by its id', async () => {
    const { store, asked } = await guestStore([payload(2)]);
    store.showAnswer({ id: 'h-old', question: 'Old?', answer: 'Old answer.' });
    assert.deepEqual(store.guestHistoryIds(), ['h-old']);

    await store.askGuestFollowUp('Next?');

    assert.deepEqual(asked[0].historyIds, ['h-old']);
});

test('a refused follow-up leaves no turn behind and keeps the counters for the dialog', async () => {
    const { store } = await guestStore([payload(1), cap('thread_questions')]);
    await store.askSearch('First?');

    await assert.rejects(store.askGuestFollowUp('One too many?'), (error) => error.code === 'guest_cap_reached');

    const state = store.getState();
    assert.deepEqual(state.messages.map((m) => m.content), ['First?', 'Answer 1.']);
    assert.equal(state.sending, false);
    assert.equal(state.lastError.code, 'guest_cap_reached');
    assert.equal(state.lastError.reason, 'thread_questions');
    assert.equal(state.guestUsage.remaining, 0);
    assert.equal(state.sources.phase, 'done');
});

test('guestCapReached() trusts the thread counters but leaves the day to the server', async () => {
    const { store } = await guestStore([payload(8, { meta: { guest: usage({ remaining: 0 }) } })]);
    await store.askSearch('Q?');
    assert.equal(store.guestCapReached(), true);

    const { store: day } = await guestStore([payload(1, { meta: { guest: usage({ remaining: 0, binding: 'daily' }) } })]);
    await day.askSearch('Q?');
    assert.equal(day.guestCapReached(), false);

    const { store: plenty } = await guestStore([payload(1)]);
    await plenty.askSearch('Q?');
    assert.equal(plenty.guestCapReached(), false);
});

test('any other follow-up failure keeps the question as FAILED, and Retry asks it again as a follow-up', async () => {
    const { MESSAGE_STATUS } = await loadStore();
    const { store, asked } = await guestStore([payload(1), new Error('offline'), payload(2)]);
    await store.askSearch('First?');

    await assert.rejects(store.askGuestFollowUp('Second?'));
    let state = store.getState();
    assert.deepEqual(state.messages.map((m) => [m.content, m.status]), [
        ['First?', MESSAGE_STATUS.COMPLETE], ['Answer 1.', MESSAGE_STATUS.COMPLETE], ['Second?', MESSAGE_STATUS.FAILED]]);

    const failed = state.messages[2];
    assert.equal(await store.retry(failed.id), true);

    state = store.getState();
    assert.deepEqual(asked.map((a) => a.historyIds), [undefined, ['h-1'], ['h-1']]);
    assert.deepEqual(state.messages.map((m) => m.content), ['First?', 'Answer 1.', 'Second?', 'Answer 2.']);
});

test('a follow-up the visitor has left behind never lands in the thread now open', async () => {
    const { createConversationStore } = await loadStore();
    let release;
    const gate = new Promise((resolve) => { release = resolve; });
    let call = 0;
    const store = createConversationStore({
        api: makeApi(),
        askAnswer: async () => { call += 1; if (call === 1) return payload(1); await gate; return payload(2); },
    });
    await store.askSearch('First?');
    const pending = store.askGuestFollowUp('Second?');
    store.startNew();
    release();

    assert.equal(await pending, null);
    assert.equal(store.getState().messages.length, 0);
});

test('signing in after several guest answers seeds from the latest and keeps the earlier turns above it', async () => {
    const created = [];
    const { createConversationStore } = await loadStore();
    let call = 0;
    const store = createConversationStore({
        api: makeApi({
            createConversation: async (args) => {
                created.push(args);
                return {
                    id: 'c1', title: 'T', minhag: 'All',
                    messages: [
                        { id: 'm1', role: 'user', content: 'Second?', status: 'complete' },
                        { id: 'm2', role: 'assistant', content: 'Answer 2.', status: 'complete' },
                    ],
                };
            },
        }),
        askAnswer: async () => { call += 1; return payload(call); },
    });
    await store.askSearch('First?');
    await store.askGuestFollowUp('Second?');

    await store.send('Now signed in?');

    assert.deepEqual(created, [{ fromHistoryId: 'h-2' }]);
    const state = store.getState();
    assert.equal(state.conversation.id, 'c1');
    assert.equal(state.guestUsage, null);
    assert.deepEqual(state.messages.map((m) => m.content), [
        'First?', 'Answer 1.', 'Second?', 'Answer 2.', 'Now signed in?', 'Thread answer.']);
    // The seeded answer keeps the on-screen payload (Copy link, feedback).
    assert.equal(state.messages[3].answer.history_id, 'h-2');
});

test('a refused new question (the day) stays on screen as a failed turn with the dialog data', async () => {
    const { store } = await guestStore([cap('daily')]);

    await assert.rejects(store.askSearch('Another?'));

    const state = store.getState();
    assert.equal(state.lastError.code, 'guest_cap_reached');
    assert.equal(state.lastError.reason, 'daily');
    assert.deepEqual(state.messages.map((m) => m.status), ['failed']);
});

// ── the words ──────────────────────────────────────────────────────────────

test('the dialog says which limit was hit, in English and Hebrew', async () => {
    const { guestCapCopy } = await loadHelpers();
    const u = usage({ questions_limit: 8 });

    const en = guestCapCopy('thread_questions', u, 'en');
    assert.equal(en.title, "You've reached the guest limit for this conversation");
    assert.match(en.body, /Guests can ask up to 8 questions per conversation\. Sign in or create a free account to keep going\./);
    assert.match(en.body, /the question you just typed will be waiting for you\.$/);

    const he = guestCapCopy('thread_questions', u, 'he');
    assert.equal(he.title, 'הגעתם למגבלת האורחים בשיחה הזו');
    assert.match(he.body, /אורחים יכולים לשאול עד 8 שאלות בשיחה/);
    assert.match(he.body, /השאלה שהקלדתם יחכו לכם\.$/);

    assert.match(guestCapCopy('thread_tokens', u, 'en').body, /used its guest allowance/);
    assert.doesNotMatch(guestCapCopy('thread_tokens', u, 'en').body, /up to 8 questions/);

    assert.equal(guestCapCopy('daily', u, 'en').title, "You've used today's guest questions");
    assert.match(guestCapCopy('daily', u, 'en').body, /Your conversations will be saved\./);
    assert.match(guestCapCopy('daily', u, 'he').body, /השיחות שלכם יישמרו/);
});

test('the limit in the sentence is the server\'s, not a constant', async () => {
    const { guestCapCopy } = await loadHelpers();
    assert.match(guestCapCopy('thread_questions', usage({ questions_limit: 5 }), 'en').body, /up to 5 questions/);
    assert.match(guestCapCopy('thread_questions', null, 'en').body, /up to 8 questions/);
});

test('the banner appears at two questions left, pluralises, and names the day when that is what binds', async () => {
    const { guestBannerText, GUEST_WARN_AT } = await loadHelpers();
    assert.equal(GUEST_WARN_AT, 2);

    assert.equal(guestBannerText(null, 'en'), null);
    assert.equal(guestBannerText(usage({ remaining: 3 }), 'en'), null);
    assert.equal(guestBannerText(usage({ remaining: 2 }), 'en'), '2 questions left in this guest conversation. Sign in to keep going.');
    assert.equal(guestBannerText(usage({ remaining: 1 }), 'en'), '1 question left in this guest conversation. Sign in to keep going.');
    assert.equal(guestBannerText(usage({ remaining: 2 }), 'he'), 'נותרו 2 שאלות בשיחת האורחים הזו. התחברו כדי להמשיך.');
    assert.equal(guestBannerText(usage({ remaining: 1 }), 'he'), 'נותרה שאלה אחת בשיחת האורחים הזו. התחברו כדי להמשיך.');
    assert.equal(guestBannerText(usage({ remaining: 0 }), 'en'), "You've reached the guest limit for this conversation. Sign in to keep going.");

    assert.equal(guestBannerText(usage({ remaining: 2, binding: 'daily' }), 'en'), '2 guest questions left today. Sign in to keep asking.');
    assert.equal(guestBannerText(usage({ remaining: 0, binding: 'daily' }), 'en'), "You've used today's guest questions. Sign in to keep asking.");
});

test('a dismissed dialog leaves a sign-in notice with the limit\'s title', async () => {
    const { noticeFor } = await loadHelpers();
    const notice = noticeFor({ code: 'guest_cap_reached', reason: 'daily', usage: usage() }, 'en');
    assert.equal(notice.action, 'sign-in');
    assert.equal(notice.text, "You've used today's guest questions");
});

// ── the /ask client ────────────────────────────────────────────────────────

async function loadAiService(fetchFn) {
    const window = {};
    const mod = await loadEsmModule('static/js/ai-service.js', {
        window, fetch: fetchFn, setTimeout: (fn) => { fn(); return 0; }, clearTimeout: () => {}, AbortController,
    });
    return mod.namespace;
}

test('askAi sends history_ids only when there are some, trimmed and without blanks', async () => {
    const bodies = [];
    const fetchFn = async (_url, opts) => {
        bodies.push(JSON.parse(opts.body));
        return { status: 200, ok: true, json: async () => ({ answer: 'A.' }) };
    };
    const { askAi } = await loadAiService(fetchFn);

    await askAi('Q?', { historyIds: [' h-1 ', '', null, 'h-2'] });
    await askAi('Q?', { historyIds: [] });
    await askAi('Q?');

    assert.deepEqual(bodies[0].history_ids, ['h-1', 'h-2']);
    assert.equal('history_ids' in bodies[1], false);
    assert.equal('history_ids' in bodies[2], false);
});

test('a guest-limit refusal throws with its code, reason and counters', async () => {
    const detail = { error: 'Limit.', code: 'guest_cap_reached', reason: 'thread_questions', usage: usage({ remaining: 0 }) };
    const fetchFn = async () => ({ status: 403, ok: false, json: async () => ({ detail }) });
    const { askAi } = await loadAiService(fetchFn);

    await assert.rejects(askAi('Q?', { historyIds: ['h-1'] }), (error) => {
        assert.equal(error.status, 403);
        assert.equal(error.code, 'guest_cap_reached');
        assert.equal(error.reason, 'thread_questions');
        assert.equal(error.usage.remaining, 0);
        return true;
    });
});
