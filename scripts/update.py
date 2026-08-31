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
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.json")
OUT_PATH = os.path.join(ROOT, "data", "standings.json")

# ESPN's public JSON, reachable on two hosts. Datacentre IPs get 403'd unless
# the request looks like a browser, so send a full header set.
ESPN_HOSTS = ["https://site.api.espn.com", "https://site.web.api.espn.com"]
ESPN_PATH = ("/apis/site/v2/sports/football/college-football"
             "/teams/{team}/schedule?season={season}&seasontype={stype}")

BROWSER = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/college-football/",
}

# Columns in the response sheet that are never games.
META_COLUMNS = {"timestamp", "email address", "email", "name", "score",
                "tiebreak difference", "tiebreaker difference", "column 1", ""}


# ── small helpers ──────────────────────────────────────────────────────────

def log(msg):
    print(msg, file=sys.stderr)


def fetch(url, tries=3, headers=None):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers or BROWSER)
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as err:                       # noqa: BLE001
            last = err
            if attempt < tries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last


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


WIN_WORDS = {"w", "win", "wins", "won", "victory"}
LOSE_WORDS = {"l", "lose", "loss", "lost", "loses"}


def resolve_pick(raw, col, ev, us_names):
    """Read one answer.

    The form can ask either way — 'Win'/'Lose', or the name of the team you
    think wins. Team names are checked against the actual opponent rather than
    by first letter, because 'Washington' starts with a W and is not a win.
    """
    s = (raw or "").strip().lower()
    if not s:
        return None
    if s in WIN_WORDS:
        return "W"
    if s in LOSE_WORDS:
        return "L"
    if s in us_names:
        return "W"

    theirs = {column_alias(col)}
    if ev:
        theirs.update(n for n in ev["opponentNames"] if n)
        theirs.add((ev.get("opponentShort") or "").lower())
        theirs.add((ev.get("abbr") or "").lower())
    theirs.discard("")
    if s in theirs or any(s in n or n in s for n in theirs):
        return "L"
    return None


def us_team_names(cfg):
    names = {str(cfg.get("cfbdTeam", "Nebraska")).lower(),
             str(cfg.get("espnTeam", "nebraska")).lower(),
             str(cfg.get("teamAbbr", "NEB")).lower(),
             "nebraska", "huskers", "cornhuskers", "nebraska cornhuskers"}
    names.discard("")
    return names


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


# ── CollegeFootballData ────────────────────────────────────────────────────

CFBD = "https://api.collegefootballdata.com"


def field(obj, *names, default=None):
    """CFBD has shipped both snake_case and camelCase; accept either."""
    for n in names:
        if isinstance(obj, dict) and obj.get(n) is not None:
            return obj[n]
    return default


def cfbd_get(path, key, params):
    url = f"{CFBD}{path}?{urllib.parse.urlencode(params)}"
    return json.loads(fetch(url, headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "husker-pool/1.0",
    }))


def cfbd_teams(key):
    """school -> {logos, colour, abbreviation}. Includes FCS, so North Dakota works."""
    out = {}
    try:
        for t in cfbd_get("/teams", key, {}):
            school = field(t, "school")
            if school:
                out[school.lower()] = t
    except Exception as err:                           # noqa: BLE001
        log(f"  ! team metadata unavailable ({err}); logos may be missing")
    return out


def load_events_cfbd(cfg, key):
    us = cfg.get("cfbdTeam", "Nebraska")
    teams = cfbd_teams(key)
    events = []

    for season_type, postseason in (("regular", False), ("postseason", True)):
        rows = cfbd_get("/games", key, {"year": cfg["season"], "team": us,
                                        "seasonType": season_type})
        for g in rows:
            home = field(g, "home_team", "homeTeam", default="")
            away = field(g, "away_team", "awayTeam", default="")
            at_home = home.lower() == us.lower()
            opp_name = away if at_home else home
            if not opp_name:
                continue

            us_pts = field(g, "home_points", "homePoints") if at_home else \
                     field(g, "away_points", "awayPoints")
            opp_pts = field(g, "away_points", "awayPoints") if at_home else \
                      field(g, "home_points", "homePoints")

            t = teams.get(opp_name.lower(), {})
            logos = field(t, "logos", default=[]) or []
            mascot = field(t, "mascot", default="")
            abbr = field(t, "abbreviation", default="") or opp_name

            events.append({
                "kickoff": field(g, "start_date", "startDate"),
                "postseason": postseason,
                "logo": logos[0] if logos else None,
                "opponent": " ".join(x for x in (opp_name, mascot) if x),
                "opponentShort": opp_name,
                "opponentNames": [n.lower() for n in
                                  (opp_name, f"{opp_name} {mascot}".strip(), mascot, abbr) if n],
                "homeAway": "home" if at_home else "away",
                "neutral": bool(field(g, "neutral_site", "neutralSite", default=False)),
                "nebraskaPoints": to_int(us_pts),
                "opponentPoints": to_int(opp_pts),
                "completed": bool(field(g, "completed", default=False)),
                "statusName": "CFBD",
                "abbr": abbr.upper()[:4],
                "color": "#" + str(field(t, "color", default="7A6857")).lstrip("#"),
            })
    return events


# ── picks ──────────────────────────────────────────────────────────────────

