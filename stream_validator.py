"""Decode real video frames instead of inferring playback from HTTP/media signatures.

Design references and revision IDs are documented in REFERENCES.md. No upstream
implementation is copied. FFmpeg handles HLS keys, init maps and live segments.
"""
import os
import logging
import re
import shutil
import subprocess
import time
from typing import NamedTuple
from urllib.parse import parse_qsl, urlparse

import m3u8
import requests

LOG = logging.getLogger('iptv.playback')
RETRYABLE = {'timeout', 'dns_error', 'connection_reset', 'http_429',
             'http_500', 'http_502', 'http_503', 'http_504'}

DEFAULT_UA = 'Mozilla/5.0 (compatible; Hither/3.0)'
HEADER_NAMES = {'user-agent': 'User-Agent', 'referer': 'Referer', 'referrer': 'Referer',
                'origin': 'Origin', 'cookie': 'Cookie', 'authorization': 'Authorization'}


class Result(NamedTuple):
    ok: bool
    reason: str
    url: str = ''
    headers: tuple = ()
    attempts: int = 1


def target(url, extras=()):
    """Return a canonical URL/header pair; never share checks across headers."""
    url, separator, suffix = url.partition('|')
    headers = {'User-Agent': DEFAULT_UA}
    pairs = []
    for line in extras:
        key, _, value = line.partition('=')
        key = key.casefold().strip()
        if key.startswith('#extvlcopt:http-'):
            pairs.append((key.removeprefix('#extvlcopt:http-'), value.strip()))
        elif key.startswith('#kodiprop:') and key.endswith(('.stream_headers', '.manifest_headers')):
            pairs.extend(parse_qsl(value))
    if separator:
        pairs.extend(parse_qsl(suffix))
    for key, value in pairs:
        name = HEADER_NAMES.get(key.lower())
        if name and not any(c in value for c in '\r\n\0'):
            headers[name] = value
    return url.strip(), tuple(sorted(headers.items()))


def player_extras(headers):
    """Serialize the exact request context for players (VLC options + Kodi headers)."""
    from urllib.parse import urlencode
    mapping = {'User-Agent': 'user-agent', 'Referer': 'referrer', 'Origin': 'origin', 'Cookie': 'cookie'}
    result = [f'#EXTVLCOPT:http-{mapping[k]}={v}' for k, v in headers if k in mapping]
    if headers:
        result.append('#KODIPROP:inputstream.adaptive.stream_headers=' + urlencode(dict(headers)))
        result.append('#KODIPROP:inputstream.adaptive.manifest_headers=' + urlencode(dict(headers)))
    return result


def ffmpeg_binary():
    configured = os.getenv('FFMPEG_BINARY')
    if configured:
        return configured if os.path.isfile(configured) else shutil.which(configured)
    return shutil.which('ffmpeg')


def error_reason(stderr):
    text = stderr.lower()
    for status in ('401', '403', '404', '410', '429', '500', '502', '503', '504'):
        if re.search(r'(?:http error |server returned |error )' + status, text):
            return 'http_' + status
    if any(message in text for message in ('connection reset', 'connection timed out', 'network is unreachable', 'end of file')):
        return 'connection_reset'
    if 'timed out' in text:
        return 'timeout'
    if 'resolve hostname' in text or 'name or service not known' in text:
        return 'dns_error'
    if 'connection refused' in text:
        return 'connection_refused'
    if 'certificate' in text:
        return 'tls_error'
    if 'matches no streams' in text:
        return 'no_video_track'
    return 'video_decode_failed'


