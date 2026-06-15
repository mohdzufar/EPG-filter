#!/usr/bin/env python3
"""
EPG Filter + Channel Name Injector + Dynamic Report with Summary
- Filters EPG by tvg-id from an M3U playlist.
- Reads EPG source URLs from a text file (--sources) or command line.
- Enriches channel names from the playlist if missing.
- Outputs a gzip‑compressed XMLTV file.
- Generates a report with playlist statistics and EPG matching summary.
- Uses a browser‑like User‑Agent and retries to avoid 403 errors.
"""

import argparse
import gzip
import io
import re
import sys
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from collections import defaultdict

# ------------------------------------------------------------
# 1. Parse playlist → (ids, id_to_name, no_id_list, duplicates, stats)
# ------------------------------------------------------------
def parse_playlist(location):
    """
    Return:
        unique_ids_set,
        id_to_name_dict (first occurrence name),
        no_id_list: list of (channel_name, line_number) for #EXTINF lines without valid tvg-id,
        duplicate_dict {id: [(name, line), ...]},
        stats_dict
    """
    ids = set()
    id_to_name = {}
    no_id_list = []          # list of (channel_name, line_number)
    id_occurrences = defaultdict(list)   # id -> list of (channel_name, line_number)
    total_extinf = 0
    with_tvg_id = 0

    if re.match(r'https?://', location):
        req = Request(location, headers={'User-Agent': 'Mozilla/5.0'})
        with urlopen(req) as response:
            content = response.read().decode('utf-8')
            lines = content.splitlines()
    else:
        with open(location, 'r', encoding='utf-8') as f:
            lines = f.readlines()

    id_quoted = re.compile(r'tvg-id="([^"]*)"', re.I)
    id_bare   = re.compile(r'tvg-id=([^\s",]+)', re.I)
    name_quoted = re.compile(r'tvg-name="([^"]*)"', re.I)
    name_bare   = re.compile(r'tvg-name=([^\s",]+)', re.I)

    for line_num, line in enumerate(lines, start=1):
        # Only process lines that start with #EXTINF
        if not re.match(r'#EXTINF', line, re.I):
            continue

        total_extinf += 1

        # ----- Try to extract tvg-id -----
        m_id = id_quoted.search(line) or id_bare.search(line)
        has_valid_id = False
        ch_id = ""

        if m_id:
            ch_id = m_id.group(1).strip()
            if ch_id:   # non‑empty ID
                has_valid_id = True
                ids.add(ch_id)
                with_tvg_id += 1
                # Store first name for id_to_name
                if ch_id not in id_to_name:
                    m_name = name_quoted.search(line) or name_bare.search(line)
                    name = m_name.group(1).strip() if m_name else None
                    id_to_name[ch_id] = name
                # Record occurrence for duplicate detection (store name and line)
                m_name = name_quoted.search(line) or name_bare.search(line)
                channel_name = m_name.group(1).strip() if m_name else "Unknown"
                id_occurrences[ch_id].append((channel_name, line_num))

        # ----- If no valid tvg-id, treat as "no tvg-id" and store with line number -----
        if not has_valid_id:
            # Extract channel name
            m_name = name_quoted.search(line) or name_bare.search(line)
            if m_name:
                channel_name = m_name.group(1).strip()
            else:
                parts = line.split(',', 1)
                if len(parts) > 1:
                    channel_name = parts[1].strip()
                else:
                    channel_name = "Unknown"
            no_id_list.append((channel_name, line_num))

    # Build duplicate dict (ids that appear more than once)
    duplicate_dict = {id: entries for id, entries in id_occurrences.items() if len(entries) > 1}

    stats = {
        'total_unique_ids': len(ids),
        'total_extinf': total_extinf,
        'with_tvg_id': with_tvg_id,
        'without_tvg_id': total_extinf - with_tvg_id,
        'duplicate_id_count': len(duplicate_dict),
    }
    return ids, id_to_name, no_id_list, duplicate_dict, stats

