#!/usr/bin/env python3
"""
EPG Filter – keep only channels that exist in a given M3U playlist (by tvg-id).

Usage:
    python filter_epg.py <playlist_url> <epg_source1> [epg_source2 ...] -o output.xml.gz
"""

import argparse
import gzip
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen
from urllib.parse import urlparse

# ------------------------------------------------------------
# 1. Extract tvg-id’s from a playlist (URL or local file)
# ------------------------------------------------------------
def extract_ids(playlist_location):
    """Fetches playlist (http/https or local path) and returns a set of tvg-id values."""
    ids = set()
    # If it looks like a URL, fetch it
    if re.match(r'https?://', playlist_location):
        with urlopen(playlist_location) as response:
            # Decode from bytes to string (assuming UTF-8)
            content = response.read().decode('utf-8')
            lines = content.splitlines()
    else:
        # Local file
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
# 2. Smart opener for EPG sources (URL, .gz, plain)
# ------------------------------------------------------------
def smart_open(source):
    if re.match(r'https?://', source):
        return urlopen(source)
    path = Path(source)
    if path.suffix == '.gz':
        return gzip.open(path, 'rt', encoding='utf-8')
    return open(path, 'r', encoding='utf-8')

# ------------------------------------------------------------
# 3. Filter and write compressed output
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
                        elem.clear()
            except Exception as e:
                print(f"Error processing {src}: {e}", file=sys.stderr)

        out.write('</tv>\n')

# ------------------------------------------------------------
# 4. CLI
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
