"""Build source-ordered IPTV playlist from publicly available streams.

Source entries are preserved, donors only supply matching channel alternatives unless
APPEND_NEW_CHANNELS=true (default). Network verification also runs in GitHub Actions.
"""
import concurrent.futures as cf
import json
import logging
import os
import re
import tempfile
import threading
import unicodedata
from collections import Counter, defaultdict
from functools import lru_cache
from rapidfuzz import fuzz, process
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlencode

import requests
from stream_validator import validate_stream, target, player_extras, ffmpeg_binary

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
LOG = logging.getLogger('iptv')
SOURCE = Path(os.getenv('INPUT_PLAYLIST', 'source_playlist.m3u'))
OUT = Path(os.getenv('OUTPUT_PLAYLIST', 'playlist.m3u'))
CACHE = Path(os.getenv('CACHE_FILE', 'working_links.json'))
REPORT = Path(os.getenv('REPORT_FILE', 'update_report.txt'))
WORKERS = max(1, min(int(os.getenv('MAX_WORKERS', '16')), 32))
TIMEOUT = max(3, int(os.getenv('STREAM_TIMEOUT', '20')))
MAX_CANDIDATES = max(1, int(os.getenv('MAX_CANDIDATES', '30')))
FUZZY_THRESHOLD = max(65, min(100, int(os.getenv('FUZZY_THRESHOLD', '88'))))
MATCH_POOL = max(1, int(os.getenv('MATCH_POOL', '24')))
ALIAS_FILE = Path(os.getenv('ALIASES_FILE', str(Path(__file__).with_name('channel_aliases.json'))))
ALIASES = json.loads(ALIAS_FILE.read_text(encoding='utf-8')) if ALIAS_FILE.exists() else {}
BLOCKED = ('stream.mcquack.net',) + tuple(x.strip().lower().rstrip('.') for x in os.getenv('BLOCKED_DOMAINS', '').split(',') if x.strip())
FILTER_ADULT = os.getenv('FILTER_ADULT', 'false').lower() == 'true'
APPEND_NEW = os.getenv('APPEND_NEW_CHANNELS', 'true').lower() == 'true'
KEEP_UNVERIFIED = os.getenv('KEEP_UNVERIFIED_CHANNELS', 'false').lower() == 'true'
MAX_NEW = max(0, int(os.getenv('MAX_NEW_CHANNELS', '0')))
SOURCE_CONFIG = Path(os.getenv('SOURCES_FILE', 'sources.json'))
UA = {'User-Agent': 'Mozilla/5.0 (compatible; PublicStreamChecker/2.0)', 'Accept': '*/*'}
# Preserve the existing search catalogs; sources.json prioritizes Russian catalogs.
# Public availability does not, by itself, prove redistribution rights for each stream.
SMTK_URL = 'https://raw.githubusercontent.com/JekaLich/smtk/5132d1f5992e523b637b9e46f94707965aa0e37d/tv.smtk.m3u'
DENISS_BASE = 'https://raw.githubusercontent.com/denisskashin/iptv/7f3e4e01839a1c2ea519f301322cb196225dc829/tv/'
PRIORITY_DONORS = [(f'denisskashin-{category}', DENISS_BASE + category + '.m3u')
                   for category in ('children', 'cinema', 'discovery', 'foreign', 'hobby',
                                    'index', 'music', 'sport', 'tv_series')]
PRIORITY_RANK = {name: rank for rank, (name, _) in enumerate(PRIORITY_DONORS)}
DONORS = [
    *PRIORITY_DONORS,
    ('jekalich-smtk', 'https://raw.githubusercontent.com/JekaLich/smtk/5132d1f5992e523b637b9e46f94707965aa0e37d/tv.smtk.m3u'),
    # Main public catalog; specialized catalogs provide overlapping fallback streams.
    ('iptv-org-index', 'https://iptv-org.github.io/iptv/index.m3u'),
    ('iptv-org-raw', 'https://iptv-org.github.io/iptv/index.country.m3u'),
    ('free-tv', 'https://raw.githubusercontent.com/Free-TV/IPTV/master/playlist.m3u8'),
    *[(f'country-{c}', f'https://iptv-org.github.io/iptv/countries/{c}.m3u')
      for c in ('iq','sa','eg','ae','kw','qa','bh','om','jo','lb','sy','ps','ma','dz','tn',
                'ly','sd','ye','tr','ir','ru','ua','by','kz','us','gb','fr','de','in','pk')],
    *[(f'region-{c}', f'https://iptv-org.github.io/iptv/regions/{c}.m3u')
      for c in ('arab', 'asia', 'eur', 'amer', 'afr')],
    *[(f'lang-{c}', f'https://iptv-org.github.io/iptv/languages/{c}.m3u')
      for c in ('ara','eng','rus','tur','fra','spa','hin','urd','fas')],
    *[(f'category-{c}', f'https://iptv-org.github.io/iptv/categories/{c}.m3u')
      for c in ('news','general','education','entertainment','sports','documentary',
                'music','animation','movies','series','science','culture','business',
                'weather','travel','cooking','religious')],
]