# ------------------------------------------------------------
# 2. Read EPG source URLs from a text file
# ------------------------------------------------------------
def read_epg_sources(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        return [line.strip() for line in f if line.strip() and not line.startswith('#')]

# ------------------------------------------------------------
# 3. Smart opener for EPG sources (with browser headers + retry)
# ------------------------------------------------------------
def smart_open(source, retries=3, delay=5):
    """Open a URL or local file, with retries and browser‑like headers."""
    # Browser‑like headers to avoid 403
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
        'Accept-Encoding': 'gzip, deflate, br',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
    }

    if re.match(r'https?://', source):
        last_exception = None
        for attempt in range(retries):
            try:
                req = Request(source, headers=headers)
                response = urlopen(req, timeout=30)
                # Handle gzipped content
                if source.lower().endswith('.gz'):
                    binary_stream = gzip.GzipFile(fileobj=response, mode='rb')
                    return io.TextIOWrapper(binary_stream, encoding='utf-8')
                else:
                    return io.TextIOWrapper(response, encoding='utf-8')
            except (HTTPError, URLError) as e:
                last_exception = e
                print(f"  Attempt {attempt+1}/{retries} failed for {source}: {e}", file=sys.stderr)
                if attempt < retries - 1:
                    time.sleep(delay)
        # If all retries fail, re‑raise the last exception
        raise last_exception
    else:
        # Local file
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
# 5. Core filter → gzipped output + report data
# ------------------------------------------------------------
def filter_and_enrich(output_path, wanted_ids, id_to_name, epg_sources, playlist_stats=None, report_path=None, no_id_list=None, duplicate_dict=None):
    total_channels = 0
    total_programmes = 0
    seen_channels = set()
    channel_sources = {}   # ch_id -> set of full source URLs

    with gzip.open(output_path, 'wt', encoding='utf-8') as out:
        out.write('<?xml version="1.0" encoding="utf-8"?>\n')
        out.write('<tv>\n')

        for src in epg_sources:
            source_identifier = src
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
                                channel_sources.setdefault(ch_id, set()).add(source_identifier)
                            elif ch_id in wanted_ids:
                                channel_sources.setdefault(ch_id, set()).add(source_identifier)
                            elem.clear()
                        elif tag == 'programme':
                            prog_ch = elem.get('channel')
                            if prog_ch in wanted_ids:
                                out.write(ET.tostring(elem, encoding='unicode'))
                                src_programmes += 1
                                channel_sources.setdefault(prog_ch, set()).add(source_identifier)
                            elem.clear()
            except Exception as e:
                print(f"  Error processing {src}: {e} (skipping source)", file=sys.stderr)
                continue

            total_channels += src_channels
            total_programmes += src_programmes
            print(f"  Kept {src_channels} channel definitions and {src_programmes} programmes.", file=sys.stderr)

        out.write('</tv>\n')

    print(f"\n--- FINAL SUMMARY ---", file=sys.stderr)
    print(f"Total unique channels kept: {len(seen_channels)}", file=sys.stderr)
    print(f"Total programmes kept:      {total_programmes}", file=sys.stderr)
    print(f"Output written to:          {output_path}", file=sys.stderr)

    if report_path:
        generate_report(report_path, seen_channels, id_to_name, channel_sources, wanted_ids, playlist_stats, no_id_list, duplicate_dict)

