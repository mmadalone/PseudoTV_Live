# mmadalone/PseudoTV_Live — fork notes

Personal fork of [PseudoTV/PseudoTV_Live](https://github.com/PseudoTV/PseudoTV_Live), rebased onto `upstream/nightly` (PseudoTV `0.7.3+nightly`) as of 2026-04-28. Maintained for the **madteevee** Kodi install (RPi5 + HDD-backed media libraries). The previous master-based branch (`0.6.1q+madteevee.14`) is preserved at tag `pre-nightly-rebase` and on archive branch `madteevee-patches-0.6.1q-archive` (after burn-in switch).

## Branch layout

| Branch | Purpose |
|---|---|
| `madteevee-patches` | (current default) Patches on top of `upstream/nightly`. Each fix is its own commit so future cherry-picks against newer nightly tips stay surgical. |
| `madteevee-patches-0.6.1q-archive` | Snapshot of the previous master-based fork, kept for reference / rollback. Tag `pre-nightly-rebase` points at the same tip. |

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

The release pipeline is automated via `.github/workflows/release.yml`. Pushing a tag matching `v*madteevee.*` triggers a workflow that runs `release.py`, publishes `_site/` to the `gh-pages` branch, and creates a GitHub Release object. Workflow can also be triggered manually from the Actions tab (`workflow_dispatch`) for republishing without a tag.

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

## Post-rebase fix series — v.12 → v.41 (2026-04-28 → 2026-04-29)

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

## Pending items / future forward-ports

These are real issues observed during the v.12-v.41 work that landed as workarounds, deferred fixes, or remain in upstream/nightly to watch.

### Priority order for next session

When picking up work in a new session, this is the recommended order. ROI = effort × likelihood of hitting the bug × severity when hit.

| Priority | Item | Effort | Why this order |
|---|---|---|---|
| **1 (do first)** | `buildFileList` cpu-cycle sleep in `_suspend` branch | one-line addition (`MONITOR().waitForAbort(CPU_CYCLE)` in the suspend branch of `builder.py:494-501`) | Real deadlock we hit twice this session. Fix is trivial. Eliminates the "don't open Kodi UI during a build" operator footgun |
| 2 | `channels.json` writeback on shutdown clobbers `changed:true` | medium | Operator gotcha but workaround (don't restart between flip and build) is fine. Real fix needs a "skip channels.json save during shutdown" guard or a separate force-rebuild signal |
| 3 | `chkChanged` debounce | small | Performance, not correctness. Multiple builds queue when channel manager save fires the property repeatedly |
| 4 | Per-instance vs class-level `M3UDATA`/`XMLTVDATA` | large refactor | v.24 snapshot/restore is the workaround. Cleaner fix is per-Builder-instance state. Defer until/unless the workaround starts breaking |
| 5 | Periodic rebase against `upstream/nightly` | varies | Pull upstream improvements. Each fork commit is one fix per file so conflicts localize. Do roughly every 2-4 weeks of upstream activity |

The 5 upstream/nightly bugs in "Things upstream/nightly should fix" below stay fork-local per `project_fork_only_no_upstream` — only revisit if the no-PR rule changes.



### Build-flow `_suspend` self-deadlock (workaround in place, not fixed)

**Symptom:** Builder.buildFiles can enter a tight `_suspend` loop (`while not abortRequested: ... elif self.service._suspend(): continue`). With no sleep in the polling loop, `pendingSuspend=True` from any concurrent activity (Kodi addonsettings dialog open, busy_dialog from a chkOverlay tick, etc.) traps the builder. We hit this twice during v.39/v.41 testing — once because the user had pseudotv's addon settings dialog open during a chkChanged-triggered build, once with a transient ~150ms suspend cycle from another thread that the builder happened to sample at the wrong instant.

**Workaround:** trigger rebuilds with no Kodi UI dialogs open. Restart Kodi if a build appears stuck (`buildFiles, _suspend` repeating in the log without progress). The build is queued into `tasks.chkChanged` which fires periodically; flipping `channels.json` `changed:false → changed:true` forces it to pick the channel up next cycle.

**Real fix (not done):** add a sleep in the `_suspend` poll branch (master fork's `buildFileList` had `MONITOR().waitForAbort(CPU_CYCLE)` here; nightly drops it). Or change `_suspend()` to return False after N consecutive checks against the same `pendingSuspend=True`. Either localizes to `builder.py` and is one of those fork-only changes that won't conflict with upstream merges.

### `channels.json` writeback on shutdown clobbers `changed:true`

**Symptom:** flipping `channels.json` to `changed:true` and immediately restarting Kodi loses the flag — pseudotv writes channels.json on shutdown, overwriting the manual edit.

**Workaround:** flip the flag AFTER Kodi finishes booting (when the chkChanged poll cycle is running), not before. Or trigger rebuild via Utility menu → Rebuild M3U/XMLTV (which deletes the M3U/XMLTV files, forcing a rebuild without the changed flag).

### Iptvsimple's `<programme><icon>` URL acceptance

**Confirmed empirically:** iptvsimple accepts http/https URLs verbatim in programme `<icon>` and stores them in EPG DB. It rejects (or transforms to channel logo path as fallback) certain other schemes — at minimum it choked on raw `image://%2fmnt%2f...%2flandscape.jpg/` in some build cycles during this session, which is why v.41 wraps to http://. Whether it consistently rejects `image://` or only under specific conditions wasn't fully nailed down. The v.41 http-wrap is a defensive form that always works.

If a future iptvsimple release accepts image:// directly (or upstream pseudotv emits something different), revisit `_toEpgIconURL` — the wrap is a no-op penalty for art that didn't need wrapping anyway, but if the round-trip via the addon HTTP server becomes a perf concern at high programme counts, switching back to raw image:// would skip the network hop.

### Forward-ports not done

These were master-era fork patches that nightly may or may not have merged differently. Worth a re-check on the next `git rebase upstream/nightly`:

- **`buildFileList` cpu-cycle sleep in suspend branch** — see "self-deadlock" above. Master had `MONITOR().waitForAbort(CPU_CYCLE)`. Nightly removed it.
- **chkChanged debounce** — currently chkChanged fires on every property poll cycle when the property is set; if the property gets set rapidly during channel-manager save it can queue multiple builds. Master fork had a debounce.
- **Class-level vs instance-level M3UDATA / XMLTVDATA** — the v.24 snapshot/restore mechanism is a workaround for the underlying issue that Builder shares mutable M3U/XMLTV state across instances at the class level. Cleaner fix is per-instance state. Larger change, deferred.

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
