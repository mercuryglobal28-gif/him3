import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import iptv_generator as g


def row(name, cid='', url='https://example.com/live.m3u8'):
    return {'name': name, 'meta': f'#EXTINF:-1 tvg-id="{cid}",{name}', 'url': url, 'extras': [], 'donor': 'fixture'}


class GeneratorTests(unittest.TestCase):
    def test_removed_domain_ban(self):
        self.assertFalse(g.forbidden('https://ru1.plist.link/live'))
        self.assertTrue(g.forbidden('file:///etc/passwd'))

    def test_optional_blocklist(self):
        with patch.object(g, 'BLOCKED', ('example.com',)):
            self.assertTrue(g.forbidden('https://sub.example.com/live'))
            self.assertFalse(g.forbidden('https://notexample.com/live'))

    def test_filters_opt_in(self):
        with patch.object(g, 'FILTER_ADULT', False):
            self.assertTrue(g.suitable('Adult'))
        with patch.object(g, 'FILTER_ADULT', True):
            self.assertFalse(g.suitable('Adult'))

    def test_provider_ids_can_match_catalog(self):
        self.assertEqual(g.channel_match(row('Россия 1 HD', 'chnl-2'), row('Россия 1 (720p)', 'Rossiya1.ru@SD')), 100)

    def test_global_id_conflict(self):
        self.assertEqual(g.channel_match(row('News', 'A.us'), row('News', 'B.us')), 0)

    def test_feed_ids(self):
        self.assertEqual(g.channel_match(row('A', 'Station.us@HD'), row('B', 'Station.us@SD')), 100)

    def test_numbered_stations_and_timeshifts(self):
        self.assertEqual(g.channel_match(row('Россия 1'), row('Россия 2')), 0)
        self.assertEqual(g.channel_match(row('Россия 1 +2'), row('Россия 1 +4')), 0)

    def test_russian_catalog_aliases(self):
        self.assertEqual(g.channel_match(row('НТВ HD', 'chnl-4'), row('NTV (576i)', 'NTV.ru@SD')), 100)
        self.assertEqual(g.channel_match(row('Первый HD', 'chnl-1'), row('Channel One', 'ChannelOne.ru@SD')), 100)

    def test_arabic_normalization(self):
        self.assertEqual(g.name_key('الإِخْبَارِيَّة HD'), g.name_key('الاخبارية'))

    def test_commas(self):
        parsed = g.parse_m3u('#EXTM3U\n#EXTINF:-1 group-title="News, World",News, World\n#EXTVLCOPT:http-referrer=https://example.org\nhttps://example.com/live\n')
        self.assertEqual(parsed[0]['name'], 'News, World')
        self.assertEqual(len(parsed[0]['extras']), 1)

    def test_new_channel_limits_and_dedup(self):
        with patch.object(g, 'APPEND_NEW', True), patch.object(g, 'MAX_NEW', 1):
            result = g.extend_channels([row('A', 'A.us')], [row('A HD', 'A.us@HD'), row('B', 'B.us'), row('C', 'C.us')])
        self.assertEqual([r['name'] for r in result], ['A', 'B'])

    def test_cache_isolation(self):
        self.assertNotEqual(g.cache_key(row('News', 'A.us')), g.cache_key(row('News', 'B.us')))

    def test_skip_unneeded_alternatives_and_share_checks(self):
        plans = [(row('A'), 'a', [('source', 'good'), ('donor', 'unused')]),
                 (row('B'), 'b', [('source', 'bad'), ('donor', 'good')])]
        with patch.object(g, 'validate', side_effect=lambda u, *args: (u == 'good', 'test')) as validate:
            checks = g.check_plans(plans)
        self.assertEqual(validate.call_count, 2)
        self.assertNotIn(g.target('unused'), checks)

    def test_no_output_keeps_playlist_but_writes_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, cache, report = [root / n for n in ('source', 'out', 'cache', 'report')]
            source.write_text('#EXTM3U\n#EXTINF:-1,News\nhttps://example.com/dead\n')
            output.write_text('previous playlist')
            cache.write_text('{}')
            with patch.multiple(g, SOURCE=source, OUT=output, CACHE=cache, REPORT=report, APPEND_NEW=False, KEEP_UNVERIFIED=False), patch.object(g, 'load_donors', return_value=[]), patch.object(g, 'validate', return_value=(False, 'http_403')):
                self.assertEqual(g.main(), 2)
            self.assertEqual(output.read_text(), 'previous playlist')
            self.assertEqual(json.loads(report.read_text())['checks']['http_403'], 1)

    def test_source_roster_and_header_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, cache, report = [root / n for n in ('source', 'out', 'cache', 'report')]
            content = '#EXTM3U url-tvg="https://example.com/epg.xml"\n#EXTINF:-1 tvg-id="chnl-1",Россия 1\nhttps://example.com/dead\n#EXTINF:-1,НТВ\nhttps://example.com/good\n'
            source.write_text(content)
            donors = [row('Unrelated foreign channel', 'Other.us')]
            with patch.multiple(g, SOURCE=source, OUT=output, CACHE=cache, REPORT=report, APPEND_NEW=False, KEEP_UNVERIFIED=True), patch.object(g, 'load_donors', return_value=[('fixture', 'https://example.com/list')]), patch.object(g, 'fetch_donor', return_value=donors), patch.object(g, 'validate', side_effect=lambda u, *args: ('good' in u, 'fixture')):
                self.assertEqual(g.main(), 0)
            self.assertEqual(source.read_text(), content)
            self.assertEqual([r['name'] for r in g.parse_m3u(output.read_text())], ['Россия 1', 'НТВ'])
            self.assertTrue(output.read_text().startswith(content.splitlines()[0]))
            result = json.loads(report.read_text())
            self.assertEqual(result['verified_count'], 1)
            self.assertEqual(result['output_count'], 2)
            self.assertEqual(result['stats']['retained_unverified'], 1)

    def test_successful_discovery_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, cache, report = [root / n for n in ('source', 'out', 'cache', 'report')]
            source.write_text('#EXTM3U\n#EXTINF:-1 tvg-id="chnl-1",News HD\nhttps://example.com/dead\n')
            donors = [row('News', 'News.us', 'https://example.com/good'), row('Other', 'Other.us', 'https://example.com/new')]
            with patch.multiple(g, SOURCE=source, OUT=output, CACHE=cache, REPORT=report, APPEND_NEW=True, MAX_NEW=2), patch.object(g, 'load_donors', return_value=[('fixture', 'https://example.com/list')]), patch.object(g, 'fetch_donor', return_value=donors), patch.object(g, 'validate', side_effect=lambda u, *args: ('dead' not in u, 'fixture')):
                self.assertEqual(g.main(), 0)
            self.assertEqual(len(g.parse_m3u(output.read_text())), 2)
            self.assertIn('https://example.com/good', output.read_text())
            self.assertEqual(json.loads(report.read_text())['discovered_count'], 1)


if __name__ == '__main__':
    unittest.main()