# ------------------------------------------------------------
# 6. Report with all tables (line numbers only for no-ID channels)
# ------------------------------------------------------------
def generate_report(report_path, found_channel_ids, id_to_name, channel_sources, wanted_ids, playlist_stats, no_id_list, duplicate_dict):
    total_unique = playlist_stats['total_unique_ids']
    total_extinf = playlist_stats['total_extinf']
    with_tvg = playlist_stats['with_tvg_id']
    without_tvg = playlist_stats['without_tvg_id']
    duplicate_id_count = playlist_stats['duplicate_id_count']
    found_count = len(found_channel_ids)
    not_found_count = total_unique - found_count

    # Missing channels (have tvg-id but no EPG)
    missing_ids = sorted(set(wanted_ids) - found_channel_ids)

    # Keep all no-id entries (each line is a separate row)
    no_id_rows = sorted(no_id_list, key=lambda x: x[0].lower())

    with open(report_path, 'w', encoding='utf-8') as f:
        f.write("EPG Filter Report\n")
        f.write("-------------------\n\n")
        f.write("Playlist Summary:\n")
        f.write(f"  Total unique tvg-id entries   : {total_unique}\n")
        f.write(f"  #EXTINF lines with tvg-id     : {with_tvg}\n")
        f.write(f"  #EXTINF lines without tvg-id  : {without_tvg}\n")
        f.write(f"  Duplicate tvg-ids (case-sensitive) : {duplicate_id_count}\n\n")
        f.write("EPG Matching:\n")
        f.write(f"  Channels found in EPG         : {found_count}\n")
        f.write(f"  Channels NOT found            : {not_found_count}\n\n")
        
        # ------------------------------------------------------------
        # 1. TABLE OF FOUND CHANNELS
        # ------------------------------------------------------------
        f.write("-" * 80 + "\n")
        f.write("FOUND CHANNELS (with EPG data)\n")
        f.write("-" * 80 + "\n")

        found_rows = []
        for cid in found_channel_ids:
            name = id_to_name.get(cid) or 'N/A'
            sources_list = sorted(channel_sources.get(cid, []))
            sources_str = ', '.join(sources_list)
            found_rows.append((name, cid, sources_str))

        found_rows.sort(key=lambda r: (r[0].lower(), r[1]))

        if found_rows:
            name_width = max(len('Channel Name'), max((len(r[0]) for r in found_rows), default=0)) + 2
            id_width = max(len('ID'), max((len(r[1]) for r in found_rows), default=0)) + 2
            source_width = max(len('Source(s)'), max((len(r[2]) for r in found_rows), default=0)) + 2

            header = f"{'Channel Name':<{name_width}} {'ID':<{id_width}} {'Source(s)':<{source_width}}"
            f.write(header + '\n')
            f.write('-' * len(header) + '\n')
            for name, cid, sources_str in found_rows:
                line = f"{name:<{name_width}} {cid:<{id_width}} {sources_str:<{source_width}}"
                f.write(line + '\n')
        else:
            f.write("No channels found.\n")
        f.write("\n")

        # ------------------------------------------------------------
        # 2. TABLE OF MISSING CHANNELS (have ID but no EPG)
        # ------------------------------------------------------------
        if missing_ids:
            f.write("-" * 80 + "\n")
            f.write("MISSING CHANNELS (have tvg-id but no EPG data)\n")
            f.write("-" * 80 + "\n")

            missing_rows = []
            for cid in missing_ids:
                name = id_to_name.get(cid, 'N/A')
                missing_rows.append((name, cid))

            missing_rows.sort(key=lambda r: (r[0].lower(), r[1]))

            name_width = max(len('Channel Name'), max((len(r[0]) for r in missing_rows), default=0)) + 2
            id_width = max(len('ID'), max((len(r[1]) for r in missing_rows), default=0)) + 2

            header = f"{'Channel Name':<{name_width}} {'ID':<{id_width}}"
            f.write(header + '\n')
            f.write('-' * len(header) + '\n')
            for name, cid in missing_rows:
                line = f"{name:<{name_width}} {cid:<{id_width}}"
                f.write(line + '\n')
        else:
            f.write("-" * 80 + "\n")
            f.write("MISSING CHANNELS (have tvg-id but no EPG data)\n")
            f.write("-" * 80 + "\n")
            f.write("None – all channels with tvg-id have EPG data!\n")
        f.write("\n")

        # ------------------------------------------------------------
        # 3. TABLE OF DUPLICATE TVG-IDS
        # ------------------------------------------------------------
        if duplicate_dict:
            f.write("-" * 80 + "\n")
            f.write("DUPLICATE TVG-IDS (same ID appears multiple times)\n")
            f.write("-" * 80 + "\n")

            duplicate_rows = []
            for cid, entries in duplicate_dict.items():
                unique_names = list(dict.fromkeys([name for name, _ in entries]))
                names_str = ', '.join(unique_names)
                duplicate_rows.append((names_str, cid))

            duplicate_rows.sort(key=lambda r: (r[0].lower(), r[1]))

            name_width = max(len('Channel Name(s)'), max((len(r[0]) for r in duplicate_rows), default=0)) + 2
            id_width = max(len('ID'), max((len(r[1]) for r in duplicate_rows), default=0)) + 2

            header = f"{'Channel Name(s)':<{name_width}} {'ID':<{id_width}}"
            f.write(header + '\n')
            f.write('-' * len(header) + '\n')
            for names_str, cid in duplicate_rows:
                line = f"{names_str:<{name_width}} {cid:<{id_width}}"
                f.write(line + '\n')
        else:
            f.write("-" * 80 + "\n")
            f.write("DUPLICATE TVG-IDS\n")
            f.write("-" * 80 + "\n")
            f.write("None – all tvg-ids are unique.\n")
        f.write("\n")

        # ------------------------------------------------------------
        # 4. TABLE OF CHANNELS WITH NO TVG-ID (with line numbers)
        # ------------------------------------------------------------
        if no_id_rows:
            f.write("-" * 80 + "\n")
            f.write("CHANNELS WITH NO TVG-ID (missing or empty tvg-id)\n")
            f.write("-" * 80 + "\n")

            name_width = max(len('Channel Name'), max((len(row[0]) for row in no_id_rows), default=0)) + 2
            line_width = max(len('Line'), max((len(str(row[1])) for row in no_id_rows), default=0)) + 2

            header = f"{'Channel Name':<{name_width}} {'Line':<{line_width}}"
            f.write(header + '\n')
            f.write('-' * len(header) + '\n')
            for ch_name, line_num in no_id_rows:
                f.write(f"{ch_name:<{name_width}} {line_num:<{line_width}}\n")
        else:
            f.write("-" * 80 + "\n")
            f.write("CHANNELS WITH NO TVG-ID\n")
            f.write("-" * 80 + "\n")
            f.write("None – all #EXTINF lines contain a non‑empty tvg-id.\n")

    print(f"Report written to: {report_path}", file=sys.stderr)

