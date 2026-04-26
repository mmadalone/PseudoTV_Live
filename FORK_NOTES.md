# mmadalone/PseudoTV_Live — fork notes

Personal fork of [PseudoTV/PseudoTV_Live](https://github.com/PseudoTV/PseudoTV_Live) at upstream tag `0.6.1q`. Maintained for the **madteevee** Kodi install (RPi5 + HDD-backed media libraries) where upstream's stock build cadence and library-walk patterns were causing chronic HDD wake-ups, EPG generation gaps, and a few outright bugs.

## Branch layout

| Branch | Purpose |
|---|---|
| `master` | Mirrors upstream `PseudoTV/PseudoTV_Live:master`. Updated via `git fetch upstream && git push fork master` (no local changes here). |
| `madteevee-patches` | Cumulative patches for our install. Squashed into a single commit so `git rebase upstream/master` is one merge resolution rather than ten. |

## Maintenance workflow

```bash
# Pull upstream changes
git fetch upstream

# Update local mirror (no-op if upstream hasn't moved)
git checkout master
git merge --ff-only upstream/master
git push fork master

# Rebase our patches against upstream
git checkout madteevee-patches
git rebase upstream/master
# resolve any conflicts, then:
git push fork madteevee-patches --force-with-lease
```

The cumulative-single-commit shape means rebases hit each conflict once instead of N times. If a future patch becomes its own logical unit (e.g. a new feature you'd consider PR'ing back upstream after burying the hatchet), do that one as a separate commit on top of the squashed base.

**Post-rebase checklist** (upstream re-adds these on every rebase; we keep them deleted in the fork):
- `git rm plugin.video.pseudotv.live/resources/lib/data/station.py plugin.video.pseudotv.live/resources/lib/data/channels_dataclass.py` — broken vestiges (SyntaxError + missing imports), see `findings.md` "Cleanup applied (2026-04-26)".

## What the fork actually changes

Single commit `bb2edfe` against `0.6.1q` (293 ins / 53 del, 10 files). The commit message has the full breakdown; high-level summary below.

### Bug fixes (would be PR-worthy if not for the upstream relationship)

| # | Severity | What broke | Where |
|---|---|---|---|
| 1 | Spammed log | `loadRules: list assignment index out of range` × 161 in one log; `EvenShowsRule` silently dropped from every channel | `rules.py:111`, `builder.py:_injectRules` |
| 2 | EPG gaps | Channels whose first `Files.GetDirectory` page was entirely filtered (extras / strm / 3D) bailed empty; `__clrChannel` then **deleted** them | `builder.py:buildFileList` |
| 3 | Channel loss | Every transient build failure (RPC timeout, interrupt, filter-races) wiped the channel entirely; recovery required a full rebuild | `builder.py:build()` |
| 4 | UI confusion | `BUILD_AT_MAX` (max-days reached, station refresh OK) and `BUILD_INTERRUPTED` (transient, leave state alone) both returned bare `True`; an interrupt-during-build left M3U entries with no programmes | `builder.py:getFileList`, sentinels added |
| 5 | Cache thrash | TV Shows / Recommended / Services TTLs were `hours=MAX_GUIDEDAYS` (= 3 hours) instead of `days=` (= 3 days). Already fixed in nightly | `library.py:updateLibrary` |
| 6 | Misleading toast | "Unable to reload <PVR> while playing..." fired whenever `Enable_PVR_RELOAD=false`, regardless of whether anything was playing — `string #30023` is unrelated to the actual gate | `globals.py:togglePVR` |
| 7 | **EPG never extends** | `clearchannels` simplecache row never drained because `setResetChannels` was add-only. Stuck channel IDs caused `__clrChannel` on every build, wiping future programmes and rebuilding from `now`. **This was the actual cause of "Rebuild Library doesn't extend the EPG"** | `kodi.py:setResetChannels`, `builder.py:setResetChannels` call site |

Bug #7 is probably affecting a lot of upstream users without their knowledge.

### Tunables added (HDD activity reduction; all defaults preserve current behaviour)

| Setting | Default | Range | Effect |
|---|---|---|---|
| `Decouple_Kodi_PVR` | false | bool | Stops `chkKodiSettings` overwriting `Min_Days` / `Max_Days` / `OSD_Timer` from Kodi PVR every hour. Required to set `Max_Days` higher than Kodi's `epg.futuredaystodisplay`. |
| `Streamdetails_Cache_Days` | 30 | 1-90 | Replaces hardcoded `MAX_GUIDEDAYS` (3) for the `Files.GetFileDetails` cache. |
| `Library_Walk_Interval` | 12 | 1-72 (h) | Replaces hardcoded `3600` for `chkLibrary` task scheduling. Channel-build still triggers immediately via `chkUpdate` event-trigger on user actions. |
| `Skip_PVR_Recordings` | false | bool | Skips `getPVRRecordings` (`pvr://recordings/tv/active/`). |
| `Skip_PVR_Searches` | false | bool | Skips `getPVRSearches` (`pvr://search/tv/savedsearches/`). |
| `Skip_Music_Library` | false | bool | Skips `getMusicInfo` (`AudioLibrary.GetGenres`). |
| `Skip_TV_Library` | false | bool | Skips `getTVInfo` (`VideoLibrary.GetTVShows` — biggest single scanner). |
| `Skip_Movie_Library` | false | bool | Skips `getMovieInfo` (`VideoLibrary.GetMovies`). |
| `Skip_Smartplaylists_Scan` | false | bool | Skips `getPlaylists` (autotune scan; doesn't affect channel building). |

Defaults match upstream behaviour, so a fresh install behaves identically. Skipped types are gated at the `__funcs()` dict level so the progress dialog doesn't even flash their type names — not just a function-level no-op.

### Minor

- `RPC_Timer` default 3 → 5 (300 s) for users with large XSP libraries on slow disks.

## How to apply this fork to a Kodi install

The Kodi addon system loads addons by ID (`plugin.video.pseudotv.live`). You can install this fork in place of the upstream one by either:

1. **Replace the addon dir contents:**
   ```bash
   cd /tmp && git clone https://github.com/mmadalone/PseudoTV_Live.git fork
   cd fork && git checkout madteevee-patches
   # backup first!
   cp -r ~/.kodi/addons/plugin.video.pseudotv.live ~/.kodi/addons/plugin.video.pseudotv.live.bak
   rsync -a --delete plugin.video.pseudotv.live/ ~/.kodi/addons/plugin.video.pseudotv.live/
   sudo systemctl restart kodi   # or restart Kodi however your distro packages it
   ```

2. **Or symlink the addon dir at the repo:** keep the working tree at `~/projects/PseudoTV_Live/plugin.video.pseudotv.live/` and symlink it into `~/.kodi/addons/`. Easier for iterative development on the fork itself.

Kodi's addon-repo update will overwrite the live addon dir if you use option 1. To prevent that, add the addon to Kodi's "ignore updates" list, or use option 2.

## Where the deep diagnostics live

The full investigation log (live findings, log snapshots, regressions caught, before/after timing per fix) is on the madteevee Kodi install at:

```
/home/madalone/.kodi/_pseudotv_project/findings.md
/home/madalone/.kodi/_pseudotv_project/logs/build-*.log
/home/madalone/.kodi/_pseudotv_project/logs/post-*.{m3u,xml}
```

It's not committed to the repo because it contains machine-specific paths and timestamps. The `bb2edfe` commit message contains the abridged version that's relevant to the patches themselves.

## Future patch additions

When adding new fixes, prefer one logical commit per fix on top of the squashed base, rather than amending `bb2edfe`. Makes upstream rebases easier when only some of the new commits conflict.

## Release / Kodi repo

The fork ships its own Kodi addon repo so the live install can auto-update from the fork instead of from upstream. Layout:

| Branch | Role |
|---|---|
| `master` | upstream mirror (no edits here) |
| `madteevee-patches` | our source patches + `release.py` + `repository.mmadalone.pseudotv/` |
| `gh-pages` | published Kodi repo (manifest + zips). Served via `raw.githubusercontent.com` directly (Pages is disabled on this fork — see below). |

Repo URL: **https://raw.githubusercontent.com/mmadalone/PseudoTV_Live/gh-pages/addons.xml**

### Why `raw.githubusercontent.com` instead of GitHub Pages

GitHub denies enabling Pages on repos forked from `PseudoTV/PseudoTV_Live` ("Pages on this forked repository is disabled due to a policy enforced by the owner of the parent repository"). Workaround: serve `addons.xml` + zips off the `gh-pages` branch directly via `raw.githubusercontent.com`. Same content, same URL stability, no Pages required. Caveats:
- ~5 min CDN cache for unauthenticated raw fetches; new releases land in Kodi within that window.
- 60 req/h unauthenticated rate limit per IP; not an issue for a single-user repo.

### One-time Kodi install of the repo addon

1. Download the bootstrap zip from https://raw.githubusercontent.com/mmadalone/PseudoTV_Live/gh-pages/repository.mmadalone.pseudotv/repository.mmadalone.pseudotv-1.0.1.zip
2. In Kodi: **Settings → System → Add-ons → Unknown sources** = ON (required to install from zip).
3. **Settings → Add-ons → Install from zip file** → pick the downloaded zip.
4. After install, **Settings → Add-ons → Install from repository → mmadalone PseudoTV Live (madteevee) → Video add-ons → PseudoTV Live (madteevee)** → Update / Install. The fork's `0.6.1q+madteevee.X` will replace upstream's `0.6.1q`.
5. Optional: leave the upstream `repository.pseudotv` enabled too so its resource packs (`resource.images.pseudotv.logos` etc.) keep updating from upstream — those aren't mirrored in the fork repo.

### Cutting a new release (after making patches)

The release pipeline is automated via `.github/workflows/release.yml`. Pushing a tag matching `v*-madteevee.*` triggers a workflow that runs `release.py`, publishes `_site/` to the `gh-pages` branch, and creates a GitHub Release object. Workflow can also be triggered manually from the Actions tab (`workflow_dispatch`) for republishing without a tag.

#### Automated release (preferred)

```bash
cd ~/_Claude_projects/PseudoTV_Live

# 1. bump version in plugin.video.pseudotv.live/addon.xml
#    (e.g. 0.6.1q+madteevee.1 -> 0.6.1q+madteevee.2). Use '+' separator
#    so Kodi treats it as newer than upstream's 0.6.1q.
#    (Optionally also bump repository.mmadalone.pseudotv/addon.xml when the
#    repo addon itself changes — rare.)

# 2. commit + push the version bump
git add plugin.video.pseudotv.live/addon.xml
git commit -m "bump: pseudotv 0.6.1q+madteevee.2"
git push fork madteevee-patches

# 3. tag and push the tag — this is what triggers the release workflow
git tag -a v0.6.1q-madteevee.2 -m "fork release v0.6.1q+madteevee.2"
git push fork v0.6.1q-madteevee.2
```

GitHub then runs the workflow: builds zips + `addons.xml`, force-replaces `gh-pages` with the new `_site/`, creates a Release in the Releases tab. Within ~5 min (raw.githubusercontent.com CDN cache), Kodi will see the new version on **Add-ons → Check for updates**.

The tag uses `-` instead of `+` because some git tooling chokes on `+` in tag names; the addon version itself stays `0.6.1q+madteevee.2` per PEP-440 local-version semantics.

#### Manual release (fallback if Actions disabled / for testing)

```bash
cd ~/_Claude_projects/PseudoTV_Live

# 1. bump version (as above)

# 2. build the site (zips + addons.xml + addons.xml.md5)
python3 release.py

# 3. publish to gh-pages
git checkout gh-pages
rsync -a --delete --exclude='.git' --exclude='_site' _site/ ./
git add -A
git commit -m "release: pseudotv X.Y.Z"
git push fork gh-pages

# 4. tag and push
git checkout madteevee-patches
git tag -a v0.6.1q-madteevee.2 -m "fork release v0.6.1q+madteevee.2"
git push fork madteevee-patches v0.6.1q-madteevee.2
```

### Mirroring more addons in the fork repo

`release.py`'s `ADDONS` list controls what gets packaged. Currently:
- `plugin.video.pseudotv.live` (the patched main addon)
- `repository.mmadalone.pseudotv` (the repo addon itself, so future repo updates also flow through GH Pages)

Resource packs (`resource.images.pseudotv.logos`, bumpers, ratings, etc.) keep coming from upstream's `repository.pseudotv` since they don't need fork-specific patches. If you ever want the fork to be fully self-contained, copy those addon dirs into this repo, add to `release.py`'s `ADDONS` list, rerun.
