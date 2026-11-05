# mmadalone/PseudoTV_Live — fork notes

Personal fork of [PseudoTV/PseudoTV_Live](https://github.com/PseudoTV/PseudoTV_Live), rebased onto `upstream/nightly` (PseudoTV `0.7.3+nightly`) as of 2026-04-28. Maintained for the **madteevee** Kodi install (RPi5 + HDD-backed media libraries). The previous master-based branch (`0.6.1q+madteevee.14`) is preserved at tag `pre-nightly-rebase` and on archive branch `madteevee-patches-0.6.1q-archive` (after burn-in switch).

## Branch layout

| Branch | Purpose |
|---|---|
| `madteevee-patches` | (current default) Patches on top of `upstream/nightly`. Each fix is its own commit so future cherry-picks against newer nightly tips stay surgical. |
| `madteevee-on-nightly` | (current working branch) Continues from `madteevee-patches`; carries both `plugin.video.pseudotv.live` patches AND the sibling `plugin.video.pseudotv.imports` addon. |
| `madteevee-patches-0.6.1q-archive` | Snapshot of the previous master-based fork, kept for reference / rollback. Tag `pre-nightly-rebase` points at the same tip. |

## The imports sibling addon

In addition to the per-commit patches against the `plugin.video.pseudotv.live` upstream, this repo carries a **sibling addon** at `plugin.video.pseudotv.imports/`. It's a fork of `plugin.video.pseudotv.live`, with a new addon ID, a different port (50002), and a substantial value-add: native ingestion of external M3U + XMLTV sources (Movistar+, epg.best, generic provider lineups) alongside PseudoTV's existing Custom channels.

**Why it exists**: PseudoTV upstream's `getImports / setImports` are vestigial (defined, zero call sites). The original feature intent was external-source import, but it never landed beyond stubs. The imports addon picks that up — ingestion engine in `resources/lib/imports.py` (~1300 LOC), web dashboard at `remotes/manager.html` (single-page UI with Imports / Channels / Live Guide / System Info / Settings tabs), atomic M3U/XMLTV write infrastructure, per-source EPG/logo caches with HTTP 304 conditional GETs, and a settings-driven dependency engine surfacing 109 PseudoTV settings as a JSON form.

**Version scheme**: `0.8.0+imports.NN` — distinct from the `.live` addon's `0.7.3+madteevee.NN`. The two run side-by-side; both can coexist in a Kodi install (different addon IDs, ports, addon_data dirs).

**Architectural decisions worth knowing about** (the per-decision durable notes live in operator memory; this is the cliff-notes summary):

- **`PSEUDOTV_SLUG = 'PseudoTV_Live'`** is shared with the `.live` parent — both addons emit Custom channels with `@PseudoTV_Live` suffix so they cross-recognize.
- **Import channel IDs are namespaced**: `<source_tvg_id>@<import_id>`. Prevents IPTV Simple's tvg-id dedup from collapsing two imports that happen to share an upstream tvg-id.
- **`mediaEnabled=false`** on IPTV Simple's instance-settings-50002.xml. Without this, IPTV Simple classifies all imported channels as "media items" not channels.
- **`CHANNEL_LIMIT=9999`** in `constants.py` (was 999). Required so disabled channels can land in the 9000+ bucket.
- **chkImports daemon thread** (`Tasks._startImportsThread`), NOT the priority queue. The queue worker can block on `chkLibrary._exe` for minutes; daemon thread guarantees imports sync regardless.
- **`PROPERTIES.setEXTProperty` for cross-thread signals.** `setProperty`/`getProperty` thread-scope the keys; only EXT variants are visible across threads. Caused a recurring `chkImports.kick` bug until traced.
- **WRITER_LOCK** (`writer_lock.py`, imports.12) — process-wide RLock around M3U/XMLTV render+os.replace. Defense in depth against in-process write races. Cross-process races (context_record.py separate process, operator hand-edits) still defended by a Custom-merge-from-disk workaround at `imports.py:916-955`.
- **Renderers module** (`renderers.py`, imports.13) — render_m3u, render_xmltv, write_atomic as module-level pure functions. Eliminates the prior duplication across m3u.py + xmltvs.py + tasks.py.

**Thread-safety hardening (imports.16/17/18):**

- `Player._chkCallback` CAS gate (imports.16) — protects against writer-races-writer on `self.playingItem['callback']` when an `_onPlay` worker swaps state between the idle thread's read and write.
- `HTTP.run()` socket-release (imports.17) — adds `self._server.server_close()` after `shutdown()` to release the listening socket; without it every `chkPVRRefresh` cycle leaked the socket and forced fallback to port 50003 with a user-visible toast.
- `Player._overlay_lock` (imports.18) — serializes `toggleOverlay`'s read-modify-write of `self.overlay`. Closes the initial-create race where two threads (idle thread + @threadit worker) both saw `self.overlay = None`, both created an Overlay, and one's `channelBug` control got orphaned in `xbmcgui.Window(12005)` — operator-visible as the previous channel's logo sticking on top of the current channel.

**Channel-transition supervisor + duration accuracy (imports.47/48/49):**

- `Player._chkTransition` (imports.47) — unified watchdog + loop-breaker in `services.py`, called every ~1s from `Monitor._chkIdle`'s not-playing branch. Watchdog completes the dormant `pendingItem['invoked']` mechanism (re-fires `PlayMedia` after `Playback_Timeout` if Kodi stalled between iptvsimple resolving the stream URL and `default.py` launching — recovers the ~3-min stall mode). Loop-breaker counts consecutive short plays (`< Seek_Tolerance`) and defers re-tune at threshold 3 — turns the file-shorter-than-EPG-slot flicker storm into cooldown-paced retries. `Playback_Timeout` schema default 90→45.
- `JSONRPC.__parseDuration` accurate flag (imports.48) — the 25% sanity-check fallback at `jsonrpc.py:477` was discarding ffprobe values that disagreed with scraped library `runtime` by more than 25%, exactly blocking the cases that need fixing (library includes scraped credits the file doesn't). Threaded `accurate` flag through `getDuration` → `__parseDuration`; gate the sanity-check on `not accurate`. When operator opts into `Duration_Type=1` globally (or per-channel via `DurationOptions` rule 500), ffprobe is trusted. Default `accurate=False` preserves stacked-file behaviour.
- `JSONRPC.queueJSON` serialize-before-add (imports.49) — latent TypeError surfaced once imports.48 + `Store_Duration=true` started actually firing `queDuration`. `self.service.jsonQue` is a `set()`; `param` is a dict; sets reject unhashable items. Two-line fix mirroring the in-codebase `logoQue` pattern at `resources.py:120`: writer `self.service.jsonQue.add(FileAccess.dumpJSON(param))`, reader at `tasks.py:1135` `FileAccess.loadJSON(self.service.jsonQue.pop())`. Set dedup preserved (identical dicts → identical JSON strings → one entry).

**Visibility, missing movies, filler caps (imports.50/51/52/53):**

- Deferral-gate visibility (imports.50) — 4 `self.log(...)` calls in tasks.py promoted from default LOGDEBUG to `xbmc.LOGINFO`. With Kodi's `debug.showloginfo=false` (the recommended operator config — onscreen debug overlay is intrusive), LOGDEBUG goes to /dev/null. The promoted lines (`_chkPropTimer, chkChanged deferred (Builder.buildChannels running)`, `chkImports, deferred (Run_While_Playing=false and a video is playing)`, `chkImports, deferred (Imports.syncAll already running)`, `chkImports, deferred (Builder.buildChannels running)`) are intentional gates — they correctly prevent thrash, but their invisibility made `/channels/rebuild.json` POSTs look like they did nothing when in fact a debounce was working as designed. Zero behavior change, log-visibility only.
- `Builder.setTrailers` in-place mutation (imports.51) — **THE SHOWSTOPPER**. `item.update({'duration':dur, 'file':item.get('trailer'), 'label':'... - Trailer', ...})` mutated the CALLER'S movie dict in place; the caller at builder.py:931-936 then appended the (now-mutated) dict to `fileList`. Effect: every movie with a populated Kodi-library `trailer` field whose URL returned `getDuration > 0` was **silently replaced by its 2-3 min trailer in the channel chain**. Live-observed on FFOD: 4 Kurosawa films (Throne of Blood, Yojimbo, Sanjuro, Samurai Rebellion) appeared in EPG ONLY as their 2-3 min trailers; the full-movie versions did not exist in the channel's 184 programmes. Fix: build `trailer_item = dict(item)` shallow copy, mutate the COPY, store the copy in trailerCache. Original `item` left intact for caller's `fileList.append`. Identical bug exists at `plugin.video.pseudotv.live/resources/lib/builder.py:742-746` — fork-only fix per project_fork_only_no_upstream. Post-fix verified: all 4 films restored to 96-120 min full runtime in FFOD.
- `Enable_Postroll` literal total cap (imports.52) — settings.xml: spinner-with-preset-options (`-1/0/1/2/3` = Auto/Off/One/Two/Three Insert) → integer slider -1..20. fillers.py: after `random.shuffle(postFileList)` apply `postFileList = postFileList[:cap]` when not in Auto mode. Pre-fix the "One Insert" label was misleading — implementation set `postFillRuntime = MIN_EPG_DURATION = 10800s` (3-hour budget) and consumed items until pool empty or budget exhausted, producing 5+ items per gap with full bumper/trailer pools. Post-fix: literal max N items per programme boundary (total across adverts + trailers).
- `Enable_Preroll` literal total cap (imports.53) — mirror of imports.52 for the pre-roll path. settings.xml: spinner (`-1/0`) → slider -1..20. fillers.py: after `getSingle(ftype, preKeys, ...)` returns preFileList, apply `preFileList = preFileList[:cap]` (with `random.shuffle` first so the cap doesn't bias toward the first preKey — movies use `[fmpaa, fcodec]`, TV uses `[chname, fgenre]`). Symmetric semantics with imports.52.

**Cache maintenance off the tune path (imports.54):**

- Root cause of "select a Custom channel → endless busy spinner until back+reselect", diagnosed live 2026-08-28 with py-spy against the running Kodi (15m20s observed stall). Every plugin invocation's module import (kodi.py:32 `Settings` class body → `Cache()`) ran `_cleanUP` synchronously when the `Max_Days` window (unit = hours) had lapsed — a per-row blocking `waitForAbort(CPU_CYCLE)` grind + per-expired-row DELETEs + VACUUM, 10-15+ min at 29k rows / 134 MB. And `lastexecuted` was stamped even on aborted passes, so expired rows were never purged → unbounded DB growth (3-week-stale expired rows observed).
- Fix: `chkCleanup(force=False)` defers in constructors; NEW `Tasks.chkCacheClean` (service queue, hourly check, sole `force=True` caller) is the only place maintenance runs; `_cleanUP` rewritten set-based (one SELECT of expired ids, one DELETE **whose cursor is checked** — `_execute_sql` returns None on any failure, and only a confirmed purge stamps `lastexecuted`; VACUUM/index only after a successful non-empty purge) with `finally`-cleared `cleanbusy`; `Max_Days` re-read at check time (was frozen at construction for the service's session-long instance); `_execute_sql`'s per-statement 16ms blocking sleep replaced by non-blocking `Service._aborted()`. Mirrors upstream 0.7.x's cache-rewrite direction (their `_cleanDB` docstring documents the same disease: "expired rows accumulated forever, bloating cache.db to 300+MB on low-RAM SOCs"). Pre-commit adversarial review (23 agents, 3 lenses + skeptic verification) caught the stamp-on-partial-failure hole before it shipped.
- Diagnostic technique worth keeping: `sudo py-spy dump --pid $(pgrep kodi.bin) --nonblocking` stack-dumps Kodi's embedded Python 3.13 — caught the stuck invoker mid-grind, twice, with progressing frames.

**Changelog popup + repo publishing (imports.55):**

- The version-change changelog popup now shows only the newest entry's operator-facing part (new `changelog_helpers.latestEntry`, cut at a `--- engineering notes ---` marker line). Entry convention going forward: short bullets first, marker, then the engineering narrative — a test pins the shipped changelog's latest entry under 2500 chars.
- `plugin.video.pseudotv.imports` is now published through the repo pipeline: release.py builds its zip (tests/ excluded from all zips), release.yml also triggers on `v*imports.*` tags (dash form, e.g. `v0.8.0-imports.55`). First release published 2026-08-28; Kodi's addon update flow now works for normal installs. madteevee itself stays on git + restart (the addon is a symlink into this checkout — Kodi's updater must never touch it; versions stay equal so it remains inert).

**Guide-failure resilience + logo fixes (imports.56):**

- Root cause of "an imported source's guide vanishes for hours": when an import's channel list loaded but its guide did not (download error, unusable 200 body, 304 with an unreadable cache), syncOne returned `epg_pairs=[]` with `epg_id_map` populated, and syncAll's XML branch pruned the import's slice BEFORE matching, then appended nothing; the refresh gate (600 m) kept it gone. Only imports with a local channel list and a remote guide (Movistar+) hit it — fully remote imports fail at the M3U step and keep their slice. A debug-logged boot did not reproduce it: boots don't strip, failed guide downloads do.
- Fix: syncOne falls back to the per-import cache (`epg_keep_previous` when there is none); a 200 body only replaces the cache when it parsed into >=1 item; per-import `epg_fail_count` drives the gate's retry curve (effective cadence = the next imports cycle, since the gate is only evaluated per cycle); `last_error` surfaces it on the dashboard while `status` stays ok/unchanged. The XML branch now matches first; keep-previous and zero-match (guide configured, nothing skipped as disabled) keep the slice minus entries for non-live channels. Self-heal: `_guideNeedsSelfHeal` forces a sync for an enabled import with a guide source, >=1 live channel and a non-stub cache whose slice has no programmes (throttled 3 h per process).
- Logos: new `logo_links.py`. render_m3u re-points own `/images/` links to the current Remote_Host; `/images/<bare image name>` inside LOGO_LOC is public (everything else under /images/ keeps the auth gate — for authenticated/loopback callers the handler can read the whole cache dir and Kodi VFS paths); `HTTP._chkAddress` republishes Remote_Host on an address change and sets `RENDER_ONLY_KICK`; `_cacheLogo` never downloads our own links; `_evictOrphanLogos` keeps every channel present at cycle start (it used to keep only channels synced that cycle, so every gated cycle wiped the logo cache). Also fixed: `Imports_Failure_Retry_Curve` "Disabled" was read as aggressive (`or 1`) in imports.py and server.py.
- Live-verified 2026-10-15: GitHub blocked via /etc/hosts → sync kicked right after boot (before Builder) → Movistar+ guide rebuilt from the cache, dashboard error shown; unblocked → a render-only (gated) cycle retried Movistar+ through the guide-retry gate while epg.best stayed skipped, fresh guide, error cleared; 58 cached logos survived the gated cycle; LAN logo fetch 200 without auth; traversal / special:// / dashboard still 401. 770 pytest.
- Open: (a) Movistar+ orphan flip-flop — fixed in imports.57 (below). (b) syncAll persists import configs before the XML branch, so that branch's validator clearing never persists. (c) Live-test recipe: kicks are deferred while Builder runs — kick right after boot, or use the render-only kick for gated-cycle checks.

**Orphan stability across 304 cycles (imports.57):**

- syncOne's 304 path used every stored record of the import as "parsed", so applyRefresh cleared `is_orphan` on channels the source had dropped; they returned on each routine (304) sync and left again on each full (200) read. The 304 parsed list (and its epg_id_map) now excludes `is_orphan` records — an unchanged source still lacks them. A channel the source lists again returns through the next 200 read. Live trigger: 11 enabled Movistar+ channels (six movie channels outside the account's package — the Movistar+ addon exports only entitled channels — plus five pop-ups gone from the catalogue). 773 pytest.

**Operator-set logos reach Kodi (imports.58):**

- Symptom: imported-channel logos set in the dashboard as web addresses never showed in Kodi, across restarts; logo-pack (local) picks did. Causes: (1) the addon configures iptvsimple with `logoFromEpg=2` (Prefer XMLTV, `kodi.py` `_IPTV_SIMPLE_SETTINGS`, inherited from upstream), so the guide `<icon>` beats `tvg-logo`, and syncAll only copied the channel logo into `<icon>` when it was not a web address — dead for cached logos since imports.44; (2) `_cacheLogo` mode 0 reused any file named after the channel without comparing the recorded source address, and mode 1 sent the old address's validators to a new one; (3) a re-downloaded picture kept its file name, and Kodi never re-checks a cached http(s) image (TextureCacheJob: not updateable); (4) the cache step wrote the addon's own `/images/` link into the channel records themselves (5 orphaned records had it on disk).
- Fix: `_guideIcon` — an operator-set logo becomes the guide `<icon>` (cached link, else as typed); local logos as before; every other channel keeps the guide source's icon (operator decision). Also applied to slices kept by keep-previous / zero-match. Cache copies are reused or revalidated only for the address they came from; records always carry `url` + `file`; new pictures are saved as `<channel_id>~<md5[:8]>.<ext>` so a new picture gets a new link (old `<channel_id>.<ext>` files stay until they change). The published address lives in a per-cycle `published_logos` map, never in the records. render_xmltv re-points own-link channel icons at the current host (as render_m3u does since imports.56).
- Live-verified 2026-10-20/21: after the restart a dashboard Refresh synced Autonomiques; TV3 and À Punt got new versioned files, the guide and playlist links pointed at them, and Kodi's TV DB stored the new links (no user-set icons). No channels.json logo changed, and no other channel's guide icon changed (one Movistar+ channel newly got a guide entry, with the guide source's icon). Note: the import's 10 h refresh interval gates a plain restart, so a logo edit needs the Save kick (or Refresh); iptvsimple then reloads within its 5 min refresh — no addon toggle needed. 799 pytest.
- Released 2026-10-21 (tag `v0.8.0-imports.58`): the Kodi repo went from imports.55 straight to .58 — .56 and .57 were committed but never tagged. The release page attached the wrong addon; see "Imports releases" under Release / Kodi repo.

**Movistar+ replays: rich guide + replay links (imports.59):**

- Goal: a past Movistar+ show picked in the guide plays from its start. The import's guide source (davidmuma's EPG_dobleM — thanks to its maintainers) stays the source of descriptions and pictures for most shows; each show's replay link (`catchup-id`) comes from the Movistar+ add-on's own guide export, `epg.xml` next to its `channels.m3u8` in its addon_data folder (thanks to the plugin.video.movistarplus author for exporting per-show links). Movistar+ 0.11.9 declares `catchup="default" catchup-source="{catchup-id}" catchup-days="7"` in its playlist; in IPTV Simple's default mode a non-empty catchup-source is the full replay address. A `KNOWN_CATCHUP_DEFAULTS` row of ours blanked that source, so past shows played live; the row is removed and channels keep the playlist's values.
- How: new import-light `replay_helpers.py` (KNOWN_EXPORTERS code table, `exporter_for`, `link_replay_days`, `merge_replay_guide`, `channels_with_links`, `export_due`). One switch: an import uses link replay only while a live channel has link-based catch-up after every catch-up setting applies. syncAll reads the export after the record merge (`_readLinks`: the safe local read, a full read every sync, a strict parse limited to the link channels) and merges it into the fresh guide (`_mergeReplayLinks`) or the kept one (`_mergeLinksIntoKept`), keeping up to 7 days of linked history that only `pseudotv.xml` holds (the export starts at its own day's 00:00). Each linked show keeps its own time and link and takes its details from, in order: the guide source's show starting at the same second; an already-published show matching it on two of link, start and title; for a show without details of its own, the guide source's show that overlaps it with the same title — the two sources often time a show a few minutes apart — chosen by episode number (the guide source's title label vs the export's sub-title; a different season or episode rules a show out, a season alone isn't enough, `T2025/2026` sports seasons are read), or, with no episode number to compare, the only such show when it overlaps no other show of that title; else its own entry (title only). Shows from before the update, a few that the two sources list differently (other titles, or times with no clear match) and channels the guide source doesn't cover stay titles-only. The guide-load trim keeps that history (`cleanProgrammes(keep_days=...)`). A changed export (`st_mtime_ns` vs the stored `links_mtime_ns`, kept as a decimal string because the dashboard's JavaScript rounds numbers that large) forces the import's sync at the next cycle (`_linksChanged`); a just-modified export that can't be parsed yet is retried instead of being recorded as damaged. About once a day, while idle with the screensaver on, Movistar+ is asked to re-export through its own export action (`tasks._maybeRefreshAddonExports`), also when the file is missing. A link-based channel is published with replay off while the show on air has no link (`_replayOffers`, record unchanged); for the first few days after the update, older past shows can still fail to play. Dashboard messages flag a missing/damaged/old export and say how to get a fresh one (`last_error` only, counters untouched). `PLAYLIST_REREAD_REV` reads every playlist in full once; `source_name` keeps renamed channels matched to their guide on unchanged playlists.
- Safety fixes that protect the kept history: the XMLTV writer attaches entries only when complete and render_xmltv returns None (file kept) instead of a `<tv/>` stub; a programme without start/stop/channel is skipped; sortProgrammes is None-safe; cleanSelf keeps every entry when channels.json can't be read; setJSON is atomic (a per-writer temp file + fsync + os.replace, so the service and a Kodi script saving at once never share a temp file) and reports failures; `_fetchLocal` reads local files with `os.stat` before/after + `open()` (never `xbmcvfs.Stat`), and a file that changes during the read is retried — the Movistar+ add-on rewrites its export files in place; setChannels adopts the import list it re-reads (only setImports writes it), so long-lived Channels instances no longer write back an old one; Builder's import sync starts from a fresh Channels read.
- Notes:
  - Never use Kodi's Versions, Install from zip or Uninstall for this add-on while its folder is a symlink into the git checkout: Kodi's installer would delete the checkout through the link. Roll back with git.
  - IPTV Simple inserts the `catchup-id` with `std::regex_replace`, which reads `$` in the replacement as a format character, so an id containing `$` would come out altered (none do today).
  - IPTV Simple identifies a channel by its name plus its stream address, so renaming an imported channel makes Kodi see a new channel (favourites and groups that held it need re-adding).
  - Builder now waits (up to 5 minutes) for a running import sync before loading the guide files, and chkImports re-checks Builder after raising its own flag. If Kodi shuts down or the service interrupts that wait, the build is skipped (and re-queued unless Kodi is exiting); a dashboard kick (Refresh, Save) taken just as a build starts is put back and fires after the build instead of being lost.
  - A stored `links_mtime_ns` means the export was read, not that its links were merged: if the merge fails (logged as an error), the new-links trigger doesn't force a re-read; the import's next sync (at most its refresh interval, 10 h here), a dashboard Refresh or a new export merges it again. The links already in the guide cover the coming days, and a repeating error must not force a sync every hour.
  - Follow-ups: a crash in the middle of a setJSON save leaves a uniquely named `<file>.<pid>.<thread id>.tmp` that nothing removes yet; `renderers.write_atomic` still uses one shared `<file>.tmp` for pseudotv.xml/m3u; the same-start rule doesn't compare episode tags, so in rare cases a show takes the details of the episode the guide source starts at that second.
  - The guide load decodes the description payload only for the add-on's own Custom programmes now; other places that decode description payloads (context menus, channel matching) are unchanged — a follow-up.
- Tests: 19 `test_imports59_*.py` files (415 tests); 1214 pytest. Offline before/after runs on copies of the live data (3 Nov files, clock frozen, no network; review-round code): Custom channels and the other two imports rendered byte-identical; the Movistar+ slice gained 9,350 linked shows with 0 overlaps, 258 of them given the guide source's details by the overlapping-show rule, none from another episode (124 episode-number matches, all 127 episode names found in the guide's title); a second sync was byte-identical. The final tag rules give the same 258 picks on the same data. Day-2 and one-week simulations: 0 overlaps, 0 replay links lost; past linked shows that show titles only fall from about 7 in 10 on the update day (older than the guide source's current file) to about 1 in 8 after a week.
- Live-verified 2026-11-03/04: after the restart, one Refresh all re-read all three playlists (`playlist_rev` 1), and the 48 live Movistar+ channels carried `catchup="default" catchup-source="{catchup-id}" catchup-days="7"` in their records and in `pseudotv.m3u`. The first merge (2 Nov export) gave 10,855 Movistar+ programmes, 9,350 linked, 0 overlaps, and a link on every channel's on-air show; the export Movistar+ wrote at start-up was merged overnight, hours before the import's 10 h refresh was due (new-links trigger). Channel 17 moved to the Movistar+ list's "Movistar Plus" through two `/imports/channel/edit.json` calls; only those two channels changed number, and Kodi showed it at 17 after IPTV Simple's next reload. On the TV, a show whose link was saved before it aired and one that aired before the export was written both replayed from their start (u7d catch-up URL, VOD stream); Kodi's play-next chained two past shows into the show on air, which started from its beginning; channel 17 played live. IPTV Simple's guide load stayed at 270-281 ms (251-276 ms before).
- Released 2026-11-04 (tag `v0.8.0-imports.59`): the release page carries the imports zip and the repository zip.

**Rollback watched as an exact undo, no duration write-back, MP4 lengths (imports.60):**

- Rollback watched: when a PseudoTV programme plays its own video file (catch-up picked in the guide, catchup="vod" channels; in playlist mode without vod only the first entry, because the queue builds the others from the first programme's file), the file's watched count, last-played date and resume point are read from Kodi (Files.GetFileDetails) and kept in memory. When the programme ends (stop, natural end, next programme, or a hand-over to other playback, where Kodi sends no stop), Kodi has already saved its marks; the file is read again and only the fields Kodi changed are put back with Files.SetFileDetails, the per-file write that touches exactly the rows Kodi's own save writes (Kodi describes it as "update the given specific file"; the Netflix add-on uses it to fix watched status). Live programmes play as `pvr://` items, which Kodi never marks, so they get no snapshot. The tracker's decisions live in the import-light `playstate_helpers.py` (PlaystateTracker, one lock, owner key per programme); `services.py` supplies the Kodi calls plus two small checks (the live-TV gate `_playstateFile` and the settings switch `_chkRollbackSwitch`), which the tests compile out of `services.py` and run against fakes. Switching the setting off cancels the pending rollback at once, not after the 15 s settings debounce. The snapshot reads skip the RPC_Delay pause, so the "still playing?" check follows the read at once. It replaces `quePlaycount`, which sent the guide-build play count plus resume {0,0} at every programme change and erased resume points. Rule 53's stop step is fixed (it restored a value lost to the per-call rule reload); its start step still runs only on a channel change, as before.
- Duration write-back removed: `queDuration` (Store_Duration) queued requests without a "method"; Kodi rejected every one ("JSONRPC: Failed to parse"), and a working version would have sent resume position 0, which clears resume points. The imports.48/.49 notes above about Store_Duration correcting the library were never true in practice. Lengths stay in the add-on's cache; Store_Duration is hidden (level 4) and kept for rule 500.
- MP4 reader: ported the fallback restructure and the always-close from upstream 10a2728b/ce64bafa (thanks, upstream). Upstream's track-header fallback and 2 GB box cap are deliberately not ported (a track header's length is in the movie's time unit, and 32-bit box sizes go up to 4 GB); tests fail if they come back in a future sync.
- Rollout in two stages at the same version: stage 1 (`PLAYSTATE_WRITES = False`, local only) logs "would send <request>" instead of writing; stage 2 flips the constant after the stage-1 log check. Only stage-2 code is released. Stage 1 means no rollback at all until stage 2.
- Notes: a restored resume point is whole seconds and loses Kodi's remembered audio/subtitle choice for that file (JSON-RPC can't set them); a change made to the same library item during a catch-up programme (another device, a manual mark) is reverted with it; snapshots are memory-only, so a Kodi shutdown mid-programme keeps Kodi's marks; overlapping fast zaps can miss a rollback but never write a wrong one; a resume point stored without a total (by another app) comes back with Kodi's stream length as its total when the play cleared it. The INFO lines ("snapshot", "would send", "restored", "no change") need the add-on's debug logging with Debug Level at LOGINFO or LOGDEBUG (the default, LOGERROR, hides them); errors are logged unless Debug Level is LOGFATAL.
- Tests: 5 `test_imports60_*.py` files (161 tests); `test_get_duration_accurate.py` now runs the real code; 1373 pytest. On a copy of the imports.59 code the new tests fail where expected (the trailer test reproduces the live "queDuration, failed! 'NoneType'" error); 8 deliberate breaks of the rollback logic (stage switch inverted, no re-check, no owner check, ...) and 15 of the `services.py`/`jsonrpc.py` glue (live-TV gate, settings switch, read pause) are each caught.
- Live-verified 2026-11-04 (Kodi 21.3). Stage 1: over a full channel build the add-on's "Failed to parse" lines (about 90 an hour before), queDuration, MP4 and NFO errors were 0; zaps between Custom channels took no snapshot; a Custom replay was snapshotted 0.5 s after it started and logged the exact undo at Stop, which Kodi accepted when sent once by hand. Stage 2: replays were restored on their own about 1.5 s after Stop, exactly to the recorded values (watched count, a 2025 last-played date, no resume point), also after a re-click mid-play. A guide replay ("Play programme") of a Custom programme still in pseudotv.xml played and was undone after Stop. Seen along the way, unchanged by this update: a rebuild of a Custom channel deletes its already-aired programmes from pseudotv.xml, and every add-on update rebuilds all Custom channels (the playlist fingerprint is cached with the version), so after an update only programmes aired since the rebuild can be replayed (IPTV Simple's catchup="vod" mode replays only entries present in the XMLTV); the first stage-1 replay test therefore opened the programme's own replay address. During a Custom replay getCallback's channel lookup (Files.GetDirectory on pvr://channels/tv/) is refused by Kodi about 40 times a minute.

**Replays of own channels survive rebuilds and updates (imports.61):**

- Why replays of own channels were lost: own (Custom) channels are `catchup="vod"` with no `catchup-days`, which is pvr.iptvsimple's "Channel K". A guide entry can be replayed only while pseudotv.xml still holds it with its catchup-id, and Kodi keeps showing past entries from its own copy of the guide. A rebuild of a changed channel deleted all of its programmes, past included (`__clrStation` → `xmltvs.delBroadcast`). And every add-on update counted as a change for every Custom channel, because the playlist fingerprint was cached with the add-on version.
- Keep aired programmes (A): a rebuild now keeps every programme of the channel that started before the rebuild point, including the one on air at that point. The new line-up starts right after that programme: no gap, no overlap.
  - Code: `replay_helpers.split_for_rebuild` / `rebuild_resume` (pure, parsed epochs); `XMLTVS.trimBroadcast` / `resumeAfter`; `__clrStation(citem, cut)`.
  - A changed build that produces nothing still counts as failed and is restored.
  - `_restoreChannel` now replaces the channel's station, channel element and programmes instead of appending them.
  - Channel delete/disable still removes everything (`delBroadcast` unchanged).
  - Exception: PauseRule (3000) channels keep the anchor (`Builder._rebuildContinuesAfterAired`): their BUILD_TIME_PRE clears all their programmes and lays one cell from the start, so continuing after the long kept cell would leave a guide hole of days.
  - `XMLTVS.addChannel` now finds the channel in the list it updates. It used the index from a sorted copy, and after a save in the same build call the real list is in another order, so the upsert overwrote another channel's element (that channel's guide was then pruned as an orphan). Rebuilds now keep the element, so every rebuild takes this upsert; the same flaw affected top-ups in a multi-channel build call before.
  - Upstream nightly still clears a changed channel's guide (builder.py:403-415 @8ad15cbd), so this is a deliberate divergence. A future forward-port must keep `trimBroadcast`, `resumeAfter`, the PauseRule exception, the snapshot before the trim, the replace-restore, the failed-build guard and the `addChannel` lookup. Upstream's 7704c63d lesson ("don't rewrite what Kodi already loaded") is the same idea.
