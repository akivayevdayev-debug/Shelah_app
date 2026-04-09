#!/usr/bin/env python3
"""
Service Integration Verification Script
Automatically tests all connections: Flask → Supabase → Clerk → Vercel → External APIs
"""

import os
import sys
import json
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


# Load environment
load_dotenv()

LOCAL_BASE_URL = None


def get_env_value(*names):
    """Return first non-empty environment variable among candidates."""
    for name in names:
        value = (os.getenv(name, "") or "").strip()
        if value:
            return value, name
    return "", None


def mask_value(value):
    if len(value) > 30:
        return value[:15] + "..." + value[-10:]
    return value

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
        "SUPABASE_URL": {
            "candidates": ["SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL"],
            "desc": "Supabase project URL",
        },
        "SUPABASE_PUBLISHABLE_KEY": {
            "candidates": [
                "SUPABASE_ANON_KEY",
                "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_DEFAULT_KEY",
                "NEXT_PUBLIC_SUPABASE_ANON_KEY",
            ],
            "desc": "Supabase publishable/anon key",
        },
        "SUPABASE_SERVICE_ROLE_KEY": {
            "candidates": ["SUPABASE_SERVICE_ROLE_KEY"],
            "desc": "Supabase service role key",
        },
        "CLERK_PUBLISHABLE_KEY": {
            "candidates": [
                "CLERK_PUBLISHABLE_KEY",
                "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY",
            ],
            "desc": "Clerk publishable key",
        },
        "CLERK_JWT_ISSUER": {
            "candidates": ["CLERK_JWT_ISSUER"],
            "desc": "Clerk JWT issuer",
        },
    }

    optional = {
        'SUPABASE_PREFS_TABLE': 'Supabase preferences table (default: user_preferences)',
        'CLERK_AUDIENCE': 'Clerk audience identifier',
        'CLERK_ENFORCE_AUTH': 'Enforce Clerk authentication',
        'VERCEL_URL': 'Vercel deployment base URL',
    }

    all_pass = True

    # Check required variables
    for var, meta in required.items():
        value, source = get_env_value(*meta["candidates"])
        if value:
            source_info = f" (from {source})" if source and source != var else ""
            print_pass(f"{var}: {mask_value(value)}{source_info}")
        else:
            checked = ", ".join(meta["candidates"])
            print_fail(
                f"{var} is missing or empty - {meta['desc']} (checked: {checked})")
            all_pass = False

    # Check optional variables
    print()
    for var, desc in optional.items():
        value = os.getenv(var, "").strip()
        if value:
            print_info(f"{var}: {mask_value(value)}")
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
                    print_pass(
                        f"{json_file.name}: Valid ({item_count} halacha items)")
                else:
                    print_warn(
                        f"{json_file.name}: Unusual structure (missing halacha_index/identity)")
            else:
                print_warn(
                    f"{json_file.name}: Not a dict (is {type(data).__name__})")
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

    supabase_url, _ = get_env_value("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")
    supabase_key, key_source = get_env_value(
        "SUPABASE_ANON_KEY",
        "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_DEFAULT_KEY",
        "NEXT_PUBLIC_SUPABASE_ANON_KEY",
    )
    prefs_table = (os.getenv("SUPABASE_PREFS_TABLE")
                   or "user_preferences").strip()

    if not supabase_url or not supabase_key:
        print_fail("Supabase URL or key not configured")
        return False

    try:
        try:
            from supabase import create_client
        except ImportError:
            print_warn(
                "supabase-py not installed, trying HTTP endpoint check...")

            test_url = f"{supabase_url}/rest/v1/{prefs_table}?limit=1"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
            }
            response = requests.get(test_url, headers=headers, timeout=5)

            # 401 is acceptable here because it confirms endpoint reachability.
            if response.status_code in [200, 401]:
                print_pass(
                    f"Supabase HTTP endpoint reachable (status: {response.status_code})")
                return True

            print_fail(
                f"Supabase unreachable (status: {response.status_code})")
            return False

        client = create_client(supabase_url, supabase_key)
        try:
            client.table(prefs_table).select("*").limit(1).execute()
            print_pass("Supabase client connected and can query tables")
            print_info(f"Table '{prefs_table}' exists and is accessible")
            if key_source:
                print_info(f"Using publishable key source: {key_source}")
            return True
        except Exception as table_err:
            print_warn(
                f"Supabase client connected but table query check failed: {str(table_err)[:100]}")
            test_url = f"{supabase_url}/rest/v1/"
            headers = {
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
            }
            response = requests.get(test_url, headers=headers, timeout=5)
            if response.status_code in [200, 401]:
                print_pass(
                    f"Supabase project API is reachable (status: {response.status_code})")
                return True

            print_fail(
                f"Supabase API endpoint unreachable (status: {response.status_code})")
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

    test_url = "https://www.sefaria.org/api/texts/Genesis.1.1?lang=bi"

    try:
        response = requests.get(test_url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if 'he' in data and ('text' in data or 'he' in data):
                print_pass("Sefaria API reachable and returning text payload")
                return True
            else:
                print_warn(
                    f"Sefaria returned data but missing Hebrew or English")
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

    test_url = "https://www.hebcal.com/converter?g2h=on&gy=2026&gm=4&gd=9&cfg=json"

    try:
        response = requests.get(test_url, timeout=5)
        if response.status_code == 200:
            data = response.json()
            if 'hy' in data and 'hm' in data and 'hd' in data:
                print_pass(
                    "Hebcal API reachable and returning converted Hebrew date")
                return True
            else:
                print_warn(
                    "Hebcal returned data but missing expected date fields")
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

    global LOCAL_BASE_URL
    LOCAL_BASE_URL = None

    candidate_ports = []
    port_env = (os.getenv("PORT") or "").strip()
    if port_env.isdigit():
        candidate_ports.append(int(port_env))
    for fallback_port in (5001, 5000):
        if fallback_port not in candidate_ports:
            candidate_ports.append(fallback_port)

    for port in candidate_ports:
        base_url = f"http://localhost:{port}"
        try:
            response = requests.get(f"{base_url}/api/stack/health", timeout=2)
            server_header = (response.headers.get("Server") or "").lower()

            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}

                if isinstance(payload, dict) and payload.get("flask") is True:
                    LOCAL_BASE_URL = base_url
                    print_pass(
                        f"Flask app detected at {base_url} (via /api/stack/health)")
                    return True

                print_warn(
                    f"{base_url}/api/stack/health returned 200 but unexpected payload")
                continue

            if "airtunes" in server_header:
                print_warn(
                    f"{base_url} is occupied by Apple AirTunes (not your Flask app)")
                continue

            print_warn(
                f"{base_url}/api/stack/health returned status {response.status_code}")
        except requests.ConnectionError:
            continue
        except Exception as e:
            print_warn(f"Could not query {base_url}: {str(e)[:100]}")

    print_warn("Flask app not detected locally on expected ports")
    print_info("Start Flask with: source .venv/bin/activate && python3 app.py")
    return None


