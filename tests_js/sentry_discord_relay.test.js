// Tests for sentry-discord-relay/src/index.js (Cloudflare Worker).
//
// The worker is the only thing standing between the public internet and a
// Discord webhook, so the behaviors worth pinning are: it authenticates
// Sentry's HMAC signature when a secret is configured (and rejects missing,
// wrong and wrong-length signatures), it acks Sentry before Discord answers,
// a failing Discord never fails the Sentry-facing response, and the Discord
// embed it builds is one Discord will accept (title <= 256 chars).

const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const WORKER_URL = pathToFileURL(
  path.join(__dirname, '..', 'sentry-discord-relay', 'src', 'index.js')
).href;

const WEBHOOK = 'https://discord.example/api/webhooks/1/abc';
const SECRET = 'shared-secret';

let worker;
test.before(async () => {
  worker = (await import(WORKER_URL)).default;
});

/** Runs the worker with a stubbed global fetch and console.error; returns
 *  { response, discordCalls, errors } after every waitUntil promise settled. */
async function invoke({ method = 'POST', body = '{}', headers = {}, env = {}, discord } = {}) {
  const realFetch = globalThis.fetch;
  const realError = console.error;
  const discordCalls = [];
  const errors = [];
  const pending = [];
  globalThis.fetch = async (url, init) => {
    discordCalls.push({ url, init, payload: init?.body ? JSON.parse(init.body) : null });
    if (discord instanceof Error) throw discord;
    return discord || new Response('', { status: 204 });
  };
  console.error = (...args) => errors.push(args.join(' '));
  try {
    const init = { method, headers };
    if (method !== 'GET' && method !== 'HEAD') init.body = body;
    const request = new Request('https://relay.example/', init);
    const response = await worker.fetch(request, { DISCORD_WEBHOOK_URL: WEBHOOK, ...env }, {
      waitUntil: (promise) => pending.push(promise),
    });
    await Promise.all(pending);
    return { response, discordCalls, errors };
  } finally {
    globalThis.fetch = realFetch;
    console.error = realError;
  }
}

const sign = (body, secret = SECRET) => crypto.createHmac('sha256', secret).update(body).digest('hex');

const ISSUE_BODY = (issue = {}, extra = {}) =>
  JSON.stringify({ action: 'created', data: { issue: { id: 7, title: 'Boom', ...issue }, ...extra } });

test('rejects anything but POST with 405 and never calls Discord', async () => {
  const { response, discordCalls } = await invoke({ method: 'GET' });
  assert.equal(response.status, 405);
  assert.equal(discordCalls.length, 0);
});

test('an unset DISCORD_WEBHOOK_URL is a 500 that says so in the logs', async () => {
  const { response, errors, discordCalls } = await invoke({ env: { DISCORD_WEBHOOK_URL: '' } });
  assert.equal(response.status, 500);
  assert.match(errors[0], /DISCORD_WEBHOOK_URL/);
  assert.equal(discordCalls.length, 0);
});

test('invalid JSON is a 400 and is not forwarded', async () => {
  const { response, discordCalls } = await invoke({ body: '{not json' });
  assert.equal(response.status, 400);
  assert.equal(discordCalls.length, 0);
});

test('without a configured secret the signature is not required', async () => {
  const { response, discordCalls } = await invoke({ body: ISSUE_BODY() });
  assert.equal(response.status, 200);
  assert.equal(discordCalls.length, 1);
});

test('with a secret: a correct HMAC signature is accepted and forwarded', async () => {
  const body = ISSUE_BODY();
  const { response, discordCalls } = await invoke({
    body, env: { SENTRY_WEBHOOK_SECRET: SECRET }, headers: { 'Sentry-Hook-Signature': sign(body) },
  });
  assert.equal(response.status, 200);
  assert.equal(await response.text(), 'ok');
  assert.equal(discordCalls.length, 1);
  assert.equal(discordCalls[0].url, WEBHOOK);
  assert.equal(discordCalls[0].init.method, 'POST');
});

for (const [label, header] of [
  ['missing', undefined],
  ['wrong (same length)', '0'.repeat(64)],
  ['wrong (different length)', 'abc'],
  ['signed with another secret', sign(ISSUE_BODY(), 'other-secret')],
]) {
  test(`with a secret: a ${label} signature is 401 and nothing reaches Discord`, async () => {
    const headers = header === undefined ? {} : { 'Sentry-Hook-Signature': header };
    const { response, discordCalls } = await invoke({
      body: ISSUE_BODY(), env: { SENTRY_WEBHOOK_SECRET: SECRET }, headers,
    });
    assert.equal(response.status, 401);
    assert.equal(discordCalls.length, 0);
  });
}

test('a signature computed over a different body is rejected (tampering)', async () => {
  const { response } = await invoke({
    body: ISSUE_BODY({ title: 'Tampered' }),
    env: { SENTRY_WEBHOOK_SECRET: SECRET },
    headers: { 'Sentry-Hook-Signature': sign(ISSUE_BODY({ title: 'Original' })) },
  });
  assert.equal(response.status, 401);
});

