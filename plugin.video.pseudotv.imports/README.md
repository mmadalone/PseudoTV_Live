![PseudoTV Live](https://raw.githubusercontent.com/PseudoTV/PseudoTV_Live/master/plugin.video.pseudotv.live/resources/images/fanart.jpg)

# PseudoTV Live (imports)

A sibling fork of [PseudoTV Live](https://github.com/PseudoTV/PseudoTV_Live) by [Lunatixz](https://github.com/Lunatixz). PseudoTV Live is the foundation — this addon builds on top of it to add native ingestion of external M3U + XMLTV sources (cable/IPTV provider lineups, EPG aggregator feeds) alongside PseudoTV's existing Custom channels.

If you want vanilla PseudoTV functionality, please use the upstream addon at [github.com/PseudoTV/PseudoTV_Live](https://github.com/PseudoTV/PseudoTV_Live). The team there has built something remarkable; this fork is operator-specific tooling on top.

## What it adds beyond upstream

- **External M3U/XMLTV ingestion** — point the addon at one or more sources (a Movistar+ M3U, an `epg.best` lineup, a generic provider) and channels appear alongside your Custom PseudoTV channels.
- **Namespaced channel IDs** — `<source_tvg_id>@<import_id>` so multiple imports sharing an upstream `tvg-id` don't collide in IPTV Simple's deduplication.
- **Cascade-allocated channel numbers** with per-channel pin overrides — set `start_num` per import and the addon assigns numbers cleanly, respecting any manual pins.
- **Web dashboard** at `http://<kodi-host>:50002/manager.html` — single-page UI with tabs for Imports, Channels, Live Guide, System Info, and Settings.
  - Inline channel edit, shift-click range select, deferred Save batching
  - Live Guide grid built from XMLTV via streaming `iterparse`
  - 109 PseudoTV settings exposed as JSON-driven form with client-side dependency engine
- **Per-import EPG cache** with HTTP 304-conditional GETs — ~5 MB/24h steady-state per source instead of ~60 MB/hr.
- **Per-channel logo cache** with three refresh policies (fetch-if-missing / conditional / always).
- **Atomic M3U + XMLTV writes** with a process-wide writer lock — eliminates partial-read races where `pvr.iptvsimple` reads mid-write.
- **Catchup wired end-to-end** with per-import known-host defaults table.
- **Drag-to-reorder** + bulk add + bulk delete + orphan/stranded sweepers for housekeeping.

## What it doesn't change

The core PseudoTV concept — turning your Kodi library into linear channels — is exactly upstream's design. The Builder, the channel rules system, the Custom channel format, the catchup mechanic, the runActions framework, and the M3U/XMLTV emission shape are all upstream's work. This fork's value-add is concentrated in the imports engine (`resources/lib/imports.py`), the web dashboard (`remotes/manager.html`), and a handful of focused thread-safety / atomic-write hardening commits.

## Version scheme

`0.8.0+imports.NN`. The `+imports.NN` suffix tracks fork iterations on top of the `0.8.0` baseline. Earlier development of the parent fork (sibling addon `plugin.video.pseudotv.live` with `0.7.3+madteevee.NN` versions) is recorded in [`FORK_NOTES.md`](../FORK_NOTES.md) at the repo root.

See [`changelog.txt`](./changelog.txt) for the imports-specific history.

## Installation

Designed to run alongside `plugin.video.pseudotv.live` in a Kodi 19+ install — both addons can coexist (they use different addon IDs, different ports, different addon_data directories). If you'd like to try this fork, the operator-specific setup is in [`FORK_NOTES.md`](../FORK_NOTES.md#how-to-apply-this-fork-to-a-kodi-install).

## Reporting issues

Issues specific to this fork: please file at [github.com/mmadalone/PseudoTV_Live/issues](https://github.com/mmadalone/PseudoTV_Live/issues). Issues with the underlying PseudoTV Live functionality (Builder, rules, Custom channels, catchup): the upstream project is the right venue — [github.com/PseudoTV/PseudoTV_Live](https://github.com/PseudoTV/PseudoTV_Live).

## Special Thanks

- [@Lunatixz](https://github.com/Lunatixz) — for PseudoTV Live itself. Years of careful work; this fork is possible because of that foundation.
- [@phunkyfish](https://github.com/phunkyfish) — for continued work on IPTV Simple.
- [@IAmJayFord](https://github.com/IAmJayFord) — for PseudoTV Live icon/fanart sets.
- [@preroller](https://github.com/preroller) — for the PseudoTV Live bumpers.
- The Kodi project — please consider [donating to Kodi](https://kodi.tv/contribute/donate).

## License

[GNU GPL v3](http://www.gnu.org/licenses/gpl.html), inherited from upstream PseudoTV Live.
Copyright Lunatixz 2009-2025; fork additions Copyright mmadalone 2026.
