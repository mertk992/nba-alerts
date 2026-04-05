"""
NBA Remarkable Game Alerts
Monitors live NBA games via ESPN API and sends email alerts
when a player is having a remarkable performance.
"""

import requests
import smtplib
import json
import os
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

# ── Configuration ──────────────────────────────────────────────
RECIPIENT_EMAILS = ["mertk992@gmail.com", "grajek760@gmail.com"]
SENDER_EMAIL = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"

CHECK_INTERVAL_SECONDS = 150  # ~2.5 minutes

# File to track already-sent alerts (avoids spam for same performance)
ALERTS_SENT_FILE = os.path.join(os.path.dirname(__file__), "alerts_sent.json")

# Player avg minutes cache (refreshed daily)
AVG_MINUTES_CACHE_FILE = os.path.join(os.path.dirname(__file__), "avg_minutes_cache.json")
ESPN_PLAYER_SPLITS = "https://site.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{}/splits"

# ── Remarkable Thresholds ──────────────────────────────────────
# "Current" thresholds — already achieved in this game
CURRENT_THRESHOLDS = {
    "PTS": 35,
    "REB": 20,
    "AST": 20,
    "STL": 5,
    "BLK": 5,
    "3PT_MADE": 7,
}

# "Pace" thresholds — projected to end of game using player's avg minutes
PACE_THRESHOLDS = {
    "PTS": 40,
    "REB": 20,
    "AST": 20,
    "STL": 8,
    "BLK": 8,
}

# Minimum minutes played before pace projections are meaningful
MIN_MINUTES_FOR_PACE = 9

# ── Kumingabad Alert ──────────────────────────────────────────
KUMINGA_PLAYER_ID = "4433247"
KUMINGA_MIN_MINUTES = 25
KUMINGA_MIN_FGA = 8       # minimum attempts before FG% matters
KUMINGA_FG_PCT_THRESHOLD = 0.40
KUMINGA_REB_THRESHOLD = 4
KUMINGA_AST_THRESHOLD = 4


def load_avg_minutes_cache():
    """Load cached player average minutes per game."""
    if os.path.exists(AVG_MINUTES_CACHE_FILE):
        try:
            with open(AVG_MINUTES_CACHE_FILE) as f:
                data = json.load(f)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if data.get("date") == today:
                return data.get("players", {})
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def save_avg_minutes_cache(cache):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(AVG_MINUTES_CACHE_FILE, "w") as f:
        json.dump({"date": today, "players": cache}, f)


def get_player_avg_minutes(player_id, cache):
    """Fetch a player's season average minutes per game, using cache."""
    if player_id in cache:
        return cache[player_id]

    try:
        resp = requests.get(
            ESPN_PLAYER_SPLITS.format(player_id), timeout=10
        )
        resp.raise_for_status()
        data = resp.json()

        # Navigate: splitCategories[0].splits[0].stats[1] = avgMinutes
        # But verify via labels/names arrays
        labels = data.get("names", [])
        min_idx = None
        for i, name in enumerate(labels):
            if name == "avgMinutes":
                min_idx = i
                break

        if min_idx is not None:
            splits = data.get("splitCategories", [{}])[0].get("splits", [{}])
            if splits:
                avg_min = float(splits[0].get("stats", [])[min_idx])
                cache[player_id] = avg_min
                return avg_min
    except Exception as e:
        print(f"      [WARN] Could not fetch avg minutes for player {player_id}: {e}")

    # Fallback: assume 32 minutes (league average for starters)
    cache[player_id] = 32.0
    return 32.0


def load_alerts_sent():
    """Load the set of already-sent alert keys."""
    if os.path.exists(ALERTS_SENT_FILE):
        try:
            with open(ALERTS_SENT_FILE) as f:
                data = json.load(f)
            # Clear alerts from previous days
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if data.get("date") != today:
                return {"date": today, "keys": []}
            return data
        except (json.JSONDecodeError, KeyError):
            pass
    return {"date": datetime.now(timezone.utc).strftime("%Y-%m-%d"), "keys": []}


def save_alerts_sent(data):
    with open(ALERTS_SENT_FILE, "w") as f:
        json.dump(data, f)


