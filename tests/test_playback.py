import functools
import http.server
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import stream_validator as v
import iptv_generator as g


class HeaderTests(unittest.TestCase):
    def test_blocked_host_never_starts_decoder(self):
        with patch.object(v.subprocess, 'run') as run, patch.object(v.requests, 'get') as get:
            result = v.validate_stream('http://stream.mcquack.net/live')
        self.assertEqual(result.reason, 'blocked_domain')
        run.assert_not_called()
        get.assert_not_called()

    def test_vlc_kodi_and_url_headers(self):
        url, headers = v.target('https://example.com/live|User-Agent=Player&Referer=https%3A%2F%2Fexample.com',
                                ['#KODIPROP:inputstream.adaptive.stream_headers=Origin=https%3A%2F%2Fexample.org'])
        self.assertEqual(dict(headers)['Referer'], 'https://example.com')
        self.assertEqual(dict(headers)['User-Agent'], 'Player')
        self.assertEqual(v.target(url, v.player_extras(headers)), (url, headers))

    def test_header_injection_rejected(self):
        self.assertNotIn('Referer', dict(v.target('https://example.com|Referer=x%0D%0AX-Test%3Ay')[1]))

    def test_no_decoder_no_success(self):
        with patch.object(v, 'ffmpeg_binary', return_value=None):
            self.assertEqual(v.validate_stream('https://example.com').reason, 'ffmpeg_unavailable')

    def test_exit_zero_without_frames_is_failure(self):
        proc = subprocess.CompletedProcess([], 0, 'frame=0\n', '')
        with patch.object(v.subprocess, 'run', return_value=proc):
            self.assertFalse(v.decode('https://example.com', (), 5, 'ffmpeg').ok)

    def test_headers_isolate_results(self):
        plans = [({}, 'a', [('a', 'https://example.com', ['#EXTVLCOPT:http-referrer=a'])]),
                 ({}, 'b', [('b', 'https://example.com', ['#EXTVLCOPT:http-referrer=b'])])]
        with patch.object(g, 'validate', return_value=(False, 'fixture')) as check:
            results = g.check_plans(plans)
        self.assertEqual(len(results), 2)
        self.assertEqual(check.call_count, 2)


class RetryTests(unittest.TestCase):
    def test_timeout_then_success(self):
        ok = v.Result(True, 'video_frames_decoded', 'https://example.com/live')
        with patch.dict(v.os.environ, {'PLAYBACK_ATTEMPTS':'3','PLAYBACK_TIMEOUT':'60','PLAYBACK_RETRY_DELAY':'2'}), patch.object(v, '_validate_once', side_effect=[v.Result(False,'timeout'), ok]) as check, patch.object(v.time, 'sleep') as sleep:
            result = v.validate_stream('https://example.com/live')
        self.assertTrue(result.ok)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(check.call_count, 2)
        self.assertEqual(check.call_args.args[2], 60)
        sleep.assert_called_once_with(2)

    def test_retry_budget_exhaustion(self):
        with patch.dict(v.os.environ, {'PLAYBACK_ATTEMPTS':'3','PLAYBACK_RETRY_DELAY':'2'}), patch.object(v, '_validate_once', return_value=v.Result(False,'timeout')) as check, patch.object(v.time, 'sleep') as sleep:
            result = v.validate_stream('https://example.com/live', timeout=8)
        self.assertFalse(result.ok)
        self.assertEqual(result.attempts, 3)
        self.assertEqual(check.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [2,4])
        self.assertTrue(all(c.args[2] == 8 for c in check.call_args_list))

    def test_permanent_failures_are_not_retried(self):
        for reason in ('http_403','http_404','tls_error','no_video_track','ffmpeg_unavailable','connection_refused'):
            with self.subTest(reason=reason), patch.object(v, '_validate_once', return_value=v.Result(False,reason)) as check, patch.object(v.time, 'sleep') as sleep:
                result = v.validate_stream('https://example.com/live')
                self.assertFalse(result.ok)
                check.assert_called_once()
                sleep.assert_not_called()

    def test_service_unavailable_can_recover(self):
        with patch.object(v, '_validate_once', side_effect=[v.Result(False,'http_503'),v.Result(True,'video_frames_decoded')]), patch.object(v.time, 'sleep'):
            self.assertTrue(v.validate_stream('https://example.com/live').ok)



class PlaybackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        binary = v.ffmpeg_binary()
        if not binary:
            raise RuntimeError('Tests require FFmpeg; install requirements.txt')
        base = [binary, '-hide_banner', '-loglevel', 'error', '-y']
        video = ['-f', 'lavfi', '-i', 'testsrc2=size=64x64:rate=10', '-t', '2', '-c:v', 'libx264', '-threads', '1', '-g', '10']
        for name, extra in [('plain', []), ('fragmented', ['-hls_segment_type', 'fmp4'])]:
            subprocess.run(base + video + ['-f', 'hls', '-hls_time', '1', '-hls_list_size', '0'] + extra + [str(cls.root / (name+'.m3u8'))], check=True, capture_output=True)
        subprocess.run(base + ['-f','lavfi','-i','sine=frequency=500','-t','1','-c:a','aac',str(cls.root/'audio.m4a')],check=True,capture_output=True)
        (cls.root/'fake.ts').write_bytes(b'G' + b'0'*8192)
        (cls.root/'fake.m3u8').write_text('#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nfake.ts\n#EXT-X-ENDLIST\n')
        (cls.root/'master.m3u8').write_text('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=100\nmissing.m3u8\n#EXT-X-STREAM-INF:BANDWIDTH=200\nfragmented.m3u8\n')
        class Handler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.headers.get('Referer') != 'https://allowed.example/' or self.headers.get('User-Agent') != 'TestPlayer':
                    self.send_error(403)
                    return
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/plain.m3u8')
                    self.end_headers()
                    return
                super().do_GET()
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1',0), functools.partial(Handler,directory=str(cls.root)))
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True)
        cls.thread.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}/'
        cls.extras=['#EXTVLCOPT:http-referrer=https://allowed.example/','#EXTVLCOPT:http-user-agent=TestPlayer']

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.directory.cleanup()

    def test_real_hls_and_fmp4(self):
        for filename in ('plain.m3u8','fragmented.m3u8','redirect'):
            with self.subTest(filename=filename):
                result=v.validate_stream(self.url+filename,self.extras,timeout=8)
                self.assertTrue(result.ok, result.reason)
                self.assertEqual(result.reason,'video_frames_decoded')

    def test_master_falls_back_to_working_variant(self):
        result=v.validate_stream(self.url+'master.m3u8',self.extras,timeout=8)
        self.assertTrue(result.ok,result.reason)
        self.assertTrue(result.url.endswith('fragmented.m3u8'))

    def test_fake_media_signature_is_not_playable(self):
        self.assertFalse(v.validate_stream(self.url+'fake.m3u8',self.extras,timeout=8).ok)

    def test_audio_is_not_tv(self):
        self.assertFalse(v.validate_stream(self.url+'audio.m4a',self.extras,timeout=8).ok)

    def test_missing_headers_fail(self):
        result=v.validate_stream(self.url+'plain.m3u8',timeout=8)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason,'http_403')
