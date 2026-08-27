#!/usr/bin/env python3
"""
Builds preview data from the real 2025 pool.

Names, final scores, bowl picks, and tiebreaker guesses are the actual numbers
from the 2025 response sheet, and Nebraska's results are the real 2025 season.
The individual game picks were blank in the exported sheet, so they're
reconstructed here: each person's picks are generated to add up to exactly the
score they really finished with, with misses assigned to the games that were
genuinely hard to call.

Everything downstream of that runs through the same compose() the live
collector uses, so the preview exercises the real ranking, history, movement,
and elimination logic.

    python scripts/demo_2025.py                 # full season
    python scripts/demo_2025.py --through 9     # as if only 9 games are done
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import compose  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO = "https://a.espncdn.com/i/teamlogos/ncaa/500/{}.png"

# column, opponent, result, NEB pts, opp pts, site, kickoff, ESPN id, abbr, colour
SEASON = [
    ("Cincinatti",   "Cincinnati",        "W", 20, 17, "neutral", "2025-08-29T00:00Z", 2132, "CIN",  "#E00122"),
    ("Akron",        "Akron",             "W", 68,  0, "home",    "2025-09-06T23:30Z", 2006, "AKR",  "#041E42"),
    ("HCU",          "Houston Christian", "W", 59,  7, "home",    "2025-09-13T16:00Z", 2277, "HCU",  "#20366B"),
    ("Michigan",     "Michigan",          "L", 27, 30, "home",    "2025-09-20T23:30Z",  130, "MICH", "#00274C"),
    ("Mich State",   "Michigan State",    "W", 38, 27, "home",    "2025-10-04T16:00Z",  127, "MSU",  "#18453B"),
    ("Maryland",     "Maryland",          "W", 34, 31, "away",    "2025-10-11T16:00Z",  120, "MD",   "#E03A3E"),
    ("Minnesota",    "Minnesota",         "L",  6, 24, "away",    "2025-10-18T16:00Z",  135, "MINN", "#7A0019"),
    ("Northwestern", "Northwestern",      "W", 28, 21, "home",    "2025-10-25T18:30Z",   77, "NW",   "#4E2A84"),
    ("USC",          "USC",               "L", 17, 21, "home",    "2025-11-01T23:30Z",   30, "USC",  "#990000"),
    ("UCLA",         "UCLA",              "W", 28, 21, "away",    "2025-11-09T01:00Z",   26, "UCLA", "#2D68C4"),
    ("Penn State",   "Penn State",        "L", 10, 37, "away",    "2025-11-23T00:00Z",  213, "PSU",  "#041E42"),
    ("Iowa",         "Iowa",              "L", 16, 40, "home",    "2025-11-28T17:00Z", 2294, "IOWA", "#FFCD00"),
    ("Bowl Game",    "Utah",              "L", 22, 44, "neutral", "2025-12-20T22:30Z",  254, "UTAH", "#CC0000"),
]

# name, final score, bowl pick, tiebreaker guess
ENTRIES = [
    ("Beth Houpt",       12, "L", 380),
    ("Dan Sullivan",     12, "W", 328),
    ("Zach Stull",       11, "W", 348),
    ("Bailey Rasmussen", 11, "W", 345),
    ("Adam Werner",      10, "W", 369),
    ("Zac Gillman",      10, "W", 337),
    ("Ross Rasmussen",   10, "W", 420),
    ("Karen Knudson",     9, "W", 397),
    ("Kevin Knudson",     9, "W", 398),
    ("Callie Knudson",    9, "W", 412),
    ("Steve Warner",      9, "W", 334),
    ("Chris Daley",       9, "W", 316),
    ("Joe Toth",          8, "W", 365),
    ("Katie Rerucha",     8, "L", 310),
    ("AJ Rerucha",        5, "W", 444),
]

# Realistically-missed games, hardest to call first. Misses are handed out down
# this list until each person's score works out.
MISS_ORDER = ["Minnesota", "USC", "Iowa", "Michigan", "Penn State", "Maryland",
              "Mich State", "UCLA", "Northwestern", "Cincinatti", "Akron", "HCU"]

ACTUAL = {row[0]: row[2] for row in SEASON}


def build(through=None):
    """`through` = pretend only that many games have finished."""
    played = len(SEASON) if through is None else max(0, min(through, len(SEASON)))

    games = []
    for i, (col, opp, res, neb, opp_pts, site, date, tid, abbr, color) in enumerate(SEASON):
        done = i < played
        games.append({
            "column": col, "opponent": opp, "kickoff": date, "site": site,
            "logo": LOGO.format(tid), "abbr": abbr, "color": color,
            "state": "official" if done else "scheduled",
            "result": res if done else None,
            "nebraskaPoints": neb if done else None,
            "opponentPoints": opp_pts if done else None,
            "note": None,
        })

    people = []
    for name, score, bowl_pick, guess in ENTRIES:
        regular_right = score - (1 if bowl_pick == ACTUAL["Bowl Game"] else 0)
        misses = set(MISS_ORDER[: 12 - regular_right])

        picks = {}
        for col, _, res, *_ in SEASON:
            if col == "Bowl Game":
                picks[col] = bowl_pick
            elif col in misses:
                picks[col] = "W" if res == "L" else "L"
            else:
                picks[col] = res

        got = sum(1 for col, _, res, *_ in SEASON if picks[col] == res)
        assert got == score, f"{name}: rebuilt {got}, sheet says {score}"
        people.append({"name": name, "picks": picks, "guess": guess})

    team = {"teamLogo": LOGO.format(158), "teamAbbr": "NEB", "teamColor": "#E41C38"}
    data = compose(games, people, 2025, team)
    data["generatedAt"] = "2025-12-21T06:00:00+00:00" if through is None \
        else games[played - 1]["kickoff"].replace("Z", "+00:00")
    return data


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--through", type=int, default=None,
                    help="only treat this many games as finished")
    ap.add_argument("--out", default="data/standings.json")
    args = ap.parse_args()

    data = build(args.through)
    path = os.path.join(ROOT, args.out)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    r = data["record"]
    nxt = data["nextGame"]
    print(f"{args.out}: Nebraska {r['wins']}-{r['losses']}, {r['pointsSoFar']} points, "
          f"{r['played']}/{r['scheduled']} played, {data['aliveCount']} alive")
    if nxt:
        s = nxt["split"]
        print(f"  next: {nxt['opponent']} — {s['nebraska']} picked Nebraska, "
              f"{s['opponent']} picked {nxt['abbr']}")
    for s in data["standings"][:6]:
        mv = "" if s["movement"] is None else f"  ({s['movement']:+d})"
        out = "  OUT" if s["eliminated"] else ""
        print(f"  {s['rank']:>2}. {s['name']:<18} {s['correct']:>2} right"
              f"  max {s['maxPossible']}{mv}{out}")


if __name__ == "__main__":
    main()
