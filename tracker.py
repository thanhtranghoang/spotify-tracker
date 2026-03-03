import json, os, time
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import requests

ARTIST_URL = "https://open.spotify.com/artist/4SiNg3BrvdFycwTlO6HGKN"
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
DATA_FILE = "spotify_data.json"


def make_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")          # New headless mode, less detectable
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    )
    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=options
    )
    # Patch navigator.webdriver to False to avoid bot detection
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
    return driver


def parse_play_count(text):
    """Convert a string like '1,234,567' or '1.234.567' to int."""
    clean = text.strip().replace(",", "").replace(".", "").replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    if clean.isdigit():
        val = int(clean)
        if 10_000 < val < 10_000_000_000:
            return val
    return None


def get_top_tracks():
    driver = make_driver()
    tracks = []

    try:
        driver.get(ARTIST_URL)
        print("Page loading... waiting 12 seconds for JS to render")
        time.sleep(12)  # Give Spotify's React app time to fully render

        page_source_len = len(driver.page_source)
        print(f"Page source length: {page_source_len} chars")

        # ── Selector Attempt 1: data-testid="tracklist-row" ──────────────
        rows = driver.find_elements(By.CSS_SELECTOR, '[data-testid="tracklist-row"]')
        print(f"Attempt 1 (tracklist-row): found {len(rows)} rows")

        # ── Selector Attempt 2: data-testid="track-row" ──────────────────
        if not rows:
            rows = driver.find_elements(By.CSS_SELECTOR, '[data-testid="track-row"]')
            print(f"Attempt 2 (track-row): found {len(rows)} rows")

        # ── Selector Attempt 3: aria-rowindex on divs ─────────────────────
        if not rows:
            rows = driver.find_elements(By.XPATH, '//div[@aria-rowindex]')
            print(f"Attempt 3 (aria-rowindex): found {len(rows)} rows")

        # ── Selector Attempt 4: role="row" ───────────────────────────────
        if not rows:
            rows = driver.find_elements(By.XPATH, '//*[@role="row"]')
            print(f"Attempt 4 (role=row): found {len(rows)} rows")

        if not rows:
            # Print a snippet of the page source for debugging
            print("DEBUG: First 3000 chars of page source:")
            print(driver.page_source[:3000])
            return []

        for row in rows[:5]:
            try:
                lines = [l.strip() for l in row.text.split("\n") if l.strip()]
                print(f"  Row text lines: {lines}")

                name = None
                play_count = None

                # First non-numeric, non-empty line is usually the track name
                for line in lines:
                    clean = line.replace(",", "").replace(".", "").replace(" ", "")
                    if not clean.isdigit() and len(line) > 1:
                        name = line
                        break

                # Find the play count from all lines
                for line in lines:
                    val = parse_play_count(line)
                    if val:
                        play_count = val
                        break

                if name and play_count:
                    tracks.append({"name": name, "count": play_count})
                    print(f"  ✅ {name}: {play_count:,}")
                else:
                    print(f"  ⚠️ Could not parse — name={name}, count={play_count}")

            except Exception as e:
                print(f"  Row error: {e}")
                continue

    except Exception as e:
        print(f"Driver error: {e}")
    finally:
        driver.quit()

    return tracks


def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def send_to_discord(tracks, prev_data):
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    prev_tracks = prev_data.get("tracks", {})
    prev_run = prev_data.get("last_run", "Never")

    fields = []
    for i, track in enumerate(tracks, 1):
        name = track["name"]
        count = track["count"]
        prev_count = prev_tracks.get(name, {}).get("count", 0)
        diff = count - prev_count if prev_count else 0
        arrow = "📈" if diff > 0 else "➡️"

        fields.append({
            "name": f"#{i} — {name}",
            "value": (
                f"🔢 **{count:,}** plays\n"
                f"{arrow} **+{diff:,}** since last check"
            ),
            "inline": False
        })

    fields.append({"name": "🕐 Checked At", "value": now, "inline": True})
    fields.append({"name": "📊 Previous Check", "value": prev_run, "inline": True})

    payload = {
        "embeds": [{
            "title": "🎵 Top 5 Track Play Counts Update",
            "url": ARTIST_URL,
            "color": 1947988,
            "fields": fields,
            "footer": {"text": "Updates every 3 hours via GitHub Actions"}
        }]
    }
    r = requests.post(DISCORD_WEBHOOK, json=payload)
    print(f"Discord status: {r.status_code}")


def send_error_to_discord(message):
    """Send an alert to Discord if scraping fails, so you know immediately."""
    payload = {
        "embeds": [{
            "title": "⚠️ Spotify Tracker Error",
            "description": message,
            "color": 15158332,  # Red
            "footer": {"text": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
        }]
    }
    requests.post(DISCORD_WEBHOOK, json=payload)


# ─── MAIN ────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[{datetime.utcnow()}] Starting top 5 tracker...")

    data = load_data()
    tracks = get_top_tracks()

    if tracks:
        print(f"\nSuccessfully found {len(tracks)} tracks.")
        send_to_discord(tracks, data)
        data["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        data["tracks"] = {t["name"]: {"count": t["count"]} for t in tracks}
        save_data(data)
        print("Data saved. ✅")
    else:
        msg = "Could not extract any tracks from the Spotify artist page. The page layout may have changed — check GitHub Actions logs."
        print(f"❌ {msg}")
        send_error_to_discord(msg)   # You'll get a red alert in Discord
        exit(1)