- Updates are not playlist edits (B): `cache.getChecksum(..., versioned=False)` returns 0, "no check number" (script.module.simplecache semantics: the stored check is not compared, the expiry still applies).
  - Only the playlist fingerprint (`Settings.getFileCRC`) and the playlist position (`JSONRPC.autoPagination`) use it, both on the database-only cache instance.
  - Rows written by earlier versions are reused (same keys, values compared), so installing the update rebuilds nothing.
  - Still reset on every update, on purpose: the changelog-once flag, the saved job queues, measured lengths and the other caches.
  - A release that needs channels rebuilt asks for "Rebuild all" (history is kept, playlists restart). Measured lengths are normally re-measured after an update, but the cache check number is a sum of characters, so two versions with the same sum (e.g. .61 and .70) share it; a fix to how lengths are measured therefore also clears the `getDuration.` rows on purpose.
  - Going back below .61 after .61 has run makes every own channel "changed" once (the old code can't read the version-free rows): the old rebuild removes the kept history, restarts the playlists and re-measures lengths. Upgrading again is safe.
- Unchanged, still open:
  - the rebuild anchor is early (getUTCstamp: 1-2 h in winter, 2-3 h in summer), so the hour or two before a rebuild is still rewritten (slightly less than before);
  - a rebuild interrupted after a real playlist edit is not retried (updates used to repair that by accident; Rebuild does).
- Size: pseudotv.xml grows about 3.2 MB per day of kept own-channel history (measured: about 790 programmes a day × 4 KB), about +10 MB at Min_Days = 3.
- Tests: `test_imports61_keep_aired_history.py` (42) and `test_imports61_update_keeps_playlist_state.py` (15); 1430 pytest.
  - Coverage: the helpers; the XMLTVS methods incl. the `addChannel` upsert; replace-restore; the PauseRule anchor; end to end with the real addProgram, addScheduling, render and read in UTC and Europe/Madrid (winter and summer); the builder wiring incl. the snapshot before the trim; the real cache on a temp sqlite file with a simulated update, the 28-day position life and a whole-add-on scan for version-free calls (functions, class bodies, positional use).
  - On the imports.60 code they fail where expected: the fingerprint and the playlist position saved by imports.60 were treated as lost after an update.
  - 26 deliberate breaks are each caught.
- Live-verified 2026-11-05 (Kodi 21.3).
  - Installing imports.61 rebuilt none of the 13 Custom channels: oldest shows and programme counts unchanged; the 13 playlist fingerprints kept their values and were rewritten without a check number; all 13 playlist positions were kept (e.g. 500 of 2098).
  - Rebuilding TMNT alone (dashboard per-channel Rebuild):
    - kept its 95 programmes that started before the 07:00 anchor byte-identical (start, stop, title, catchup-id; history back to 3 Nov 23:00);
    - wrote 500 new ones starting exactly at the last kept stop (07:20:28), with no gaps or overlaps;
    - left the other 12 channels untouched;
    - stored TMNT's new playlist position without a check number.
  - Kodi reported the kept shows replayable (PVR.GetBroadcastIsPlayable). A guide replay of one of them (Player.Open by broadcastid → IPTV Simple EPG catchup URL → the add-on's fast path) played the right episode, and the imports.60 rollback undid its marks after Stop.

**Restart from the beginning, own channels and LiveTV (imports.62):**

- Goal: a show that is already running can be started again from its beginning, on own (Custom) channels and on LiveTV channels whose show on now can be replayed (Movistar+ link replay). Kodi 21 has no restart button for live TV; its built-in way is "Play programme" on the guide entry (offered only for playable entries), and Smart select switches to live for a show on now. On own channels "Play programme" on the show on now joins it mid-way on purpose (2026-05-11) and stays that way.
- Three ways in, one action (`services.Player.requestRestart`, one at a time):
  - **B, the Restart pop-up**: offered about 5 s after a tune (debounced, OSD_TIMER), once full-screen video plays with Kodi's channel info, the channel preview and any other dialog gone; it shows for about 10 s. It is offered when the show has played at least "Restart Percentage" (`Replay_Percentage`, default now 5, 0 = off) and less than `Seek_Threshold` (98). Own channels: the playing episode's progress, never fillers or radio; channel rule 54 can set its own value. LiveTV: the guide's show on now on a live channel with replays (`hasarchive`) that Kodi says can be replayed (`PVR.GetBroadcastIsPlayable`), with the global value. It closes itself when playback changes (a playback "epoch" counted in the player events) or the LiveTV show ends.
  - **C, a remote button**: `NotifyAll(pseudotv, restart)` restarts what plays.
  - **D, "Restart from beginning"** in the Menu of the TV guide and of the in-player channel guide (`kodi.context.item`, a top-level line), only on a show on now that Kodi says is playable. `context_restart.py` sends the entry's own path (`pvr://guide/<epg id>/<start>.epg`) with the same notification; the service matches it against the channels' show on now.
  - What a restart does: own channels jump back to 0:00 in place (`Player.seekTime(0)`, no re-tune), so at the end of the episode the channel returns to live as it always does. LiveTV calls Kodi's own "Play programme" path (`Player.Open {broadcastid}`, PlayEpgTag), so after the show Kodi carries on behind live (Kodi's "Play next programme automatically"). A replay that already plays jumps to its start. D on an own channel that isn't playing that programme tunes the channel and then jumps to 0:00 when it plays (a second of the show mid-way is visible); when Kodi plays that very channel with another programme, it is stopped first. Where a restart is impossible: "Restart Unavailable...".
  - Kodi doesn't tell Python that a replay was refused, so after `Player.Open {broadcastid}` the result is read from the player (`watch_outcome`), timed from the moment the call returned: live still playing after 10 s = refused (note). Kodi's player reports exactly one of stop, error or end when playback goes, and switches to the replay without a stop; only a playback error, with nothing playing, no busy dialog and no key pressed since the call, tunes the channel that played before again (note). A stop (any key or a remote app's JSON-RPC Player.Stop, which doesn't reset Kodi's idle time) or a key press leaves it to the viewer. A request made while a zap settles is decided again for up to 3 s, then gets the note.
- Code: import-light `restart_helpers.py` holds every decision (no Kodi imports, no clock reads). `services.py` reads Kodi and acts; `overlay.Replay` is a passive view. New narrow reads in `jsonrpc.py` (`getPlayerItemRef`, `getPVRChannelInfo`, `getPVRChannelsNow`, `isBroadcastPlayable`): no `file` or all-fields requests (Movistar+ stream links carry tokens, and debug logging writes whole replies) and no RPC_Delay pause. A channel's `broadcastnow` is read only after its `hasarchive` (the channels without replays have signed artwork links).
- Old bugs fixed: the pop-up closed the moment it opened (`Replay` used a `self.service` it never set), it was offered once per Kodi session at most (`self.replay` was never cleared), its key pass-on fired at once (`AlarmClock(...,.5,...)` reads `.5` as 0 minutes; now a real 0.5 s timer after the close animation), and rule 54's stop step restored a value lost to the per-call rule reload, switching the pop-up off on every channel (same fix as rule 53 in imports.60). Upstream nightly has the pop-up call commented out (services.py @8ad15cbd); the fork keeps it and owns it, so a future forward-port must keep `restart_helpers`, the playback epoch, the `toggleReplay` try/finally and `Replay` as a passive view.
- Keys while the pop-up is up (Python dialogs are always modal, so it takes the remote for those seconds): OK restarts; Back only closes it (Python windows consume Back); Up/Down, Info and the number keys are passed on once it has closed (Kodi's PVR ignores channel-number keys while a Python dialog is on top); channel +/- arrive in a dialog as PageUp/PageDown (keymap global section) and are passed on as ChannelUp/ChannelDown while live TV plays; any other key only closes it. Typing a two-digit channel number very fast right as it closes can still lose or swap a digit.
- Remote button set-up (pick a free button):
  - UC Remote 3 (Kodi integration, Remote entity, "Send command"): `JSONRPC.NotifyAll {"sender":"pseudotv","message":"restart"}` (59 characters; the integration's limit for custom commands is 64; the sender is short for that reason).
  - Or a Kodi keymap in its own file (not gen.xml, which keymap editors rewrite), for example `userdata/keymaps/pseudotv-restart.xml` with the button's key under `<fullscreenvideo>` and `<fullscreenlivetv>` mapped to `NotifyAll(pseudotv,restart)`.
  - Any device allowed to use Kodi's JSON-RPC can send the same notification.
- Notes: the new `Restart Percentage` default (5) reaches new installs only. Kodi's add-on settings load re-applies every saved value after the settings manager's default handling (CAddonSettings::Load), even one saved as default, so an existing install keeps 95 until it is changed in Settings → Global → Overlay (set to 5 on madteevee through the add-on's settings API). During a restarted own-channel episode the skin's info bar keeps showing the guide's progress. The pop-up waits for the JSON-RPC lock the add-on shares with the Builder, so a long library call can delay it; every step re-checks the channel and the playback epoch. With debug logging on, the guide entry's channel-list read is logged in full, the same data matchChannel already reads and logs at every own-channel tune. A same-channel tune after a stop doesn't re-run the channel's start rules (unchanged), so a rule-54 value applies again from the next channel change. Left alone: the unused `jsonrpc.getPlayerItem` (calls a missing `getActivePlayer`, asks for all fields) and `getPVRChannelsDetails` (reads `channels`, Kodi answers `channeldetails`).
- Tests: `test_imports62_restart_helpers.py` (114) and `test_imports62_restart_glue.py` (72: the restart methods compiled out of `services.py` and run against a stand-in player, `Replay`'s keys, `onNotification`, rule 54 for real, the jsonrpc requests, settings, strings, addon.xml, `context_restart.py`); 1616 pytest; 59 deliberate breaks are each caught.
- Live-verified 2026-11-05 (Kodi 21.3), driven through Kodi's JSON-RPC (the remote's OK = `Input.Select`):
  - Own channels: the pop-up showed about 3 s after the tune once the channel info had gone; OK jumped the episode to 0:00 in place (Kodi's `Player.Time`; JSON-RPC `Player.GetProperties` reports the guide's times for TV channels). A restarted 12-minute episode played to its end, then the channel re-tuned live and joined the show on now, with no pop-up.
  - Movistar+: OK started Kodi's own replay of the show on now from its start (`started` about 1.5 s after OK); a restart of a show still on air played past the show's scheduled end (then the TV's CEC standby paused it). The remote message did the same on live, and inside a replay it jumped back to the replay's start.
  - Channels without replays (A Punt, TV3): no pop-up, the remote gave "Restart Unavailable...", and only their `hasarchive` was read.
  - The guide Menu showed "Restart from beginning" only on the show on now (not on past or future shows); choosing it on the show a replay was playing jumped it to its start and returned to full screen. The same message for an own channel that wasn't playing tuned it and then jumped to 0:00; for Movistar+ it started the replay while an own channel played.
  - Keys while it was up: Back stayed in full screen; channel + changed channel; Up and Info showed the info; a number key started channel-number entry; Play/Pause paused.
  - A restart plus Stop during an own-file guide replay restored the film's watched marks exactly (imports.60).
  - Right after the update the add-on's changelog window (modal) keeps the pop-up away until it is closed.
  - Found, not from this update: "Play programme" started through Kodi's JSON-RPC (`Player.Open {broadcastid}`) on an own channel's show on now plays from 0:00, because that route hands the add-on no programme times (fitem holds only file and title), so playPlaylist's join check never runs. Where the times do arrive, that check compares them with `getUTCstamp()` (`time.time()` minus the UTC offset), so a show less than an hour in (two in summer) would also start at 0:00: the clock issue "D".

**One real clock (imports.63):**

- Why: `getUTCstamp()` was `time.time()` minus the UTC offset, 1 h slow in winter and 2 h in summer here. It set the rebuild cut, the start of new channels, the guide-extension window, the "Play programme" join, the Min_Days trim, the recording slot, PadScheduling and the PauseRule "last viewed" stamp. `epochTime(..., tz=True)` subtracted the offset once more for the Up next time and the "last viewed" line, and the guide writer stamped every time with the offset of the moment of writing (a show written before a change of summer time but airing after it was 1 h off in Kodi until it aired).
- Code: new import-light `clock_helpers.py` (standard library only, Python 3.8, no Kodi imports):
  - `now()` is the add-on's only clock read (`time.time()`); callers write `import clock_helpers` and call `clock_helpers.now()`, so tests can pin it.
  - `local_floor(epoch, minutes)` uses the instant's own `tm_gmtoff` (deterministic in the repeated autumn hour); `globals.roundTimeDown` now calls it.
  - `xmltv_time(epoch)` writes each time with its own instant's offset; the digits are those of `datetime.fromtimestamp`, so every own guide time written today is byte-identical.
  - `xmltv_to_epoch` (moved from `replay_helpers`, re-exported) reads a time without an offset as UTC, as the XMLTV DTD and IPTV Simple do (it read local time before; no guide here has such times).
  - `epg_utc_to_epoch` (moved from `restart_helpers`, re-exported), `build_window` (the Builder and the tasks build filter share one anchor and extend threshold), `join_offset` (the one join rule, half-open `[start, stop)`) and `plot_times_match`.
  - Retired: `getUTCstamp`, `getTimeoffset`, `getGMTstamp`, `roundTimeUp`, `M3U.getTZShift`, `xmltvs._xmltvTzSuffix` / `_stripXmltvTzSuffix`. `epochTime(timestamp)` keeps one argument; a `tz=` caller raises TypeError. `diffRuntime` is plain arithmetic.
- Rebuild cut at the real moment the new line-up is laid: `Builder._rebuildStart`, called right before `addScheduling` (after the build, about 9 minutes for a big playlist), trims at the real now and continues after the show on air, or at the later of the local hour floor and the last kept stop when nothing is on air; PauseRule (3000) channels cut and start at the real hour floor. `__clrStation(citem)` now only resets the playlist position, and `citem['changed']` is cleared unconditionally. Kept from imports.61: the snapshot before the trim, the replace-restore, the failed-build guard and the `addChannel` lookup. Replayed on the 11-05 TMNT guide: imports.62 replaced 6 shows that had started; the late cut keeps all 101 and continues at 08:42:45. A show that starts in the few seconds between the cut and the file write is still replaced.
- Guide readers: `XMLTVS.loadStopTimes(..., fallback_epoch=None)` and `hasProgrammes(..., now_epoch=None)` take and give epochs (the keywords are renamed so an old text caller fails loudly) and read parsed times through `replay_helpers.guide_bounds`. The Min_Days trim compares parsed stops with one clock read, so own and Movistar+ '+0100' shows leave the guide exactly Min_Days after they end (1 h sooner than before).
- "Play programme": `Plugin._setResume` passes TotalTime in seconds (it multiplied the duration by 60, which repeated the URL text on channel tunes and raised KeyError on the broadcast route, so a join there played nothing). `playPlaylist` and `playLive` join with `join_offset` on the real clock, and `playPlaylist` stores the show's length for TotalTime. `_getPVRItems`' by-time match used an undefined name and now works. Routes without programme times (JSON-RPC `Player.Open {broadcastid}`, the in-player guide, widgets) play from 0:00 with no lookup, by decision. New guard in `default.py`: start/stop decoded from the focused ListItem.Plot are dropped when that payload's file (`originalfile`, else `file`) isn't the requested `vid` (logged at INFO: "Default: dropped plot start/stop of another programme").
- Unchanged on purpose: `imports.py` (through the re-export), the restart logic's clocks, `context_record.py`, the cache/seasonal/backup local-time labels, the M3U writer and the writer lock. No new settings and no data migration: stored guide times and epochs are already right, and going back below .63 is data-safe.
- Upstream fixed the same clock in 009b2401 (v0.7.8) and 4c58e817 (v0.8.0) — thanks, upstream. The fork follows the same meaning with its own helpers, so a future merge maps:
  - `_getGMTstamp` → `clock_helpers.now`; `_getUTCstamp` → the real clock (`clock_helpers.now`);
  - `_epochTime(x, tz=False).strftime(f)` → `epochTime(x).strftime(f)`;
  - `_utcOffset` / `_offsetProgramme` → `clock_helpers.xmltv_time`;
  - `_roundTimeDown` (a UTC floor upstream) → `roundTimeDown` / `clock_helpers.local_floor` (a local floor here).
  - Not ported: `_getTimeoffset` (on this box it returns 3599 s in about 68% of calls), `_recover`, and the Seek_Threshold reset in upstream 20b4966a's `_setResume` (only its seconds fix is taken).
  - Conflicts are expected in `globals.py`, `xmltvs.py`, `builder.py`, `tasks.py` and `plugin.py`.
- Tests: `test_imports63_clock.py` (529; shared `tz` / `clock_zone` fixtures in `conftest.py` over UTC, Madrid winter and summer, both 02:30s of 2026-10-25, 2027-03-28, New York, Kolkata and Lord Howe, each pin checked). It covers the writer/reader round trip every 5 minutes around 9 offset changes against an independent IPTV Simple-style reader, byte identity, `local_floor` against the old `roundTimeDown`, the shared build window, the join and guard rules, the real `_setResume` / `playPlaylist` / `playLive` / `_getPVRItems`, the real `__hasChanged` / `__clrStation` / `__needsUpdate` closures and `Tasks._filterChannelsNeedingBuild` compiled out of `builder.py` and `tasks.py` (the filter queues exactly the channels the Builder extends), the late cut on the real 11-05 TMNT guide, and AST guards over every module in `resources/lib` (retired names, one-argument `epochTime`, an explicit `import clock_helpers`, a Python 3.9+ API deny-list, the build window bound once). The imports.59/.61/.62 tests that pinned the old rules are updated. 2151 pytest, the same in the Madrid, UTC, New York and Kolkata machine zones; 110 deliberate breaks each caught. Review round: playLive's join keeps a usable show length when the URL's `{duration}` was zeroed, and `playPaused`'s resume `TotalTime` is in seconds (dormant here).
- Deliberate breaks: 110 (the plan's 54 and 56 more of the same rules), each applied alone to a scratch copy, are each caught, also with the tests run in UTC. The first pass missed 7, each now caught by a new test: a rounded hour floor, `changed` tied to the playlist reset through a variable, a build threshold shifted in the Builder or in the filter, a stricter filter compare, a join without a programme, and an unpinned zone fixture.

**Maintenance posture**: the imports addon does NOT rebase against upstream — it's a one-time fork that diverged from `plugin.video.pseudotv.live` after the `madteevee-on-nightly` rebase. Forward-ports from upstream into imports/ happen manually when needed.

**Eventual replacement plan** (operator-stated, not yet executed): replace `plugin.video.pseudotv.live` with the imports addon. The plan is to deprecate the live sibling once imports has full burn-in confidence. Migration concerns: Kodi addon_data path rename, addon ID change throughout the codebase, M3U URL rewrites for already-emitted Custom channels, pvr.iptvsimple instance config. Deferred for a dedicated session.

**Per-version history**: see [`plugin.video.pseudotv.imports/changelog.txt`](./plugin.video.pseudotv.imports/changelog.txt).

## Maintenance workflow

```bash
# Pull upstream changes
git fetch upstream

# Rebase our patches against the latest nightly
git checkout madteevee-patches
git rebase upstream/nightly
# resolve any conflicts, then:
git push fork madteevee-patches --force-with-lease
```

Each forward-ported fix sits on its own commit (executescript wrapper, PSEUDOTV_SLUG, B3, B6, `_tune_ts`, build infra, logos addon) so that conflicts during a future `git rebase upstream/nightly` localize to the file that changed instead of one big merge resolution.

## What the fork actually changes (post-rebase to nightly)

Most of the master-era fork patches landed in nightly upstream. The forward-ported set is now smaller — only the bugs nightly still has and the fork-specific decoupling.

### Forward-ported from the master-era fork

| Commit | What broke (in nightly) | Where |
|---|---|---|
| `kodi.py executescript wrapper` | `xbmc.executescript()` is the wrong API — takes only a path, but callers pass `'foo.py, ARG1, ARG2'`. Comma-args become part of the filename and `CFileUtils::Exists()` fails, script never runs. Replaced with `xbmc.executebuiltin('RunScript(...)')` which parses commas correctly. | `kodi.py:1193-1200` |
| `PSEUDOTV_SLUG` decoupling | nightly generates chids via `Globals._slugify(ADDON_NAME)`. Renaming the addon to `PseudoTV Live (madteevee)` slugifies to a new value; existing channels saved with the old slug stop matching → `isPseudoTV` check fails → channels appear non-PseudoTV. The constant pins the slug to `'PseudoTV_Live'` regardless of display name. | `constants.py:53`, `globals.py:48,54`, `services.py:115`, `m3u.py:277`, `xmltvs.py:239` |
| B3 don't-clrStation-on-empty | nightly's per-channel `else: __clrStation(citem)` wipes M3U/XMLTV references whenever a build returns empty (BUILD_AT_MAX, paginate-stalled, or transient). Master B3 only clears via the explicit reset flow (channel manager) — never from the build path. | `builder.py:289-298` |
| B6 paginate-past-extras | when `dirList` drains but pagination shows more content (`end < total`) and the page is unfilled, re-insert the last path. Handles "first page is all extras / 3D / sub-min-duration / strm" — without it, channels with skewed first pages bail empty. Capped at `MAX_BUILDFILELIST_REPARSE=10`. | `builder.py:401-432`, `constants.py:78` |
| `_tune_ts` filter (fast-zap) | Kodi back-fills `onAVStarted` in load-finish order, not user-zap order. During fast CH+/CH- bursts the LAST event often corresponds to the FIRST tune, anchoring player state to the wrong channel. Each `default.py` invocation stamps a per-tune timestamp; `services.onAVStarted` drops events whose `_tune_ts` is older than the max seen so far (gated on `isPseudoTV`). | `default.py:52-58`, `services.py:84-101` |

### Already in nightly upstream (no longer in the fork)

These were master-era fork patches that nightly merged. The fork no longer carries them:

- EvenShowsRule schema (Issue #1) — nightly's version has 3 slots wired to Force_Episode/Force_Random.
- Quota / PageLimit rule consolidation — nightly merged these as `HandleLimits` (rule 951).
- Library-walk tunables (`Decouple_Kodi_PVR`, `Streamdetails_Cache_Days`, `Library_Walk_Interval`, `Skip_*`) — superseded by nightly's `Cache_MEM_Limit`, recursive-depth controls, and Client Only mode.
- `setResetChannels` clearchannels-drain fix (Issue #7) — fixed differently in nightly via the singleton XMLTVS pattern.
- `RPC_Timer` rename — nightly renamed to `RPC_Wait`, the value carries through default.

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
| `madteevee-patches` | our source patches on top of `upstream/nightly` + `release.py` + `repository.mmadalone.pseudotv/` |
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
4. After install, **Settings → Add-ons → Install from repository → mmadalone PseudoTV Live (madteevee) → Video add-ons → PseudoTV Live (madteevee)** → Update / Install. The fork's `0.7.3+madteevee.X` will replace upstream's `0.7.3+nightly` (or any earlier version).
5. Optional: leave the upstream `repository.pseudotv` enabled too so its resource packs keep updating from upstream — most of them aren't mirrored in the fork repo.
   - Exception: `resource.images.pseudotv.logos.madteevee` IS shipped from the fork repo (a parallel logos addon with the same content as upstream's `resource.images.pseudotv.logos` plus custom additions). Different addon ID so it coexists with upstream's official one.

### Cutting a new release (after making patches)

The release pipeline is automated via `.github/workflows/release.yml`. Pushing a tag matching `v*madteevee.*` (or `v*imports.*`, see "Imports releases" below) triggers a workflow that runs `release.py`, publishes `_site/` to the `gh-pages` branch, and creates a GitHub Release object. Workflow can also be triggered manually from the Actions tab (`workflow_dispatch`) for republishing without a tag.

> **NOTE (2026-04-29):** The trigger pattern was previously `v*-madteevee.*` plus `v*+madteevee.*`. GitHub Actions' workflow parser rejects `+` as a literal in tag glob patterns (HTTP 422 "invalid tags patterns") — every push since v.25 produced 0-second "workflow file issue" failures. Replaced with the single pattern `v*madteevee.*` which absorbs both `-` and `+` separators (`*` matches any non-`/` char). Tag with whichever separator is convenient.

#### Automated release (preferred)

```bash
cd ~/_Claude_projects/PseudoTV_Live

# 1. bump version in plugin.video.pseudotv.live/addon.xml
#    (e.g. 0.7.3+madteevee.1 -> 0.7.3+madteevee.2). Use '+' separator
#    so Kodi treats it as newer than upstream's 0.7.3+nightly.
#    Bump resource.images.pseudotv.logos.madteevee/addon.xml in lockstep:
#    plugin .madteevee.N → logos 0.1.N (epoch-shifted from the master-era
#    0.0.N to clear Kodi's semver upgrade gate after the rebase reset).
#    (Optionally also bump repository.mmadalone.pseudotv/addon.xml when the
#    repo addon itself changes — rare.)

# 2. commit + push the version bump
git add plugin.video.pseudotv.live/addon.xml resource.images.pseudotv.logos.madteevee/addon.xml
git commit -m "bump: pseudotv 0.7.3+madteevee.2"
git push fork madteevee-patches

# 3. tag and push the tag — this is what triggers the release workflow
git tag -a v0.7.3-madteevee.2 -m "fork release v0.7.3+madteevee.2"
git push fork v0.7.3-madteevee.2
```

GitHub then runs the workflow: builds zips + `addons.xml`, force-replaces `gh-pages` with the new `_site/`, creates a Release in the Releases tab. Within ~5 min (raw.githubusercontent.com CDN cache), Kodi will see the new version on **Add-ons → Check for updates**.

The tag uses `-` instead of `+` because some git tooling chokes on `+` in tag names; the addon version itself stays `0.7.3+madteevee.2` per PEP-440 local-version semantics.

#### Imports releases

`plugin.video.pseudotv.imports` ships the same way, from `madteevee-on-nightly`, with its own tag form. The release page is titled "PseudoTV Live (imports)" and carries the imports zip plus the repository zip (release.yml's "Pick the released addon" step chooses the addon from the tag).

```bash
# after the imports.NN fix commit is pushed to fork/madteevee-on-nightly:

# 1. build locally first — the zip is made from the working tree, so check
#    that only the imports addon changes version and that every file in its
#    zip is git-tracked (nothing local or private slips in)
python3 release.py

# 2. tag and push the tag
git tag -a v0.8.0-imports.NN -m "fork release v0.8.0+imports.NN"
git push fork v0.8.0-imports.NN

# 3. after the workflow (~1 min): check both the Kodi repo and the release page
curl -s https://raw.githubusercontent.com/mmadalone/PseudoTV_Live/gh-pages/addons.xml | grep -o 'id="plugin.video.pseudotv.imports"[^>]*version="[^"]*"'
gh release view v0.8.0-imports.NN --repo mmadalone/PseudoTV_Live --json name,assets
```

A committed imports.NN is not released until it is tagged: .56 and .57 never reached the Kodi repo.

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
git tag -a v0.7.3-madteevee.2 -m "fork release v0.7.3+madteevee.2"
git push fork madteevee-patches v0.7.3-madteevee.2
```

### Mirroring more addons in the fork repo

`release.py`'s `ADDONS` list controls what gets packaged. Currently:
- `plugin.video.pseudotv.live` (the patched main addon)
- `repository.mmadalone.pseudotv` (the repo addon itself, so future repo updates also flow through GH Pages)

Resource packs (`resource.images.pseudotv.logos`, bumpers, ratings, etc.) keep coming from upstream's `repository.pseudotv` since they don't need fork-specific patches. If you ever want the fork to be fully self-contained, copy those addon dirs into this repo, add to `release.py`'s `ADDONS` list, rerun.

---

## Post-rebase fix series — v.12 → v.45 (2026-04-28 → 2026-04-30)

The initial rebase (`b6409b5` `bump: pseudotv 0.7.3+madteevee.1`) shipped with regressions in roughly five orthogonal areas: utility-menu wiring, channel-build resilience, rules system load/edit/dispatch, fast-zap player state, and the entire programme-thumbnail / channel-logo art pipeline. The series below resolved them; commits are intentionally one-fix-per-version so a future `git rebase upstream/nightly` localizes conflicts to the file that changed.

### Utility menu / launcher wiring

| ver | What broke | Fix |
|---|---|---|
| `madteevee.12` | `Utilities.buildMenu` referenced `_runCleanup`, `_runReload`, `_runRestart`, `_runFillers`, `_runLibrary` as bare names but they're `@staticmethod` on `Utilities` — the closure can't resolve them; menu NameErrors before opening | Refer through the class (`Utilities._runCleanup`); add missing `_runLibrary` body matching nightly's chkLibrary epoch-property contract |
| `madteevee.13` | `setPropertyBool` is a master-fork API not in nightly; was used to clear `chkLibrary` | Use `clrEXTProperty('chkLibrary')` |
| `madteevee.14` / `.15` / `.16` | Smartplay/Node/LiveTV launcher buttons in addon settings were missing `<control>` tag; clicks were no-ops | Restored buttons + close-all-modals before activate |
| `madteevee.17` | `Dialog.Close(addonsettings,true)` left modal stack non-empty; `ActivateWindow` got "refused because there are active modal dialogs" | `Dialog.Close(all,true)` + 300ms wait before activate |

### Channel-build resilience

| ver | What broke | Fix |
|---|---|---|
| `madteevee.18` | `chkChannels` queued `Builder.buildChannels` for every channel on every boot iteration even when nothing was stale; cycle-cost scaled with channel count | Pre-filter via `_filterChannelsNeedingBuild` reading XMLTV last_stop, skip queue when no channel needs build (CRC check deferred to inside builder for correctness) |
| `madteevee.24` | `__hasChanged` fires `__clrStation` BEFORE `buildVideo` runs — wipes the channel from in-memory M3U+XMLTV. If buildVideo returns False / [] / True (Enable_Extras=false skipping all-Specials sources, `_suspend` interrupt mid-build, smartplaylist returning nothing, plugin source timeout, etc.), the unconditional `__setStation` persists the cleared state to disk. Class-level shared M3U/XMLTV across Builder instances compounded it | `_snapshotChannel` captures pre-clear M3U+XMLTV; `_restoreChannel` re-appends if buildVideo doesn't produce new programmes; `_discardSnapshot` drops on success. Restore fires from every failure path |
| `madteevee.33` | Live PVR playback silently failed: `iptvsimple → plugin://...?vid={catchup-id}&start={utc}&...&stop={utcend}.pvr` reached `default.py` with literal catchup tokens (iptvsimple substitutes `{lutc}` for live but leaves `{catchup-id}/{utc}/{duration}/{utcend}` as-is). Nightly aborted with `setResolvedUrl(False)`; even if it hadn't, `plugin.py:33` does `int(sysInfo.get('start'))` and crashes on `'{utc}'` | Master fork pattern: detect templates, normalize to `0`/`''`, set `chkPVRRefresh`, proceed to play path |
| `madteevee.42` | Three Builder methods polled `service._suspend()` inside `while not abortRequested()` loops with no throttle: `buildFileList:498` was bare `_suspend()`; `buildChannels:326` and `buildVideo:433` passed `CPU_CYCLE` to `_suspend(...)` but `services.py:593`'s `_suspend(self, wait=0)` never acts on the parameter, so all three call sites had no throttle. With `pendingSuspend=True` (Kodi addon settings dialog open, or a transient `suspendActivity()` context manager around a concurrent plugin invocation), each loop spun at full CPU until the suspend trigger cleared. Hit twice during v.39/v.41 testing. **FORK_NOTES priority #1.** | `self.monitor.waitForAbort(CPU_CYCLE)` (~60Hz / 16ms throttle) inserted immediately before each `continue` in the three suspend branches — three one-line additions, no signature changes. Verified empirically on madteevee post-deploy: build thread (T:181236) averaged ~8.7% CPU during a 70s window of dialog-open suspend pressure, file-enumeration log lines (`CUtil::GetMatchingSource`) cleanly stopped at dialog-open and resumed at dialog-close, build completed normally. The unused `wait` parameter in `services.py:593._suspend` is left as-is — separate cleanup, would change nothing at runtime |
| `madteevee.43` | `Builder.channels` is a class-level shared `Channels(writable=True)` instance (`builder.py:55`) loaded once at class-definition time with no reload method. After every build, `self.channels.setChannels()` at `builder.py:389` wrote the entire cached `channelDATA['channels']` back to disk, including stale init-time state for channels NOT in the current build batch. Any operator disk-edits to untouched channels' fields (notably `"changed":true` flipped manually to force a rebuild) were silently overwritten. Most operator-visible symptom: flip a channel's `changed:true` on disk, restart Kodi quickly, lose the flag because some build path completes during the shutdown window and writes stale state. **FORK_NOTES priority #2.** | `Channels.setChannels(modified_ids=set())` merge-on-write: when called with `modified_ids`, re-reads disk before writing and writes only the channels in the set from in-memory state (build is authoritative for them); all other channels are preserved verbatim from disk. `Builder.buildChannels` tracks `modified_ids` inside the `__hasChanged` closure (only IDs that `addChannel` actually mutated) and passes the set at the line 389 call. Manager and HTTP API callers don't pass `modified_ids` — they keep full-state-write semantics, which is correct because they construct their own `Channels(writable=True)` from a fresh disk load. Verified empirically on madteevee with the option (c) test: flipped channel A `changed:true`, started Kodi, then flipped channel B `changed:true` mid-build of A. Post-build disk state showed A=`changed:false` (built and reset), B=`changed:true` (preserved by merge — pre-v.43 would have been clobbered) |
| `madteevee.44` | `chkChanged` at `tasks.py:271` instantiates a fresh `Builder(service=self.service)` per channel queued; the bound `buildChannels` method on each new instance is a distinct Python object, so `cqueue._exists` package-equality dedup (`cqueue.py:126`) doesn't recognize redundant `buildChannels` tasks queued by successive `chkChanged` invocations. **Mostly self-mitigated** in practice: existing dedup of `chkChanged` tasks themselves at the same priority + FIFO ordering at priority 3 means by the time a second `chkChanged` runs, the first batch's builds have drained and disk is post-build, so zero redundant builds queue. **Residual edge case**: if timing aligns adversely (e.g., a higher-priority task preempts FIFO), a redundant build could fire. v.43's `setChannels(modified_ids=...)` merge-on-write already neutralizes the worst-case data-corruption fallout; what remained was a CPU/IO concern. **FORK_NOTES priority #3 (rated small effort, performance not correctness).** | `_chkPropTimer` (`tasks.py:156`) gains a `chkChanged`-specific guard: if `key == 'chkChanged' and PROPERTIES.isRunning('Builder.buildChannels')`, return early without clearing the property and without queueing. Property stays True so the next service tick after the build completes retries cleanly. Reuses the existing `PROPERTIES.isRunning` primitive (`kodi.py:779`) that `Builder` already wires via `chkRunning` at `builder.py:291`. ~7-line addition in one method, no new state, no new imports. Note: FORK_NOTES claim that "master fork had a debounce" was inaccurate — `master` has no `chkChanged` at all and `upstream/nightly`'s code is identical to ours, no prior-art to forward-port |
| `madteevee.45` | `Builder.m3u` and `Builder.xmltv` (`builder.py:53-54`) were class-level shared `M3U(writable=True)` / `XMLTVS(writable=True, m3u=m3u)` instances loaded once at class-definition time. After every build, `__setStation` called `self.m3u._save()` / `self.xmltv._save()` — and because both classes' `_load()` is only invoked from `__init__` (with no working `_reload` — both `_reload` methods at `m3u.py:258` and `xmltvs.py:98` call bare `__init__()` and have zero callers), any post-import disk modifications by external writers were clobbered. **Concrete bug class:** `context_record.py:51,69` (separate Kodi process triggered via right-click context menu in `tvguide` / `tvsearch` / `tvchannels`) constructs fresh `M3U(writable=True)` / `XMLTVS(writable=True)` and writes recordings to `pseudotv.m3u` / `pseudotv.xml`. The next `Builder.buildChannels._save()` then overwrites with the build-side stale class-level state, losing the recording. **Latent shutdown footgun on the same class-level instances:** their `__del__` (`m3u.py:75-79`, `xmltvs.py:40-44`) calls `_save()` if `self.writable` — at process exit / module teardown / addon disable, this could write addon-startup state to disk and clobber every build of the session (CPython doesn't guarantee `__del__` at exit, but the risk parallels the v.43 stale-state class). **FORK_NOTES priority #4 (scoped down from "large refactor" framing).** | Two coordinated edits in `builder.py`. **Edit 1:** at the top of `buildChannels` inside `chkRunning`, gated on `not preview`, construct fresh `M3U(writable=True)` and `XMLTVS(writable=True, m3u=self.m3u)` and assign to `self.m3u` / `self.xmltv` as instance attrs shadowing the class-level. Builds are serial (cqueue's single-threaded `popThread`; `_exe` at `cqueue.py:120` calls `func` synchronously even with `useExecutor=True` due to the `executor.submit(func(*args, **kwargs))` eager-evaluation pattern), so each fresh load reflects the prior build's `_save`. Preview path skips the fresh load — preview is a non-persistent dry run, and constructing fresh `writable=True` instances during preview would let `M3U.__del__` persist any mid-preview `__clrStation` mutations (m3u.findStation matches by id OR url at `m3u.py:387-390`) to disk on Builder GC. **Edit 2:** change the class-level `Builder.m3u` / `Builder.xmltv` declarations from `writable=True` to `writable=False`. Their `__del__` becomes a no-op, so process exit / module teardown / addon disable can't persist their frozen-at-import-time state to disk. They remain as defensive fallbacks (verified zero external accessors of `Builder.m3u` / `Builder.xmltv` across the codebase). **Acknowledged scope limit:** concurrent `context_record.add` *during* an in-flight build is not fully closed — the failure window narrows from "any time between addon startup and now" (pre-v.45) to "between this build's start and end" (post-v.45). Full fix would require merge-on-write (v.43-style) for M3U/XMLTV, deferred. v.24 snapshot/restore at `builder.py:138-182` stays as the failure-path workaround |

### Rules system

| ver | What broke | Fix |
|---|---|---|
| `madteevee.19` | `PadScheduling` rule had no operator-configurable target; always padded to `MIN_EPG_DURATION` whether or not that was wanted | Added a target-hours selector option |
| `madteevee.20` | `Channel Manager → channel → Rules → Add` raised TypeError because `buildMenuListItem` signature changed in nightly (positional vs keyword) | Pass keyword args |
| `madteevee.21` | Rule selector with dict-options threw `KeyError` — code did `dict[int_index]` but options is a dict not a list | Use local list of keys (`options[select]`) |
| `madteevee.22` | Persisted rules silently no-op'd: rule keys in channels.json are JSON strings (`"1000"`), but `rule.myId` is int. `loadRules` / `runActions` used the str key against the int dict-key, the elif branch fell through. **Symptom: rules saved fine but never fired** | Normalize keys to int in `loadRules`; runActions prefers fresh citem keys over stale snapshot |
| `madteevee.23` | Channel Manager → Rules editor: applied rules couldn't be edited or deleted again — same str-vs-int mismatch on the click handler's `ruleLST.get(str(myId))` after v.22 normalized to int | Use int `myId` consistently on edit/delete |

### Fast-zap player state

| ver | What broke | Fix |
|---|---|---|
| `madteevee.26` | Stuck wrong-channel logo after rapid CH+/CH-: `_onPlay` (decorated `@threadit`) closes overlay then later updates `self.playingItem`; `__chkOverlay` poll between those steps captures stale citem; subsequent toggleOverlay calls saw `overlay != None` and no-op'd → wrong logo persisted indefinitely | Added `Player._overlay_chid` tracking; toggleOverlay closes+rebuilds when chid mismatches current playingItem.citem.id (idempotent when chid unchanged so programme transitions don't flicker) |

### Channel logo / programme thumbnail art (the long arc)

This was the dominant problem area — UC Remote 3 channel logos broke after the rebase, then Kodi's native PVR EPG view also showed channel logo where per-show thumbnail belongs. The path through v.26-v.40 was a sequence of partial diagnoses; the canonical fix is **v.41 only**, but the intermediate versions stay in the commit log for context.

| ver | What it tried | Status |
|---|---|---|
| `madteevee.26-v.32` | Series of `_buildWebImage` reshapings: `resource://` → http, `special://` → http, image:// idempotency, HTTP/1.1 server, host-empty guards | Each fixed a real edge case but none addressed the actual UC3-display root cause. Idempotency guard (v.31, `if 'http' in image or '/image/' in image: return image`) is independently correct, retained |
| `madteevee.33` | (above — playback fix, unrelated to art) | Independent, retained |
| `madteevee.34` | Empty-host write guards (`if not remote_host: return image`) so build cycles before service publishes Local_Host/Remote_Host don't bake `http:///images/...` URLs | Independently correct, retained |
| `madteevee.35` | Restored master fork's narrow `_buildWebImage` scope (drop resource:// and special:// branches that v.26+ piled on) | Retained |
| `madteevee.36` | `_getThumb` early-return on first matching art key + drop the `_buildWebImage` wrap; nightly's `for: art = ... ; return _buildWebImage(art)` always returned the LAST iteration's value (typically None → fallback) instead of FIRST match | Real bug fix, retained |
| `madteevee.37 / .38 / .39` | UC3 channel-logo display experiments: force channel logo as programme `<icon>`; emit `/image/<encoded image://>` host-less proxy form to make UC3's `thumbnail_url` reject and fall back to icon | All reverted in `madteevee.39`; operator handled UC3 channel-logo display in the albaintor integration layer instead |
| `madteevee.40` | Wrap `image://` programme icons via Kodi's `/image/` proxy URL using `Local_Host` (`http://<kodi-user>:<kodi-pass>@<host>:8080/image/<encoded>`) | Hit a Window-property race: `Local_Host` reads back empty during build calls (T:builder-thread) despite being set briefly seconds earlier (T:service-thread). Verified via `_getProperty` log lines |
| **`madteevee.41`** | **Canonical fix.** Wrap `image://` programme icons via pseudotv's own HTTP server (`Remote_Host`, port 50001, no auth, `/images/<abspath>` handler served by `FileAccess`). Decode the encoded path inside `image://<urlencoded>/`, re-encode per URL segment, emit `http://<remote_host>/images<abspath>`. iptvsimple stores the URL verbatim; Kodi's EPG renderer fetches via the addon's own server, which round-trips to the file. | Verified end-to-end: `Globals._toEpgIconURL` in `variables.py`, called from `xmltvs.addProgram`. After v.41 build + iptvsimple re-import, `Epg16.db.epgtags.sIconPath` holds the http URL form. Kodi EPG renders landscape art same as for any other PVR client. |

### CI / release pipeline

| ver | What broke | Fix |
|---|---|---|
| `madteevee.25` | `release.yml` only matched `v*-madteevee.*` (legacy dash); plus-form tags didn't trigger | Added `v*+madteevee.*` to triggers — but GitHub's workflow parser rejected `+` as a literal in glob patterns (HTTP 422), which created a separate breakage in v.25 itself |
| (post-v.41 ci-only commit) | Every push since v.25 produced 0-second "workflow file issue" failures because of the rejected pattern | Replaced the two-pattern union with single broader `v*madteevee.*` (the `*` matches `/` exclusion only, so it absorbs both `-` and `+` separators). Verified: `gh workflow run --ref madteevee-on-nightly` runs cleanly in 41s, publishes `_site/` to `gh-pages`. |
| imports.55 → .58 (fixed in `f190ac47`, 2026-10-22) | "Create GitHub Release" always attached the Live zip and used a "PseudoTV Live (madteevee)" title, so the `v*imports.*` release pages offered `plugin.video.pseudotv.live-0.7.3+madteevee.46.zip` instead of the imports addon. gh-pages (what Kodi installs from) was correct throughout | New "Pick the released addon" step maps the tag to the addon (`*imports.*` → imports, else Live) and sets the title; the tag reaches the shell through `env`; `fail_on_unmatched_files: true`. The .55 and .58 pages were repaired by hand (`gh release upload` of the zip gh-pages published — .55's from gh-pages history — then `delete-asset` the Live zip and `edit --title`); the downloads were checked byte-identical to gh-pages. |

## Pending items / future forward-ports

These are real issues observed during the v.12-v.41 work that landed as workarounds, deferred fixes, or remain in upstream/nightly to watch.

### Priority order for next session

When picking up work in a new session, this is the recommended order. ROI = effort × likelihood of hitting the bug × severity when hit.

| Priority | Item | Effort | Why this order |
|---|---|---|---|
| ~~**1**~~ ✓ v.42 | ~~`buildFileList` cpu-cycle sleep in `_suspend` branch~~ — **done**. Extended to `buildChannels:326` and `buildVideo:433` since `services.py:593`'s `_suspend(self, wait=0)` doesn't act on the wait parameter, so those sibling `_suspend(CPU_CYCLE)` calls had the same effective bug | 3 × one-line addition | ✓ Resolved in v.42 |
| ~~**2**~~ ✓ v.43 | ~~`channels.json` writeback on shutdown clobbers `changed:true`~~ — **done**. Root cause was broader than the "shutdown" framing: `Builder.channels` is a class-level shared instance loaded once; `setChannels()` writes the entire cached state back, clobbering any disk edits to channels not in the current build batch. Fix: `Channels.setChannels(modified_ids=...)` merge-on-write; Builder passes the set of IDs `__hasChanged` actually mutated | medium | ✓ Resolved in v.43 |
| ~~**3**~~ ✓ v.44 | ~~`chkChanged` debounce~~ — **done** (defensive). Investigation showed FORK_NOTES framing was misleading: manager save fires `setPropTimer` once (not in a loop), and existing `cqueue._exists` dedup + FIFO ordering at priority 3 already prevent the bug from manifesting in normal operation. The remaining residual edge case (fresh `Builder()` per chkChanged → distinct bound methods → `_exists` package-equality misses redundant builds) is closed by deferring `chkChanged` in `_chkPropTimer` while a build is running | small | ✓ Resolved in v.44 |
| ~~**4**~~ ✓ v.45 | ~~Per-instance vs class-level `M3UDATA`/`XMLTVDATA`~~ — **scoped fix landed**. Reframed during investigation: the original "large refactor" framing assumed full per-`Builder()`-instance state with merge-on-write semantics; the actual concrete bug was narrower (external writers like `context_record.py:51,69` writing to disk between builds, then clobbered by the next build's `_save` of stale class-level state). Fix shipped as per-build fresh writable M3U/XMLTV instances at the top of `buildChannels` (gated on `not preview`) plus class-level `writable=False` on the fallbacks to neutralize shutdown `__del__` stale-write risk. v.24 snapshot/restore stays. Acknowledged scope limit: concurrent `context_record.add` *during* an in-flight build still vulnerable; full merge-on-write deferred indefinitely | small (after rescoping) | ✓ Resolved in v.45 |
| 5 | Periodic rebase against `upstream/nightly` | varies | Pull upstream improvements. Each fork commit is one fix per file so conflicts localize. Do roughly every 2-4 weeks of upstream activity |

The 5 upstream/nightly bugs in "Things upstream/nightly should fix" below stay fork-local per `project_fork_only_no_upstream` — only revisit if the no-PR rule changes.



### Build-flow `_suspend` self-deadlock (fixed in v.42)

**Symptom:** Builder.buildFiles can enter a tight `_suspend` loop (`while not abortRequested: ... elif self.service._suspend(): continue`). With no sleep in the polling loop, `pendingSuspend=True` from any concurrent activity (Kodi addonsettings dialog open, busy_dialog from a chkOverlay tick, etc.) traps the builder. We hit this twice during v.39/v.41 testing — once because the user had pseudotv's addon settings dialog open during a chkChanged-triggered build, once with a transient ~150ms suspend cycle from another thread that the builder happened to sample at the wrong instant.

**Workaround (no longer needed post-v.42):** trigger rebuilds with no Kodi UI dialogs open. Restart Kodi if a build appears stuck (`buildFiles, _suspend` repeating in the log without progress). The build is queued into `tasks.chkChanged` which fires periodically; flipping `channels.json` `changed:false → changed:true` forces it to pick the channel up next cycle.

**Resolution (v.42):** added `self.monitor.waitForAbort(CPU_CYCLE)` immediately before `continue` in the `_suspend` poll branches of `buildFileList:498`, `buildChannels:326`, and `buildVideo:433`. The latter two pass `CPU_CYCLE` to `_suspend(...)` but `services.py:593`'s `_suspend(self, wait=0)` doesn't act on the wait parameter, so those call sites had no throttle either. Three one-line additions, no signature changes. The unused `wait` parameter in `services.py:593._suspend` is left as-is — separate cleanup, would change nothing at runtime.

### `channels.json` writeback clobbers operator-flipped `changed:true` (fixed in v.43)

**Symptom:** flipping `channels.json` to `changed:true` and immediately restarting Kodi loses the flag — pseudotv writes channels.json on shutdown, overwriting the manual edit.

**Workaround (no longer needed post-v.43):** flip the flag AFTER Kodi finishes booting (when the chkChanged poll cycle is running), not before. Or trigger rebuild via Utility menu → Rebuild M3U/XMLTV (which deletes the M3U/XMLTV files, forcing a rebuild without the changed flag).

**Root cause (clarified during v.43 work; broader than the "shutdown" framing above):** `Builder.channels` is a class-level shared `Channels(writable=True)` instance (`builder.py:55`) constructed once at class-definition time. `Channels.__init__` (`channels.py:28-42`) calls `_load()` exactly once and caches the result with no reload method. During a build, `addChannel()` refreshes only the channels in the current build batch (`builder.py:255` inside `__hasChanged`); all other channels carry stale init-time state. The post-build `self.channels.setChannels()` at `builder.py:389` writes the entire cached `channelDATA['channels']` back to disk, overwriting any disk edits to untouched channels. The shutdown symptom is the most operator-visible manifestation but the bug fires on any build that runs after an operator disk-edit, not just at shutdown.

**Resolution (v.43):** `Channels.setChannels` accepts an optional `modified_ids=set()` parameter. When provided, it re-reads disk before writing and merges: channels in `modified_ids` are written from in-memory state (build is authoritative); all other channels are preserved verbatim from disk. `Builder.buildChannels` tracks `modified_ids` inside the `__hasChanged` closure (only IDs that `addChannel` actually mutated) and passes the set at the line 389 call. Other callers (`Manager.saveChanges`, `server.py` HTTP API) construct their own `Channels(writable=True)` from a fresh load and don't pass `modified_ids` — their full-state writes are unaffected.

### Iptvsimple's `<programme><icon>` URL acceptance

**Confirmed empirically:** iptvsimple accepts http/https URLs verbatim in programme `<icon>` and stores them in EPG DB. It rejects (or transforms to channel logo path as fallback) certain other schemes — at minimum it choked on raw `image://%2fmnt%2f...%2flandscape.jpg/` in some build cycles during this session, which is why v.41 wraps to http://. Whether it consistently rejects `image://` or only under specific conditions wasn't fully nailed down. The v.41 http-wrap is a defensive form that always works.

If a future iptvsimple release accepts image:// directly (or upstream pseudotv emits something different), revisit `_toEpgIconURL` — the wrap is a no-op penalty for art that didn't need wrapping anyway, but if the round-trip via the addon HTTP server becomes a perf concern at high programme counts, switching back to raw image:// would skip the network hop.

### Forward-ports not done

These were master-era fork patches that nightly may or may not have merged differently. Worth a re-check on the next `git rebase upstream/nightly`:

- ~~**`buildFileList` cpu-cycle sleep in suspend branch**~~ — ✓ Done in v.42. Extended to `buildChannels:326` and `buildVideo:433`. See "Build-flow `_suspend` self-deadlock" above.
- **chkChanged debounce** — currently chkChanged fires on every property poll cycle when the property is set; if the property gets set rapidly during channel-manager save it can queue multiple builds. Master fork had a debounce.
- ~~**Class-level vs instance-level M3UDATA / XMLTVDATA**~~ — ✓ Scoped fix done in v.45. `buildChannels` constructs fresh writable `M3U` / `XMLTVS` instances per build (gated on `not preview`) and the class-level fallbacks are now `writable=False` to neutralize shutdown `__del__` stale-writes. v.24 snapshot/restore stays for the `__clrStation`-before-`buildVideo` failure path. Full per-instance + merge-on-write is deferred indefinitely; the remaining gap (concurrent `context_record.add` *during* an in-flight build) is narrow.

### Things upstream/nightly should fix (we work around but they're not fork-specific)

These are upstream bugs we currently mask. Worth filing as upstream issues if pseudotv accepts contributions in the future (currently this fork is one-way pulls only per `feedback_public_facing_thank_upstream`, but a careful PR with no fork-language could be welcome).

- `_getThumb` returning the LAST iteration's match instead of FIRST (v.36)
- `default.py` aborting on catchup-token URLs in mode=live (v.33)
- `_buildWebImage` wrapping http URLs that already point to our own server (v.28 idempotency guard)
- `_suspend` poll loop without sleep (deadlock potential — see above)
- `Loadrules` str-vs-int key mismatch (v.22)

## Operational gotchas (carry-overs)

These were already in CLAUDE.md but worth restating in fork-notes for future maintainers:

- **Editing `~/.kodi/userdata/addon_data/plugin.video.pseudotv.live/settings.xml` while the addon is enabled** gets clobbered. Right order: disable → wait 5s+ → edit → enable. UI is more reliable for one-off changes.
- **`Addons.SetAddonEnabled` on `plugin.video.pseudotv.live` right after a Kodi auto-update** races and leaves the addon stuck `enabled=false`. Symptom: log fills with `Unable to find plugin plugin.video.pseudotv.live`. Fix: a single `SetAddonEnabled enabled:true` call. Going forward, prefer to bounce a different addon (e.g., `pvr.iptvsimple`) when forcing a settings/repo refresh.
- **Don't edit `~/.kodi/userdata/Database/Epg*.db` as part of any fix.** Inspect-only via sqlite3. Real fixes have to work on a clean install where the operator doesn't hand-edit Kodi's databases. Captured as memory: `feedback_no_epg_db_edits.md`.
- **Always ask before `sudo systemctl restart kodi`.** Plan-level approval doesn't extend to specific restart timing. Memory: `feedback_kodi_restart.md`.
- **The brief wrong-channel-logo flash on the first-loaded channel of a fast zap is unavoidable.** That channel really did start playing for a moment — its `onAVStarted` is genuine, not stale, so it passes the `_tune_ts` filter and the overlay opens with its logo. The latest user-intended channel's overlay only rebuilds once *its* `onAVStarted` fires. Documented in commit `9be4969`'s message.