def get_live_games():
    """Fetch today's NBA scoreboard and return games that are in progress."""
    resp = requests.get(ESPN_SCOREBOARD, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    live_games = []
    for event in data.get("events", []):
        status = event.get("status", {})
        state = status.get("type", {}).get("state", "")
        if state == "in":  # "in" = live
            live_games.append(event)
    return live_games


def get_game_details(game_id):
    """Fetch detailed box score for a specific game."""
    resp = requests.get(ESPN_SUMMARY, params={"event": game_id}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def parse_minutes(min_str):
    """Parse minutes string like '32' or '28:45' into float minutes."""
    if not min_str or min_str == "--":
        return 0.0
    try:
        if ":" in str(min_str):
            parts = str(min_str).split(":")
            return float(parts[0]) + float(parts[1]) / 60
        return float(min_str)
    except (ValueError, IndexError):
        return 0.0


def parse_stat(val):
    """Parse a stat value, handling made-attempted format like '10-15'."""
    if not val or val == "--":
        return 0
    try:
        if "-" in str(val):
            return int(str(val).split("-")[0])  # return made
        return int(float(val))
    except (ValueError, IndexError):
        return 0


def get_game_progress(game_data):
    """Estimate how far through the game we are (0.0 to 1.0+)."""
    try:
        header = game_data.get("header", {})
        competitions = header.get("competitions", [{}])
        status = competitions[0].get("status", {})
        period = status.get("period", 1)
        clock = status.get("displayClock", "12:00")

        # Parse clock
        clock_parts = clock.replace(".", ":").split(":")
        minutes_left = float(clock_parts[0]) if clock_parts else 12.0
        seconds_left = float(clock_parts[1]) if len(clock_parts) > 1 else 0

        quarter_minutes_elapsed = 12.0 - minutes_left - seconds_left / 60
        total_minutes_elapsed = (period - 1) * 12.0 + quarter_minutes_elapsed

        return min(total_minutes_elapsed / 48.0, 1.0), period, total_minutes_elapsed
    except Exception:
        return 0.5, 2, 24.0  # fallback to mid-game


def check_remarkable_players(game_data, game_info, avg_minutes_cache):
    """Check all players in a game for remarkable performances."""
    remarkable = []
    game_progress, current_period, game_minutes = get_game_progress(game_data)

    boxscore = game_data.get("boxscore", {})
    players_data = boxscore.get("players", [])

    for team_data in players_data:
        team_info = team_data.get("team", {})
        team_name = team_info.get("displayName", "Unknown")

        for stat_group in team_data.get("statistics", []):
            stat_labels = [l.lower() for l in stat_group.get("labels", [])]
            if not stat_labels:
                continue

            for athlete in stat_group.get("athletes", []):
                player = athlete.get("athlete", {})
                player_name = player.get("displayName", "Unknown")
                player_id = player.get("id", "")
                stats_raw = athlete.get("stats", [])

                if len(stats_raw) != len(stat_labels):
                    continue

                stat_map = dict(zip(stat_labels, stats_raw))

                minutes = parse_minutes(stat_map.get("min", "0"))
                pts = parse_stat(stat_map.get("pts", "0"))
                reb = parse_stat(stat_map.get("reb", "0"))
                ast = parse_stat(stat_map.get("ast", "0"))
                stl = parse_stat(stat_map.get("stl", "0"))
                blk = parse_stat(stat_map.get("blk", "0"))
                three_made = parse_stat(stat_map.get("3pt", "0"))

                current_stats = {
                    "PTS": pts, "REB": reb, "AST": ast,
                    "STL": stl, "BLK": blk, "3PT_MADE": three_made,
                }

                reasons = []

                # Debug: log high-stat players
                if pts >= 20 or reb >= 10 or ast >= 10:
                    print(f"    [DEBUG] {player_name}: {pts}pts/{reb}reb/{ast}ast in {minutes:.0f}min (progress={game_progress:.2f})")

                # Check current thresholds
                for stat_name, threshold in CURRENT_THRESHOLDS.items():
                    val = current_stats.get(stat_name, 0)
                    if val >= threshold:
                        reasons.append(f"{val} {stat_name} (threshold: {threshold})")

                # Check pace thresholds using player's avg minutes per game
                # Only project if the player still has meaningful time left
                if minutes >= MIN_MINUTES_FOR_PACE and minutes > 0 and game_progress < 0.90:
                    avg_min = get_player_avg_minutes(player_id, avg_minutes_cache)
                    remaining_min = max(0, avg_min - minutes)
                    if remaining_min >= 3:  # at least 3 min left to play
                        proj_total_min = minutes + remaining_min
                        pace_factor = proj_total_min / minutes
                        for stat_name, threshold in PACE_THRESHOLDS.items():
                            val = current_stats.get(stat_name, 0)
                            projected = val * pace_factor
                            if projected >= threshold and val >= threshold * 0.4:
                                reasons.append(
                                    f"On pace for {projected:.0f} {stat_name} "
                                    f"({val} in {minutes:.0f} min, ~{remaining_min:.0f} min left)"
                                )

                # ── Kumingabad Alert ──────────────────────────
                if player_id == KUMINGA_PLAYER_ID and minutes >= KUMINGA_MIN_MINUTES:
                    kuminga_reasons = []
                    # FG% check (min attempts)
                    fg_str = stat_map.get("fg", "0-0")
                    fg_parts = str(fg_str).split("-") if fg_str and fg_str != "--" else ["0", "0"]
                    fg_made = int(fg_parts[0]) if len(fg_parts) >= 2 else 0
                    fg_att = int(fg_parts[1]) if len(fg_parts) >= 2 else 0
                    if fg_att >= KUMINGA_MIN_FGA:
                        fg_pct = fg_made / fg_att
                        if fg_pct < KUMINGA_FG_PCT_THRESHOLD:
                            kuminga_reasons.append(
                                f"Shooting {fg_pct:.1%} ({fg_made}/{fg_att} FG)"
                            )
                    if reb < KUMINGA_REB_THRESHOLD:
                        kuminga_reasons.append(f"Only {reb} REB")
                    if ast < KUMINGA_AST_THRESHOLD:
                        kuminga_reasons.append(f"Only {ast} AST")

                    if kuminga_reasons:
                        reasons.extend([f"🚨 KUMINGABAD: {r}" for r in kuminga_reasons])

                if reasons:
                    remarkable.append({
                        "player_name": player_name,
                        "player_id": player_id,
                        "team": team_name,
                        "minutes": minutes,
                        "stats": current_stats,
                        "stat_line": stat_map,
                        "reasons": reasons,
                        "period": current_period,
                        "game_info": game_info,
                    })

    return remarkable


def format_game_info(event):
    """Extract readable game info from an ESPN event."""
    comps = event.get("competitions", [{}])
    comp = comps[0] if comps else {}
    competitors = comp.get("competitors", [])

    teams = []
    for c in competitors:
        team_name = c.get("team", {}).get("displayName", "?")
        score = c.get("score", "0")
        home_away = c.get("homeAway", "")
        teams.append({"name": team_name, "score": score, "homeAway": home_away})

    home = next((t for t in teams if t["homeAway"] == "home"), teams[0] if teams else {})
    away = next((t for t in teams if t["homeAway"] == "away"), teams[1] if len(teams) > 1 else {})

    status = event.get("status", {})
    period = status.get("period", 1)
    clock = status.get("displayClock", "")
    period_label = f"Q{period}" if period <= 4 else f"OT{period - 4}"

    game_link = event.get("links", [{}])
    link_url = ""
    for link in event.get("links", []):
        if "gamecast" in link.get("href", "") or "game" in link.get("href", ""):
            link_url = link.get("href", "")
            break
    if not link_url and event.get("links"):
        link_url = event["links"][0].get("href", "")

    return {
        "home": home.get("name", "?"),
        "away": away.get("name", "?"),
        "home_score": home.get("score", "0"),
        "away_score": away.get("score", "0"),
        "period": period_label,
        "clock": clock,
        "link": link_url,
        "game_id": event.get("id", ""),
    }


def build_email(remarkable_players):
    """Build an HTML email from a list of remarkable performances."""
    subject = "🏀 NBA Remarkable Game Alert"

    if len(remarkable_players) == 1:
        p = remarkable_players[0]
        subject = f"🏀 {p['player_name']} is going off! ({p['stats']['PTS']}pts/{p['stats']['REB']}reb/{p['stats']['AST']}ast)"

    html_parts = [
        "<html><body style='font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;'>",
        "<h2 style='color: #1a1a2e;'>🏀 NBA Remarkable Game Alert</h2>",
    ]

    for p in remarkable_players:
        gi = p["game_info"]
        html_parts.append(f"""
        <div style='background: #f8f9fa; border-left: 4px solid #e63946; padding: 15px; margin: 15px 0; border-radius: 4px;'>
            <h3 style='margin: 0 0 8px 0; color: #1a1a2e;'>{p['player_name']} — {p['team']}</h3>
            <p style='margin: 4px 0; font-size: 22px; font-weight: bold; color: #e63946;'>
                {p['stats']['PTS']} PTS | {p['stats']['REB']} REB | {p['stats']['AST']} AST
            </p>
            <p style='margin: 4px 0; color: #555;'>
                {p['stats']['STL']} STL | {p['stats']['BLK']} BLK | {p['stats']['3PT_MADE']} 3PM | {p['minutes']:.0f} MIN
            </p>
            <p style='margin: 8px 0 4px 0; color: #333;'><strong>Why it's remarkable:</strong></p>
            <ul style='margin: 4px 0; padding-left: 20px; color: #333;'>
                {"".join(f"<li>{r}</li>" for r in p['reasons'])}
            </ul>
            <p style='margin: 8px 0 0 0; color: #666; font-size: 14px;'>
                📍 {gi['away']} ({gi['away_score']}) @ {gi['home']} ({gi['home_score']}) — {gi['period']} {gi['clock']}
            </p>
            {"<p style='margin: 4px 0 0 0;'><a href='" + gi['link'] + "' style='color: #457b9d;'>Watch on ESPN →</a></p>" if gi.get('link') else ""}
        </div>
        """)

    html_parts.append(
        "<p style='color: #999; font-size: 12px; margin-top: 20px;'>"
        "NBA Remarkable Game Alerts • Powered by ESPN data</p>"
        "</body></html>"
    )

    return subject, "".join(html_parts)


def send_email(subject, html_body):
    """Send email via Gmail SMTP."""
    if not SENDER_EMAIL or not GMAIL_APP_PASSWORD:
        print(f"[SKIP] Email not configured. Would send: {subject}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"NBAPopoff <{SENDER_EMAIL}>"
    msg["To"] = ", ".join(RECIPIENT_EMAILS)
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, GMAIL_APP_PASSWORD)
        server.sendmail(SENDER_EMAIL, RECIPIENT_EMAILS, msg.as_string())

    print(f"[EMAIL SENT] {subject}")
    return True


def should_run_now():
    """Check if we should be monitoring (NBA game hours during season)."""
    from datetime import datetime
    import pytz

    et = pytz.timezone("US/Eastern")
    now = datetime.now(et)

    # NBA regular season: Oct-Apr, Playoffs: Apr-Jun
    month = now.month
    if month in (7, 8, 9):  # NBA offseason
        print("[SKIP] NBA offseason")
        return False

    # Games typically run 12pm-1am ET (early games start at noon, late games end ~1am)
    hour = now.hour
    if hour >= 1 and hour < 12:
        print(f"[SKIP] Outside game hours ({hour}:00 ET)")
        return False

    return True


def run_check():
    """Run a single check cycle."""
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Checking for live NBA games...")

    live_games = get_live_games()
    if not live_games:
        print("[INFO] No live games right now.")
        return

    print(f"[INFO] Found {len(live_games)} live game(s)")

    alerts_data = load_alerts_sent()
    avg_minutes_cache = load_avg_minutes_cache()
    all_remarkable = []

    for event in live_games:
        game_info = format_game_info(event)
        game_id = game_info["game_id"]
        print(f"  Checking: {game_info['away']} @ {game_info['home']} ({game_info['period']} {game_info['clock']})")

        try:
            details = get_game_details(game_id)
            remarkable = check_remarkable_players(details, game_info, avg_minutes_cache)

            for player in remarkable:
                # Create a unique key: player + game + threshold tiers
                # Re-alert if they cross a higher tier in any major stat
                pts_tier = player["stats"]["PTS"] // 10 * 10
                reb_tier = player["stats"]["REB"] // 5 * 5
                ast_tier = player["stats"]["AST"] // 5 * 5
                alert_key = f"{player['player_id']}_{game_id}_p{pts_tier}_r{reb_tier}_a{ast_tier}"

                if alert_key not in alerts_data["keys"]:
                    all_remarkable.append(player)
                    alerts_data["keys"].append(alert_key)
                    print(f"    🔥 {player['player_name']}: {', '.join(player['reasons'])}")
                else:
                    print(f"    [already alerted] {player['player_name']}")

        except Exception as e:
            print(f"    [ERROR] Failed to get details for game {game_id}: {e}")

    # Save avg minutes cache for future runs
    save_avg_minutes_cache(avg_minutes_cache)

    if all_remarkable:
        subject, body = build_email(all_remarkable)
        send_email(subject, body)
        save_alerts_sent(alerts_data)
    else:
        print("[INFO] No new remarkable performances to alert on.")


def run_loop():
    """Run continuous monitoring loop."""
    print("=" * 60)
    print("  NBA Remarkable Game Alert Monitor")
    print(f"  Checking every {CHECK_INTERVAL_SECONDS}s (~{CHECK_INTERVAL_SECONDS/60:.1f} min)")
    print(f"  Alerts → {RECIPIENT_EMAIL}")
    print("=" * 60)

    while True:
        try:
            if should_run_now():
                run_check()
            else:
                print("[SLEEP] Outside active hours, sleeping...")
        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        run_check()
    else:
        run_loop()
