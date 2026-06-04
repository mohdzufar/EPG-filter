#!/usr/bin/env python3
"""
EPG Filter by tvg-id from an M3U playlist (remote or local).
Output: a gzip‑compressed XMLTV file containing only matching channels/programmes.

Fixes:
- Properly handles remote .gz files (decompresses on‑the‑fly).
- Converts all remote responses to text (UTF‑8).
- Keeps detailed debug logging.
"""

import argparse
import gzip
import io
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError

# ------------------------------------------------------------
# 1. Extract tvg-id’s (supports quoted / unquoted)
# ------------------------------------------------------------
def extract_ids(playlist_location):
    ids = set()
    if re.match(r'https?://', playlist_location):
        with urlopen(playlist_location) as response:
            content = response.read().decode('utf-8')
            lines = content.splitlines()
    else:
        with open(playlist_location, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    quoted = re.compile(r'tvg-id="([^"]*)"', re.I)
    bare   = re.compile(r'tvg-id=([^\s",]+)', re.I)
    for line in lines:
        m = quoted.search(line) or bare.search(line)
        if m:
            val = m.group(1).strip()
            if val:
                ids.add(val)
    return ids

# ------------------------------------------------------------
# 2. Smart opener – now correctly handles remote .gz / plain
# ------------------------------------------------------------
def smart_open(source):
    """Return a text-mode file‑like object for the EPG source."""
    if re.match(r'https?://', source):
        # Open remote file (binary stream)
        response = urlopen(source)
        # Is it a gzip compressed file? (by extension)
        if source.lower().endswith('.gz'):
            # Decompress on‑the‑fly and wrap in text reader
            binary_stream = gzip.GzipFile(fileobj=response, mode='rb')
            return io.TextIOWrapper(binary_stream, encoding='utf-8')
        else:
            # Plain text response
            return io.TextIOWrapper(response, encoding='utf-8')
    else:
        # Local file
        path = Path(source)
        if path.suffix == '.gz':
            return gzip.open(path, 'rt', encoding='utf-8')
        return open(path, 'r', encoding='utf-8')

# ------------------------------------------------------------
# 3. Core filter → gzipped output + logging
# ------------------------------------------------------------
def filter_and_compress(output_path, wanted_ids, epg_sources):
    total_channels = 0
    total_programmes = 0
    seen_channels = set()

    with gzip.open(output_path, 'wt', encoding='utf-8') as out:
        out.write('<?xml version="1.0" encoding="utf-8"?>\n')
        out.write('<tv>\n')

        for src in epg_sources:
            print(f"\n>>> Processing source: {src}", file=sys.stderr)
            src_channels = 0
            src_programmes = 0
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
                                src_channels += 1
                        elif tag == 'programme':
                            prog_ch = elem.get('channel')
                            if prog_ch in wanted_ids:
                                out.write(ET.tostring(elem, encoding='unicode'))
                                src_programmes += 1
                        elem.clear()
            except HTTPError as e:
                print(f"HTTP Error: {e.code} {e.reason}", file=sys.stderr)
            except ET.ParseError as e:
                print(f"XML Parse Error: {e}", file=sys.stderr)
            except Exception as e:
                print(f"Error processing {src}: {e}", file=sys.stderr)

            total_channels += src_channels
            total_programmes += src_programmes
            print(f"  Kept {src_channels} channel definitions and {src_programmes} programmes.", file=sys.stderr)

        out.write('</tv>\n')

    print(f"\n--- FINAL SUMMARY ---", file=sys.stderr)
    print(f"Total unique channels kept: {len(seen_channels)}", file=sys.stderr)
    print(f"Total programmes kept:      {total_programmes}", file=sys.stderr)
    print(f"Output written to:          {output_path}", file=sys.stderr)
    if total_channels == 0:
        print("WARNING: No channels matched! Check if playlist tvg-id's match EPG channel IDs.", file=sys.stderr)

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
    print(f"Extracted {len(wanted)} unique tvg-id(s) from playlist.", file=sys.stderr)
    if wanted:
        sample = list(wanted)[:10]
        print(f"Sample (first 10): {sample}", file=sys.stderr)
    else:
        print("No tvg-id found – nothing to filter. Exiting.", file=sys.stderr)
        sys.exit(1)

    filter_and_compress(args.output, wanted, args.epg_sources)
