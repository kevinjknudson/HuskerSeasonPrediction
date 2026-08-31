# Husker Prediction Pool

Standings that keep themselves current. A scheduled job checks Nebraska's score
after each game, marks everyone right or wrong, adds up the season point total
for the tiebreaker, and rewrites the leaderboard. You set it up once in August
and don't touch it again.

```
config.json                  settings you edit (once)
photos/                      headshots for each entrant (optional)
scripts/update.py            collects results, recalculates standings
scripts/demo_2025.py         builds the 2025 preview data
.github/workflows/           the hourly job
data/standings.json          generated — don't edit by hand
index.html                   the page people visit
mockup.html                  offline preview, 2025 final
mockup-midseason.html        offline preview, as of game 11
```

## Trying it before the season starts

`mockup.html` is self-contained — open it in any browser, no server needed.
Names, final scores, bowl picks, and tiebreaker guesses are the real numbers
from last year's sheet, and the results are the real 2025 season (Nebraska 7-6,
373 points, matching the `Actual` row exactly).

The individual game picks were blank in the exported sheet, so
`scripts/demo_2025.py` reconstructs them: each person's picks total exactly the
score they really finished with, with misses assigned to the games that were
genuinely hard to call. The layout and math are real; those thirteen cells per
person are the invented part.

Two previews ship, because the page looks quite different partway through:

- `mockup.html` — the finished 2025 season.
- `mockup-midseason.html` — the same pool as of game 11, so you can see the
  things that only exist mid-season: rank movement, who's been eliminated, the
  next-game pick split, and a chart that stops partway.

Regenerate either with `python scripts/demo_2025.py --through 11`. Delete the
mockups, `scripts/demo_2025.py`, and `data/standings.json` whenever you like —
real data replaces them on the first workflow run.

## Setup

**1. Create the repo.** Drop these files in, commit to `main`.

**2. Turn on Pages.** Settings → Pages → Deploy from a branch → `main` / `/ (root)`.
Site lands at `https://<you>.github.io/<repo>/`.

**3. Allow the job to commit.** Settings → Actions → General → Workflow
permissions → **Read and write permissions** → Save. Without this the job runs
but can't push its results.

**4. Point it at your picks.** In `config.json`, replace `picksLink` with the
share link to your Google Sheet of responses. Either a normal `/edit` link (set
to "Anyone with the link can view") or a File → Share → Publish to web CSV link.

**5. Check the column names.** Every sheet column that isn't Timestamp, Email,
Name, or a tiebreaker is treated as a game. A leading `Nebraska vs`, `Nebraska
at`, or `Nebraska @` is stripped, and the rest is matched against the real
opponent — exact names beat partial ones, and longer partials beat shorter ones,
so `Nebraska vs Ohio` can't steal the Ohio State game.

Anything that still won't match needs an entry in `aliases`:

```json
"aliases": { "HCU": ["houston christian"] }
```

Whichever column holds the bowl pick goes in `bowlColumns` — for 2026 that's
`Bowl Game Performance`.

**6. Add the results API key.** Get a free key at
collegefootballdata.com/key, then in the repo go to Settings → Secrets and
variables → Actions → New repository secret. Name it `CFBD_API_KEY` and paste
the key. Never put it in `config.json` — this repo is public.

This step is optional. Without it everything still works off ESPN, but ESPN
blocks datacentre IPs unpredictably, so the key is worth the two minutes.

**7. Run it once.** Actions tab → Update standings → Run workflow.

## Photos

Drop headshots in `photos/` and map them by name in `config.json`:

```json
"photos": {
  "Beth Houpt": "photos/beth-houpt.jpg",
  "Dan Sullivan": "photos/dan-sullivan.jpg"
}
```

