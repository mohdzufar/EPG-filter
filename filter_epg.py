#!/usr/bin/env python3
"""
EPG Filter + Channel Name Injector
- Filters EPG by tvg-id from an M3U playlist.
- If a <channel> has no (or empty) <display-name>, it inserts the tvg-name
  from the playlist (if available).
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
# 1. Parse playlist → (set of ids, dict of id->name)
# ------------------------------------------------------------
def parse_playlist(location):
    """
    Returns (wanted_ids: set, id_to_name: dict)
    id_to_name maps tvg-id → the first tvg-name found (or None if no name).
    """
    ids = set()
    id_to_name = {}
    if re.match(r'https?://', location):
        with urlopen(location) as response:
            content = response.read().decode('utf-8')
            lines = content.splitlines()
    else:
        with open(location, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    # Patterns for tvg-id and tvg-name (both quoted and bare)
    id_quoted = re.compile(r'tvg-id="([^"]*)"', re.I)
    id_bare   = re.compile(r'tvg-id=([^\s",]+)', re.I)
    name_quoted = re.compile(r'tvg-name="([^"]*)"', re.I)
    name_bare   = re.compile(r'tvg-name=([^\s",]+)', re.I)

    for line in lines:
        # extract id
        m_id = id_quoted.search(line) or id_bare.search(line)
        if not m_id:
            continue
        ch_id = m_id.group(1).strip()
        if not ch_id:
            continue
        ids.add(ch_id)

        # extract name if not already stored for this id
        if ch_id not in id_to_name:
            m_name = name_quoted.search(line) or name_bare.search(line)
            name = m_name.group(1).strip() if m_name else None
            id_to_name[ch_id] = name

    return ids, id_to_name

# ------------------------------------------------------------
# 2. Smart opener for EPG sources
# ------------------------------------------------------------
def smart_open(source):
    if re.match(r'https?://', source):
        response = urlopen(source)
        if source.lower().endswith('.gz'):
            binary_stream = gzip.GzipFile(fileobj=response, mode='rb')
            return io.TextIOWrapper(binary_stream, encoding='utf-8')
        else:
            return io.TextIOWrapper(response, encoding='utf-8')
    else:
        path = Path(source)
        if path.suffix == '.gz':
            return gzip.open(path, 'rt', encoding='utf-8')
        return open(path, 'r', encoding='utf-8')

# ------------------------------------------------------------
# 3. Helper: ensure a channel element has at least one non‑empty display-name
# ------------------------------------------------------------
def inject_display_name(channel_elem, ch_id, id_to_name):
    """If the channel element has no <display-name> with actual text,
    insert one using the tvg-name from the playlist (if available)."""
    # Check if any existing display-name has text
    existing = channel_elem.findall('display-name')
    for dn in existing:
        if dn.text and dn.text.strip():
            return  # already has a name, do nothing

    # If we have a name in the playlist, insert it
    name = id_to_name.get(ch_id)
    if name:
        # Create a <display-name> element and insert at the beginning
        # (after the attributes but before other children)
        dn_elem = ET.Element('display-name')
        dn_elem.text = name
        channel_elem.insert(0, dn_elem)

# ------------------------------------------------------------
# 4. Core filter → gzipped output
# ------------------------------------------------------------
def filter_and_enrich(output_path, wanted_ids, id_to_name, epg_sources):
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
                                # Enrich with playlist name if missing
                                inject_display_name(elem, ch_id, id_to_name)
                                out.write(ET.tostring(elem, encoding='unicode'))
                                src_channels += 1
                        elif tag == 'programme':
                            if elem.get('channel') in wanted_ids:
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

# ------------------------------------------------------------
# 5. CLI
# ------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Filter EPG by tvg-id and enrich channel names from playlist'
    )
    parser.add_argument('playlist', help='URL or local path to M3U playlist')
    parser.add_argument('epg_sources', nargs='+', help='EPG URLs or local files')
    parser.add_argument('-o', '--output', default='EPG.xml.gz', help='Output file')
    args = parser.parse_args()

    wanted_ids, id_to_name = parse_playlist(args.playlist)
    print(f"Extracted {len(wanted_ids)} tvg-ids, {len(id_to_name)} have names.", file=sys.stderr)
    if wanted_ids:
        sample = list(wanted_ids)[:10]
        print(f"Sample IDs: {sample}", file=sys.stderr)
    else:
        print("No tvg-ids found. Exiting.", file=sys.stderr)
        sys.exit(1)

    filter_and_enrich(args.output, wanted_ids, id_to_name, args.epg_sources)
