import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import iptv_generator as g


def row(name, cid='', url='https://example.com/live.m3u8'):
    return {'name': name, 'meta': f'#EXTINF:-1 tvg-id="{cid}",{name}', 'url': url, 'extras': [], 'donor': 'fixture'}


class GeneratorTests(unittest.TestCase):
    def test_spacing_quality_and_cyrillic_spelling(self):
        variants = ['amedia1', 'Amedia 1 HD', 'AMEDIA1HD', 'amedia1hd',
                    'Amedia_1', 'Amedia-1 (1080p)', 'A media 1', 'Амедиа 1 HD',
                    'Amedia 1 FHD', 'Amedia 1 UHD', 'Amedia 1 Full HD']
        for left in variants:
            for right in variants:
                with self.subTest(left=left, right=right):
                    self.assertEqual(g.channel_match(row(left), row(right)), 100)

    def test_do_not_merge_different_stations(self):
        for left, right in [('Amedia1','Amedia2 HD'), ('Amedia','Amedia Premium'),
                            ('Россия 1 +2','Россия 1'), ('1+1','11'),
                            ('TV 1 2','TV 12'), ('НТВ','НТВ4')]:
            with self.subTest(left=left, right=right):
                self.assertLess(g.channel_match(row(left), row(right)), g.FUZZY_THRESHOLD)

    def test_shift_guard_precedes_shared_id_and_aliases(self):
        self.assertEqual(g.channel_match(row('НТВ +2','NTV.ru@SD'), row('НТВ +4','NTV.ru@HD')), 0)
        self.assertNotEqual(g.cache_key(row('НТВ +2','NTV.ru')), g.cache_key(row('НТВ +4','NTV.ru')))

    def test_minor_typo_with_same_channel_number(self):
        self.assertGreaterEqual(g.channel_match(row('Amedia 1'), row('Amedya 1 HD')), g.FUZZY_THRESHOLD)

    def test_normalized_names_reach_candidate_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, cache, report = [root / n for n in ('source','out','cache','report')]
            source.write_text('#EXTM3U\n#EXTINF:-1,amedia1\nhttps://example.com/dead\n')
            donors = [row('Amedia 2 HD', url='https://example.com/wrong'),
                      row('Амедиа 1 HD', url='https://example.com/good')]
            with patch.multiple(g, SOURCE=source, OUT=output, CACHE=cache, REPORT=report, APPEND_NEW=False, KEEP_UNVERIFIED=False), patch.object(g, 'load_donors', return_value=[('fixture','https://example.com/list')]), patch.object(g, 'fetch_donor', return_value=donors), patch.object(g, 'validate', side_effect=lambda u, *args: ('good' in u, 'fixture')) as check:
                self.assertEqual(g.main(), 0)
            result = g.parse_m3u(output.read_text())
            self.assertEqual(result[0]['name'], 'amedia1')
            self.assertEqual(result[0]['url'], 'https://example.com/good')
            self.assertNotIn('https://example.com/wrong', [call.args[0] for call in check.call_args_list])

    def test_mcquack_block_and_subdomains(self):
        for url in ('http://stream.mcquack.net/live','https://sub.stream.mcquack.net/live','https://STREAM.MCQUACK.NET./live'):
            self.assertTrue(g.forbidden(url))
        self.assertFalse(g.forbidden('https://notstream.mcquack.net/live'))

    def test_older_sources_config_includes_smtk(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'sources.json'
            config.write_text('[{"name":"old","url":"https://example.com/list"}]')
            with patch.object(g, 'SOURCE_CONFIG', config):
                self.assertIn(('jekalich-smtk', g.SMTK_URL), g.load_donors())

    def test_channels_progress_without_global_round_barrier(self):
        ready = threading.Event()
        order = []
        def check(url, *args):
            if url == 'slow':
                ready.wait(2)
                order.append('slow_finished')
                return False, 'timeout'
            if url == 'fallback':
                order.append('fallback_checked')
                ready.set()
                return True, 'fixture'
            return False, 'fixture'
        plans = [({}, 'slow', [('original','slow')]),
                 ({}, 'fast', [('original','bad'),('donor','fallback')])]
        with patch.object(g, 'WORKERS', 2), patch.object(g, 'validate', side_effect=check):
            g.check_plans(plans)
        self.assertLess(order.index('fallback_checked'), order.index('slow_finished'))

    def test_smtk_is_checked_before_dead_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output, cache, report = [root/n for n in ('source','out','cache','report')]
            source.write_text('#EXTM3U\n#EXTINF:-1,Amedia 1 HD\nhttps://example.com/dead\n')
            donor = dict(row('Amedia 1',url='https://example.com/good'),donor='jekalich-smtk')
            with patch.multiple(g, SOURCE=source,OUT=output,CACHE=cache,REPORT=report,APPEND_NEW=False,KEEP_UNVERIFIED=False), patch.object(g,'load_donors',return_value=[('jekalich-smtk',g.SMTK_URL)]), patch.object(g,'fetch_donor',return_value=[donor]), patch.object(g,'validate',return_value=(True,'fixture')) as check:
                self.assertEqual(g.main(),0)
            self.assertEqual(check.call_count,1)
            self.assertEqual(check.call_args.args[0],'https://example.com/good')
            self.assertEqual(g.parse_m3u(output.read_text())[0]['name'],'Amedia 1 HD')

    def test_all_requested_sources_are_first_in_old_config(self):
        with tempfile.TemporaryDirectory() as folder:
            config = Path(folder) / 'sources.json'
            config.write_text('[{"name":"old","url":"https://example.com/list"}]')
            with patch.object(g, 'SOURCE_CONFIG', config):
                sources = g.load_donors()
            self.assertEqual(sources[:9], g.PRIORITY_DONORS)
            self.assertEqual(len({url for _, url in sources}), len(sources))

    def test_unlimited_discovery_and_spelling_dedup(self):
        original = [row('Amedia1 HD')]
        donors = [row('Амедиа 1'), row('Amedia 1 FHD')]
        donors += [row(f'New channel {i}', f'New{i}.ru') for i in range(205)]
        donors += [row('New channel 0 HD', 'New0.ru'), row('Blocked',url='http://stream.mcquack.net/live')]
        with patch.multiple(g, APPEND_NEW=True, MAX_NEW=0):
            result = g.extend_channels(original, donors)
        self.assertEqual(len(result), 206)
        self.assertEqual(result[0], original[0])

    def test_deniss_priority_discovery_and_failed_channel_exclusion(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, output, cache, report = [root/n for n in ('source','out','cache','report')]
            content = '#EXTM3U\n#EXTINF:-1,Amedia 1 HD\nhttps://example.com/original\n'
            source.write_text(content)
            donors = [dict(row('Amedia1',url='https://example.com/first'),donor='denisskashin-cinema'),
                      dict(row('Amedia 1',url='https://example.com/second'),donor='jekalich-smtk'),
                      dict(row('New channel',url='https://example.com/new'),donor='denisskashin-children'),
                      dict(row('Dead channel',url='https://example.com/dead'),donor='denisskashin-index')]
            with patch.multiple(g,SOURCE=source,OUT=output,CACHE=cache,REPORT=report,APPEND_NEW=True,MAX_NEW=0,KEEP_UNVERIFIED=False), patch.object(g,'load_donors',return_value=[('fixture','https://example.com/list')]), patch.object(g,'fetch_donor',return_value=donors), patch.object(g,'validate',side_effect=lambda u,*args: ('dead' not in u,'fixture')) as check:
                self.assertEqual(g.main(),0)
            result = g.parse_m3u(output.read_text())
            self.assertEqual([r['name'] for r in result],['Amedia 1 HD','New channel'])
            self.assertEqual(result[0]['url'],'https://example.com/first')
            checked = [call.args[0] for call in check.call_args_list]
            self.assertNotIn('https://example.com/second',checked)
            self.assertNotIn('https://example.com/original',checked)
            self.assertEqual(source.read_text(),content)

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

    def test_no_verified_channels_clears_stale_output_and_writes_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output, cache, report = [root / n for n in ('source', 'out', 'cache', 'report')]
            source.write_text('#EXTM3U\n#EXTINF:-1,News\nhttps://example.com/dead\n')
            output.write_text('previous playlist')
            cache.write_text('{}')
            with patch.multiple(g, SOURCE=source, OUT=output, CACHE=cache, REPORT=report, APPEND_NEW=False, KEEP_UNVERIFIED=False), patch.object(g, 'load_donors', return_value=[]), patch.object(g, 'validate', return_value=(False, 'http_403')):
                self.assertEqual(g.main(), 0)
            self.assertEqual(g.parse_m3u(output.read_text()), [])
            self.assertTrue(output.read_text().startswith('#EXTM3U'))
            self.assertEqual(json.loads(cache.read_text()), {})
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
