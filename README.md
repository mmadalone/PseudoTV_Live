![PseudoTV Live](https://raw.githubusercontent.com/PseudoTV/PseudoTV_Live/master/plugin.video.pseudotv.live/resources/images/fanart.jpg)

# PseudoTV Live (madteevee fork)

A maintenance fork of [PseudoTV Live](https://github.com/PseudoTV/PseudoTV_Live) for Kodi™.

**Huge thanks to [Lunatixz](https://github.com/Lunatixz) and the upstream PseudoTV Live community for the foundation** — this repo only adds patches and tooling; the core addon is their excellent work. If you enjoy PseudoTV Live, please support [Kodi](https://kodi.tv/contribute/donate) and [Lunatixz](https://www.patreon.com/pseudotv).

[![GitHub release](https://img.shields.io/github/v/release/mmadalone/PseudoTV_Live?style=flat-square)](https://github.com/mmadalone/PseudoTV_Live/releases)
[![Supports Kodi 19+](https://img.shields.io/badge/Supports-Kodi%2019+-blue.svg?style=flat-square)](https://kodi.tv/download)
[![License](https://img.shields.io/github/license/PseudoTV/PseudoTV_Live?style=flat-square)](https://github.com/PseudoTV/PseudoTV_Live/blob/master/LICENSE)

## Why this fork

To layer additional reliability fixes, performance tuning, and release-pipeline tooling on top of upstream's `0.6.1q` master while staying easy to rebase against future upstream commits. Patches stay in this fork; upstream's release cadence is unchanged.

## What this fork adds

### Reliability fixes
- **`PSEUDOTV_SLUG` decoupled from `ADDON_NAME`** — the fork's renamed display name (`PseudoTV Live (madteevee)`) was breaking pseudotv's internal `isPseudoTV` check via `slugify(ADDON_NAME)`, which silently disabled auto-continuation, the channel-bug logo, rule actions, etc. Hardcoded constant slug `'PseudoTV_Live'` keeps the channel-id machinery independent of the user-facing display name.
- **Stale-`onAVStarted` filter** — during fast CH+/CH- bursts Kodi back-fills `onAVStarted` for each aborted tune in load-finish order, *not* zap-order, which used to anchor `Player.sysInfo` to the wrong channel. Each URL invocation now stamps a `_tune_ts`; older events are filtered. Fixes the channel-bug logo lock-up and end-of-programme continuation routing to the wrong channel.
- **`playLive` Branch 2 joins live at offset** — `Enter` on a currently-airing EPG cell used to restart the episode from frame 0 with a "Now playing VOD" toast. Now computes `seek = now - start` and joins at the live offset, mode stays `live`, no toast.
- **Channel-bug logo lookup by `chid` not `citem.id`** — channel-zap URLs without `fitem` previously fell through to `BUILTIN.getInfoLabel('Art(icon)','Player')`, leaking the previous channel's art onto the new tune. Now looks up the logo from `channels.json` via `chid` (always present on URLs).
- **`fitem`-leak guard** — `getPlayerSysInfo` rejects any `fitem`/`nitem` candidate read from `BUILTIN.getInfoLabel('Plot'/'NextPlot','VideoPlayer')` whose `citem.id` doesn't match the current `chid`. Kodi's video-player info-labels lag behind playback transitions; the guard prevents the previous channel's plot from leaking into the new tune's OSD.
- **`toggleOverlay` chid-aware idempotence** — overlay rebuild only fires on a real channel change, not on every `__chkOverlay` idle-timer tick. Eliminates flicker that would otherwise come from the channel-zap refresh path.
- **`Overlay.sysInfo` is a `@property`** — reads `Player.sysInfo` live instead of snapshotting at construction time, so post-construction channel changes propagate to overlay use sites.
- **`Builder.build` preserves channels on transient failure** — empty-result builds no longer wipe M3U/XMLTV state. Channels are only removed via the explicit reset flow or channel-manager removal. Aligns with the upstream v0.6.2 "failed library parsing inadvertently clearing auto-tuned channels" fix.
- **`buildFileList` paginates past fully-filtered pages** — channels whose first page was entirely filtered (extras / strm / 3D / sub-min-duration) used to bail out with no programmes (e.g., the SleepyTV "extras-page-stall" we hit). Now reparses while more content is available, capped to prevent runaway loops on pathological smartplaylists.
- **`BUILD_AT_MAX` / `BUILD_INTERRUPTED` sentinels** — `getFileList` now distinguishes "channel already extends to `MAX_GUIDEDAYS`" (refresh station, no new programmes) from "build interrupted by playback / settings dialog" (transient, do not touch state). Replaces a single overloaded `True` return that previously conflated the two.
- **`clearchannels` cache fix** — `kodi.py:setResetChannels` gained a `replace=True` mode and `Builder.build`'s post-loop write now uses it. Channels no longer get wiped + rebuilt every cycle when an old `clearchannels` cache row went stale; EPG correctly extends past 7 days.
- **`library.py` `hours=` → `days=`** — three type-funcs (TV Shows / Recommended / Services) were caching for hours instead of days; backport from upstream/nightly.
- **Misleading "Unable to reload while playing" toast** — replaced with a silent log when the actual gate is `Enable_PVR_RELOAD=false` (not playback state).
- **Rules `optionValues` IndexError fix** — `EvenShowsRule` declared one option but the builder wrote three; the resulting `IndexError` silently dropped the rule from every channel on each build.

### HDD-activity / performance tuning (opt-in)
- **`Decouple_Kodi_PVR`** — when on, PseudoTV's `Min_Days` / `Max_Days` / `OSD_Timer` become authoritative; skips the hourly auto-sync from Kodi PVR settings that otherwise overwrites user values.
- **`Streamdetails_Cache_Days`** — slider for the streamdetails cache TTL (default 30 days, range 1-90); was hardcoded to 3 days.
- **`Library_Walk_Interval`** — slider for the autotune library-walk interval (default 12 hours, range 1-72); was hardcoded to 1 hour.
- **Six scanner opt-out toggles** — `Skip_PVR_Recordings`, `Skip_PVR_Searches`, `Skip_Music_Library`, `Skip_TV_Library`, `Skip_Movie_Library`, `Skip_Smartplaylists_Scan`. Useful for setups that don't use a given autotune source; turning these on near-no-op's `chkLibrary`'s scans of those types.

### Release & repository tooling
- **`repository.mmadalone.pseudotv`** — dedicated Kodi repository addon so users of this fork install the addon and receive updates through a single Kodi-side bootstrap.
- **`release.py`** — local builder that produces a Kodi-style addon repo (`addons.xml` + addon zips + mirrored asset paths) into `_site/`. See [FORK_NOTES.md](FORK_NOTES.md).
- **`.github/workflows/release.yml`** — push a tag matching `v*-madteevee.*` and GitHub Actions auto-runs `release.py`, publishes `_site/` to the `gh-pages` branch, and creates a GitHub Release with the addon zips attached. Manual `workflow_dispatch` also available.
- **`.github/workflows/addon-checker.yml`** — bumped to non-deprecated action versions (`actions/checkout@v4`, `actions/setup-python@v5`, Python `3.10`) so addon validation runs cleanly.
- **gh-pages assets mirrored** — addon `<assets>` paths copied to `_site/` alongside zips so Kodi's repo browser displays icons and fanart correctly.

## Installation

[FORK_NOTES.md](FORK_NOTES.md) has the full step-by-step. Quick version:

1. Download the bootstrap repo zip:
   `https://raw.githubusercontent.com/mmadalone/PseudoTV_Live/gh-pages/repository.mmadalone.pseudotv/repository.mmadalone.pseudotv-1.0.1.zip`
2. In Kodi: **Settings → System → Add-ons → Unknown sources = ON**
3. **Settings → Add-ons → Install from zip file** → pick the downloaded zip
4. **Settings → Add-ons → Install from repository → mmadalone PseudoTV Live (madteevee) → Video add-ons → PseudoTV Live (madteevee)** → Install

GitHub Pages is disabled on this fork by parent-repo policy; the Kodi repo is served directly via `raw.githubusercontent.com`. CDN propagation is ~5 min after a release; rate limit is 60 req/h unauthenticated per IP (a non-issue for a single-user repo).

## Tracking upstream

This fork periodically rebases against [`upstream/master`](https://github.com/PseudoTV/PseudoTV_Live) and selectively cherry-picks from [`upstream/nightly`](https://github.com/PseudoTV/PseudoTV_Live/tree/nightly) where the fix is small enough to backport without inviting wider regressions. Upstream's release cadence is unaffected.

---

# Upstream PseudoTV Live for Kodi™

The original upstream README follows below.

## What is it?

PseudoTV Live transforms your Kodi Library and Sources (Plugins, UPnP, etc...) into linear TV similar to broadcast television, complete with configurable channels & Advanced channel rules. Interface provided by Kodi via IPTV Simple PVR Backend.

## What it isn't!

PseudoTV Live is not an IPTV service, it does not provide content or support live streams. Users are required to supply media via Kodi library.

---

[Changelog](https://github.com/PseudoTV/PseudoTV_Live/raw/master/plugin.video.pseudotv.live/changelog.txt)

[Wiki: Github](https://github.com/PseudoTV/PseudoTV_Live/wiki)

[Forum: Kodi](https://forum.kodi.tv/showthread.php?tid=355549)

[Discussion: Kodi](https://forum.kodi.tv/showthread.php?tid=346803)

[Discussion: Github](https://github.com/PseudoTV/PseudoTV_Live/discussions)

[Discussion: Reddit](https://www.reddit.com/r/PseudoTV/)

[![Codacy Badge](https://img.shields.io/codacy/grade/efcc007bd689449f8cf89569ac6a311b.svg?style=flat-square)](https://www.codacy.com/app/PseudoTV/PseudoTV_Live/dashboard)
[![GitHub commit activity](https://img.shields.io/github/commit-activity/m/PseudoTV/PseudoTV_Live.svg?color=red&style=flat-square)](https://github.com/PseudoTV/PseudoTV_Live/commits?author=Lunatixz)
[![Donate to Kodi](https://img.shields.io/badge/Donate%20to-Kodi-blue.svg?style=flat-square)](https://kodi.tv/contribute/donate)
[![Donate to Lunatixz](https://img.shields.io/badge/Donate%20to-Lunatixz-blue.svg?style=flat-square)](https://paypal.me/Lunatixz)

# Special Thanks:
- @xbmc If you are enjoying this project please donate to Kodi!
- @phunkyfish for his continued work and help with IPTV Simple.
- @IAmJayFord for awesome PseudoTV Live Icon/Fanart sets.
- @preroller for fantastic PseudoTV Live Bumpers.

### License

* [GNU GPL v3](http://www.gnu.org/licenses/gpl.html)
* Copyright 2009-2025