# ------------------------------------------------------------
# 7. CLI
# ------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Filter EPG by tvg-id and enrich channel names, with report'
    )
    parser.add_argument('playlist', help='URL or local path to M3U playlist')
    parser.add_argument('epg_sources', nargs='*', help='EPG URLs or local files (ignored if --sources is used)')
    parser.add_argument('--sources', help='Text file containing EPG source URLs (one per line)')
    parser.add_argument('-o', '--output', default='EPG.xml.gz', help='Output gzipped XML file')
    parser.add_argument('--report', action='store_true', help='Generate channel report')
    parser.add_argument('--report-file', default='EPG_Report.txt', help='Report file name (default: EPG_Report.txt)')
    args = parser.parse_args()

    if args.sources:
        epg_list = read_epg_sources(args.sources)
    elif args.epg_sources:
        epg_list = args.epg_sources
    else:
        print("No EPG sources provided. Use --sources or positional arguments.", file=sys.stderr)
        sys.exit(1)

    wanted_ids, id_to_name, no_id_list, duplicate_dict, stats = parse_playlist(args.playlist)
    print(f"Extracted {stats['total_unique_ids']} unique tvg-ids, {len(id_to_name)} have names.", file=sys.stderr)
    print(f"Found {len(no_id_list)} #EXTINF lines without or with empty tvg-id.", file=sys.stderr)
    print(f"Found {stats['duplicate_id_count']} duplicate tvg-ids.", file=sys.stderr)
    if wanted_ids:
        sample = list(wanted_ids)[:10]
        print(f"Sample IDs: {sample}", file=sys.stderr)
    else:
        print("No valid tvg-ids found. Exiting.", file=sys.stderr)
        sys.exit(1)

    report_path = args.report_file if args.report else None
    filter_and_enrich(args.output, wanted_ids, id_to_name, epg_list, playlist_stats=stats,
                      report_path=report_path, no_id_list=no_id_list, duplicate_dict=duplicate_dict)
