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

VN_TZ = timedelta(hours=7)

def format_time():
    utc = datetime.utcnow()
    vn  = utc + VN_TZ
    return f"{utc.strftime('%H:%M UTC')} ({vn.strftime('%H:%M ICT, %b %d')})"

def today_utc():
    return datetime.utcnow().strftime("%Y-%m-%d")


# ─── DRIVER ──────────────────────────────────────────────────

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


# ─── SCRAPER ─────────────────────────────────────────────────

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
                    if not clean.isdigit() and len(line) > 1 and ":" not in line:
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


# ─── DATA HELPERS ────────────────────────────────────────────

def load_data():
    with open(DATA_FILE, "r") as f:
        return json.load(f)


def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_daily_increase(track_name, current_count, prev_tracks):
    prev = prev_tracks.get(track_name, {})
    day_start_count = prev.get("day_start_count")
    day_start_date  = prev.get("day_start_date")
    if day_start_date != today_utc() or day_start_count is None:
        return None
    return current_count - day_start_count


def calc_rate(track_name, current_count, prev_tracks):
    prev = prev_tracks.get(track_name)
    if not prev:
        return None
    prev_count      = prev.get("count", 0)
    prev_changed_at = prev.get("last_changed_at")
    if not prev_changed_at or prev_count == current_count:
        return None
    diff = current_count - prev_count
    if diff <= 0:
        return None
    hours_elapsed = (datetime.utcnow() - datetime.fromisoformat(prev_changed_at)).total_seconds() / 3600
    if hours_elapsed < 0.1:
        return None
    return diff / hours_elapsed


def has_any_change(tracks, prev_tracks):
    for t in tracks:
        if prev_tracks.get(t["name"], {}).get("count", 0) != t["count"]:
            return True
    return False


def build_updated_tracks(tracks, prev_tracks, now_iso):
    updated = {}
    for t in tracks:
        name  = t["name"]
        count = t["count"]
        prev  = prev_tracks.get(name, {})
        prev_count = prev.get("count", 0)

        last_changed_at = now_iso if count != prev_count else prev.get("last_changed_at", now_iso)

        if prev.get("day_start_date") != today_utc():
            day_start_count = prev_count if prev_count else count
            day_start_date  = today_utc()
        else:
            day_start_count = prev.get("day_start_count", count)
            day_start_date  = prev.get("day_start_date")

        updated[name] = {
            "count":           count,
            "last_changed_at": last_changed_at,
            "day_start_count": day_start_count,
            "day_start_date":  day_start_date
        }
    return updated


# ─── PREDICTION ──────────────────────────────────────────────