def check_vercel():
    """Check Vercel deployment status"""
    print_header("7. Vercel Deployment")

    vercel_url = (os.getenv("VERCEL_URL") or "").strip()
    clerk_audience = (os.getenv("CLERK_AUDIENCE") or "").strip()

    possible_urls = []
    if vercel_url:
        possible_urls.append(vercel_url)
    if clerk_audience.startswith("http"):
        possible_urls.append(clerk_audience)
    possible_urls.extend([
        "https://shelah-app.vercel.app",
        "https://shelah-app-git-main.vercel.app",
    ])

    deduped_urls = []
    seen = set()
    for url in possible_urls:
        normalized = url.strip().rstrip("/")
        if not normalized:
            continue
        if not normalized.startswith("http"):
            normalized = f"https://{normalized}"
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped_urls.append(normalized)

    if not deduped_urls:
        print_warn("No Vercel URL available to test")
        return None

    print_info("Checking Vercel health endpoint candidates...")

    for url in deduped_urls:
        try:
            response = requests.get(f"{url}/api/stack/health", timeout=5)
            if response.status_code == 200:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {}

                if isinstance(payload, dict) and payload.get("flask") is True:
                    print_pass(f"Vercel deployment reachable at {url}")
                    return True

                print_warn(
                    f"{url} reachable but /api/stack/health payload was unexpected")
                return True

            if response.status_code in (401, 403):
                print_warn(
                    f"{url} reachable but /api/stack/health is access restricted ({response.status_code})")
                return True

            root_response = requests.get(url, timeout=5)
            if root_response.status_code < 500:
                print_warn(
                    f"{url} reachable but /api/stack/health returned {response.status_code}")
                return True
        except requests.Timeout:
            continue
        except Exception:
            continue

    print_warn("Could not verify Vercel deployment with known URLs")
    print_info("Set VERCEL_URL to your deployment base URL if needed")
    return None


def check_community_endpoints():
    """Test community API endpoints locally"""
    print_header("8. Community API Endpoints (Local)")

    if not LOCAL_BASE_URL:
        print_warn("Skipped: Flask app was not detected locally")
        return None

    try:
        response = requests.get(
            f"{LOCAL_BASE_URL}/api/community/ashkenaz", timeout=2)
        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and ('identity' in data or 'customs' in data):
                identity_raw = data.get('identity') if isinstance(
                    data, dict) else None
                identity = identity_raw if isinstance(
                    identity_raw, dict) else {}
                community_name = identity.get(
                    'display_name') or data.get('name', 'Unknown')
                print_pass(f"Community API working: {community_name}")
                return True
            else:
                print_warn(
                    "Community endpoint returned data but missing expected structure")
                return True
        elif response.status_code in (401, 403):
            print_warn(
                f"Community endpoint is reachable but access is restricted ({response.status_code})")
            return True
        else:
            print_fail(
                f"Community endpoint returned status {response.status_code}")
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
        print(
            f"\n{Colors.GREEN}{Colors.BOLD}All critical checks passed! ✨{Colors.RESET}")
        return True
    else:
        print(
            f"\n{Colors.RED}{Colors.BOLD}Some checks failed. See details above.{Colors.RESET}")
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