def load_donors():
    """Allow sources to be added without changing Python; reject broken configuration."""
    if not SOURCE_CONFIG.exists():
        return DONORS
    data = json.loads(SOURCE_CONFIG.read_text(encoding='utf-8'))
    # An older sources.json must not silently hide the user's requested source.
    if not any(entry.get('url') == SMTK_URL for entry in data):
        data.insert(0, {'name': 'jekalich-smtk', 'url': SMTK_URL, 'enabled': True})
    required = {url: name for name, url in PRIORITY_DONORS}
    for name, url in reversed(PRIORITY_DONORS):
        if not any(entry.get('url') == url for entry in data):
            data.insert(0, {'name': name, 'url': url, 'enabled': True})
    result = []
    seen = set()
    for entry in data:
        if not entry.get('enabled', True):
            continue
        label, url = entry['name'], entry['url']
        label = required.get(url, label)
        if url == SMTK_URL:
            label = 'jekalich-smtk'
        if not isinstance(label, str) or not label or forbidden(url):
            raise ValueError('Invalid source configuration')
        if url not in seen:
            result.append((label, url))
            seen.add(url)
    result.sort(key=lambda source: PRIORITY_RANK.get(source[0], len(PRIORITY_RANK)))
    return result




def forbidden(url):
    try:
        p = urlparse((url or '').strip())
        h = (p.hostname or '').rstrip('.').lower()
        return p.scheme not in ('http', 'https') or not h or any(h == d or h.endswith('.'+d) for d in BLOCKED)
    except ValueError:
        return True


def suitable(meta):
    return not FILTER_ADULT or not re.search(r'(?i)(?:\bxxx\b|\badult\b|\bporn\b|18\+|для взрослых)', meta)


@lru_cache(maxsize=65536)
def name_key(name):
    name = unicodedata.normalize('NFKC', name)
    name = re.sub(r'[\u064b-\u065f\u0670\u0640]', '', name)
    name = name.translate(str.maketrans('أإآىё', 'ااايе'))
    name = re.sub(r'(?i)\((?:\d{3,4}[pi]|дубль)\)|\[(?:geo-blocked|not 24/7)\]', ' ', name)
    # Remove standalone labels first so «FHD» cannot be mistaken for «F» + «HD».
    name = re.sub(r'(?i)\b(?:full[ .-]*hd|fhd|uhd|hd|sd|4k|8k|\d{3,4}[pi]|live)\b', ' ', name)
    name = name.rstrip()
    name = re.sub(r'(?<=[\w])(?:FHD|UHD|HD|SD)$', '', name)
    name = re.sub(r'(?i)(?<=\d)(?:fhd|uhd|hd|sd)$', '', name)
    name = re.sub(r'(?i)(?<=\w)(?:2160p|1080p|720p)$', '', name)
    name = re.sub(r'([^\W\d_])(?=\d)|(?<=\d)([^\W\d_])', r'\1 \2', name)
    # Keep plus signs: «1+1», «11» and time-shifted feeds are different.
    name = re.sub(r'[^\w+]+|_', ' ', name.casefold(), flags=re.UNICODE)
    return ' '.join(name.split())


@lru_cache(maxsize=65536)
def transliterated(name):
    alphabet = 'абвгдеёжзийклмнопрстуфхцчшщъыьэюя'
    latin = ['a','b','v','g','d','e','e','zh','z','i','y','k','l','m','n','o',
             'p','r','s','t','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya']
    return name.translate(str.maketrans(dict(zip(alphabet, latin))))


def spelling_keys(name):
    normalized = name_key(name)
    return {normalized, normalized.replace(' ', ''),
            transliterated(normalized), transliterated(normalized).replace(' ', '')} - {''}


def shift_key(name):
    return tuple(re.findall(r'\+\s*(\d+)', unicodedata.normalize('NFKC', name)))


def row_names(row):
    return [row['name'], *ALIASES.get(channel_id(row['meta']), [])]


