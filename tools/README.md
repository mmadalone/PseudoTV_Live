# tools/

Repository-internal diagnostic / verification scripts. Not part of the
installed addon — read-only utilities run from the host shell, used during
investigation cycles to inspect data the addon writes (`pseudotv.xml`,
`channels.json`, `cache.db`).

| Script | Purpose |
|---|---|
| `sched_audit.py` | Parses `pseudotv.xml`, decodes each programme's pickled `fitem` (zlib+base64+pickle in the `[COLOR item="…"]` tag of `<desc>`), and reports the loop-risk class: programmes whose EPG `<length>` (slot) exceeds the library's recorded video duration (`streamdetails.video[0].duration` / `runtime`). Originated during the 2026-05-22 investigation of *Five Fingers of Death*'s 25-min dead zones; cited in the imports.48 changelog as the verification step (`Re-run the audit → loop-risk count should drop from 61 toward 0`). |
| `wt_ffprobe.py` | Spot-checks a sample of *Weird Tales* episodes by running ffprobe on the actual files and comparing the result to the library's recorded duration. Used to confirm whether a slot/file mismatch is a Builder-side scheduling bug (slot > library) or a metadata-staleness bug (library > real file). |

Both scripts are read-only and idempotent — running them twice is fine.

## Running

```sh
python3 tools/sched_audit.py
python3 tools/wt_ffprobe.py     # requires ffprobe on PATH
```

Paths to `pseudotv.xml` and userdata are hard-coded to the development host
(`/home/madalone/.kodi/userdata/addon_data/plugin.video.pseudotv.imports/cache/…`);
adjust the constants near the top of each script if you run them elsewhere.
