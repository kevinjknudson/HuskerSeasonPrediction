#!/usr/bin/env python3
"""
Husker prediction pool — results collector.

Reads config.json, pulls Nebraska's schedule/scores from ESPN's public JSON
feed and everyone's picks from the Google Sheet, then writes data/standings.json
for the site to render.

Run locally:   python scripts/update.py --dry-run
In CI:         python scripts/update.py
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
OUT_PATH = os.path.join(ROOT, "data", "standings.json")

ESPN = ("https://site.api.espn.com/apis/site/v2/sports/football/"
        "college-football/teams/{team}/schedule?season={season}&seasontype={stype}")

# Columns in the response sheet that are never games.
META_COLUMNS = {"timestamp", "email address", "email", "name", "score",
                "tiebreak difference", "tiebreaker difference", "column 1", ""}


# ── small helpers ──────────────────────────────────────────────────────────

def log(msg):
    print(msg, file=sys.stderr)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "husker-pool/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def to_csv_url(link):
    """Accept any Google Sheets link and turn it into a CSV export link."""
    if link.startswith(("./", "/", "file:")):          # local file, for testing
        return link
    if "output=csv" in link or "format=csv" in link:
        return link
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", link)
    if m:
        gid = re.search(r"[#&?]gid=(\d+)", link)
        return (f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export"
                f"?format=csv&gid={gid.group(1) if gid else '0'}")
    return link


def read_text(url):
    if url.startswith(("./", "/")):
        with open(url if url.startswith("/") else os.path.join(ROOT, url[2:])) as f:
            return f.read()
    return fetch(url)


PREFIX = re.compile(r"^\s*nebraska\b", re.I)
LEADIN = re.compile(r"^\s*(vs\.?|at|@|v\.?)\s+", re.I)


def column_alias(col):
    """'Nebraska vs. North Dakota' -> 'north dakota'."""
    s = LEADIN.sub("", PREFIX.sub("", col).strip())
    s = re.sub(r"[^a-z0-9 ]+", " ", s.lower())
    return " ".join(s.split())


def normalize_pick(v):
    s = (v or "").strip().lower()
    if s.startswith("w"):
        return "W"
    if s.startswith("l"):
        return "L"
    return None


def to_int(v):
    try:
        return int(round(float(re.sub(r"[^0-9.\-]", "", str(v)))))
    except (TypeError, ValueError):
        return None


def score_of(competitor):
    """ESPN returns score as a number, a string, or {'value': n}."""
    s = competitor.get("score")
    if isinstance(s, dict):
        s = s.get("value", s.get("displayValue"))
    return to_int(s)


# ── picks ──────────────────────────────────────────────────────────────────

def load_picks(cfg):
    text = read_text(to_csv_url(cfg["picksLink"]))
    rows = list(csv.DictReader(io.StringIO(text)))
    if not rows:
        raise SystemExit("The picks sheet came back empty. Check that the link is shareable.")

    headers = [h.strip() for h in rows[0].keys() if h is not None]
    name_col = next((h for h in headers if h.lower() == "name"), headers[0])
    tb_col = next((h for h in headers
                   if "tiebreak" in h.lower() and "point" in h.lower()), None)
    game_cols = [h for h in headers
                 if h.lower() not in META_COLUMNS and "tiebreak" not in h.lower()]

    people = []
    for row in rows:
        clean = {(k.strip() if k else ""): (v or "").strip() for k, v in row.items()}
        name = clean.get(name_col, "")
        if not name or name.lower() == "actual":      # legacy manual-results row
            continue
        people.append({
            "name": name,
            "picks": {g: normalize_pick(clean.get(g)) for g in game_cols},
            "guess": to_int(clean.get(tb_col)) if tb_col else None,
        })

    # One entry per person: a re-submitted form wins.
    latest = {}
    for p in people:
        latest[p["name"].strip().lower()] = p
    return game_cols, list(latest.values())


# ── results ────────────────────────────────────────────────────────────────

def load_events(cfg):
    """Regular season (type 2) plus postseason (type 3)."""
    events = []
    for stype in (2, 3):
        url = ESPN.format(team=cfg["espnTeam"], season=cfg["season"], stype=stype)
        try:
            payload = json.loads(fetch(url))
        except Exception as err:                       # noqa: BLE001
            log(f"  ! seasontype {stype} unavailable: {err}")
            continue
        for ev in payload.get("events", []):
            parsed = parse_event(ev, cfg, postseason=(stype == 3))
            if parsed:
                events.append(parsed)
    return events


def parse_event(ev, cfg, postseason):
    comp = (ev.get("competitions") or [{}])[0]
    sides = comp.get("competitors") or []
    us = next((c for c in sides if str(c.get("team", {}).get("id")) == str(cfg["espnTeamId"])), None)
    them = next((c for c in sides if c is not us), None)
    if not us or not them:
        return None

    opp = them.get("team", {})
    status = (comp.get("status") or ev.get("status") or {}).get("type", {}) or {}
    kickoff = ev.get("date") or comp.get("date")
    us_pts, opp_pts = score_of(us), score_of(them)

    logos = opp.get("logos") or []
    logo = (logos[0].get("href") if logos else None) or opp.get("logo")
    if not logo and opp.get("id"):
        logo = f"https://a.espncdn.com/i/teamlogos/ncaa/500/{opp['id']}.png"

    return {
        "kickoff": kickoff,
        "postseason": postseason,
        "logo": logo,
        "abbr": (opp.get("abbreviation") or opp.get("shortDisplayName") or "")[:4].upper(),
        "color": "#" + str(opp.get("color") or "444444").lstrip("#"),
        "opponent": opp.get("displayName") or opp.get("name") or "TBD",
        "opponentNames": [str(opp.get(k, "")).lower() for k in
                          ("displayName", "shortDisplayName", "name",
                           "location", "nickname", "abbreviation")],
        "homeAway": us.get("homeAway"),
        "neutral": bool(comp.get("neutralSite")),
        "nebraskaPoints": us_pts,
        "opponentPoints": opp_pts,
        "completed": bool(status.get("completed")),
        "statusName": status.get("name", ""),
    }


def hours_since(iso):
    if not iso:
        return 0.0
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0


def assign(game_cols, events, cfg):
    """Match each sheet column to a real game.

    Scored so an exact opponent name always beats a partial one, and a longer
    partial beats a shorter one. That's what keeps 'Nebraska vs Ohio' off the
    Ohio State game and 'Michigan State' out of a 'Michigan' column."""
    aliases = {k.lower(): [a.lower() for a in v] for k, v in cfg.get("aliases", {}).items()}
    bowl_cols = {c.lower() for c in cfg.get("bowlColumns", [])}

    candidates = []
    for col in game_cols:
        for alias in aliases.get(col.lower(), [column_alias(col)]):
            if not alias:
                continue
            for i, ev in enumerate(events):
                names = ev["opponentNames"]
                if alias in names:
                    candidates.append((2, len(alias), col, i))
                elif any(alias in n for n in names if n):
                    candidates.append((1, len(alias), col, i))
    candidates.sort(reverse=True)

    taken_cols, taken_events, pairs = set(), set(), {}
    for _, _, col, i in candidates:
        if col in taken_cols or i in taken_events:
            continue
        taken_cols.add(col)
        taken_events.add(i)
        pairs[col] = events[i]

    # Bowl / playoff column takes whatever postseason game shows up.
    for col in game_cols:
        if col in pairs or col.lower() not in bowl_cols:
            continue
        for i, ev in enumerate(events):
            if ev["postseason"] and i not in taken_events:
                taken_events.add(i)
                pairs[col] = ev
                break
    return pairs


def settle(col, ev, cfg):
    """Decide whether a game counts yet, and what the outcome was."""
    override = (cfg.get("overrides") or {}).get(col)
    if override:
        return {"state": "official", "result": override.get("result"),
                "nebraskaPoints": override.get("nebraskaPoints"),
                "opponentPoints": override.get("opponentPoints"),
                "note": "Entered by hand"}

    if ev is None:
        return {"state": "scheduled", "result": None, "nebraskaPoints": None,
                "opponentPoints": None, "note": "No matching game found yet"}

    us, them = ev["nebraskaPoints"], ev["opponentPoints"]
    if not ev["completed"] or us is None or them is None:
        return {"state": "scheduled", "result": None, "nebraskaPoints": us,
                "opponentPoints": them, "note": None}

    result = "W" if us > them else "L"
    wait = float(cfg.get("settleHours", 6))
    if hours_since(ev["kickoff"]) < wait:
        return {"state": "unofficial", "result": result, "nebraskaPoints": us,
                "opponentPoints": them,
                "note": f"Locks in {wait:.0f} hours after kickoff"}

    return {"state": "official", "result": result, "nebraskaPoints": us,
            "opponentPoints": them, "note": None}


# ── standings ──────────────────────────────────────────────────────────────

def order(people, scored, total_points=None, complete=False):
    """Rank everyone against a given set of finished games."""
    rows = []
    for p in people:
        correct = sum(1 for g in scored if p["picks"].get(g["column"]) == g["result"])
        diff = (abs(total_points - p["guess"])
                if complete and total_points is not None and p["guess"] is not None
                else None)
        rows.append({"name": p["name"], "correct": correct, "diff": diff,
                     "guess": p["guess"], "photo": p.get("photo")})

    rows.sort(key=lambda r: (-r["correct"],
                             r["diff"] if r["diff"] is not None else 10**9,
                             r["name"].lower()))
    rank, prev = 0, None
    for i, r in enumerate(rows):
        key = (r["correct"], r["diff"])
        if key != prev:                       # ties share a rank
            rank, prev = i + 1, key
        r["rank"] = rank
        r["position"] = i + 1                 # unique slot, for the chart
    return rows


def compose(games, people, season, team, photos=None):
    """Turn finished games plus everyone's picks into the payload the page reads."""
    lookup = {k.strip().lower(): v for k, v in (photos or {}).items()}
    for p in people:
        p["photo"] = lookup.get(p["name"].strip().lower())

    ordered = sorted(games, key=lambda g: (g.get("kickoff") or "9999",
                                           games.index(g)))
    official = [g for g in ordered if g["state"] == "official"]

    points = sum(g["nebraskaPoints"] or 0 for g in official)
    wins = sum(1 for g in official if g["result"] == "W")
    complete = len(official) == len(games) and len(games) > 0

    # Week by week: re-rank everyone using only the first k finished games.
    # Recomputed from scratch each run, so it self-heals if a result changes.
    history = []
    for k in range(1, len(official) + 1):
        snap = order(people, official[:k])
        g = official[k - 1]
        history.append({
            "played": k,
            "column": g["column"],
            "abbr": g.get("abbr") or g["column"][:4].upper(),
            "result": g["result"],
            "ranks": {r["name"]: {"rank": r["rank"], "position": r["position"],
                                  "correct": r["correct"]} for r in snap},
        })

    rows = order(people, official, points, complete)
    previous = history[-2]["ranks"] if len(history) >= 2 else None
    leader = rows[0]["correct"] if rows else 0
    picks_by_name = {p["name"]: p["picks"] for p in people}

    standings = []
    for r in rows:
        picks = [picks_by_name[r["name"]].get(g["column"]) for g in games]
        still_open = sum(1 for g, pick in zip(games, picks)
                         if g["state"] != "official" and pick)
        ceiling = r["correct"] + still_open
        was = previous.get(r["name"], {}).get("rank") if previous else None
        standings.append({
            **r,
            "picks": picks,
            "maxPossible": ceiling,
            "movement": (was - r["rank"]) if was is not None else None,
            # You're out when even a perfect finish can't catch the current leader.
            "eliminated": (not complete) and ceiling < leader,
        })

    # What's next, and how the pool called it.
    upcoming = next((g for g in ordered if g["state"] != "official"), None)
    next_game = None
    if upcoming:
        neb = sum(1 for p in people if p["picks"].get(upcoming["column"]) == "W")
        opp = sum(1 for p in people if p["picks"].get(upcoming["column"]) == "L")
        next_game = {
            "column": upcoming["column"],
            "opponent": upcoming.get("opponent") or upcoming["column"],
            "abbr": upcoming.get("abbr"), "color": upcoming.get("color"),
            "logo": upcoming.get("logo"), "kickoff": upcoming.get("kickoff"),
            "site": upcoming.get("site"),
            "split": {"nebraska": neb, "opponent": opp,
                      "none": len(people) - neb - opp},
        }

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "season": season,
        "seasonComplete": complete,
        **team,
        "record": {"wins": wins, "losses": len(official) - wins,
                   "played": len(official), "scheduled": len(games),
                   "pointsSoFar": points},
        "aliveCount": sum(1 for s in standings if not s["eliminated"]),
        "nextGame": next_game,
        "history": history,
        "games": games,
        "standings": standings,
    }


