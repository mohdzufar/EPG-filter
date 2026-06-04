#!/usr/bin/env python3
"""
EPG Filter + Channel Name Injector + Report
- Filters EPG by tvg-id from an M3U playlist.
- Reads EPG source URLs from a text file (--sources) or command line.
- Enriches channel names from the playlist if missing.
- Outputs a gzip‑compressed XMLTV file.
- Optionally generates a plain‑text report listing each channel,
  its ID, and which EPG source(s) provided data.
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
from urllib.parse import urlparse

# ------------------------------------------------------------
# 1. Parse playlist → (set of ids, dict of id->name)
# ------------------------------------------------------------
def parse_playlist(location):
    ids = set()
    id_to_name = {}
    if re.match(r'https?://', location):
        with urlopen(location) as response:
            content = response.read().decode('utf-8')
            lines = content.splitlines()
    else:
        with open(location, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    id_quoted = re.compile(r'tvg-id="([^"]*)"', re.I)
    id_bare   = re.compile(r'tvg-id=([^\s",]+)', re.I)
    name_quoted = re.compile(r'tvg-name="([^"]*)"', re.I)
    name_bare   = re.compile(r'tvg-name=([^\s",]+)', re.I)

    for line in lines:
        m_id = id_quoted.search(line) or id_bare.search(line)
        if not m_id:
            continue
        ch_id = m_id.group(1).strip()
        if not ch_id:
            continue
        ids.add(ch_id)
        if ch_id not in id_to_name:
            m_name = name_quoted.search(line) or name_bare.search(line)
            name = m_name.group(1).strip() if m_name else None
            id_to_name[ch_id] = name

    return ids, id_to_name

# ------------------------------------------------------------
# 2. Read EPG source URLs from a text file
# ------------------------------------------------------------
def read_epg_sources(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip() and not line.startswith('#')]

# ------------------------------------------------------------
# 3. Smart opener for EPG sources
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
# 4. Inject display name if missing
# ------------------------------------------------------------
def inject_display_name(channel_elem, ch_id, id_to_name):
    existing = channel_elem.findall('display-name')
    for dn in existing:
        if dn.text and dn.text.strip():
            return
    name = id_to_name.get(ch_id)
    if name:
        dn_elem = ET.Element('display-name')
        dn_elem.text = name
        channel_elem.insert(0, dn_elem)

# ------------------------------------------------------------
# 5. Core filter → gzipped output + report data collection
# ------------------------------------------------------------
def filter_and_enrich(output_path, wanted_ids, id_to_name, epg_sources, report_path=None):
    total_channels = 0
    total_programmes = 0
    seen_channels = set()
    channel_sources = {}   # ch_id -> set of source identifiers (for report)

    with gzip.open(output_path, 'wt', encoding='utf-8') as out:
        out.write('<?xml version="1.0" encoding="utf-8"?>\n')
        out.write('<tv>\n')

        for src in epg_sources:
            # Determine a short name for the source
            if re.match(r'https?://', src):
                source_name = urlparse(src).path.split('/')[-1]
            else:
                source_name = Path(src).name

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
                                inject_display_name(elem, ch_id, id_to_name)
                                out.write(ET.tostring(elem, encoding='unicode'))
                                src_channels += 1
                                # Record source for this channel
                                channel_sources.setdefault(ch_id, set()).add(source_name)
                            elif ch_id in wanted_ids:
                                # Already seen this channel definition, but still note source
                                channel_sources.setdefault(ch_id, set()).add(source_name)
                            elem.clear()
                        elif tag == 'programme':
                            prog_ch = elem.get('channel')
                            if prog_ch in wanted_ids:
                                out.write(ET.tostring(elem, encoding='unicode'))
                                src_programmes += 1
                                # Record source for this channel (even if no <channel> tag)
                                channel_sources.setdefault(prog_ch, set()).add(source_name)
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

    # ---- Generate report ----
    if report_path:
        generate_report(report_path, seen_channels, id_to_name, channel_sources)

# ------------------------------------------------------------
# 6. Report generation (plain‑text table)
# ------------------------------------------------------------
def generate_report(report_path, channel_ids, id_to_name, channel_sources):
    """Write a fixed‑width table to report_path."""
    # Sort channels by name (case‑insensitive), then by ID
    sorted_ids = sorted(channel_ids,
                        key=lambda cid: (id_to_name.get(cid, '').lower(), cid))

    # Column widths
    NAME_W = 30
    ID_W = 12
    SOURCE_W = 50  # wide enough for multiple sources

    with open(report_path, 'w', encoding='utf-8') as r:
        # Header
        header = f"{'Channel Name':<{NAME_W}} {'ID':<{ID_W}} {'Source(s)':<{SOURCE_W}}"
        r.write(header + '\n')
        r.write('-' * len(header) + '\n')

        for cid in sorted_ids:
            name = id_to_name.get(cid) or 'N/A'
            sources = ', '.join(sorted(channel_sources.get(cid, [])))
            line = f"{name:<{NAME_W}} {cid:<{ID_W}} {sources:<{SOURCE_W}}"
            r.write(line + '\n')

    print(f"Report written to: {report_path}", file=sys.stderr)

# ------------------------------------------------------------
# 7. CLI
# ------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Filter EPG by tvg-id, enrich names, and optionally generate a report'
    )
    parser.add_argument('playlist', help='URL or local path to M3U playlist')
    parser.add_argument('epg_sources', nargs='*', help='EPG URLs or local files (ignored if --sources is used)')
    parser.add_argument('--sources', help='Text file containing EPG source URLs (one per line)')
    parser.add_argument('-o', '--output', default='EPG.xml.gz', help='Output gzipped XML file')
    parser.add_argument('--report', action='store_true', help='Generate channel report')
    parser.add_argument('--report-file', default='EPG_Report.txt', help='Report file name (default: EPG_Report.txt)')
    args = parser.parse_args()

    # EPG source list
    if args.sources:
        epg_list = read_epg_sources(args.sources)
    elif args.epg_sources:
        epg_list = args.epg_sources
    else:
        print("No EPG sources provided. Use --sources or positional arguments.", file=sys.stderr)
        sys.exit(1)

    wanted_ids, id_to_name = parse_playlist(args.playlist)
    print(f"Extracted {len(wanted_ids)} tvg-ids, {len(id_to_name)} have names.", file=sys.stderr)
    if wanted_ids:
        sample = list(wanted_ids)[:10]
        print(f"Sample IDs: {sample}", file=sys.stderr)
    else:
        print("No tvg-ids found. Exiting.", file=sys.stderr)
        sys.exit(1)

    report_path = args.report_file if args.report else None
    filter_and_enrich(args.output, wanted_ids, id_to_name, epg_list, report_path)
