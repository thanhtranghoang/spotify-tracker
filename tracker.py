import json, os, time
from datetime import datetime
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager
import requests

ARTIST_URL = "https://open.spotify.com/artist/4SiNg3BrvdFycwTlO6HGKN"
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
DATA_FILE = "spotify_data.json"


def get_top_tracks():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    driver = webdriver.Chrome(
        service=Service(ChromeDriverManager().install()), options=options
    )

    tracks = []
    try:
        driver.get(ARTIST_URL)
        # Wait until track rows are visible on the page
        WebDriverWait(driver, 15).until(
            EC.presence_of_all_elements_located(
                (By.CSS_SELECTOR, '[data-testid="track-row"]')
            )
        )
        time.sleep(3)  # Extra buffer for play counts to load

        rows = driver.find_elements(By.CSS_SELECTOR, '[data-testid="track-row"]')

        for row in rows[:5]:  # Top 5 only
            try:
                # Track name
                name_el = row.find_element(
                    By.CSS_SELECTOR, '[data-testid="internal-track-link"] div'
                )
                name = name_el.text.strip()

                # Play count — shown as a formatted number in the row
                # It's typically the last meaningful text column
                cols = row.find_elements(By.XPATH, ".//*[not(*)]")
                play_count = None
                for col in cols:
                    text = col.text.strip().replace(",", "").replace(".", "").replace("\u202f", "")
                    if text.isdigit() and len(text) >= 5:
                        candidate = int(text)
                        if 10_000 < candidate < 10_000_000_000:
                            play_count = candidate
                            break

                if name and play_count:
                    tracks.append({"name": name, "count": play_count})

            except Exception as e:
                print(f"Row parse error: {e}")
                continue

        # Fallback: if CSS selector fails due to Spotify layout change,
        # try aria-rowindex rows
        if not tracks:
            print("Primary selector failed, trying fallback...")
            rows = driver.find_elements(By.XPATH, '//div[@aria-rowindex]')
            for row in rows[:5]:
                try:
                    all_text = row.text.split("\n")
                    name = all_text[0].strip() if all_text else None
                    play_count = None
                    for part in all_text:
                        clean = part.replace(",", "").replace(".", "").replace(" ", "")
                        if clean.isdigit() and 10_000 < int(clean) < 10_000_000_000:
                            play_count = int(clean)
                    if name and play_count:
                        tracks.append({"name": name, "count": play_count})
                except Exception:
                    continue

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

    fields.append({
        "name": "🕐 Checked At",
        "value": now,
        "inline": True
    })
    fields.append({
        "name": "📊 Previous Check",
        "value": prev_run,
        "inline": True
    })

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


# ─── MAIN ────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"[{datetime.utcnow()}] Starting top 5 tracker...")

    data = load_data()
    tracks = get_top_tracks()

    if tracks:
        print(f"Found {len(tracks)} tracks:")
        for t in tracks:
            print(f"  {t['name']}: {t['count']:,}")

        send_to_discord(tracks, data)

        # Save new counts
        data["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        data["tracks"] = {t["name"]: {"count": t["count"]} for t in tracks}
        save_data(data)
        print("Done! Data saved.")
    else:
        print("❌ No tracks found. Spotify may have changed its layout.")
