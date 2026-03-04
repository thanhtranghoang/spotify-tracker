import json, os
from datetime import datetime, timedelta
import requests
from googleapiclient.discovery import build

YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
DISCORD_WEBHOOK = os.environ["DISCORD_WEBHOOK"]
YT_DATA_FILE = "youtube_data.json"

VIDEO_IDS = [
    "1Trlr6fWn-Y",
    "XZXq5lP7xCk",
    "Iy_qH9EvuJY"
]


# ─── DATA HELPERS ────────────────────────────────────────────

def load_data():
    if os.path.exists(YT_DATA_FILE):
        with open(YT_DATA_FILE) as f:
            return json.load(f)
    return {"last_run": "Never", "videos": {}}


def save_data(data):
    with open(YT_DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def today_utc():
    return datetime.utcnow().strftime("%Y-%m-%d")


# ─── YOUTUBE API FETCH ───────────────────────────────────────

def fetch_video_stats(video_ids):
    """
    Fetch title, viewCount, likeCount, commentCount for all video IDs
    in a single API call (quota-efficient).
    """
    youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
    response = youtube.videos().list(
        part="snippet,statistics",
        id=",".join(video_ids)
    ).execute()

    videos = []
    for item in response.get("items", []):
        vid_id = item["id"]
        title = item["snippet"]["title"]
        stats = item.get("statistics", {})
        views    = int(stats.get("viewCount", 0))
        likes    = int(stats.get("likeCount", 0))
        comments = int(stats.get("commentCount", 0))
        url = f"https://www.youtube.com/watch?v={vid_id}"
        videos.append({
            "id": vid_id,
            "title": title,
            "url": url,
            "views": views,
            "likes": likes,
            "comments": comments
        })

    # Sort by views descending (rank #1 = most viewed)
    videos.sort(key=lambda x: x["views"], reverse=True)
    return videos


# ─── RATE & DAILY HELPERS ────────────────────────────────────

def calc_rate(vid_id, current_val, key, prev_videos):
    """Plays/hour for any metric based on real elapsed time since last change."""
    prev = prev_videos.get(vid_id, {})
    prev_val = prev.get(key, 0)
    prev_changed_at = prev.get(f"{key}_changed_at")
    if not prev_changed_at or prev_val == current_val:
        return None
    diff = current_val - prev_val
    if diff <= 0:
        return None
    hours = (datetime.utcnow() - datetime.fromisoformat(prev_changed_at)).total_seconds() / 3600
    if hours < 0.1:
        return None
    return diff / hours


def get_daily(vid_id, current_val, key, prev_videos):
    """Returns gain since UTC midnight for any metric."""
    prev = prev_videos.get(vid_id, {})
    day_key = f"{key}_day_start"
    date_key = f"{key}_day_date"
    if prev.get(date_key) != today_utc():
        return None  # Will be initialized on save
    start_val = prev.get(day_key)
    if start_val is None:
        return None
    return current_val - start_val


def has_any_change(videos, prev_videos):
    for v in videos:
        prev = prev_videos.get(v["id"], {})
        if (prev.get("views", 0) != v["views"] or
            prev.get("likes", 0) != v["likes"] or
            prev.get("comments", 0) != v["comments"]):
            return True
    return False


# ─── SAVE LOGIC ──────────────────────────────────────────────

def build_updated_videos(videos, prev_videos, now_iso):
    updated = {}
    for v in videos:
        vid_id = v["id"]
        prev = prev_videos.get(vid_id, {})

        entry = {
            "title":    v["title"],
            "views":    v["views"],
            "likes":    v["likes"],
            "comments": v["comments"],
        }

        # Per-metric change tracking
        for key in ["views", "likes", "comments"]:
            curr_val = v[key]
            prev_val = prev.get(key, 0)
            changed_key = f"{key}_changed_at"
            day_key     = f"{key}_day_start"
            date_key    = f"{key}_day_date"

            # last_changed_at
            entry[changed_key] = now_iso if curr_val != prev_val else prev.get(changed_key, now_iso)

            # Daily reset
            if prev.get(date_key) != today_utc():
                entry[day_key]  = prev_val if prev_val else curr_val
                entry[date_key] = today_utc()
            else:
                entry[day_key]  = prev.get(day_key, curr_val)
                entry[date_key] = prev.get(date_key)

        updated[vid_id] = entry
    return updated


# ─── DISCORD ─────────────────────────────────────────────────

def medal(rank):
    return ["🥇", "🥈", "🥉"][rank - 1] if rank <= 3 else f"#{rank}"


def fmt_diff(diff):
    if diff is None or diff == 0:
        return "no change"
    return f"+{diff:,}" if diff > 0 else f"{diff:,}"


def fmt_rate(rate):
    if rate is None:
        return "—"
    return f"+{rate:,.1f}/hr"


def send_to_discord(videos, prev_data):
    prev_videos = prev_data.get("videos", {})
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    prev_run = prev_data.get("last_run", "Never")

    fields = []
    for i, v in enumerate(videos, 1):
        vid_id   = v["id"]
        title    = v["title"]
        views    = v["views"]
        likes    = v["likes"]
        comments = v["comments"]
        prev = prev_videos.get(vid_id, {})

        # Since last check
        d_views    = views    - prev.get("views", 0)
        d_likes    = likes    - prev.get("likes", 0)
        d_comments = comments - prev.get("comments", 0)

        # Daily totals
        dv_today = get_daily(vid_id, views,    "views",    prev_videos)
        dl_today = get_daily(vid_id, likes,    "likes",    prev_videos)
        dc_today = get_daily(vid_id, comments, "comments", prev_videos)

        # Hourly rates
        rate_v = calc_rate(vid_id, views,    "views",    prev_videos)
        rate_l = calc_rate(vid_id, likes,    "likes",    prev_videos)

        daily_str = ""
        if dv_today is not None:
            daily_str = (
                f"\n   📅 **Today:** "
                f"👁 +{dv_today:,}  "
                f"👍 +{dl_today:,}  "
                f"💬 +{dc_today:,}"
            ) if dv_today > 0 else "\n   📅 **Today:** no plays yet"

        fields.append({
            "name": f"{medal(i)} #{i} — {title}",
            "value": (
                f"👁 **{views:,}** views  ({fmt_diff(d_views)})\n"
                f"👍 **{likes:,}** likes  ({fmt_diff(d_likes)})\n"
                f"💬 **{comments:,}** comments  ({fmt_diff(d_comments)})\n"
                f"   ⚡ View rate: **{fmt_rate(rate_v)}** | Like rate: **{fmt_rate(rate_l)}**"
                f"{daily_str}\n"
                f"   🔗 [Watch]({v['url']})"
            ),
            "inline": False
        })

    # Gap table between ranks
    gap_lines = []
    for i in range(1, len(videos)):
        gap = videos[i-1]["views"] - videos[i]["views"]
        gap_lines.append(
            f"  • #{i} → #{i+1}: **{gap:,}** views apart"
        )

    fields.append({
        "name": "─────────────────────",
        "value": "** **",
        "inline": False
    })
    fields.append({
        "name": "↕️ View Gaps Between Ranks",
        "value": "\n".join(gap_lines),
        "inline": False
    })
    fields.append({"name": "🕐 Updated At",  "value": now_str,  "inline": True})
    fields.append({"name": "📋 Prev Check",  "value": prev_run, "inline": True})

    payload = {
        "embeds": [{
            "title": "🎬 YouTube Video Tracker — Ranked by Views",
            "color": 16711680,   # YouTube red #FF0000
            "fields": fields,
            "footer": {"text": "Only posts when stats change • Daily totals reset at UTC midnight"}
        }]
    }
    r = requests.post(DISCORD_WEBHOOK, json=payload)
    print(f"Discord status: {r.status_code}")


def send_error_to_discord(message):
    payload = {
        "embeds": [{
            "title": "⚠️ YouTube Tracker Error",
            "description": message,
            "color": 15158332,
            "footer": {"text": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")}
        }]
    }
    requests.post(DISCORD_WEBHOOK, json=payload)


# ─── MAIN ────────────────────────────────────────────────────

if __name__ == "__main__":
    now_iso = datetime.utcnow().isoformat()
    print(f"[{now_iso}] Starting YouTube tracker...")

    data = load_data()
    prev_videos = data.get("videos", {})

    try:
        videos = fetch_video_stats(VIDEO_IDS)
    except Exception as e:
        msg = f"YouTube API error: {e}"
        print(f"❌ {msg}")
        send_error_to_discord(msg)
        exit(1)

    for v in videos:
        print(f"  #{videos.index(v)+1} {v['title'][:40]}: {v['views']:,} views")

    if not has_any_change(videos, prev_videos):
        print("⏭️  No change detected — skipping Discord message.")
    else:
        print("✅ Changes detected — sending to Discord...")
        send_to_discord(videos, data)

    data["last_run"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    data["videos"] = build_updated_videos(videos, prev_videos, now_iso)
    save_data(data)
    print("Data saved. ✅")
