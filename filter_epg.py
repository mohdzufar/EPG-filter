#!/usr/bin/env python3
"""
EPG Filter by tvg-id from an M3U playlist (remote or local).
Output: a gzip‑compressed XMLTV file containing only matching channels/programmes.

Usage examples:
  python filter_epg.py https://example.com/playlist.m3u epg.xml.gz -o out.xml.gz
  python filter_epg.py ./local_playlist.m3u https://epg.example.com/guide.xml -o out.xml.gz
"""

import argparse
import gzip
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen

# ------------------------------------------------------------
# 1. Extract tvg-id’s from playlist (URL or local file)
# ------------------------------------------------------------
def extract_ids(playlist_location):
    """Fetch playlist (http/https or local path) and return a set of tvg-id values."""
    ids = set()

    if re.match(r'https?://', playlist_location):
        with urlopen(playlist_location) as response:
            content = response.read().decode('utf-8')
            lines = content.splitlines()
    else:
        with open(playlist_location, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    for line in lines:
        m = re.search(r'tvg-id="([^"]*)"', line)
        if m:
            val = m.group(1).strip()
            if val:
                ids.add(val)
    return ids

# ------------------------------------------------------------
# 2. Smart opener for EPG sources (URL, .gz, plain XML)
# ------------------------------------------------------------
def smart_open(source):
    if re.match(r'https?://', source):
        return urlopen(source)
    path = Path(source)
    if path.suffix == '.gz':
        return gzip.open(path, 'rt', encoding='utf-8')
    return open(path, 'r', encoding='utf-8')

# ------------------------------------------------------------
# 3. Core filter → gzipped output
# ------------------------------------------------------------
def filter_and_compress(output_path, wanted_ids, epg_sources):
    with gzip.open(output_path, 'wt', encoding='utf-8') as out:
        out.write('<?xml version="1.0" encoding="utf-8"?>\n')
        out.write('<tv>\n')

        seen_channels = set()

        for src in epg_sources:
            print(f"Processing: {src}", file=sys.stderr)
            try:
                with smart_open(src) as fh:
                    context = ET.iterparse(fh, events=('end',))
                    for event, elem in context:
                        tag = elem.tag
                        if tag == 'channel':
                            ch_id = elem.get('id')
                            if ch_id in wanted_ids and ch_id not in seen_channels:
                                seen_channels.add(ch_id)
                                out.write(ET.tostring(elem, encoding='unicode'))
                        elif tag == 'programme':
                            if elem.get('channel') in wanted_ids:
                                out.write(ET.tostring(elem, encoding='unicode'))
                        # Free memory
                        elem.clear()
            except Exception as e:
                print(f"Error processing {src}: {e}", file=sys.stderr)

        out.write('</tv>\n')

# ------------------------------------------------------------
# 4. Command‑line interface
# ------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Filter EPG by tvg-id from an M3U playlist (URL or local file)'
    )
    parser.add_argument('playlist', help='URL or local path to the M3U playlist')
    parser.add_argument('epg_sources', nargs='+', help='EPG URLs or local files (.gz or .xml)')
    parser.add_argument('-o', '--output', default='EPG.xml.gz', help='Output gzipped XML file')
    args = parser.parse_args()

    wanted = extract_ids(args.playlist)
    print(f"Found {len(wanted)} tvg-ids in playlist", file=sys.stderr)

    if not wanted:
        print("No tvg-id found – nothing to filter.", file=sys.stderr)
        sys.exit(1)

    filter_and_compress(args.output, wanted, args.epg_sources)
    print(f"Filtered EPG written to {args.output}", file=sys.stderr)
