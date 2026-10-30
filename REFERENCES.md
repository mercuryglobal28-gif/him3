# GitHub references used for the playback checker

The Python implementation in `stream_validator.py` is new code, informed by the following projects. No source implementation was copied.

- [freearhey/iptv-checker](https://github.com/freearhey/iptv-checker/tree/6265833d1a4140fa9f39fb75db9b4a223054b3e1), particularly `src/core/FFprobeCommandBuilder.js` and `StreamLoader.js`: carry playlist User-Agent/Referer into actual media probing, and require the media tool to exist. We use argv arrays rather than shell commands.
- [philips-ni/iptv-checker](https://github.com/philips-ni/iptv-checker/tree/a81875d072255d3b1034c94d9e28ff4bc871b150), `iptv_checker/cli.py`: verify video by decoding/capturing frames rather than trusting an HTTP response. Here success requires three decoded video frames, a zero exit status, and a bounded process lifetime. Text-screen heuristics from that project are not used: captions/logos do not prove an error screen.
- [iptv-org API](https://github.com/iptv-org/api): `channel_aliases.json` is a Russian-channel subset of `channels.json` names/alt_names, retrieved 2026-10-30. Local aliases include «Первый» and «Россия-Культура». The [database](https://github.com/iptv-org/database) is published under the Unlicense. The JSON file can be extended to map additional known channel names; no guessed mapping overrides conflicting catalog IDs.
- [iptv-org Russian source streams](https://github.com/iptv-org/iptv/blob/master/streams/ru.m3u): added as a donor source because the raw source list exposes alternative stream entries as well as the public best-option catalog. It supplies matching replacements, never expands the user's channel roster by default.

## Differences from the old checker

A TS-looking first byte, MP4 box signature, HTTP 200, or successful playlist download no longer means a channel passes. FFmpeg must decode video. FFmpeg handles HLS segment maps, supported encryption, relative paths and live segment selection; Python's `m3u8` parser supplies bounded master-variant fallback. Master playlists with separate audio renditions stay intact to avoid exporting a video-only variant.

VLC/Kodi metadata and URL-suffix headers are normalized before both checking and exporting. Checks and cached replacements preserve their header context. Unverified original entries stay in the full roster but never enter `verified_playlist.m3u`.

Decodable video is evidence of playback at test time, not proof of channel identity, uninterrupted playback, or availability in another region. A broadcaster's error-screen video may itself be decodable.