test('a failing Discord (non-2xx) is logged but Sentry still gets 200', async () => {
  const { response, errors } = await invoke({
    body: ISSUE_BODY(), discord: new Response('rate limited', { status: 429 }),
  });
  assert.equal(response.status, 200);
  assert.match(errors[0], /Discord webhook failed \(429\): rate limited/);
});

test('a network-level Discord failure is logged and does not crash the worker', async () => {
  const { response, errors } = await invoke({ body: ISSUE_BODY(), discord: new Error('socket hang up') });
  assert.equal(response.status, 200);
  assert.match(errors[0], /Discord webhook request threw: Error: socket hang up/);
});

test('embed: label, colour, project, level, status and footer come from the issue', async () => {
  const body = ISSUE_BODY({
    level: 'WARNING', status: 'resolved', permalink: 'https://sentry.example/i/7',
    project: { name: 'shelah-api' }, environment: 'production', count: 12, firstSeen: '2026-01-02',
    culprit: 'app.py in ask',
  });
  const { discordCalls } = await invoke({ body: body.replace('"created"', '"resolved"') });
  const embed = discordCalls[0].payload.embeds[0];

  assert.equal(discordCalls[0].payload.username, 'Sentry');
  assert.equal(embed.title, '✅ Resolved — Boom');
  assert.equal(embed.url, 'https://sentry.example/i/7');
  assert.equal(embed.color, 0xf7b500);
  assert.equal(embed.footer.text, 'Sentry • Issue #7');
  const fields = Object.fromEntries(embed.fields.map((f) => [f.name, f.value]));
  assert.deepEqual(fields, {
    Project: 'shelah-api', Level: 'warning', Status: 'resolved', Environment: 'production',
    'Times Seen': '12', 'First Seen': '2026-01-02', Culprit: '`app.py in ask`',
  });
});

test('embed: defaults for a bare payload (unknown project, error colour, created label)', async () => {
  const { discordCalls } = await invoke({ body: '{}' });
  const embed = discordCalls[0].payload.embeds[0];
  assert.equal(embed.title, '🔔 New Issue — Sentry Alert');
  assert.equal(embed.color, 0xe52b50);
  assert.equal(embed.footer.text, 'Sentry • Issue #?');
  assert.equal(embed.fields[0].value, 'Unknown');
});

test('embed: an unrecognised action and an unrecognised level fall back sensibly', async () => {
  const body = JSON.stringify({ action: 'archived', data: { issue: { title: 'T', level: 'catastrophic' } } });
  const { discordCalls } = await invoke({ body });
  const embed = discordCalls[0].payload.embeds[0];
  assert.equal(embed.title, '📌 archived — T');
  assert.equal(embed.color, 0xe52b50);
});

test('embed: a title over 256 characters is truncated so Discord accepts the webhook', async () => {
  const { discordCalls } = await invoke({ body: ISSUE_BODY({ title: 'x'.repeat(500) }) });
  const { title } = discordCalls[0].payload.embeds[0];
  assert.equal(title.length, 256);
  assert.ok(title.endsWith('...'));
});

test('embed: the last five stack frames are listed most-relevant first', async () => {
  const frames = Array.from({ length: 8 }, (_, i) => ({ function: `f${i}`, filename: `m${i}.py`, lineno: i + 1 }));
  frames[7] = { filename: 'top.py' };  // no function / line / column
  const body = ISSUE_BODY({}, { event: { exception: { values: [{ stacktrace: { frames } }] } } });
  const { discordCalls } = await invoke({ body });
  const trace = discordCalls[0].payload.embeds[0].fields.find((f) => f.name === 'Stacktrace (last 5)').value;

  const lines = trace.replaceAll('```', '').split('\n');
  assert.equal(lines.length, 5);
  assert.equal(lines[0], '  at <anonymous> (top.py:?:?)');
  assert.equal(lines[4], '  at f3 (m3.py:4:?)');
});

test('embed: the event message becomes the description only when it differs from the title', async () => {
  const different = ISSUE_BODY({ title: 'T' }, { event: { message: 'Detailed message ' + 'y'.repeat(2000) } });
  const same = ISSUE_BODY({ title: 'T' }, { event: { message: 'T' } });

  const a = (await invoke({ body: different })).discordCalls[0].payload.embeds[0];
  const b = (await invoke({ body: same })).discordCalls[0].payload.embeds[0];

  assert.ok(a.description.startsWith('```Detailed message'));
  assert.equal(a.description.length, 1000 + 6, 'message is cut to 1000 chars inside the fences');
  assert.equal(b.description, undefined);
});

test('embed: level and culprit fall back to the event when the issue lacks them', async () => {
  const body = ISSUE_BODY({}, { event: { level: 'fatal', culprit: 'worker.py', environment: 'staging' } });
  const embed = (await invoke({ body })).discordCalls[0].payload.embeds[0];
  assert.equal(embed.color, 0x820000);
  const names = new Set(embed.fields.map((f) => f.name));
  assert.ok(names.has('Culprit'));
  assert.ok(names.has('Environment'));
});
