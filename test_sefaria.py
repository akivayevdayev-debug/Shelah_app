import requests
url = "https://www.sefaria.org/api/index"
headers = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,he;q=0.8",
    "Referer": "https://www.sefaria.org/",
    "Origin": "https://www.sefaria.org",
}
r = requests.get(url, headers=headers)
print(r.status_code)
