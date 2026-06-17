# Service Integration Verification Guide

> Sync status (2026-04-21): Verified against current implementation (report-driven library filtering, topbar menu icon layering fix, global warm icon tones, and backup template sync).

## Overview
This guide helps you verify that **Flask**, **Supabase**, **Clerk**, and **Vercel** are all properly connected.

---

## 1. Environment Variables Checklist

### Required Variables (check `.env.local` or Vercel dashboard)

#### Supabase Variables
- [ ] `SUPABASE_URL` - Your Supabase project URL (e.g., `https://xyz.supabase.co`)
- [ ] `SUPABASE_ANON_KEY` - Public anon key from Supabase dashboard
- [ ] `SUPABASE_SERVICE_ROLE_KEY` - Private service role key (backend only)
- [ ] `SUPABASE_PREFS_TABLE` - Table name for user preferences (default: `user_preferences`)

#### Clerk Variables
- [ ] `CLERK_PUBLISHABLE_KEY` - Public key from Clerk dashboard
- [ ] `CLERK_JWT_ISSUER` - Your Clerk issuer URL (e.g., `https://xyz.clerk.accounts.dev`)
- [ ] `CLERK_AUDIENCE` - Audience identifier (usually blank or your app URL)
- [ ] `CLERK_ENFORCE_AUTH` - `true` or `false` to require authentication

#### Vercel Variables
- [ ] Project linked to GitHub repository
- [ ] Environment variables synced to Vercel deployment settings
- [ ] Production and preview environments configured

---

## 2. Local Development Verification

### A. Flask Backend Connection Check

```bash
# 1. Activate virtual environment
source .venv/bin/activate

# 2. Run the test script (see next section)
python scripts/verify_integrations.py

# 3. Manually test endpoints:
curl http://localhost:5000/health
curl http://localhost:5000/api/health
```

**Expected output:**
- No error messages about missing environment variables
- Flask starts without warnings
- `GET /health` returns `{ "status": "ok" }`

### B. Check Supabase Connection

```python
# In Python shell:
from supabase import create_client
import os

url = os.getenv("SUPABASE_URL")
key = os.getenv("SUPABASE_ANON_KEY")
client = create_client(url, key)

# Try a simple query
response = client.table("user_preferences").select("*").limit(1).execute()
print(response)  # Should not raise an error
```

**Expected output:**
- No authentication errors
- Successfully connects to your Supabase project
- Returns empty list or existing data (no connection error)

### C. Check Clerk Integration

```bash
# Verify Clerk JWT with a valid token:
curl -H "Authorization: Bearer <your_clerk_token>" \
  http://localhost:5000/api/user/profile
```

**Expected output:**
- Either `200` with user data (if token valid)
- Or `401 Unauthorized` (if no token/invalid token - this is correct!)
- NOT `500` or missing Clerk errors

### D. Check Sefaria Integration (Third-party API)

```bash
curl https://www.sefaria.org/api/texts/Siddur%20Sefard,Weekday%20Shacharit,Modeh%20Ani

# Should return JSON with prayer text
```

**Expected output:**
- `200` status with prayer data
- Both Hebrew and English text present

---

## 3. Vercel Deployment Verification

### A. Check Deployment Status

