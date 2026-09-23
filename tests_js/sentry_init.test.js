/**
 * Tests for static/js/sentry-init.js — plan.md §17 STEP 2's required test:
 * "submit a question through the instrumented path and assert the question
 * string appears nowhere in the captured event."
 *
 * Run with: node --test tests_js/
 * (Node's built-in test runner — no new devDependency needed.)
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const sentryInit = require('../static/js/sentry-init.js');

const QUESTION_TEXT = 'Can I take my SSRI medication before the fast on Yom Kippur, ' +
    'and does my abusive husband refusing to let me see a doctor change the halacha?';
const ANSWER_TEXT = 'Consult a rabbi and your physician about your specific medical situation.';
const ALLOWED_ORIGIN = 'https://shelah.org';

function buildAskErrorEvent() {
    return {
        message: 'TypeError: Cannot read properties of undefined (reading \'sources\')',
        request: {
            url: 'https://shelah.org/ask?debug=1',
            data: { question: QUESTION_TEXT, mode: 'balanced' },
            cookies: { __session: 'clerk-session-token-abc123' },
            headers: {
                Authorization: 'Bearer clerk-jwt-token-xyz',
                'Content-Type': 'application/json',
            },
        },
        exception: {
            values: [
                {
                    type: 'TypeError',
                    value: 'Cannot read properties of undefined (reading \'sources\')',
                    stacktrace: {
                        frames: [
                            { filename: 'https://shelah.org/static/js/main.js', lineno: 42 },
                        ],
                    },
                },
            ],
        },
        breadcrumbs: [
            {
                category: 'xhr',
                type: 'http',
                message: 'https://shelah.org/ask?debug=1',
                data: {
                    url: 'https://shelah.org/ask?debug=1',
                    method: 'POST',
                    status_code: 200,
                    body: JSON.stringify({ question: QUESTION_TEXT }),
                    response_body: JSON.stringify({ answer: ANSWER_TEXT }),
                    headers: { Authorization: 'Bearer clerk-jwt-token-xyz' },
                },
            },
            {
                category: 'console',
                message: `User asked: ${QUESTION_TEXT}`,
            },
        ],
        extra: {
            question: QUESTION_TEXT,
            last_answer: ANSWER_TEXT,
        },
        contexts: {
            backend_error: { question: QUESTION_TEXT, ruling: ANSWER_TEXT },
        },
    };
}

test('beforeSend scrubs the question and answer text out of a captured /ask error event', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const event = beforeSend(buildAskErrorEvent());

    assert.ok(event, 'a legitimate same-origin error must not be dropped');
    const serialized = JSON.stringify(event);
    assert.doesNotMatch(serialized, new RegExp(QUESTION_TEXT.slice(0, 20)));
    assert.doesNotMatch(serialized, new RegExp(ANSWER_TEXT));
    assert.ok(!serialized.includes(QUESTION_TEXT), 'question text must appear nowhere in the captured event');
    assert.ok(!serialized.includes(ANSWER_TEXT), 'answer text must appear nowhere in the captured event');
});

test('beforeSend strips Clerk auth tokens and cookies from the event', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const event = beforeSend(buildAskErrorEvent());

    const serialized = JSON.stringify(event);
    assert.ok(!serialized.includes('clerk-jwt-token-xyz'));
    assert.ok(!serialized.includes('clerk-session-token-abc123'));
});

test('beforeSend strips query strings from request and breadcrumb URLs', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const event = beforeSend(buildAskErrorEvent());

    assert.equal(event.request.url, 'https://shelah.org/ask');
    assert.equal(event.breadcrumbs[0].data.url, 'https://shelah.org/ask');
});

test('beforeBreadcrumb scrubs an /ask XHR breadcrumb the instant it is recorded', () => {
    const beforeBreadcrumb = sentryInit.makeBeforeBreadcrumb();
    const breadcrumb = beforeBreadcrumb({
        category: 'xhr',
        data: {
            url: 'https://shelah.org/ask',
            body: QUESTION_TEXT,
            response_body: ANSWER_TEXT,
        },
    });

    const serialized = JSON.stringify(breadcrumb);
    assert.ok(!serialized.includes(QUESTION_TEXT));
    assert.ok(!serialized.includes(ANSWER_TEXT));
});

test('beforeSend drops ResizeObserver loop noise', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const dropped = beforeSend({ message: 'ResizeObserver loop completed with undelivered notifications.' });
    assert.equal(dropped, null);
});

test('beforeSend drops errors whose only stack frames are browser-extension-origin', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const dropped = beforeSend({
        exception: {
            values: [{
                value: 'boom',
                stacktrace: { frames: [{ filename: 'chrome-extension://abcdefg/content.js' }] },
            }],
        },
    });
    assert.equal(dropped, null);
});

test('beforeSend drops navigation-abort noise', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const dropped = beforeSend({
        exception: { values: [{ value: 'AbortError: The user aborted a request.' }] },
    });
    assert.equal(dropped, null);
});

test('beforeSend keeps a legitimate same-origin error with no noise markers', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const event = beforeSend({
        message: 'ReferenceError: foo is not defined',
        exception: {
            values: [{
                value: 'foo is not defined',
                stacktrace: { frames: [{ filename: 'https://shelah.org/static/js/main.js' }] },
            }],
        },
    });
    assert.ok(event);
});

test('beforeSend deduplicates the same fingerprint within the dedupe window', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const makeEvent = () => ({ message: 'Same error looping' });

    const first = beforeSend(makeEvent());
    const second = beforeSend(makeEvent());

    assert.ok(first, 'first occurrence should be sent');
    assert.equal(second, null, 'immediate repeat should be dropped as a loop');
});

test('beforeSend throttles after the per-session cap is exceeded', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    let lastResult;
    for (let i = 0; i < 45; i += 1) {
        lastResult = beforeSend({ message: `Distinct error #${i}` });
    }
    assert.equal(lastResult, null, 'events beyond the session cap must be dropped');
});

test('stripQuery removes query strings and fragments but leaves bare paths alone', () => {
    assert.equal(sentryInit.stripQuery('https://shelah.org/ask?question=abc&mode=balanced'), 'https://shelah.org/ask');
    assert.equal(sentryInit.stripQuery('https://shelah.org/ask#section'), 'https://shelah.org/ask');
    assert.equal(sentryInit.stripQuery('https://shelah.org/static/js/main.js'), 'https://shelah.org/static/js/main.js');
});

test('beforeSend redacts a console breadcrumb message even when it echoes a question', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const event = beforeSend(buildAskErrorEvent());
    const consoleCrumb = event.breadcrumbs.find((b) => b.category === 'console');
    assert.equal(consoleCrumb.message, '[Filtered]');
});

test('beforeSend truncates an overlong exception value as a defense-in-depth backstop', () => {
    const beforeSend = sentryInit.makeBeforeSend(ALLOWED_ORIGIN);
    const longValue = 'x'.repeat(500);
    const event = beforeSend({
        exception: { values: [{ value: longValue }] },
    });
    assert.ok(event.exception.values[0].value.length < longValue.length);
    assert.ok(event.exception.values[0].value.endsWith('[truncated]'));
});

test('isAskUrl matches /ask with and without query strings', () => {
    assert.ok(sentryInit.isAskUrl('https://shelah.org/ask'));
    assert.ok(sentryInit.isAskUrl('https://shelah.org/ask?debug=1'));
    assert.ok(!sentryInit.isAskUrl('https://shelah.org/api/library/search'));
});

// ── initSentry (the actual SDK bootstrap; only ever runs on `root`, i.e.
// `window` in a browser or Node's own `globalThis` here) ───────────────────

function withGlobals(overrides, run) {
    const keys = Object.keys(overrides);
    const had = {};
    const saved = {};
    for (const key of keys) {
        had[key] = Object.prototype.hasOwnProperty.call(globalThis, key);
        saved[key] = globalThis[key];
        globalThis[key] = overrides[key];
    }
    try {
        return run();
    } finally {
        for (const key of keys) {
            if (had[key]) {
                globalThis[key] = saved[key];
            } else {
                delete globalThis[key];
            }
        }
    }
}

function makeStubSentry() {
    const calls = [];
    return { calls, init: (options) => { calls.push(options); } };
}

test('initSentry is a silent no-op when the Sentry CDN script never loaded', () => {
    withGlobals({ Sentry: undefined, __SHELAH_SENTRY_DSN__: 'https://example.ingest.sentry.io/1' }, () => {
        assert.doesNotThrow(() => sentryInit.initSentry());
    });
});

test('initSentry is a silent no-op when SENTRY_DSN_BROWSER is unset', () => {
    const sentry = makeStubSentry();
    withGlobals({ Sentry: sentry, __SHELAH_SENTRY_DSN__: undefined }, () => {
        sentryInit.initSentry();
    });
    assert.equal(sentry.calls.length, 0);
});

test('initSentry is a silent no-op when Sentry.init is not a function', () => {
    withGlobals({ Sentry: { init: 'not-a-function' }, __SHELAH_SENTRY_DSN__: 'https://example.ingest.sentry.io/1' }, () => {
        assert.doesNotThrow(() => sentryInit.initSentry());
    });
});

test('initSentry calls Sentry.init with the DSN, the privacy overrides, and origin-derived defaults', () => {
    const sentry = makeStubSentry();
    withGlobals({
        Sentry: sentry,
        __SHELAH_SENTRY_DSN__: 'https://example.ingest.sentry.io/1',
        location: { origin: 'https://shelah.org' },
    }, () => {
        sentryInit.initSentry();
    });

    assert.equal(sentry.calls.length, 1);
    const options = sentry.calls[0];
    assert.equal(options.dsn, 'https://example.ingest.sentry.io/1');
    assert.equal(options.environment, 'development');
    assert.equal(options.release, undefined);
    assert.equal(options.sendDefaultPii, false);
    assert.equal(options.tracesSampleRate, 0);
    assert.deepEqual(options.dataCollection, { userInfo: false, httpBodies: [] });
    assert.equal(typeof options.beforeSend, 'function');
    assert.equal(typeof options.beforeBreadcrumb, 'function');
});

test('initSentry reads environment and release from their global overrides when present', () => {
    const sentry = makeStubSentry();
    withGlobals({
        Sentry: sentry,
        __SHELAH_SENTRY_DSN__: 'https://example.ingest.sentry.io/1',
        __SHELAH_SENTRY_ENV__: 'production',
        __SHELAH_SENTRY_RELEASE__: 'shelah@1.2.3',
        location: { origin: 'https://shelah.org' },
    }, () => {
        sentryInit.initSentry();
    });
    assert.equal(sentry.calls[0].environment, 'production');
    assert.equal(sentry.calls[0].release, 'shelah@1.2.3');
});

test('initSentry swallows an exception thrown by Sentry.init instead of breaking the app', () => {
    const sentry = { init: () => { throw new Error('boom'); } };
    withGlobals({
        Sentry: sentry,
        __SHELAH_SENTRY_DSN__: 'https://example.ingest.sentry.io/1',
        location: { origin: 'https://shelah.org' },
    }, () => {
        assert.doesNotThrow(() => sentryInit.initSentry());
    });
});

// ── isOffOriginNoise's catch branch (an unparseable frame filename) ────────

test('isOffOriginNoise treats an unparseable frame filename as same-origin (fails safe, keeps the event)', () => {
    const event = {
        exception: {
            values: [
                { stacktrace: { frames: [{ filename: '://not-a-valid-url' }] } },
            ],
        },
    };
    assert.equal(sentryInit.isOffOriginNoise(event, ALLOWED_ORIGIN), false);
});
