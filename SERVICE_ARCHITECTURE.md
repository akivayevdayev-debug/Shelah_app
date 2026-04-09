# Service Architecture Overview

```mermaid
graph TB
    User["👤 User Browser"]
    
    subgraph Frontend["Frontend (Your Vercel/Local)"]
        HTML["index.html<br/>(Clerk auth, UI)"]
    end
    
    subgraph Backend["Backend (Flask - app.py)"]
        Flask["Flask Server<br/>localhost:5000"]
        Auth["Auth Middleware<br/>(Clerk JWT validation)"]
        Routes["API Routes<br/>(/api/community, etc)"]
    end
    
    subgraph Storage["Data Storage"]
        Supabase["Supabase<br/>(PostgreSQL Database)"]
        LocalJSON["customs/*.json<br/>(Local Files)"]
    end
    
    subgraph External["External APIs"]
        Sefaria["Sefaria API<br/>(Prayer texts)"]
        Hebcal["Hebcal API<br/>(Holiday dates)"]
        Clerk["Clerk Auth<br/>(User verification)"]
    end
    
    subgraph Deployment["Deployment"]
        Vercel["Vercel<br/>(Production hosting)"]
        GitHub["GitHub<br/>(Source code)"]
    end
    
    %% Frontend connections
    User -->|"1. Loads page"| HTML
    HTML -->|"2. Authenticates"| Clerk
    
    %% Frontend to Backend
    HTML -->|"3. API calls<br/>(with Clerk token)"| Flask
    
    %% Backend routing
    Flask -->|"4. Validate JWT"| Auth
    Auth -->|"5. OK/403"| Routes
    
    %% Backend to Storage
    Routes -->|"6a. User prefs"| Supabase
    Routes -->|"6b. Community customs"| LocalJSON
    
    %% Backend to External
    Routes -->|"7a. Fetch prayers"| Sefaria
    Routes -->|"7b. Holiday info"| Hebcal
    
    %% Deployment pipeline
    GitHub -->|"8. Auto-deploy on push"| Vercel
    Vercel -->|"9. Serves frontend"| User
    
    %% Styling
    classDef frontend fill:#4A90E2
    classDef backend fill:#7ED321
    classDef storage fill:#F5A623
    classDef external fill:#BD10E0
    classDef deployment fill:#50E3C2
    
    class Frontend frontend
    class Backend backend
    class Storage storage
    class External external
    class Deployment deployment
```

## Connection Flow Details

### 1️⃣ User Authentication
```
Browser → Clerk (Login) → Clerk Token Created
         ↓
    Token stored in browser
```

### 2️⃣ API Request with Auth
```
HTML page → Click "Load prayer"
         ↓
JS sends: GET /api/community/ashkenaz
         + Header: Authorization: Bearer <clerk_token>
         ↓
Flask receives request
```

### 3️⃣ Backend Validation
```
Flask /api/community/ route
    ↓
Auth middleware validates JWT with Clerk issuer
    ↓
If valid → Continue to route handler
If invalid → Return 401 Unauthorized
```

### 4️⃣ Data Retrieval
```
Route handler executes:
    ↓
Check local customs/*.json files ← Fast (local file system)
    ↓
Optional: Query Supabase for user preferences ← Your database
    ↓
Optional: Fetch from Sefaria API ← External prayer library
    ↓
Return combined response to frontend
```

### 5️⃣ Deployment Pipeline
```
Code change → Push to GitHub
           ↓
GitHub detects push
           ↓
Vercel webhook triggered
           ↓
Vercel rebuilds and deploys
           ↓
Environment variables synced
           ↓
Live app updated at vercel URL
```

---

## Environment Variables Required at Each Stage

### Local Development (.env.local)
```
SUPABASE_URL=https://xyz.supabase.co
SUPABASE_ANON_KEY=eyJhbGc...
SUPABASE_SERVICE_ROLE_KEY=eyJhbGc...
CLERK_PUBLISHABLE_KEY=pk_test_...
CLERK_JWT_ISSUER=https://xyz.clerk.accounts.dev
CLERK_AUDIENCE=https://shelah-app.vercel.app
```

### Vercel Production (Project Settings → Environment Variables)
```
Same variables as above (Vercel copies from .env.local or you set manually)
```

### Note
- Public keys (with `NEXT_PUBLIC_` or just `CLERK_PUBLISHABLE_KEY`) can be exposed in frontend
- Service roles (`SUPABASE_SERVICE_ROLE_KEY`) must stay private (backend only)
- Clerk uses multiple "check all these places" pattern for compatibility

---

## Testing Each Connection

### Test 1: Is Supabase connected?
```bash
python3 -c "
from supabase import create_client
import os
from dotenv import load_dotenv
load_dotenv()
client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_ANON_KEY'))
print('✅ Connected to Supabase' if client else '❌ Failed')
"
```

### Test 2: Is Clerk issuer reachable?
```bash
curl -s https://$(echo $CLERK_JWT_ISSUER | cut -d/ -f3)/.well-known/openid-configuration | jq '.issuer'
# Should return your issuer URL
```

### Test 3: Can Flask start?
```bash
python3 app.py
# Should see: "Running on http://127.0.0.1:5000" (no errors)
```

### Test 4: Can Flask reach Supabase?
```bash
curl http://localhost:5000/api/community/ashkenaz
# Should return: {"identity": {...}, "halacha_index": [...]}
```

### Test 5: Is Vercel deploying?
```bash
# Go to: https://vercel.com → Your Project → Deployments
# Latest deployment should show: ✅ Production (Success)
```

---

## Troubleshooting Connection Issues

| Symptom | Cause | Solution |
|---------|-------|----------|
| `CLERK_PUBLISHABLE_KEY is None` | Missing env var | Add to .env.local and re-run |
| `Supabase connection refused` | Network/URL wrong | Check SUPABASE_URL format and internet |
| `401 Unauthorized` on API calls | Invalid/missing Clerk token | Verify CLERK_JWT_ISSUER matches Clerk dashboard |
| `503 Service Unavailable` on Vercel | Deployment still in progress | Wait 30-60 seconds and refresh |
| Flask returns 403 on local test | CSRF protection active | This is normal, use authenticated requests |

---

Last updated: April 2026