def decode(url, headers, timeout, binary):
    if urlparse(url).scheme not in ('http', 'https'):
        return Result(False, 'unsupported_protocol')
    arguments = [binary, '-nostdin', '-hide_banner', '-loglevel', 'error',
                 '-protocol_whitelist', 'http,https,tcp,tls,crypto,data',
                 '-rw_timeout', str(int(timeout * 1_000_000)),
                 '-user_agent', dict(headers).get('User-Agent', DEFAULT_UA)]
    if urlparse(url).scheme == 'https':
        arguments += ['-tls_verify', '1']
    other = ''.join(f'{k}: {v}\r\n' for k, v in headers if k != 'User-Agent')
    if other:
        arguments += ['-headers', other]
    arguments += ['-threads', '1', '-i', url, '-map', '0:v:0', '-an', '-sn', '-dn',
                  '-threads', '1', '-frames:v', '3', '-progress', 'pipe:1', '-nostats', '-f', 'null', '-']
    try:
        proc = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                              encoding='utf-8', errors='replace', check=False)
    except subprocess.TimeoutExpired:
        return Result(False, 'timeout')
    except OSError:
        return Result(False, 'ffmpeg_unavailable')
    frames = [int(n) for n in re.findall(r'^frame=(\d+)\s*$', proc.stdout, re.M)]
    if proc.returncode == 0 and frames and max(frames) >= 3:
        return Result(True, 'video_frames_decoded', url, headers)
    if proc.returncode < 0:
        return Result(False, 'decoder_crashed')
    return Result(False, error_reason(proc.stderr))


def _validate_once(url, extras=(), timeout=None):
    url, headers = target(url, extras)
    if urlparse(url).scheme not in ('http', 'https'):
        return Result(False, 'unsupported_protocol')
    binary = ffmpeg_binary()
    if not binary:
        return Result(False, 'ffmpeg_unavailable')
    timeout = timeout or max(5, int(os.getenv('PLAYBACK_TIMEOUT', '60')))
    deadline = time.monotonic() + timeout
    candidates = [url]
    # Parse master manifests for bounded variant fallback. All media checks still
    # require decoding; preflight errors are not themselves proof of failed playback.
    try:
        with requests.get(url, headers=dict(headers), stream=True,
                          timeout=(min(10, timeout), min(10, timeout))) as response:
            body = bytearray()
            if response.ok:
                for chunk in response.iter_content(4096):
                    body.extend(chunk)
                    if time.monotonic() >= deadline:
                        break
                    if len(body) >= 512_000 or (len(body) >= 4096 and not body.lstrip().startswith(b'#EXTM3U')):
                        break
                if body.lstrip(b'\xef\xbb\xbf \r\n').startswith(b'#EXTM3U'):
                    manifest = m3u8.loads(body.decode('utf-8-sig'), uri=response.url)
                    if manifest.is_variant:
                        variants = sorted(manifest.playlists, key=lambda p: p.stream_info.bandwidth or 0)
                        # Keep the master when audio lives in separate renditions.
                        if not any(p.stream_info.audio for p in variants):
                            candidates = [p.absolute_uri for p in variants[:3]] or [url]
    except (requests.RequestException, ValueError, UnicodeError):
        pass
    last = Result(False, 'timeout')
    for i, candidate in enumerate(candidates):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        # Reserve a share of the deadline for each alternate quality.
        last = decode(candidate, headers, max(0.1, remaining / (len(candidates)-i)), binary)
        if last.ok:
            return last
    return last


def validate_stream(url, extras=(), timeout=None):
    """Retry transient failures with a fresh manifest and a full attempt budget.

    PLAYBACK_TIMEOUT is per attempt; up to three 60-second attempts by default,
    plus 2/4-second delays. Permanent failures never consume the retry budget.
    """
    attempts = max(1, min(6, int(os.getenv('PLAYBACK_ATTEMPTS', '3'))))
    timeout = max(1, float(timeout)) if timeout is not None else max(5, int(os.getenv('PLAYBACK_TIMEOUT', '60')))
    backoff = max(0, min(30, float(os.getenv('PLAYBACK_RETRY_DELAY', '2'))))
    for attempt in range(1, attempts + 1):
        result = _validate_once(url, extras, timeout)._replace(attempts=attempt)
        if result.ok or result.reason not in RETRYABLE or attempt == attempts:
            return result
        LOG.info('Temporary playback failure (%s); retry %d/%d', result.reason, attempt + 1, attempts)
        time.sleep(min(30, backoff * 2 ** (attempt - 1)))
    return result