def channel_id(meta):
    match = re.search(r'\btvg-id=[\"\']([^\"\']+)[\"\']', meta, re.I)
    value = match.group(1).strip().casefold() if match else ''
    # Provider-local numeric identifiers cannot be compared with global catalog IDs.
    if re.fullmatch(r'(?:chnl-|channel[-_]?)?\d+', value):
        return ''
    return value.split('@', 1)[0]


def channel_match(source, candidate):
    # Explicit time shifts must agree even when the channel IDs are equal.
    if shift_key(source['name']) != shift_key(candidate['name']):
        return 0
    source_id, candidate_id = channel_id(source['meta']), channel_id(candidate['meta'])
    if source_id and candidate_id:
        return 100 if source_id == candidate_id else 0
    best = 0
    for left in row_names(source):
        for right in row_names(candidate):
            a, b = name_key(left), name_key(right)
            if not a or not b:
                continue
            # Check number sequences, not concatenated digits (1 2 != 12).
            if re.findall(r'\d+', a) != re.findall(r'\d+', b):
                continue
            if spelling_keys(left) & spelling_keys(right):
                return 100
            a, b = transliterated(a), transliterated(b)
            if min(len(a.replace(' ', '')), len(b.replace(' ', ''))) <= 4:
                continue
            # Full-name comparisons only; partial/token-set matching can confuse
            # channels such as «Amedia» and «Amedia Premium».
            best = max(best, fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b))
    return round(best)


def parse_m3u(text):
    rows, current = [], None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith('#EXTINF:'):
            if current and current['url']:
                rows.append(current)
            current = {'meta': line.rstrip(), 'name': re.split(r',(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)', line, maxsplit=1)[-1].strip(), 'extras': [], 'url': ''}
        elif current and s:
            if s.startswith('#'):
                current['extras'].append(line.rstrip())
            elif not current['url']:
                current['url'] = s
    if current and current['url']:
        rows.append(current)
    return rows


def checked_response(session, url, *, read_limit=131072):
    if forbidden(url):
        raise ValueError('blocked_url')
    with session.get(url, headers=UA, timeout=(TIMEOUT, TIMEOUT), stream=True,
                     allow_redirects=True) as response:
        if response.status_code not in (200, 206):
            raise ValueError('http_' + str(response.status_code))
        if any(forbidden(x.url) for x in response.history) or forbidden(response.url):
            raise ValueError('blocked_redirect')
        body = bytearray()
        for piece in response.iter_content(16384):
            body.extend(piece)
            if len(body) >= read_limit:
                break
        return response.url, response.headers.get('Content-Type', '').lower(), bytes(body)


def fetch_donor(source):
    label, url = source
    try:
        with requests.Session() as s:
            final, _, body = checked_response(s, url, read_limit=6_000_000)
        rows = [dict(row, donor=label) for row in parse_m3u(body.decode('utf-8-sig', 'replace'))
                if suitable(row['meta']) and not forbidden(row['url'])]
        LOG.info('%s: %d candidates', label, len(rows))
        return rows
    except Exception as exc:
        LOG.warning('%s unavailable: %s', label, type(exc).__name__)
        return []


def validate(url, extras=()):
    if forbidden(url.partition('|')[0]):
        return False, 'blocked_domain'
    return validate_stream(url, extras)


def check_key(candidate):
    return target(candidate[1], candidate[2] if len(candidate) > 2 else ())


def read_cache():
    try:
        data = json.loads(CACHE.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(v, str) and not forbidden(v)}
    except (OSError, ValueError):
        pass
    return {}


def cache_key(row):
    cid = channel_id(row['meta'])
    if cid:
        shift = shift_key(row['name'])
        return 'id:' + cid + (':shift:' + ','.join(shift) if shift else '')
    return 'name:' + name_key(row['name'])


def discovery_keys(row):
    # Ignore spacing/quality and use known aliases, but preserve channel numbers
    # and time shifts so «TV 1 2» is not merged into «TV 12».
    return {(key.replace(' ', ''), tuple(re.findall(r'\d+', name_key(name))), shift_key(row['name']))
            for name in row_names(row) for key in spelling_keys(name)}


def extend_channels(original, donors):
    """Append all unique discovered stations by default; 0 means no new-channel cap."""
    result = list(original)
    if not APPEND_NEW:
        return result
    known_ids = {(channel_id(r['meta']), shift_key(r['name'])) for r in result if channel_id(r['meta'])}
    known_names = set().union(*(discovery_keys(r) for r in result)) if result else set()
    for row in donors:
        if MAX_NEW and len(result) - len(original) >= MAX_NEW:
            break
        keys, cid = discovery_keys(row), channel_id(row['meta'])
        identity = (cid, shift_key(row['name']))
        if not name_key(row['name']) or keys & known_names or (cid and identity in known_ids):
            continue
        if forbidden(row['url'].partition('|')[0]) or not suitable(row['meta']):
            continue
        result.append(row)
        known_names.update(keys)
        if cid:
            known_ids.add(identity)
    return result


