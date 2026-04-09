import requests


def search_wikipedia(title):
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{title.replace(' ', '_')}"
        print("[Wiki Request]", url)

        r = requests.get(url, timeout=10)
        print("[Wiki Status]", r.status_code)

        if r.status_code == 200:
            data = r.json()
            print("[Wiki Found]", data.get("title"))

            return {
                "title": data.get("title", ""),
                "summary": data.get("extract", "")[:300]
            }
    except Exception as e:
        print("[Wiki Error]", e)

    return None


def get_daily_learning():
    """Fetch daily portions using Hebcal API"""
    try:
        url = "https://www.hebcal.com/hebcal?v=1&cfg=json&maj=on&min=on&mod=on&nx=on&year=now&month=now&ss=on&mf=on&c=on&geo=zip&zip=11213"
        response = requests.get(url, timeout=10)
        data = response.json()

        items = data.get('items', [])

        parsha = None
        rambam_portions = []

        for i in items:
            title = i.get('title', '')
            if 'Parashat' in title:
                parsha = title
            elif 'Rambam' in title or 'Chitas' in title:
                rambam_portions.append(title)

        return {
            "parsha": parsha,
            "portions": rambam_portions
        }
    except Exception as e:
        print("[Hebcal Error]", e)
        return {"parsha": None, "portions": []}


def search_halachipedia(query):
    """Search Halachipedia MediaWiki API for relevant articles"""
    try:
        # Search for title
        search_url = f"https://halachipedia.com/api.php?action=query&list=search&srsearch={query}&utf8=&format=json"

        r_search = requests.get(search_url, timeout=10)
        data = r_search.json()

        search_results = data.get("query", {}).get("search", [])
        if not search_results:
            return None

        top_title = search_results[0]["title"]

        # Get intro extract for top article
        extract_url = f"https://halachipedia.com/api.php?action=query&prop=extracts&exsentences=10&exintro=1&explaintext=1&titles={top_title}&format=json"
        r_extract = requests.get(extract_url, timeout=10)
        ext_data = r_extract.json()

        pages = ext_data.get("query", {}).get("pages", {})
        for page_info in pages.values():
            return {
                "title": f"[Halachipedia] {page_info.get('title', '')}",
                "summary": page_info.get("extract", "")[:1000]
            }

        return None
    except Exception as e:
        print(f"[Halachipedia Error] {e}")
        return None
