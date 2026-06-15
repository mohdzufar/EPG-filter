#!/usr/bin/env python3
"""
EPG Filter + Channel Name Injector + Dynamic Report with Summary
- Filters EPG by tvg-id from an M3U playlist.
- Reads EPG source URLs from a text file (--sources) or command line.
- Enriches channel names from the playlist if missing.
- Outputs a gzip‑compressed XMLTV file.
- Generates a report with playlist statistics and EPG matching summary.
- Generates separate files: all playlist channels, matched per source, full EPG dump (2 columns).
- Uses a browser‑like User‑Agent, retries, auto‑detects gzip content.
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
# 1. Parse playlist → (ids, id_to_name, no_id_list, duplicates, stats, all_channels)
# ------------------------------------------------------------
def parse_playlist(location):
    """
    Return:
        unique_ids_set,
        id_to_name_dict (first occurrence name),
        no_id_list: list of (channel_name, line_number) for #EXTINF lines without valid tvg-id,
        duplicate_dict {id: [(name, line), ...]},
        stats_dict,
        all_channels: list of (channel_name, tvg_id, line_number) for every #EXTINF line
    """
    ids = set()
    id_to_name = {}
    no_id_list = []
    id_occurrences = defaultdict(list)
    all_channels = []
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
        if not re.match(r'#EXTINF', line, re.I):
            continue

        total_extinf += 1

        # Extract channel name
        m_name = name_quoted.search(line) or name_bare.search(line)
        if m_name:
            channel_name = m_name.group(1).strip()
        else:
            parts = line.split(',', 1)
            channel_name = parts[1].strip() if len(parts) > 1 else "Unknown"

        # Try to extract tvg-id
        m_id = id_quoted.search(line) or id_bare.search(line)
        if m_id:
            ch_id = m_id.group(1).strip()
            if ch_id:
                ids.add(ch_id)
                with_tvg_id += 1
                if ch_id not in id_to_name:
                    id_to_name[ch_id] = channel_name
                id_occurrences[ch_id].append((channel_name, line_num))
                all_channels.append((channel_name, ch_id, line_num))
            else:
                all_channels.append((channel_name, None, line_num))
                no_id_list.append((channel_name, line_num))
        else:
            all_channels.append((channel_name, None, line_num))
            no_id_list.append((channel_name, line_num))

    duplicate_dict = {id: entries for id, entries in id_occurrences.items() if len(entries) > 1}
    stats = {
        'total_unique_ids': len(ids),
        'total_extinf': total_extinf,
        'with_tvg_id': with_tvg_id,
        'without_tvg_id': total_extinf - with_tvg_id,
        'duplicate_id_count': len(duplicate_dict),
    }
    return ids, id_to_name, no_id_list, duplicate_dict, stats, all_channels

# ------------------------------------------------------------
# 2. Read EPG source URLs (robust: skip empty lines and comments)
# ------------------------------------------------------------
def read_epg_sources(file_path):
    urls = []
    with open(file_path, 'r', encoding='utf-8') as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            if '#' in line:
                line = line.split('#', 1)[0].strip()
            if line:
                urls.append(line)
    return urls

# ------------------------------------------------------------
# 3. Smart opener with retry, browser headers, and gzip detection
# ------------------------------------------------------------
def smart_open(source, retries=3, delay=5):
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
                # Peek at first two bytes for gzip magic
                magic = response.read(2)
                response.close()
                req = Request(source, headers=headers)
                response = urlopen(req, timeout=30)
                if magic == b'\x1f\x8b':
                    binary_stream = gzip.GzipFile(fileobj=response, mode='rb')
                    return io.TextIOWrapper(binary_stream, encoding='utf-8')
                else:
                    return io.TextIOWrapper(response, encoding='utf-8')
            except (HTTPError, URLError) as e:
                last_exception = e
                print(f"  Attempt {attempt+1}/{retries} failed for {source}: {e}", file=sys.stderr)
                if attempt < retries - 1:
                    time.sleep(delay)
        raise last_exception
    else:
        path = Path(source)
        if path.suffix == '.gz':
            return gzip.open(path, 'rt', encoding='utf-8')
        else:
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
# 5. Write the overall playlist channel list (all channels)
# ------------------------------------------------------------
def write_channel_list(file_path, all_channels):
    sorted_channels = sorted(all_channels, key=lambda x: x[0].lower())
    name_width = max(len('Channel Name'), max((len(ch[0]) for ch in sorted_channels), default=0)) + 2
    id_width = max(len('tvg-id'), max((len(ch[1] if ch[1] else 'N/A') for ch in sorted_channels), default=0)) + 2
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(f"{'Channel Name':<{name_width}} {'tvg-id':<{id_width}}\n")
        f.write('-' * (name_width + id_width) + '\n')
        for name, tvg_id, _ in sorted_channels:
            id_str = tvg_id if tvg_id else 'N/A'
            f.write(f"{name:<{name_width}} {id_str:<{id_width}}\n")
    print(f"Channel list (playlist) written to: {file_path}", file=sys.stderr)

# ------------------------------------------------------------
# 6. Write per‑source matched channel list (only channels that were kept)
# ------------------------------------------------------------
def write_per_source_channel_list(file_path, per_source_channels):
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write("Matched Channels by EPG Source\n")
        f.write("==============================\n\n")
        for source, channels in sorted(per_source_channels.items()):
            f.write(f"Source: {source}\n")
            f.write("-" * 80 + "\n")
            # Remove duplicates per source (same ID may appear multiple times)
            unique = {}
            for name, cid in channels:
                if cid not in unique:
                    unique[cid] = name
            for cid, name in sorted(unique.items(), key=lambda x: x[1].lower()):
                f.write(f"  {name} ({cid})\n")
            f.write("\n")
    print(f"Per‑source matched channel list written to: {file_path}", file=sys.stderr)

# ------------------------------------------------------------
# 7. Write full EPG channel dump (2 columns per line) – all channels from each source
# ------------------------------------------------------------
def write_full_epg_channel_list(file_path, epg_sources, columns=2):
    """Write all channels from every EPG source, with configurable columns (default 2)."""
    with open(file_path, 'w', encoding='utf-8') as out:
        out.write("Full EPG Channel List by Source\n")
        out.write("===============================\n\n")
        for src in epg_sources:
            out.write(f"\nSource: {src}\n")
            out.write("=" * 80 + "\n")
            channels = []
            try:
                with smart_open(src) as fh:
                    context = ET.iterparse(fh, events=('end',))
                    for event, elem in context:
                        if elem.tag == 'channel':
                            ch_id = elem.get('id')
                            if not ch_id:
                                elem.clear()
                                continue
                            # Get display-name (fallback to id)
                            name_elem = elem.find('display-name')
                            if name_elem is not None and name_elem.text:
                                ch_name = name_elem.text.strip()
                            else:
                                ch_name = ch_id
                            channels.append((ch_name, ch_id))
                            elem.clear()
            except Exception as e:
                out.write(f"ERROR: Could not read source – {e}\n\n")
                print(f"  Error reading full channel list from {src}: {e}", file=sys.stderr)
                continue

            if not channels:
                out.write("No channels found in this source.\n\n")
                continue

            # Sort by channel name
            channels.sort(key=lambda x: x[0].lower())
            formatted = [f"{name} ({cid})" for name, cid in channels]

            # Split into columns
            n = len(formatted)
            col_size = (n + columns - 1) // columns  # ceiling division
            cols = []
            for i in range(columns):
                start = i * col_size
                end = min(start + col_size, n)
                cols.append(formatted[start:end])

            # Calculate max width for each column
            col_widths = []
            for col in cols:
                max_len = max((len(item) for item in col), default=0) + 2  # +2 padding
                col_widths.append(max_len)

            # Write header
            header_parts = []
            for c in range(columns):
                header_parts.append(f"Column {c+1:<{col_widths[c]-2}}")
            out.write('   '.join(header_parts) + '\n')
            out.write('-' * (sum(col_widths) + 2*(columns-1)) + '\n')

            # Write rows
            for row_idx in range(col_size):
                row_parts = []
                for c in range(columns):
                    if row_idx < len(cols[c]):
                        row_parts.append(f"{cols[c][row_idx]:<{col_widths[c]}}")
                    else:
                        row_parts.append(" " * col_widths[c])
                out.write('   '.join(row_parts).rstrip() + '\n')
            out.write("\n")
    print(f"Full EPG channel list (2 columns) written to: {file_path}", file=sys.stderr)

# ------------------------------------------------------------
# 8. Generate main EPG report (found, missing, duplicate, no‑ID tables)
# ------------------------------------------------------------
def generate_report(report_path, found_channel_ids, id_to_name, channel_sources, wanted_ids,
                    playlist_stats, no_id_list, duplicate_dict):
    total_unique = playlist_stats['total_unique_ids']
    total_extinf = playlist_stats['total_extinf']
    with_tvg = playlist_stats['with_tvg_id']
    without_tvg = playlist_stats['without_tvg_id']
    duplicate_id_count = playlist_stats['duplicate_id_count']
    found_count = len(found_channel_ids)
    not_found_count = total_unique - found_count
    missing_ids = sorted(set(wanted_ids) - found_channel_ids)
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
        
        # Found channels table
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
                f.write(f"{name:<{name_width}} {cid:<{id_width}} {sources_str:<{source_width}}\n")
        else:
            f.write("No channels found.\n")
        f.write("\n")

        # Missing channels table
        if missing_ids:
            f.write("-" * 80 + "\n")
            f.write("MISSING CHANNELS (have tvg-id but no EPG data)\n")
            f.write("-" * 80 + "\n")
            missing_rows = [(id_to_name.get(cid, 'N/A'), cid) for cid in missing_ids]
            missing_rows.sort(key=lambda r: (r[0].lower(), r[1]))
            name_width = max(len('Channel Name'), max((len(r[0]) for r in missing_rows), default=0)) + 2
            id_width = max(len('ID'), max((len(r[1]) for r in missing_rows), default=0)) + 2
            header = f"{'Channel Name':<{name_width}} {'ID':<{id_width}}"
            f.write(header + '\n')
            f.write('-' * len(header) + '\n')
            for name, cid in missing_rows:
                f.write(f"{name:<{name_width}} {cid:<{id_width}}\n")
        else:
            f.write("-" * 80 + "\n")
            f.write("MISSING CHANNELS (have tvg-id but no EPG data)\n")
            f.write("-" * 80 + "\n")
            f.write("None – all channels with tvg-id have EPG data!\n")
        f.write("\n")

        # Duplicate IDs table
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
                f.write(f"{names_str:<{name_width}} {cid:<{id_width}}\n")
        else:
            f.write("-" * 80 + "\n")
            f.write("DUPLICATE TVG-IDS\n")
            f.write("-" * 80 + "\n")
            f.write("None – all tvg-ids are unique.\n")
        f.write("\n")

        # Channels with no tvg-id (with line numbers)
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
# 9. Core filter and output generation
# ------------------------------------------------------------
def filter_and_enrich(output_path, wanted_ids, id_to_name, epg_sources, playlist_stats=None,
                      report_path=None, no_id_list=None, duplicate_dict=None, all_channels=None,
                      channel_list_path=None, per_source_path=None, epg_full_dump_path=None):
    total_channels = 0
    total_programmes = 0
    seen_channels = set()
    channel_sources = {}
    per_source_channels = defaultdict(list)   # source -> list of (name, id)

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
                                name = id_to_name.get(ch_id, ch_id)
                                per_source_channels[source_identifier].append((name, ch_id))
                            elif ch_id in wanted_ids:
                                channel_sources.setdefault(ch_id, set()).add(source_identifier)
                                name = id_to_name.get(ch_id, ch_id)
                                per_source_channels[source_identifier].append((name, ch_id))
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
        generate_report(report_path, seen_channels, id_to_name, channel_sources, wanted_ids,
                        playlist_stats, no_id_list, duplicate_dict)

    if channel_list_path and all_channels:
        write_channel_list(channel_list_path, all_channels)

    if per_source_path and per_source_channels:
        write_per_source_channel_list(per_source_path, per_source_channels)

    if epg_full_dump_path:
        write_full_epg_channel_list(epg_full_dump_path, epg_sources, columns=2)

# ------------------------------------------------------------
# 10. CLI
# ------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='EPG Filter with multiple output files and full EPG dump'
    )
    parser.add_argument('playlist', help='URL or local path to M3U playlist')
    parser.add_argument('epg_sources', nargs='*', help='EPG URLs or local files (ignored if --sources is used)')
    parser.add_argument('--sources', help='Text file containing EPG source URLs (one per line)')
    parser.add_argument('-o', '--output', default='EPG.xml.gz', help='Output gzipped XML file')
    parser.add_argument('--report', action='store_true', help='Generate channel report')
    parser.add_argument('--report-file', default='EPG_Report.txt', help='Report file name')
    parser.add_argument('--channel-list', default='channels_list.txt', help='All channels from playlist (name + id)')
    parser.add_argument('--per-source', default='matched_by_source.txt', help='Per‑source list of matched channels')
    parser.add_argument('--epg-full-dump', default='EPG_channel_and_ID_list.txt',
                        help='Full EPG channel dump (all channels from each source, 2 columns)')
    args = parser.parse_args()

    if args.sources:
        epg_list = read_epg_sources(args.sources)
    elif args.epg_sources:
        epg_list = args.epg_sources
    else:
        print("No EPG sources provided. Use --sources or positional arguments.", file=sys.stderr)
        sys.exit(1)

    wanted_ids, id_to_name, no_id_list, duplicate_dict, stats, all_channels = parse_playlist(args.playlist)
    print(f"Extracted {stats['total_unique_ids']} unique tvg-ids, {len(id_to_name)} have names.", file=sys.stderr)
    print(f"Found {len(no_id_list)} #EXTINF lines without or with empty tvg-id.", file=sys.stderr)
    print(f"Found {stats['duplicate_id_count']} duplicate tvg-ids.", file=sys.stderr)
    if not wanted_ids:
        print("No valid tvg-ids found. Exiting.", file=sys.stderr)
        sys.exit(1)

    report_path = args.report_file if args.report else None
    filter_and_enrich(args.output, wanted_ids, id_to_name, epg_list, playlist_stats=stats,
                      report_path=report_path, no_id_list=no_id_list,
                      duplicate_dict=duplicate_dict, all_channels=all_channels,
                      channel_list_path=args.channel_list, per_source_path=args.per_source,
                      epg_full_dump_path=args.epg_full_dump)