def check_plans(planned):
    """Each channel advances independently; duplicate targets share one future.

    A slow original URL from another channel must not postpone a ready SMTK
    alternative. A channel stops immediately after its first verified candidate.
    """
    lock = threading.Lock()
    shared = {}

    def check_channel(plan):
        for candidate in plan[2]:
            key = check_key(candidate)
            with lock:
                future = shared.get(key)
                owner = future is None
                if owner:
                    future = cf.Future()
                    shared[key] = future
            if owner:
                try:
                    result = validate(candidate[1], candidate[2] if len(candidate) > 2 else ())
                except Exception as exc:
                    result = (False, type(exc).__name__)
                future.set_result(result)
            if future.result()[0]:
                return

    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(check_channel, plan) for plan in planned]
        for count, future in enumerate(cf.as_completed(futures), 1):
            future.result()
            if count % 25 == 0 or count == len(planned):
                LOG.info('Finished channel checks: %d/%d', count, len(planned))
    return {key: future.result() for key, future in shared.items()}


def main():
    if not ffmpeg_binary():
        LOG.error('FFmpeg is required; install requirements.txt or set FFMPEG_BINARY')
        return 3
    if not SOURCE.is_file():
        LOG.error('Missing %s', SOURCE)
        return 1
    source_text = SOURCE.read_text(encoding='utf-8-sig')
    header = next((line for line in source_text.splitlines() if line.startswith('#EXTM3U')), '#EXTM3U')
    original = [x for x in parse_m3u(source_text) if suitable(x['meta'])]
    source_count = len(original)
    cached = read_cache()
    configured_donors = load_donors()
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        donor_rows = [item for chunk in pool.map(fetch_donor, configured_donors) for item in chunk]
    source_counts = Counter(row['donor'] for row in donor_rows)
    donor_rows = list({(channel_id(r['meta']), name_key(r['name']), r['url'], tuple(r['extras'])): r for r in donor_rows}.values())
    original = extend_channels(original, donor_rows)
    donor_by_url = {r['url']: r for r in donor_rows}
    index = defaultdict(list)
    ids = defaultdict(list)
    for row in donor_rows:
        names = row_names(row)
        for k in {key for n in names for key in spelling_keys(n)}:
            index[k].append(row)
        if channel_id(row['meta']):
            ids[channel_id(row['meta'])].append(row)
    keys = list(index)
    planned = []
    all_urls = set()
    for row in original:
        k = name_key(row['name'])
        candidates = [('original', row['url'], row['extras'])]
        cached_url = cached.get(cache_key(row), '')
        if cached_url:
            candidates.append(('cache', cached_url, donor_by_url.get(cached_url, {}).get('extras', [])))
        source_id = channel_id(row['meta'])
        matched = []
        seen_rows = set()
        def consider(donor):
            identity = (donor['url'], tuple(donor['extras']), donor['meta'])
            if identity in seen_rows:
                return
            seen_rows.add(identity)
            score = channel_match(row, donor)
            if score >= FUZZY_THRESHOLD:
                matched.append((score, donor))
        for donor in ids.get(source_id, []):
            consider(donor)
        query_keys = {key for n in row_names(row) for key in spelling_keys(n)}
        for key in sorted(query_keys):
            for donor in index.get(key, []):
                consider(donor)
        # Evaluate fuzzy alternatives too; duplicates must not consume the pool
        # and prevent a different working URL from ever being considered.
        if keys:
            for key in sorted(query_keys):
                for candidate_key, _, _ in process.extract(key, keys, scorer=fuzz.ratio,
                                                          limit=MATCH_POOL):
                    for donor in index[candidate_key]:
                        consider(donor)
        matched.sort(key=lambda entry: (PRIORITY_RANK.get(entry[1]['donor'], len(PRIORITY_RANK)), -entry[0]))
        preferred = [(f"{d['donor']}:{score}", d['url'], d['extras'])
                     for score, d in matched if d['donor'] in PRIORITY_RANK]
        smtk = [(f"{d['donor']}:{score}", d['url'], d['extras'])
                for score, d in matched if d['donor'] == 'jekalich-smtk' and score == 100]
        others = [(f"{d['donor']}:{score}", d['url'], d['extras'])
                  for score, d in matched if d['donor'] not in PRIORITY_RANK
                  and not (d['donor'] == 'jekalich-smtk' and score == 100)]
        candidates = preferred + smtk + candidates + others
        seen, unique = set(), []
        for label, url, extras in candidates:
            key = target(url, extras)
            if url and key not in seen and not forbidden(key[0]):
                unique.append((label, url, extras))
                seen.add(key)
            if len(unique) >= MAX_CANDIDATES:
                break
        planned.append((row, k, unique))
        all_urls.update(url for _, url, _ in unique)
    LOG.info('Source channels: %d; donors: %d; candidate URLs: %d',len(original), len(donor_rows), len(all_urls))
    checks = check_plans(planned)
    output, new_cache = [], {}
    stats = Counter()
    failures = []
    verified_rows = []
    channel_results = []
    verified_count = 0
    for row_index, (row, k, candidates) in enumerate(planned):
        chosen = next((c for c in candidates if checks.get(check_key(c), (False,))[0]), None)
        if not chosen:
            stats['unverified'] += 1
            failures.append(row['name'])
            channel_results.append({'name': row['name'], 'status': 'unverified',
                'attempts': [{'source': c[0], 'reason': checks.get(check_key(c), (False, 'not_checked'))[1],
                              'playback_attempts': getattr(checks.get(check_key(c)), 'attempts', 1)} for c in candidates]})
            if KEEP_UNVERIFIED and row_index < source_count and not forbidden(row['url']):
                output.append((row, row['url']))
                stats['retained_unverified'] += 1
            continue
        verified_count += 1
        label, url, extras = chosen
        result = checks[check_key(chosen)]
        url = getattr(result, 'url', '') or url
        if forbidden(url):
            verified_count -= 1
            failures.append(row['name'])
            stats['blocked_result'] += 1
            continue
        headers = getattr(result, 'headers', ()) or target(url, extras)[1]
        # Cache preserves request headers as URL suffix for the next run.
        new_cache[cache_key(row)] = url + '|' + urlencode(dict(headers))
        selected = dict(row)
        selected['extras'] = [line for line in row['extras']
                              if not line.lower().startswith(('#extvlcopt:http-', '#kodiprop:'))]
        selected['extras'] += player_extras(headers)
        output.append((selected, url))
        verified_rows.append((selected, url))
        channel_results.append({'name': row['name'], 'status': 'verified', 'source': label, 'reason': result[1], 'playback_attempts': getattr(result, 'attempts', 1)})
        stats['from_' + label] += 1
    timestamp = datetime.now(timezone.utc).isoformat(timespec='seconds')
    lines = [header, '# Updated: ' + timestamp, '# Verified channels: ' + str(verified_count),
             '# Unverified original channels retained: ' + str(stats['retained_unverified'])]
    for row, url in output:
        lines += [row['meta'], *row['extras'], url]
    verified_lines = [header, '# Only streams with decoded video frames']
    for row, url in verified_rows:
        verified_lines += [row['meta'], *row['extras'], url]
    verified_path = Path(os.getenv('VERIFIED_PLAYLIST', str(OUT.with_name('verified_playlist.m3u'))))
    # Atomic replacement prevents truncated playlist if a job is interrupted.
    for path, value in ((OUT, '\n'.join(lines)+'\n'),
                        (verified_path, '\n'.join(verified_lines)+'\n'),
                        (CACHE, json.dumps(new_cache, ensure_ascii=False, indent=2)+'\n'),
                        (REPORT, json.dumps({'updated': timestamp, 'source_count': source_count,
                                             'validation_method': 'ffmpeg_3_video_frames',
                                             'discovered_count': len(original) - source_count,
                                             'donor_count': len(donor_rows),
                                             'sources': {name: source_counts[name] for name, _ in configured_donors},
                                             'output_count': len(output),
                                             'verified_count': verified_count, 'stats': dict(stats),
                                             'unverified_names': failures,
                                             'channels': channel_results,
                                             'checks': Counter(x[1] for x in checks.values())},
                                            ensure_ascii=False, indent=2)+'\n')):
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                         delete=False) as handle:
            handle.write(value)
            temp = Path(handle.name)
        temp.replace(path)
    if output:
        LOG.info('Playlist saved: %s entries, %s verified, %s originals retained unverified',
                 len(output), verified_count, stats['retained_unverified'])
    else:
        LOG.warning('No channels verified; output playlists and cache cleared; source preserved')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