def predict_catchup(rank1, rank2, prev_tracks):
    name1, count1 = rank1["name"], rank1["count"]
    name2, count2 = rank2["name"], rank2["count"]
    rate1 = calc_rate(name1, count1, prev_tracks)
    rate2 = calc_rate(name2, count2, prev_tracks)
    gap   = count1 - count2

    def rate_str(rate, rank):
        if rate is None:
            return f"  • `#{rank}` rate: **no change detected yet**"
        return f"  • `#{rank}` grows **+{rate:,.1f}** plays/hr"

    rate_lines = f"{rate_str(rate1, 1)}\n{rate_str(rate2, 2)}"

    if gap <= 0:
        return f"🏆 **#2 has already passed #1!**\n{rate_lines}"

    if rate1 is None or rate2 is None:
        return (
            f"⏳ **Waiting for Spotify to update counts...**\n"
            f"  • Gap: **{gap:,}** plays\n"
            f"{rate_lines}"
        )

    net_gain_per_hour = rate2 - rate1
    if net_gain_per_hour <= 0:
        return (
            f"📉 **#2 is not catching up at current rates**\n"
            f"  • Gap: **{gap:,}** plays — widening **{abs(net_gain_per_hour):,.1f}** plays/hr\n"
            f"{rate_lines}"
        )

    hours_needed   = gap / net_gain_per_hour
    catch_utc      = datetime.utcnow() + timedelta(hours=hours_needed)
    catch_vn       = catch_utc + VN_TZ                          # ← ICT version
    days           = int(hours_needed // 24)
    hrs            = int(hours_needed % 24)
    time_str       = f"{days}d {hrs}h" if days > 0 else f"{int(hours_needed)}h {int((hours_needed % 1)*60)}m"

    return (
        f"🔮 **#2 could catch #1 in ~{time_str}**\n"
        f"  • Est. date: **{catch_utc.strftime('%b %d, %Y %H:%M UTC')}**"
        f" ({catch_vn.strftime('%H:%M ICT')})\n"      # ← shows both timezones
        f"{rate_lines}\n"
        f"  • Gap: **{gap:,}** plays\n"
        f"  • Closing at: **+{net_gain_per_hour:,.1f}** plays/hr"
    )


# ─── DISCORD ─────────────────────────────────────────────────

def send_to_discord(tracks, prev_data):
    prev_tracks = prev_data.get("tracks", {})
    now_str     = format_time()                         # ← UTC + ICT
    prev_run    = prev_data.get("last_run", "Never")

    fields = []
    for i, track in enumerate(tracks, 1):
        name  = track["name"]
        count = track["count"]
        prev_count = prev_tracks.get(name, {}).get("count", 0)
        diff  = count - prev_count
        arrow = "📈" if diff > 0 else "➡️"

        gap_str = ""
        if i > 1:
            gap     = tracks[i - 2]["count"] - count
            gap_str = f"\n   ↕️ Gap to #{i-1}: **{abs(gap):,}** plays"

        rate     = calc_rate(name, count, prev_tracks)
        rate_str = f"\n   ⚡ Rate: **{rate:,.1f}** plays/hr" if rate else ""

        daily = get_daily_increase(name, count, prev_tracks)
        if daily is None:
            daily_str = f"\n   📅 Today (ICT): **calculating...** (resets 07:00 ICT)"
        elif daily == 0:
            daily_str = f"\n   📅 Today (ICT): **no plays yet today**"
        else:
            daily_str = f"\n   📅 Today (ICT): **+{daily:,}** plays so far"

        fields.append({
            "name": f"#{i} — {name}",
            "value": (
                f"🔢 **{count:,}** total plays\n"
                f"{arrow} **+{diff:,}** since last update"
                f"{daily_str}"
                f"{gap_str}"
                f"{rate_str}"
            ),
            "inline": False
        })

    fields.append({"name": "─────────────────────", "value": "** **", "inline": False})
    fields.append({
        "name":  "📊 Can #2 Catch #1?",
        "value": predict_catchup(tracks[0], tracks[1], prev_tracks),
        "inline": False
    })
    fields.append({"name": "🕐 Updated At", "value": now_str,  "inline": True})  # ← UTC + ICT
    fields.append({"name": "📋 Prev Check", "value": prev_run, "inline": True})

    payload = {
        "embeds": [{
            "title":  "🎵 Top 5 Track Play Counts Update",
            "url":    ARTIST_URL,
            "color":  1947988,
            "fields": fields,
            "footer": {"text": "Only updates when counts change • Daily resets at 00:00 UTC (07:00 ICT)"}
        }]
    }
    r = requests.post(DISCORD_WEBHOOK, json=payload)
    print(f"Discord status: {r.status_code}")


def send_error_to_discord(message):
    payload = {
        "embeds": [{
            "title":       "⚠️ Spotify Tracker Error",
            "description": message,
            "color":       15158332,
            "footer":      {"text": format_time()}      # ← UTC + ICT
        }]
    }
    requests.post(DISCORD_WEBHOOK, json=payload)


# ─── MAIN ────────────────────────────────────────────────────

if __name__ == "__main__":
    now_iso = datetime.utcnow().isoformat()
    print(f"[{now_iso}] Starting tracker...")

    data        = load_data()
    prev_tracks = data.get("tracks", {})
    tracks      = get_top_tracks()

    if not tracks:
        msg = "Could not extract tracks. Check GitHub Actions logs."
        print(f"❌ {msg}")
        send_error_to_discord(msg)
        exit(1)

    if not has_any_change(tracks, prev_tracks):
        print("⏭️  No change detected — skipping Discord message.")
        data["last_run"] = format_time()                # ← UTC + ICT
        data["tracks"]   = build_updated_tracks(tracks, prev_tracks, now_iso)
        save_data(data)
        exit(0)

    print("✅ Changes detected — sending to Discord...")
    send_to_discord(tracks, data)

    data["last_run"] = format_time()                    # ← UTC + ICT
    data["tracks"]   = build_updated_tracks(tracks, prev_tracks, now_iso)
    save_data(data)
    print("Data saved. ✅")
