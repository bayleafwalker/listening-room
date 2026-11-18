"""Projection and safe markup for the Twitch browser."""
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import channel_browse as browse


class BrowseProjection(unittest.TestCase):
    def setUp(self):
        self.channels = [
            {'login': 'alpha', 'name': 'Alpha', 'title': 'Late night piano', 'live': True},
            {'login': 'beta', 'name': 'Beta', 'title': '<b>soft & quiet</b>', 'live': None},
        ]

    def test_searches_name_login_and_title_case_insensitively(self):
        for query in ('ALPHA', 'alpha', 'NIGHT'):
            self.assertEqual([c['login'] for c in browse.project(self.channels, query)], ['alpha'])
        self.assertEqual(browse.project(self.channels, 'missing'), [])

    def test_saved_filter_and_global_exclusion_flags_do_not_mutate_input(self):
        projected = browse.project(self.channels, saved_only=True, saved=['BETA'], excluded=['beta'])
        self.assertEqual([c['login'] for c in projected], ['beta'])
        self.assertTrue(projected[0]['saved'])
        self.assertTrue(projected[0]['excluded'])
        self.assertNotIn('saved', self.channels[1])

    def test_saved_membership_uses_the_current_pool_after_removal(self):
        old=[dict(self.channels[0],curated=True)]
        self.assertEqual(browse.project(old,saved_only=True,saved=[]),[])
        self.assertFalse(browse.project(old,saved=[])[0]['saved'])

    def test_highlight_escapes_markup_even_when_query_matches_markup(self):
        self.assertEqual(browse.highlight('<b>soft & quiet</b>', 'soft'),
                         '&lt;b&gt;<b>soft</b> &amp; quiet&lt;/b&gt;')
        self.assertEqual(browse.highlight('<b>', '<b>'), '<b>&lt;b&gt;</b>')
        self.assertEqual(browse.highlight('a & b', ''), 'a &amp; b')


if __name__ == '__main__':
    unittest.main()
