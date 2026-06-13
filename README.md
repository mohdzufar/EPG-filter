# EPG Filter

Generate a filtered EPG (.xml.gz) that contains only the channels in your IPTV playlist (matched by tvg-id).
This should remove any EPG channel that are not listed in your IPTV list.
This will make the EPG load faster for your IPTV.

Remember, this repo is matching up EPG based on tvg-id. So, you will need to have correct tvg-id each of your channels.
Runs automatically on GitHub Actions – updates the file daily, ready to use in any IPTV player.

## Quick Start

1. Fork / create this repo (e.g., EPG-filter)
2. Add EPG URLs to EPG_Sources.txt (one per line)
3. Set your playlist URL in .github/workflows/filter-epg.yml (the default value)
4. Enable write permissions: Settings → Actions → General → Read and write permissions
5. Run the workflow manually (Actions tab) or wait for the daily schedule

The filtered EPG is committed as EPG.xml.gz – use its raw URL in your player:

https://raw.githubusercontent.com/<your-username>/EPG-filter/main/EPG.xml.gz

A report (EPG_Report.txt) is also generated, showing which channels were found and from which sources.

## Files You’ll Edit

| File | Purpose |
|------|---------|
| EPG_Sources.txt | EPG source URLs (one per line) |
| .github/workflows/filter-epg.yml | Default playlist URL & schedule |
| filter_epg.py | Python filter script (usually no changes needed) |

## Need Help?

- No channels matched? Your playlist’s tvg-id values probably don’t match the EPG IDs. Check a sample from the source.
- HTTP 403? The EPG server may block bots – try a different source or add a User-Agent header in filter_epg.py.
- Workflow can’t push? Ensure repo permissions are set to “Read and write” (see step 4 above).

That’s it – a custom EPG, always up‑to‑date, with zero maintenance.
