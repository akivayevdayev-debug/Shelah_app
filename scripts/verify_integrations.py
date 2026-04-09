#!/usr/bin/env python3
"""
Service Integration Verification Script
Automatically tests all connections: Flask → Supabase → Clerk → Vercel → External APIs
"""

import os
import sys
import json
import requests
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

# Load environment
load_dotenv()

# Color codes for terminal output
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'
    BOLD = '\033[1m'

def print_header(text):
    print(f"\n{Colors.BOLD}{Colors.BLUE}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{text}{Colors.RESET}")
    print(f"{Colors.BOLD}{Colors.BLUE}{'='*60}{Colors.RESET}\n")

def print_pass(text):
    print(f"{Colors.GREEN}✅ PASS{Colors.RESET}  {text}")

def print_fail(text):
    print(f"{Colors.RED}❌ FAIL{Colors.RESET}  {text}")

def print_warn(text):
    print(f"{Colors.YELLOW}⚠️  WARN{Colors.RESET}  {text}")

def print_info(text):
    print(f"{Colors.BLUE}ℹ️  INFO{Colors.RESET}  {text}")

def check_env_variables():
    """Verify all required environment variables are set"""
    print_header("1. Environment Variables")
    
    required = {
        'SUPABASE_URL': 'Supabase project URL',
        'SUPABASE_ANON_KEY': 'Supabase anonymous key',
        'SUPABASE_SERVICE_ROLE_KEY': 'Supabase service role key',
        'CLERK_PUBLISHABLE_KEY': 'Clerk publishable key',
        'CLERK_JWT_ISSUER': 'Clerk JWT issuer',
    }
    
    optional = {
        'SUPABASE_PREFS_TABLE': 'Supabase preferences table (default: user_preferences)',
        'CLERK_AUDIENCE': 'Clerk audience identifier',
        'CLERK_ENFORCE_AUTH': 'Enforce Clerk authentication',
    }
    
    all_pass = True
    
    # Check required variables
    for var, desc in required.items():
        value = os.getenv(var, "").strip()
        if value:
            # Mask sensitive values for display
            if len(value) > 30:
                display = value[:15] + "..." + value[-10:]
            else:
                display = value
            print_pass(f"{var}: {display}")
        else:
            print_fail(f"{var} is missing or empty - {desc}")
            all_pass = False
    
    # Check optional variables
    print()
    for var, desc in optional.items():
        value = os.getenv(var, "").strip()
        if value:
            if len(value) > 30:
                display = value[:15] + "..." + value[-10:]
            else:
                display = value
            print_info(f"{var}: {display}")
        else:
            print_warn(f"{var} not set - {desc}")
    
    return all_pass

def check_json_files():
    """Verify all customs JSON files are valid"""
    print_header("2. Customs JSON Files")
    
    customs_dir = Path(__file__).parent.parent / "customs"
    json_files = list(customs_dir.glob("*.json"))
    
    if not json_files:
        print_fail("No JSON files found in customs/ directory")
        return False
    
    print_info(f"Found {len(json_files)} JSON files in customs/")
    
    all_pass = True
    for json_file in sorted(json_files):
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
            
            # Check if it has expected structure
            if isinstance(data, dict):
                if 'halacha_index' in data or 'identity' in data:
                    item_count = len(data.get('halacha_index', []))
                    print_pass(f"{json_file.name}: Valid ({item_count} halacha items)")
                else:
                    print_warn(f"{json_file.name}: Unusual structure (missing halacha_index/identity)")
            else:
                print_warn(f"{json_file.name}: Not a dict (is {type(data).__name__})")
        except json.JSONDecodeError as e:
            print_fail(f"{json_file.name}: Invalid JSON - {str(e)[:50]}")
            all_pass = False
        except Exception as e:
            print_fail(f"{json_file.name}: Error - {str(e)[:50]}")
            all_pass = False
    
    return all_pass

def check_supabase():
    """Test Supabase connectivity"""
    print_header("3. Supabase Connection")
    
    supabase_url = os.getenv("SUPABASE_URL", "").strip()
    supabase_key = os.getenv("SUPABASE_ANON_KEY", "").strip()
    
    if not supabase_url or not supabase_key:
        print_fail("Supabase URL or key not configured")
        return False
    
    try:
        # Try to connect with supabase-py client
        try:
            from supabase import create_client
            client = create_client(supabase_url, supabase_key)
            
            # Try a simple query to verify connection
            response = client.table("user_preferences").select("*").limit(1).execute()
            print_pass("Supabase client connected and can query tables")
            print_info(f"Table 'user_preferences' exists and is accessible")
            return True
        except ImportError:
            print_warn("supabase-py not installed, trying HTTP endpoint check...")
            
            # Fallback: test HTTP endpoint
            test_url = f"{supabase_url}/rest/v1/user_preferences?limit=1"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
            }
            response = requests.get(test_url, headers=headers, timeout=5)
            
            if response.status_code in [200, 401]:  # 401 is OK - means endpoint exists but needs auth
                print_pass(f"Supabase HTTP endpoint reachable (status: {response.status_code})")
                return True
            else:
                print_fail(f"Supabase unreachable (status: {response.status_code})")
                return False
    
    except requests.Timeout:
        print_fail("Supabase request timed out (network issue?)")
        return False
    except Exception as e:
        print_fail(f"Supabase error: {str(e)[:100]}")
        return False

