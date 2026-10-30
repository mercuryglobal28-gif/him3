"""Build source-ordered IPTV playlist from publicly available streams.

Source entries are preserved, donors only supply matching channel alternatives unless
APPEND_NEW_CHANNELS=true (opt-in). Network verification also runs in GitHub Actions.
"""
import concurrent.futures as cf
import json
import logging
import os
import re
import tempfile
import unicodedata
from collections import Counter, defaultdict
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
TIMEOUT = max(3, int(os.getenv('STREAM_TIMEOUT', '9')))
MAX_CANDIDATES = max(1, int(os.getenv('MAX_CANDIDATES', '10')))
FUZZY_THRESHOLD = max(65, min(100, int(os.getenv('FUZZY_THRESHOLD', '88'))))
MATCH_POOL = max(1, int(os.getenv('MATCH_POOL', '8')))
ALIAS_FILE = Path(os.getenv('ALIASES_FILE', str(Path(__file__).with_name('channel_aliases.json'))))
ALIASES = json.loads(ALIAS_FILE.read_text(encoding='utf-8')) if ALIAS_FILE.exists() else {}
BLOCKED = tuple(x.strip().lower().rstrip('.') for x in os.getenv('BLOCKED_DOMAINS', '').split(',') if x.strip())
FILTER_ADULT = os.getenv('FILTER_ADULT', 'false').lower() == 'true'
APPEND_NEW = os.getenv('APPEND_NEW_CHANNELS', 'false').lower() == 'true'
KEEP_UNVERIFIED = os.getenv('KEEP_UNVERIFIED_CHANNELS', 'true').lower() == 'true'
MAX_NEW = max(0, int(os.getenv('MAX_NEW_CHANNELS', '200')))
SOURCE_CONFIG = Path(os.getenv('SOURCES_FILE', 'sources.json'))
UA = {'User-Agent': 'Mozilla/5.0 (compatible; PublicStreamChecker/2.0)', 'Accept': '*/*'}
# Preserve the existing search catalogs; sources.json prioritizes Russian catalogs.
# Public availability does not, by itself, prove redistribution rights for each stream.
DONORS = [
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
    result = []
    seen = set()
    for entry in data:
        if not entry.get('enabled', True):
            continue
        label, url = entry['name'], entry['url']
        if not isinstance(label, str) or not label or forbidden(url):
            raise ValueError('Invalid source configuration')
        if url not in seen:
            result.append((label, url))
            seen.add(url)
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


def name_key(name):
    name = unicodedata.normalize('NFKC', name)
    name = re.sub(r'[\u064b-\u065f\u0670\u0640]', '', name)
    name = name.translate(str.maketrans('أإآى', 'اااي'))
    name = re.sub(r'(?i)\((?:\d{3,4}[pi]|дубль)\)|\[(?:geo-blocked|not 24/7)\]', ' ', name)
    name = re.sub(r'(?i)\b(?:hd|fhd|uhd|4k|1080p|720p|sd|live)\b', ' ', name)
    name = re.sub(r'[^\w]+', ' ', name.casefold(), flags=re.UNICODE)
    return ' '.join(name.split())


def channel_id(meta):
    match = re.search(r'\btvg-id=[\"\']([^\"\']+)[\"\']', meta, re.I)
    value = match.group(1).strip().casefold() if match else ''
    # Provider-local numeric identifiers cannot be compared with global catalog IDs.
    if re.fullmatch(r'(?:chnl-|channel[-_]?)?\d+', value):
        return ''
    return value.split('@', 1)[0]


def channel_match(source, candidate):
    # A stable tvg-id takes priority; different known IDs reject false positives.
    source_id, candidate_id = channel_id(source['meta']), channel_id(candidate['meta'])
    if source_id and candidate_id:
        if source_id == candidate_id:
            return 100
        # Different broadcasters often share very similar display names.
        return 0
    a, b = name_key(source['name']), name_key(candidate['name'])
    aliases = [name_key(n) for n in ALIASES.get(candidate_id, [])]
    if a and a in aliases:
        return 100
    if not a or not b:
        return 0
    if a == b:
        return 100
    # Require a meaningful shared token for longer channel names.
    a_words, b_words = set(a.split()), set(b.split())
    if max(len(a), len(b)) > 5 and not (a_words & b_words):
        return 0
    # Never confuse numbered stations or time-shifted feeds.
    if re.findall(r'\d+', a) != re.findall(r'\d+', b):
        return 0
    score = fuzz.ratio(a, b)
    # Short names are ambiguous: use stricter equality.
    if min(len(a), len(b)) <= 4 and score < 100:
        return 0
    return score


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
    return 'id:' + channel_id(row['meta']) if channel_id(row['meta']) else 'name:' + name_key(row['name'])


def extend_channels(original, donors):
    """Discover bounded, unique stations in catalog order, preserving original order."""
    result = list(original)
    if not APPEND_NEW:
        return result
    known_ids = {channel_id(r['meta']) for r in result} - {''}
    known_names = {name_key(r['name']) for r in result}
    for row in donors:
        if len(result) - len(original) >= MAX_NEW:
            break
        key, cid = name_key(row['name']), channel_id(row['meta'])
        if not key or key in known_names or (cid and cid in known_ids):
            continue
        result.append(row)
        known_names.add(key)
        if cid:
            known_ids.add(cid)
    return result


def check_plans(planned):
    """Bounded rounds; identical URLs with different headers are separate checks."""
    checks = {}
    with cf.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for rank in range(max((len(c) for _, _, c in planned), default=0)):
            pending = {}
            for _, _, candidates in planned:
                if any(checks.get(check_key(c), (False,))[0] for c in candidates):
                    continue
                if rank < len(candidates):
                    c = candidates[rank]
                    key = check_key(c)
                    if key not in checks:
                        pending[key] = c
            futures = {pool.submit(validate, c[1], c[2] if len(c) > 2 else ()): key
                       for key, c in pending.items()}
            for future in cf.as_completed(futures):
                try:
                    checks[futures[future]] = future.result()
                except Exception as exc:
                    checks[futures[future]] = (False, type(exc).__name__)
            LOG.info('Validation round %d: %d targets', rank + 1, len(pending))
    return checks


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
    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        donor_rows = [item for chunk in pool.map(fetch_donor, load_donors()) for item in chunk]
    donor_rows = list({(channel_id(r['meta']), name_key(r['name']), r['url'], tuple(r['extras'])): r for r in donor_rows}.values())
    original = extend_channels(original, donor_rows)
    donor_by_url = {r['url']: r for r in donor_rows}
    index = defaultdict(list)
    ids = defaultdict(list)
    for row in donor_rows:
        names = [row['name'], *ALIASES.get(channel_id(row['meta']), [])]
        for k in {name_key(n) for n in names} - {''}:
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
        matched.extend((100, r) for r in ids.get(source_id, []))
        matched.extend((100, r) for r in index.get(k, []) if channel_match(row, r) == 100)
        if k and len(matched) < MAX_CANDIDATES and keys:
            for candidate_key, _score, _ in process.extract(k, keys, scorer=fuzz.WRatio,
                                                              limit=MATCH_POOL):
                if candidate_key == k:
                    continue
                for donor in index[candidate_key]:
                    score = channel_match(row, donor)
                    if score >= FUZZY_THRESHOLD:
                        matched.append((score, donor))
        matched.sort(key=lambda entry: -entry[0])
        candidates.extend((f"{d['donor']}:{score}", d['url'], d['extras']) for score, d in matched)
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
                'attempts': [{'source': c[0], 'reason': checks.get(check_key(c), (False, 'not_checked'))[1]} for c in candidates]})
            if KEEP_UNVERIFIED and row_index < source_count:
                output.append((row, row['url']))
                stats['retained_unverified'] += 1
            continue
        verified_count += 1
        label, url, extras = chosen
        result = checks[check_key(chosen)]
        url = getattr(result, 'url', '') or url
        headers = getattr(result, 'headers', ()) or target(url, extras)[1]
        # Cache preserves request headers as URL suffix for the next run.
        new_cache[cache_key(row)] = url + '|' + urlencode(dict(headers))
        selected = dict(row)
        selected['extras'] = [line for line in row['extras']
                              if not line.lower().startswith(('#extvlcopt:http-', '#kodiprop:'))]
        selected['extras'] += player_extras(headers)
        output.append((selected, url))
        verified_rows.append((selected, url))
        channel_results.append({'name': row['name'], 'status': 'verified', 'source': label, 'reason': result[1]})
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
                                             'output_count': len(output),
                                             'verified_count': verified_count, 'stats': dict(stats),
                                             'unverified_names': failures,
                                             'channels': channel_results,
                                             'checks': Counter(x[1] for x in checks.values())},
                                            ensure_ascii=False, indent=2)+'\n')):
        if not output and path in (OUT, CACHE):
            continue
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
        LOG.error('No channels verified; existing playlist and cache retained; report updated')
    return 0 if output else 2


if __name__ == '__main__':
    raise SystemExit(main())