def build(cfg):
    log("Reading picks\u2026")
    game_cols, people = load_picks(cfg)
    log(f"  {len(people)} entries, {len(game_cols)} games on the sheet")

    log("Reading results\u2026")
    events = load_events(cfg)
    log(f"  {len(events)} games on Nebraska's schedule")
    pairs = assign(game_cols, events, cfg)

    games = []
    for col in game_cols:
        ev = pairs.get(col)
        outcome = settle(col, ev, cfg)
        games.append({
            "column": col,
            "opponent": ev["opponent"] if ev else col,
            "kickoff": ev["kickoff"] if ev else None,
            "site": ("neutral" if ev and ev["neutral"] else
                     ev["homeAway"] if ev else None),
            "logo": ev["logo"] if ev else None,
            "abbr": ev["abbr"] if ev else (column_alias(col)[:4].upper() or "TBD"),
            "color": ev["color"] if ev else "#7A6857",
            **outcome,
        })

    team = {
        "teamLogo": f"https://a.espncdn.com/i/teamlogos/ncaa/500/{cfg['espnTeamId']}.png",
        "teamAbbr": cfg.get("teamAbbr", "NEB"),
        "teamColor": cfg.get("teamColor", "#E41C38"),
    }
    return compose(games, people, cfg["season"], team, cfg.get("photos"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print instead of writing")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG_PATH))
    if "PASTE" in cfg.get("picksLink", ""):
        raise SystemExit("Add your picks link to config.json first.")

    data = build(cfg)
    text = json.dumps(data, indent=2) + "\n"

    if args.dry_run:
        print(text)
        return

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w") as f:
        f.write(text)
    r = data["record"]
    log(f"Wrote standings.json — Nebraska {r['wins']}-{r['losses']}, "
        f"{r['played']} of {r['scheduled']} official")


if __name__ == "__main__":
    main()