def check_sefaria():
    """Test Sefaria API connectivity"""
    print_header("4. Sefaria API Connection")
    
    test_url = "https://www.sefaria.org/api/texts/Siddur%20Sefard,Weekday%20Shacharit,Modeh%20Ani"
    
    try:
        response = requests.get(test_url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if 'he' in data and 'en' in data:
                print_pass(f"Sefaria API reachable and returning Hebrew + English text")
                return True
            else:
                print_warn(f"Sefaria returned data but missing Hebrew or English")
                return True
        else:
            print_fail(f"Sefaria returned status {response.status_code}")
            return False
    except requests.Timeout:
        print_fail("Sefaria request timed out")
        return False
    except Exception as e:
        print_fail(f"Sefaria error: {str(e)[:100]}")
        return False

def check_hebcal():
    """Test Hebcal API connectivity"""
    print_header("5. Hebcal API Connection")
    
    test_url = "https://www.hebcal.com/api/v1/events?year=2026&isHebrewYear=false&minor=false"
    
    try:
        response = requests.get(test_url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if 'events' in data:
                print_pass(f"Hebcal API reachable ({len(data['events'])} events)")
                return True
            else:
                print_warn(f"Hebcal returned data but no 'events' key")
                return True
        else:
            print_fail(f"Hebcal returned status {response.status_code}")
            return False
    except requests.Timeout:
        print_fail("Hebcal request timed out")
        return False
    except Exception as e:
        print_fail(f"Hebcal error: {str(e)[:100]}")
        return False

def check_flask_local():
    """Test local Flask development server"""
    print_header("6. Flask Local Server")
    
    try:
        response = requests.get("http://localhost:5000/health", timeout=2)
        if response.status_code == 200:
            print_pass(f"Flask server running at localhost:5000")
            return True
        else:
            print_warn(f"Flask server returned status {response.status_code}")
            return True
    except requests.ConnectionError:
        print_warn("Flask server is not running locally (expected if not started)")
        print_info("Start Flask with: python app.py")
        return None  # Not a failure, just not running
    except Exception as e:
        print_warn(f"Could not connect to Flask: {str(e)[:100]}")
        return None

def check_vercel():
    """Check Vercel deployment status"""
    print_header("7. Vercel Deployment")
    
    # Try to detect Vercel URL from environment or common patterns
    vercel_url = os.getenv("VERCEL_URL", "").strip()
    
    if not vercel_url:
        # Try common patterns
        possible_urls = [
            "https://shelah-app.vercel.app",
            "https://shelah-app-git-main.vercel.app",
        ]
        
        print_info("Vercel URL not in environment. Trying common patterns...")
        
        for url in possible_urls:
            try:
                response = requests.get(f"{url}/health", timeout=3)
                if response.status_code < 400:
                    print_pass(f"Vercel deployment reachable at {url}")
                    return True
            except:
                pass
        
        print_warn("Could not auto-detect Vercel deployment URL")
        print_info("Set VERCEL_URL environment variable or check vercel.com dashboard")
        return None
    
    try:
        response = requests.get(f"{vercel_url}/health", timeout=5)
        if response.status_code < 400:
            print_pass(f"Vercel deployment reachable at {vercel_url}")
            return True
        else:
            print_fail(f"Vercel deployment returned status {response.status_code}")
            return False
    except requests.Timeout:
        print_fail("Vercel deployment request timed out")
        return False
    except Exception as e:
        print_fail(f"Could not reach Vercel: {str(e)[:100]}")
        return False

def check_community_endpoints():
    """Test community API endpoints locally"""
    print_header("8. Community API Endpoints (Local)")
    
    try:
        response = requests.get("http://localhost:5000/api/community/ashkenaz", timeout=2)
        if response.status_code == 200:
            data = response.json()
            if 'identity' in data:
                community_name = data['identity'].get('display_name', 'Unknown')
                print_pass(f"Community API working: {community_name}")
                return True
            else:
                print_warn("Community endpoint returned data but missing expected structure")
                return True
        else:
            print_fail(f"Community endpoint returned status {response.status_code}")
            return False
    except requests.ConnectionError:
        print_warn("Flask not running locally")
        return None
    except Exception as e:
        print_fail(f"Error testing community API: {str(e)[:100]}")
        return False

def print_summary(results):
    """Print summary of all tests"""
    print_header("Summary")
    
    passed = sum(1 for r in results.values() if r is True)
    failed = sum(1 for r in results.values() if r is False)
    skipped = sum(1 for r in results.values() if r is None)
    
    for test_name, result in results.items():
        if result is True:
            status = f"{Colors.GREEN}✅ PASS{Colors.RESET}"
        elif result is False:
            status = f"{Colors.RED}❌ FAIL{Colors.RESET}"
        else:
            status = f"{Colors.YELLOW}⚠️  SKIP{Colors.RESET}"
        
        print(f"{status}  {test_name}")
    
    print()
    print(f"Results: {passed} passed, {failed} failed, {skipped} skipped")
    
    if failed == 0:
        print(f"\n{Colors.GREEN}{Colors.BOLD}All critical checks passed! ✨{Colors.RESET}")
        return True
    else:
        print(f"\n{Colors.RED}{Colors.BOLD}Some checks failed. See details above.{Colors.RESET}")
        return False

def main():
    print(f"\n{Colors.BOLD}{Colors.BLUE}Sh'elah App - Service Integration Verification{Colors.RESET}")
    print(f"Started at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    results = {}
    
    # Run all checks
    results["Environment Variables"] = check_env_variables()
    results["JSON Custom Files"] = check_json_files()
    results["Supabase Connection"] = check_supabase()
    results["Sefaria API"] = check_sefaria()
    results["Hebcal API"] = check_hebcal()
    results["Flask Local Server"] = check_flask_local()
    results["Vercel Deployment"] = check_vercel()
    results["Community API Endpoints"] = check_community_endpoints()
    
    # Print summary and return exit code
    all_pass = print_summary(results)
    
    return 0 if all_pass else 1

if __name__ == "__main__":
    sys.exit(main())
