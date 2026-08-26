# 🔗 Quick Connection Reference

> Sync status (2026-04-21): Verified against current implementation (report-driven library filtering, topbar menu icon layering fix, global warm icon tones, and backup template sync).

## All-in-One Verification (1 command)

```bash
cd /Users/akivayevdayev/Documents/Sh'elah_app
source .venv/bin/activate
python3 scripts/verify_integrations.py
```

---

## Services & Their Purposes

| Service | Purpose | Status | Action |
|---------|---------|--------|--------|
| **Flask** | Backend API server | ✅ Running | None - use `python3 app.py` to start |
| **Supabase** | Database (user preferences) | ✅ Connected | Check credentials in .env.local |
| **Clerk** | User authentication | ⚠️ Missing key | Add CLERK_PUBLISHABLE_KEY to .env.local |
| **Vercel** | Live deployment | ✅ Deployed | Push code → auto-deploys |
| **Sefaria** | Prayer text library | ℹ️ External | No action needed (fallback available) |
| **Hebcal** | Holiday dates | ℹ️ External | No action needed |

---

## How They Talk (Simplified)

```
User clicks in browser
    ↓
Authenticates with Clerk (login)
    ↓
Browser has Clerk token
    ↓
"Load prayer" → Flask API with token
    ↓
Flask checks token with Clerk
    ↓
✅ Valid? Continue and return prayer + customs
❌ Invalid? Return 401 Unauthorized
    ↓
Browser displays prayer
```

---

## Required Environment Variables

```bash
# Your .env.local should have:

# Supabase (your database)
SUPABASE_URL=https://xyz.supabase.co
SUPABASE_ANON_KEY=eyJ...
SUPABASE_SERVICE_ROLE_KEY=eyJ...

# Clerk (your auth system)
CLERK_PUBLISHABLE_KEY=pk_test_...          ← ⚠️ Currently MISSING
CLERK_JWT_ISSUER=https://xyz.clerk.accounts.dev
CLERK_AUDIENCE=https://shelah-app.vercel.app

# Optional
CLERK_ENFORCE_AUTH=false
SUPABASE_PREFS_TABLE=user_preferences
```

---

## Checklists for Each Service

### ✅ Supabase Working?
- [ ] Run script and see "✅ PASS Supabase Connection"
- [ ] Can log into supabase.com dashboard
- [ ] `user_preferences` table exists (check in Table Editor)

### ✅ Clerk Working?
- [ ] CLERK_PUBLISHABLE_KEY is set in .env.local
- [ ] Can log into dashboard.clerk.com
- [ ] See users/sign-in active in Users tab
- [ ] JWT issuer matches your environment

### ✅ Flask Working?
- [ ] Run `python3 app.py` → no errors
- [ ] `curl http://localhost:5000/api/community/ashkenaz` returns data
- [ ] Community customs load in browser

### ✅ Vercel Deployed?
- [ ] Go to vercel.com → Your Project → Deployments
- [ ] Latest deployment shows ✅ Success
- [ ] Live URL accessible and serving latest code

---

## Common Commands

```bash
# Start Flask locally
python3 app.py

# Run verification script
python3 scripts/verify_integrations.py

# Check if Flask is running
curl http://localhost:5000/health

# Test a community endpoint
curl http://localhost:5000/api/community/ashkenaz | jq

# Check environment variables
grep -E "SUPABASE|CLERK" .env.local

# View Vercel deployments (if vercel CLI installed)
vercel list-deployments
```

---

## 🚨 If Something Breaks

| Issue | Fix |
|-------|-----|
| `CLERK_PUBLISHABLE_KEY missing` | Add it to .env.local: `echo "CLERK_PUBLISHABLE_KEY=pk_test_..." >> .env.local` |
| `Supabase connection refused` | Check `SUPABASE_URL` is correct (check supabase.com dashboard) |
| `401 Unauthorized on API calls` | Check your Clerk token is in Authorization header |
| `Vercel deployment failed` | Check `vercel.com` → Project → Deployments → View Build Logs |
| `Flask won't start` | Check for Python syntax errors: `python3 -m py_compile app.py` |
| `Community customs return 404` | Verify `customs/ashkenaz.json` exists and is valid JSON |

---

## Architecture (30-second version)

```
Browser
   ↓ (Clerk login)
Clerk API
   ↓ (token)
Browser
   ↓ (GET /api/community with token)
Flask Server
   ↓ (validate token)
Flask Server
   ↓ (read customs/*.json + query Supabase)
Database + Local Files
   ↓ (return data)
Flask Server
   ↓ (JSON response)
Browser
   ↓ (display prayer/customs)
User
```

---

## Status Dashboard (Check Daily)

1. **Supabase Dashboard**: https://supabase.com → project → Logs
   - Look for any recent errors
   - Check backup/storage usage

2. **Clerk Dashboard**: https://dashboard.clerk.com
   - Check Users tab for authentications
   - Verify JWT tokens issuing correctly

3. **Vercel Dashboard**: https://vercel.com
   - Check latest deployment status
   - Monitor function duration/analytics

4. **Your App**: https://shelah-app.vercel.app
   - Load a prayer
   - Switch communities
   - Check browser console (F12) for any errors

---

## Next Actions

- [ ] Add CLERK_PUBLISHABLE_KEY to .env.local
- [ ] Re-run `python3 scripts/verify_integrations.py`
- [ ] Verify all checks pass
- [ ] Test in browser at https://shelah-app.vercel.app
- [ ] Done! You now know all services are connected ✨

---

**Last Updated:** April 2026  
**Script Location:** `scripts/verify_integrations.py`  
**Full Docs:** See `INTEGRATION_VERIFICATION.md` and `SERVICE_ARCHITECTURE.md`
