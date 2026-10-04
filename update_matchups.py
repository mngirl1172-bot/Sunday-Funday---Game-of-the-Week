"""Verify schedule times and read FOX's weekly featured broadcast assignments."""
import csv
import io
import json
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
SEASON = 2026
CT = ZoneInfo('America/Chicago')

def fetch(url):
    for attempt in range(3):
        try:
            with urlopen(Request(url, headers={'User-Agent': 'Mozilla/5.0'}), timeout=25) as response:
                return response.read()
        except HTTPError as exc:
            if exc.code == 404 or attempt == 2:
                raise
        except Exception:
            if attempt == 2:
                raise

def scoreboard(week):
    url = f'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={SEASON}&seasontype=2&week={week}&limit=100'
    data = json.loads(fetch(url))
    if data.get('season', {}).get('year') != SEASON or data.get('week', {}).get('number') != week:
        raise ValueError('Score feed returned a different season or week')
    return data.get('events', [])

def team_pair(event):
    competitors = event['competitions'][0]['competitors']
    return tuple(next(c['team'] for c in competitors if c['homeAway'] == side) for side in ('away', 'home'))

def is_fox(event):
    return any('FOX' in b.get('names', []) for b in event['competitions'][0].get('broadcasts', []))

def featured(week, events):
    url = f'https://www.foxsports.com/stories/presspass/fox-nfl-week-{week}-broadcast-assignments'
    try:
        page = fetch(url).decode()
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    # Require the current season in the image URL; FOX reuses story slugs.
    images = sorted(set(re.findall(r'https://statics\.foxsports\.com/[^\s"<>]+\.(?:png|jpg)', page)))
    images = [u for u in images if f'/{SEASON}/' in u and re.search(rf'week[-_]{week}[-_].*announcer', u, re.I)]
    matches = set()
    for image_url in images:
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / 'assignment.png'
            image.write_bytes(fetch(image_url))
            result = subprocess.run(['tesseract', str(image), 'stdout', 'tsv'], capture_output=True, text=True, check=True)
            words = [r for r in csv.DictReader(io.StringIO(result.stdout), delimiter='\t') if r['text'].strip()]
        anchors = [r for r in words if 'BURKHARDT' in r['text'].upper()]
        for anchor in anchors:
            y, h, x = int(anchor['top']), int(anchor['height']), int(anchor['left'])
            band = [r for r in words if abs(int(r['top']) - y) <= max(50, h * 4)]
            if not any('BRADY' in r['text'].upper() for r in band):
                continue
            # Team nicknames are printed to the left of the broadcast crew.
            names = re.sub(r'[^A-Z0-9]', '', ''.join(r['text'].upper() for r in band if int(r['left']) < x))
            for event in events:
                away, home = team_pair(event)
                normalize = lambda s: re.sub(r'[^A-Z0-9]', '', s.upper())
                if is_fox(event) and all(normalize(t['name']) in names for t in (away, home)):
                    matches.add(event['id'])
    selected = [e for e in events if e['id'] in matches]
    if len(selected) > 1:
        raise ValueError('FOX assignment is ambiguous; keeping existing matchup')
    return (selected[0], url) if selected else None

def apply_event(game, event, source=None):
    away, home = team_pair(event)
    kickoff = datetime.fromisoformat(event['date'].replace('Z', '+00:00')).astimezone(CT)
    if kickoff.year not in (SEASON, SEASON + 1):
        raise ValueError('Invalid kickoff year')
    game.update(date=kickoff.strftime('%A, %B ') + str(kickoff.day) + (f', {kickoff.year}' if kickoff.year != SEASON else ''),
                time=kickoff.strftime('%I:%M %p CT').lstrip('0'),
                away=away['displayName'], awayAbbr=away['abbreviation'],
                home=home['displayName'], homeAbbr=home['abbreviation'],
                kickoff=kickoff.isoformat(), announced=True)
    if source:
        game['source'] = source

def main():
    path = ROOT / 'game-of-the-week.json'
    games = json.loads(path.read_text())
    if len(games) != 18 or [g['week'] for g in games] != list(range(1, 19)):
        raise ValueError('Expected the 18 regular season boards')
    checked, errors = [], []
    def prepare(week):
        try:
            events = scoreboard(week)
            return events, featured(week, events), None
        except Exception as exc:
            return None, None, exc
    with ThreadPoolExecutor(max_workers=4) as pool:
        prepared = list(pool.map(prepare, range(1, 19)))
    for game, (events, announcement, error) in zip(games, prepared):
        week = game['week']
        try:
            if error:
                raise error
            if announcement:
                apply_event(game, *announcement)
                checked.append(week)
                print(f'Week {week}: verified FOX featured broadcast')
            elif game.get('announced'):
                # Keep the chosen matchup, but follow official flexed kickoff times.
                candidates = [e for e in events if all(t['abbreviation'] == game.get(key) for t, key in zip(team_pair(e), ('awayAbbr', 'homeAbbr'))) and is_fox(e)]
                if len(candidates) != 1:
                    raise ValueError('Existing matchup is absent from the FOX schedule')
                apply_event(game, candidates[0])
                checked.append(week)
                print(f'Week {week}: verified existing matchup and kickoff')
            else:
                print(f'Week {week}: awaiting a verified FOX announcement')
        except Exception as exc:
            errors.append({'week': week, 'error': str(exc)})
            print(f'Week {week}: {exc}')
    path.write_text(json.dumps(games, indent=2) + '\n')
    (ROOT / 'matchup-update-status.json').write_text(json.dumps({'checkedAt': datetime.now(timezone.utc).isoformat(), 'verifiedWeeks': checked, 'errors': errors}, indent=2) + '\n')
    if errors:
        raise SystemExit('Some weeks could not be verified; see matchup-update-status.json')

if __name__ == '__main__':
    main()
