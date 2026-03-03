import json, os, time
from datetime import datetime, timedelta
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from webdriver_manager.chrome import ChromeDriverManager
import requests

ARTIST_URL = "https://open.spotify.com/artist/4SiNg3BrvdFycwTlO6HGKN"
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
DATA_FILE = "spotify_data.json"
CHECK_INTERVAL_HOURS = 3  # Must match your cron schedule


def make_driver():
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
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
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def parse_play_count(text):
    clean = (
        text.strip()
        .replace(",", "").replace(".", "")
        .replace("\u202f", "").replace("\xa0", "").replace(" ", "")
    )
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
        print("Waiting 12 seconds for page render...")
        time.sleep(12)

        rows = driver.find_elements(By.CSS_SELECTOR, '[data-testid="tracklist-row"]')
        if not rows:
            rows = driver.find_elements(By.CSS_SELECTOR, '[data-testid="track-row"]')
        if not rows:
            rows = driver.find_elements(By.XPATH, '//div[@aria-rowindex]')
        if not rows:
            rows = driver.find_elements(By.XPATH, '//*[@role="row"]')

        if not rows:
            print("DEBUG:", driver.page_source[:3000])
            return []

        for row in rows[:5]:
            try:
                lines = [l.strip() for l in row.text.split("\n") if l.strip()]
                name, play_count = None, None
                for line in lines:
                    clean = line.replace(",", "").replace(".", "").replace(" ", "")
                    if not clean.isdigit() and len(line) > 1 and not ":" in line:
                        name = line
                        break
                for line in lines:
                    val = parse_play_count(line)
                    if val:
                        play_count = val
                        break
                if name and play_count:
                    tracks.append({"name": name, "count": play_count})
                    print(f"  ✅ {name}: {play_count:,}")
            except Exception as e:
                print(f"  Row error: {e}")
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


def predict_catchup(rank1, rank2, prev_tracks):
    """
    Estimate when rank2 can reach rank1's play count.
    Uses per-check growth rates stored from previous runs.
    Returns a human-readable string.
    """
    name1, count1 = rank1["name"], rank1["count"]
    name2, count2 = rank2["name"], rank2["count"]

    prev1 = prev_tracks.get(name1, {})
    prev2 = prev_tracks.get(name2, {})

    # Need at least one previous data point to calculate growth
    if not prev1 or not prev2:
        return "⏳ **Collecting data...** Need at least 2 checks to predict. Check back in 3 hours."

    # Growth per check interval (every 3 hours)
    growth1 = count1 - prev1.get("count", count1)
    growth2 = count2 - prev2.get("count", count2)

    gap = count1 - count2

    # Format line for Discord
    growth_line = (
        f"  • `#{1}` grows **+{growth1:,}** / 3h\n"
        f"  • `#{2}` grows **+{growth2:,}** / 3h"
    )

    if gap <= 0:
        return f"🏆 **#{2} has already passed #{1}!**\n{growth_line}"

    net_gain_per_check = growth2 - growth1

    if net_gain_per_check <= 0:
        return (
            f"📉 **#{2} is not catching up** at current rates.\n"
            f"{growth_line}\n"
            f"  • Gap: **{gap:,}** plays — widening by **{abs(net_gain_per_check):,}** every 3h"
        )

    checks_needed = gap / net_gain_per_check
    hours_needed = checks_needed * CHECK_INTERVAL_HOURS
    catch_date = datetime.utcnow() + timedelta(hours=hours_needed)

    days = int(hours_needed // 24)
    hours = int(hours_needed % 24)
    time_str = f"{days}d {hours}h" if days > 0 else f"{hours}h"

    return (
        f"🔮 **#{2} could reach #{1} in ~{time_str}**\n"
        f"  • Est. date: **{catch_date.strftime('%b %d, %Y at %H:%M UTC')}**\n"
        f"{growth_line}\n"
        f"  • Gap now: **{gap:,}** plays\n"
        f"  • Closing at: **+{net_gain_per_check:,}** plays / 3h"
    )


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

        # Gap to track above
        gap_str = ""
        if i > 1:
            gap = tracks[i - 2]["count"] - count   # gap to the rank above
            gap_str = f"\n   ↕️ Gap to #{i-1}: **{abs(gap):,}** plays"

        fields.append({
            "name": f"#{i} — {name}",
            "value": (
                f"🔢 **{count:,}** plays\n"
                f"{arrow} **+{diff:,}** since last check"
                f"{gap_str}"
            ),
            "inline": False
        })

    # Divider
    fields.append({
        "name": "─────────────────────",
        "value": "** **",
        "inline": False
    })

    # Prediction: when can #2 reach #1?
    prediction = predict_catchup(tracks[0], tracks[1], prev_tracks)
    fields.append({
        "name": "📊 Can #2 Catch #1?",
        "value": prediction,
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
    payload = {
        "embeds": [{
            "title": "⚠️ Spotify Tracker Error",
            "description": message,
            "color": 15158332,
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
        msg = "Could not extract tracks. Check GitHub Actions logs."
        print(f"❌ {msg}")
        send_error_to_discord(msg)
        exit(1)