def load_picks(cfg):
    text = read_text(to_csv_url(cfg["picksLink"]))
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    # A header row with no responses under it is a normal preseason state,
    # so only a missing header row counts as a broken link.
    headers = [h.strip() for h in (reader.fieldnames or []) if h is not None]
    if not headers:
        raise SystemExit("The picks sheet came back empty. Check that the link is shareable.")
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
        # A name typed in all lowercase gets tidied; anything with existing
        # capitals is left exactly as entered.
        if name == name.lower():
            name = name.title()
        people.append({
            "name": name,
            "raw": {g: clean.get(g, "") for g in game_cols},
            "picks": {},
            "guess": to_int(clean.get(tb_col)) if tb_col else None,
        })

    # One entry per person: a re-submitted form wins.
    latest = {}
    for p in people:
        latest[p["name"].strip().lower()] = p
    return game_cols, list(latest.values())


# ── results ────────────────────────────────────────────────────────────────

def load_events(cfg):
    """CollegeFootballData if a key is available, ESPN otherwise."""
    key = os.environ.get("CFBD_API_KEY") or cfg.get("cfbdKey")
    if key:
        try:
            events = load_events_cfbd(cfg, key)
            if events:
                log(f"  source: CollegeFootballData ({len(events)} games)")
                return events
            log("  ! CollegeFootballData returned no games — falling back to ESPN")
        except Exception as err:                       # noqa: BLE001
            log(f"  ! CollegeFootballData failed ({err}) — falling back to ESPN")
    else:
        log("  no CFBD_API_KEY set — using ESPN")
    return load_events_espn(cfg)


def load_events_espn(cfg):
    """Regular season (type 2) plus postseason (type 3), across both hosts."""
    events, regular_ok = [], False

    for stype in (2, 3):
        payload, problem = None, None
        for host in ESPN_HOSTS:
            url = host + ESPN_PATH.format(team=cfg["espnTeam"], season=cfg["season"],
                                          stype=stype)
            try:
                payload = json.loads(fetch(url))
                log(f"  seasontype {stype}: {host.split('//')[1]}")
                break
            except Exception as err:                   # noqa: BLE001
                problem = err

        if payload is None:
            log(f"  ! seasontype {stype} unavailable: {problem}")
            continue
        if stype == 2:
            regular_ok = True

        for ev in payload.get("events", []):
            parsed = parse_event(ev, cfg, postseason=(stype == 3))
            if parsed:
                events.append(parsed)

    # Never overwrite good standings with an empty season because a feed was
    # down. Fail the run instead and leave the last good file in place.
    if not regular_ok:
        raise SystemExit(
            "Couldn't reach ESPN's schedule feed on any host, so nothing was written "
            "and the previous standings are untouched. If this keeps happening, the "
            "overrides block in config.json can carry the season by hand."
        )
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
        # "Ohio" rather than "Ohio Bobcats", for the next-game headline
        "opponentShort": (opp.get("location") or opp.get("shortDisplayName")
                          or opp.get("displayName") or "TBD"),
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
        is_bowl = col.lower() in bowl_cols
        for alias in aliases.get(col.lower(), [column_alias(col)]):
            if not alias:
                continue
            for i, ev in enumerate(events):
                names = ev["opponentNames"]
                if alias in names:
                    exact = 2
                elif any(alias in n for n in names if n):
                    exact = 1
                else:
                    continue
                regular = 0 if (ev["postseason"] and not is_bowl) else 1
                candidates.append((regular, exact, len(alias), col, i))
    candidates.sort(reverse=True)

    taken_cols, taken_events, pairs = set(), set(), {}
    for _, _, _, col, i in candidates:
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
                "note": f"Locks in {wait:g} hour{'' if wait == 1 else 's'} after kickoff"}

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
            "opponentShort": (upcoming.get("opponentShort")
                              or upcoming.get("opponent") or upcoming["column"]),
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

    matched = [c for c in game_cols if c in pairs]
    log(f"  matched {len(matched)} of {len(game_cols)} sheet columns to real games")
    for col in game_cols:
        ev = pairs.get(col)
        if ev:
            log(f"    {col} -> {ev['opponent']} ({ev['kickoff'] or 'no kickoff time yet'})")
        else:
            log(f"  ! no match for {col!r} — searched for {column_alias(col)!r}")

    games = []
    for col in game_cols:
        ev = pairs.get(col)
        outcome = settle(col, ev, cfg)
        games.append({
            "column": col,
            "opponent": ev["opponent"] if ev else col,
            "opponentShort": ev["opponentShort"] if ev else col,
            "kickoff": ev["kickoff"] if ev else None,
            "site": ("neutral" if ev and ev["neutral"] else
                     ev["homeAway"] if ev else None),
            "logo": ev["logo"] if ev else None,
            "abbr": ev["abbr"] if ev else (column_alias(col)[:4].upper() or "TBD"),
            "color": ev["color"] if ev else "#7A6857",
            **outcome,
        })

    # Now that each column has an opponent, the answers can be read.
    us_names = us_team_names(cfg)
    unreadable = {}
    for p in people:
        for col in game_cols:
            raw = p["raw"].get(col, "")
            pick = resolve_pick(raw, col, pairs.get(col), us_names)
            p["picks"][col] = pick
            if raw and pick is None:
                unreadable.setdefault(f"{col} = {raw!r}", 0)
                unreadable[f"{col} = {raw!r}"] += 1

    read = sum(1 for p in people for v in p["picks"].values() if v)
    total = len(people) * len(game_cols)
    log(f"  read {read} of {total} answers")
    for label, n in sorted(unreadable.items(), key=lambda kv: -kv[1])[:12]:
        log(f"  ! couldn't read {label} ({n}x)")

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
