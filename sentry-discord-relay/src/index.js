/**
 * Sentry → Discord webhook relay (Cloudflare Worker)
 *
 * Receives Sentry webhook alerts, formats them as Discord embeds,
 * and posts to a Discord channel via webhook.
 *
 * Setup:
 *   1. npx wrangler secret put DISCORD_WEBHOOK_URL
 *   2. (optional) npx wrangler secret put SENTRY_WEBHOOK_SECRET
 *   3. npx wrangler deploy
 *   4. Point your Sentry Internal Integration webhook URL at the Worker URL.
 */

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const LEVEL_COLORS = {
  fatal: 0x820000, // dark red
  error: 0xe52b50, // red
  warning: 0xf7b500, // amber
  info: 0x1e90ff, // blue
  debug: 0x808080, // gray
};

const ACTION_LABELS = {
  created: "🔔 New Issue",
  resolved: "✅ Resolved",
  unresolved: "🔄 Re-opened",
  assigned: "👤 Assigned",
  ignored: "🔇 Ignored",
  deleted: "🗑️ Deleted",
};

function colorForLevel(level) {
  return LEVEL_COLORS[(level || "error").toLowerCase()] ?? LEVEL_COLORS.error;
}

function labelForAction(action) {
  return ACTION_LABELS[action] ?? `📌 ${action || "Alert"}`;
}

/**
 * Verify Sentry's HMAC-SHA256 signature if a shared secret is configured.
 * Sentry sends the signature in the `Sentry-Hook-Signature` header.
 */
async function verifySignature(request, rawBody, secret) {
  if (!secret) return true; // no secret configured — skip verification

  const signature = request.headers.get("Sentry-Hook-Signature");
  if (!signature) return false;

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );

  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(rawBody));
  const expected = Array.from(new Uint8Array(mac))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

  // timing-safe-ish comparison
  if (expected.length !== signature.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) {
    diff |= expected.codePointAt(i) ^ signature.codePointAt(i);
  }
  return diff === 0;
}

/**
 * Build a Discord webhook payload from a Sentry webhook body.
 *
 * Sentry issue-alert payloads look like:
 * {
 *   "action": "created",
 *   "data": {
 *     "issue": {
 *       "id": 123,
 *       "title": "Error: ...",
 *       "culprit": "path/to/file.py in function",
 *       "permalink": "https://sentry.io/...",
 *       "level": "error",
 *       "status": "unresolved",
 *       "project": { "name": "my-app", "slug": "my-app" }
 *     }
 *   }
 * }
 *
 * Some payloads also include `data.event` with stacktrace / metadata.
 */
function buildDiscordPayload(body) {
  const action = body.action || "created";
  const issue = body.data?.issue ?? {};
  const event = body.data?.event;
  const project = issue.project?.name ?? issue.project?.slug ?? "Unknown";

  const rawTitle = issue.title || issue.message || "Sentry Alert";
  const url = issue.permalink || issue.url || "";
  const level = (issue.level || event?.level || "error").toLowerCase();
  const culprit = issue.culprit || event?.culprit || "";
  const environment = issue.environment || event?.environment;

  const fields = [
    { name: "Project", value: project, inline: true },
    { name: "Level", value: level, inline: true },
    { name: "Status", value: issue.status || "unresolved", inline: true },
  ];

  if (environment) {
    fields.push({ name: "Environment", value: environment, inline: true });
  }
  if (issue.count != null) {
    fields.push({ name: "Times Seen", value: String(issue.count), inline: true });
  }
  if (issue.firstSeen) {
    fields.push({ name: "First Seen", value: issue.firstSeen, inline: true });
  }

  if (culprit) {
    fields.push({ name: "Culprit", value: `\`${culprit}\``, inline: false });
  }

  // Include first few stacktrace frames if available
  if (event?.exception?.values?.[0]?.stacktrace?.frames) {
    const frames = event.exception.values[0].stacktrace.frames;
    const lastFrames = frames.slice(-5).reverse(); // most-relevant first
    const trace = lastFrames
      .map(
        (f) =>
          `  at ${f.function || "<anonymous>"} (${f.filename}:${f.lineno || "?"}:${f.colno || "?"})`
      )
      .join("\n");
    fields.push({ name: "Stacktrace (last 5)", value: `\`\`\`${trace}\`\`\``, inline: false });
  }

  // Include event message if distinct from issue title
  let description = "";
  if (event?.message && event.message !== rawTitle) {
    description = `\`\`\`${event.message.slice(0, 1000)}\`\`\``;
  }

  // Discord rejects the entire webhook if embed.title exceeds 256 chars —
  // truncate rather than lose the whole notification to one long issue title.
  const fullTitle = `${labelForAction(action)} — ${rawTitle}`;
  const title = fullTitle.length > 256 ? `${fullTitle.slice(0, 253)}...` : fullTitle;

  return {
    username: "Sentry",
    embeds: [
      {
        title,
        url,
        color: colorForLevel(level),
        description: description || undefined,
        fields,
        footer: { text: `Sentry • Issue #${issue.id ?? issue.shortId ?? "?"}` },
        timestamp: new Date().toISOString(),
      },
    ],
  };
}

// ---------------------------------------------------------------------------
// Worker entry point
// ---------------------------------------------------------------------------

export default {
  async fetch(request, env, ctx) {
    // Only accept POST
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    // Guard: webhook URL must be configured
    if (!env.DISCORD_WEBHOOK_URL) {
      console.error("DISCORD_WEBHOOK_URL secret is not set");
      return new Response("Server misconfigured", { status: 500 });
    }

    // Read raw body for signature verification
    const rawBody = await request.text();

    // Verify signature if a shared secret is set
    const ok = await verifySignature(request, rawBody, env.SENTRY_WEBHOOK_SECRET);
    if (!ok) {
      return new Response("Invalid signature", { status: 401 });
    }

    let body;
    try {
      body = JSON.parse(rawBody);
    } catch {
      return new Response("Invalid JSON", { status: 400 });
    }

    const discordPayload = buildDiscordPayload(body);

    // Post to Discord in the background via waitUntil so a slow or failing
    // Discord webhook never delays or fails the Sentry-facing response —
    // Sentry sees "ok" as soon as validation passes, matching Sentry's own
    // expectation of a fast webhook ack. wrapped in try/catch so a network-
    // level failure (not just a non-2xx response) can't crash the worker.
    ctx.waitUntil(
      fetch(env.DISCORD_WEBHOOK_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(discordPayload),
      })
        .then(async (discordRes) => {
          if (!discordRes.ok) {
            const errText = await discordRes.text();
            console.error(`Discord webhook failed (${discordRes.status}): ${errText}`);
          }
        })
        .catch((err) => {
          console.error(`Discord webhook request threw: ${err}`);
        })
    );

    return new Response("ok", { status: 200 });
  },
};