Names must match the sheet exactly (case doesn't matter). Square images around
400×400 work best — they're cropped to a circle, so keep faces centred. Anyone
without a photo falls back to initials on a scarlet disc, and a photo that fails
to load falls back the same way, so a broken path never leaves a hole.

## Movement, stakes, and the week-by-week chart

Rank history isn't stored anywhere. Every run recomputes the whole season from
scratch — rank after game 1, after game 2, and so on — by scoring everyone
against only the games finished by that point. That means it self-heals: fix a
wrong result or a mistyped pick and the entire history corrects itself on the
next run, including the chart and every movement arrow.

- **Movement** on each card compares the current rank to the rank before the
  most recent game. No arrow means no change.
- **Eliminated** means that even a perfect finish can't reach the leader's
  current score. Those cards grey out and the panel header counts how many can
  still win. Nobody is eliminated until it's mathematically true.
- **The chart** plots each person's rank across every finished game. Drag the
  slider or press play and the faces move to their position that week.

## How answers are read

The form can ask either way. `Win`/`Lose` and `W`/`L` both work, and so does the
name of the team you think wins — `Nebraska` or `Iowa`. Team names are matched
against that game's actual opponent rather than by first letter, because
`Washington` starts with a W and is not a win. The log reports how many answers
it read and lists anything it couldn't, so a mismatch shows up immediately
instead of quietly scoring as blank.

## How the pick chart reads

Each cell is the team that person picked to **win** that game — the Nebraska
mark if they picked a Husker win, the opponent's mark if they picked a loss.
Correct picks are full colour, wrong ones grey out. The top row is what actually
happened. Games that haven't been played sit on a neutral grey column, so
they're never mistaken for wrong picks.

Team marks are the school's colour and abbreviation with the real logo layered
on top, pulled from ESPN. If a logo ever fails to load, the coloured badge
underneath still reads correctly, so the chart never breaks.

## How it runs

Hourly, August through January. Each run:

1. Pulls Nebraska's schedule, scores, logos, and team colours from
   CollegeFootballData if `CFBD_API_KEY` is set, and from ESPN's public JSON
   feed otherwise. If CFBD errors or returns nothing, it falls back to ESPN on
   its own — the log names whichever source answered.
2. Ignores any game that isn't final, or that kicked off less than
   `settleHours` (1) ago — it shows as *result under review* but doesn't count
   yet. This is the buffer against a feed calling a game early.
3. Pulls current picks from the sheet.
4. Recomputes correct picks, best possible finish, rank history, movement,
   elimination, the next game's pick split, Nebraska's running point total, and
   — once every game is official — each person's tiebreaker distance.
5. Commits `data/standings.json` only if something actually changed.

Late entries and corrected picks are picked up automatically, since the sheet is
re-read every hour. If someone submits the form twice, the later entry wins.

## When something looks wrong

**A game shows "No matching game found yet."** The column name didn't match any
opponent. Add an alias in `config.json` and commit — that push re-runs the job.

**ESPN has it wrong, or you want to force a result.** Use an override:

```json
"overrides": {
  "Iowa": { "result": "W", "nebraskaPoints": 24, "opponentPoints": 17 }
}
```

An override wins over anything the feed says and takes effect on the next run.

**Nothing has updated in a while.** GitHub pauses scheduled workflows on repos
with no commits for 60 days — push anything to wake it up. Scheduled runs also
get delayed when GitHub is busy. Actions → Run workflow forces it immediately.

## Notes

- Nobody's email address ever reaches the site. The script reads the sheet
  inside the job and writes only names, picks, and scores.
- ESPN's JSON feed is public but unofficial. CollegeFootballData is a proper
  documented API and is the better primary. If both break, the overrides above
  will carry you through the season.
- Before the opener the page shows everyone who entered rather than a podium of
  people tied at zero, and the chart stays hidden until two games are done.
- `settleHours` controls the delay before a result counts, and the workflow
  runs every 20 minutes in season, so a final score lands on the page inside
  about 80 minutes of kickoff plus game length. Set it to `0` for the moment
  the feed says final.
- Type is Rubik and Karla from Google Fonts. Colours live in the `:root` block
  at the top of `index.html` — `--turf`, `--scarlet`, `--cream`, and `--chalk`
  between them control almost everything, including the mown-stripe and
  yard-line background.
- The plate look comes from four `clip-path` variables in that same `:root`
  block (`--cut-lg`, `--cut-md`, `--cut-sm`, `--cut-tab`). Raising or lowering
  the pixel values makes every cut corner on the page sharper or softer at once;
  set them all to `none` for plain rectangles.
