# Sentry → Discord Relay (Cloudflare Worker)

A lightweight Cloudflare Worker that receives Sentry webhook alerts and forwards them to a Discord channel as rich embeds.

## Features

- **Zero cost** — runs on the Workers free tier
- **Rich Discord embeds** — color-coded by severity, includes project, level, status, culprit, and stacktrace
- **HMAC signature verification** — optional shared-secret validation so only Sentry can trigger it
- **Secret management** — Discord webhook URL stored as an encrypted Worker secret, never in source

## Quick Start

```bash
# 1. Install dependencies
npm install

# 2. Set your Discord webhook URL as a secret
npx wrangler secret put DISCORD_WEBHOOK_URL
# Paste: https://discord.com/api/webhooks/XXXXX/YYYYY

# 3. (Optional) Set a shared secret for Sentry to sign requests
npx wrangler secret put SENTRY_WEBHOOK_SECRET
# Paste any random string — you'll enter the same in Sentry

# 4. Deploy
npm run deploy
```

Your Worker URL will look like `https://sentry-discord-relay.<your-subdomain>.workers.dev`.

## Sentry Configuration

1. In Sentry, go to **Settings → Integrations → Internal Integrations** (or **Webhooks** for legacy).
2. Set the **Webhook URL** to your deployed Worker URL.
3. If you set `SENTRY_WEBHOOK_SECRET`, enter the same value in Sentry's signing secret field.
4. Subscribe to the events you want (e.g., `issue.created`, `issue.resolved`).
5. Add the integration as an action in your **Alert Rules**.

## Local Development

```bash
npm run dev
```

Create a `.dev.vars` file for local secrets:

```
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/XXXXX/YYYYY
SENTRY_WEBHOOK_SECRET=your-secret
```

## Payload Structure

Sentry sends issue-alert webhooks like:

```json
{
  "action": "created",
  "data": {
    "issue": {
      "id": 123,
      "title": "Error: Something broke",
      "culprit": "src/app.py in handle_request",
      "permalink": "https://sentry.io/org/project/issues/123",
      "level": "error",
      "status": "unresolved",
      "project": { "name": "my-app" }
    }
  }
}
```

The Worker extracts these fields and builds a Discord embed with:
- Color based on severity (fatal=dark red, error=red, warning=amber, info=blue)
- Clickable link to the Sentry issue
- Stacktrace frames (last 5) when available
- Action label (🔔 New Issue, ✅ Resolved, etc.)