1. Go to [vercel.com](https://vercel.com)
2. Select your **Sh'elah_app** project
3. Look for **latest deployment**:
   - Check status (should show ✅ Success)
   - Check **environment variables** section
   - Verify all Supabase/Clerk keys are listed

### B. Test Live Endpoints

```bash
# Replace with your actual Vercel URL
VERCEL_URL="https://shelah-app.vercel.app"

# 1. Health check
curl $VERCEL_URL/health

# 2. Community API
curl $VERCEL_URL/api/community/ashkenaz

# 3. Check logs
curl $VERCEL_URL/api/debug/health  # If endpoint exists
```

**Expected outputs:**
- `200` responses (not `502` or `503`)
- No timeout errors
- Response times under 2 seconds

### C. Verify Environment Variables in Vercel

```bash
# Via Vercel CLI:
vercel env list

# Or manually:
# 1. Go to vercel.com → Project Settings → Environment Variables
# 2. Verify these are set for "Production":
#    - SUPABASE_URL
#    - SUPABASE_ANON_KEY
#    - SUPABASE_SERVICE_ROLE_KEY
#    - CLERK_PUBLISHABLE_KEY
#    - CLERK_JWT_ISSUER
```

---

## 4. Integration Flow Verification

### Flow 1: User Preferences (Supabase)

```
Flask App → Supabase Client → user_preferences table
```

**Test:**
```bash
curl -X POST http://localhost:5000/api/user/preferences \
  -H "Content-Type: application/json" \
  -d '{"fontSize": 16, "community": "ashkenaz"}'
```

**Expected:**
- `200` or `201` response
- Preferences saved in Supabase (check dashboard)
- `GET /api/user/preferences` retrieves saved data

### Flow 2: Clerk Authentication

```
Frontend → Clerk Token → Flask JWT Validation → Protected Route
```

**Test locally:**
```python
# Get a test token from your Clerk dashboard (dev instance)
# Then verify Flask can validate it

import requests
headers = {"Authorization": "Bearer <test_clerk_token>"}
response = requests.get("http://localhost:5000/api/user/profile", headers=headers)
print(response.status_code)  # Should be 200 or 401, not 500
```

**Expected:**
- Valid tokens return `200`
- Invalid tokens return `401` or `403` (not `500`)

### Flow 3: Community Customs (File System)

```
Flask App → customs/*.json files + Supabase → Client
```

**Test:**
```bash
# Should return all 14 communities
curl http://localhost:5000/api/community/ashkenaz | jq '.identity.display_name'

# Try several communities:
for community in ashkenaz sefardic turkish-ottoman-sefardic bukharian; do
  echo "Testing $community..."
  curl -s http://localhost:5000/api/community/$community | jq '.identity.display_name'
done
```

**Expected:**
- All requests return `200`
- Each returns correct community name and practices

---

## 5. Connectivity Matrix

| Service | Connection | Status | How to Verify |
|---------|-----------|--------|---------------|
| **Flask** | N/A | Local | `python app.py` - should start without errors |
| **Supabase** | Flask → Supabase | Remote | `curl http://localhost:5000/api/community/ashkenaz` |
| **Clerk** | Frontend → Clerk | Remote | Check Clerk dashboard for active users/tokens |
| **Vercel** | GitHub → Vercel | Live | Check vercel.com dashboard for successful deployments |
| **Sefaria** | Flask → Sefaria API | Remote | `curl https://www.sefaria.org/api/texts/...` |
| **Hebcal** | Flask → Hebcal API | Remote | Check backend/calendar_service.py calls |

---

## 6. Common Issues & Diagnostics

### Issue: `SUPABASE_URL is empty`
**Solution:** Check `.env.local` file exists and has `SUPABASE_URL=...` line
```bash
# Quick check:
grep SUPABASE_URL .env.local
echo $SUPABASE_URL  # Should print URL, not empty
```

### Issue: `Supabase connection refused`
**Solution:** Verify URL is correct and network is connected
```bash
# Test if Supabase is reachable:
curl https://your-project.supabase.co/rest/v1/
# Should return {"error":"Invalid API key"} or {"message":...}, not connection error
```

### Issue: `Clerk JWT validation failed`
**Solution:** Verify JWT issuer matches your Clerk domain
```python
# Check what Clerk says:
print(f"Issuer from env: {os.getenv('CLERK_JWT_ISSUER')}")
# Should match your Clerk dashboard URL
```

### Issue: `Vercel deployment failed`
**Solution:** Check deployment logs
```bash
# Via CLI:
vercel logs --follow

# Or manually:
# 1. Go to vercel.com → Project → latest deployment
# 2. Click "View Build Logs" tab
# 3. Look for error messages
```

### Issue: Community customs return 404
**Solution:** Check if custom JSON file exists
```bash
# List all custom files:
ls -la customs/*.json

# Verify file is formatted correctly:
jq empty customs/ashkenaz.json
# Should return no output if valid JSON
```

---

## 7. Automated Testing

Run the provided test script:

```bash
python scripts/verify_integrations.py
```

This will:
- ✅ Check all environment variables are set
- ✅ Test Flask local connection
- ✅ Test Supabase connectivity
- ✅ Test Clerk JWT validation (if token provided)
- ✅ Test JSON customs file integrity
- ✅ Test Sefaria API responsiveness
- ✅ Check Vercel deployment status (if deployed)

---

## 8. Quick Health Check Command

```bash
# All-in-one verification:
python -c "
import os
from dotenv import load_dotenv
load_dotenv()

checks = {
    'SUPABASE_URL': os.getenv('SUPABASE_URL'),
    'SUPABASE_ANON_KEY': bool(os.getenv('SUPABASE_ANON_KEY')),
    'CLERK_PUBLISHABLE_KEY': bool(os.getenv('CLERK_PUBLISHABLE_KEY')),
    'CLERK_JWT_ISSUER': os.getenv('CLERK_JWT_ISSUER'),
}

for key, val in checks.items():
    status = '✅' if val else '❌'
    print(f'{status} {key}: {str(val)[:50]}...' if isinstance(val, str) and len(str(val)) > 50 else f'{status} {key}: {val}')
"
```

---

## 9. Next Steps

After verifying all connections:
1. ✅ Confirm local dev works (all endpoints return 200)
2. ✅ Push code to GitHub
3. ✅ Verify Vercel re-deploys automatically
4. ✅ Test live endpoints on your Vercel URL
5. ✅ Check Supabase dashboard for any recent activity/errors
6. ✅ Monitor Clerk dashboard for authentication events

---

**Last updated:** April 2026  
**Script:** `scripts/verify_integrations.py`
