import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import update_matchups as updater


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 10, 6, 18, 0, tzinfo=timezone.utc)


class UpdateTests(unittest.TestCase):
    def run_update(self, games, events=None, announcement=None, error=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'game-of-the-week.json').write_text(json.dumps(games))
            with patch.object(updater, 'ROOT', root), patch.object(updater, 'datetime', FixedClock), patch.object(updater, 'scoreboard', return_value=events or [], side_effect=error) as feed, patch.object(updater, 'featured', return_value=announcement):
                failed = False
                try:
                    updater.main()
                except SystemExit:
                    failed = True
                return (json.loads((root / 'game-of-the-week.json').read_text()),
                        json.loads((root / 'matchup-update-status.json').read_text()),
                        {call.args[0] for call in feed.call_args_list}, failed)

    def setUp(self):
        self.games = []
        for week in range(1, 19):
            day = datetime(2026, 9, 13) + timedelta(weeks=week - 1)
            game = {'week': week, 'date': day.strftime('%A, %B ') + str(day.day) + (f', {day.year}' if day.year != 2026 else ''), 'time': 'TBD', 'announced': False}
            if week <= 5 or week % 2:
                game.update(announced=True, kickoff=day.replace(hour=15, tzinfo=updater.CT).isoformat(), awayAbbr='AAA', homeAbbr='BBB')
            self.games.append(game)

    def test_missing_announcements_preserve_boards_without_failure(self):
        games, status, requested, failed = self.run_update(self.games)
        self.assertFalse(failed)
        self.assertEqual(games, self.games)
        self.assertEqual(status['lockedWeeks'], [1, 2, 3, 4])
        self.assertEqual(requested, set(range(5, 19)))
        self.assertEqual(status['awaitingWeeks'], list(range(5, 19)))
        self.assertEqual(status['errors'], [])

    def test_real_feed_errors_still_fail(self):
        games, status, requested, failed = self.run_update(self.games, error=RuntimeError('feed unavailable'))
        self.assertTrue(failed)
        self.assertEqual(games, self.games)
        self.assertEqual(len(status['errors']), 14)
        self.assertEqual(requested, set(range(5, 19)))

    def test_verified_announcement_updates_only_upcoming_board(self):
        event = {'id': '123', 'date': '2026-10-11T20:25:00Z', 'competitions': [{'competitors': [
            {'homeAway': 'away', 'team': {'name': '49ers', 'displayName': 'San Francisco 49ers', 'abbreviation': 'SF'}},
            {'homeAway': 'home', 'team': {'name': 'Seahawks', 'displayName': 'Seattle Seahawks', 'abbreviation': 'SEA'}}
        ], 'broadcasts': [{'names': ['FOX']}]}]}
        def announcement(week, events):
            return (event, 'https://www.foxsports.com/verified') if week == 5 else None
        with patch.object(updater, 'featured', side_effect=announcement):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / 'game-of-the-week.json').write_text(json.dumps(self.games))
                with patch.object(updater, 'ROOT', root), patch.object(updater, 'datetime', FixedClock), patch.object(updater, 'scoreboard', return_value=[event]):
                    updater.main()
                games = json.loads((root / 'game-of-the-week.json').read_text())
                self.assertEqual(games[:4], self.games[:4])
                self.assertEqual(games[4]['source'], 'https://www.foxsports.com/verified')
                self.assertEqual(games[5:], self.games[5:])

    def test_past_unannounced_date_and_next_year_date(self):
        games = copy.deepcopy(self.games)
        games[3] = {'week': 4, 'date': 'Sunday, October 4', 'time': 'TBD', 'announced': False}
        result, status, requested, failed = self.run_update(games)
        self.assertFalse(failed)
        self.assertNotIn(4, requested)
        self.assertIn(18, requested)
        self.assertEqual(result, games)


if __name__ == '__main__':
    unittest.main()
