# -*- coding: utf-8 -*-
"""imports.69: commitSync for the syncAll tests' in-memory channel stores.

madteevee (imports fork, imports.69). Imports.syncAll saves a sync's results
with one call, self.channels.commitSync(...), and has no fallback branch: one
production persistence path. The real Channels.commitSync runs
importcfg_helpers.plan_sync_commit on a strict fresh read of channels.json,
merges the channel records and writes once under the channels.json lock.

The syncAll tests' fake stores (getImports / getChannels / setChannels /
setImports on lists in memory) get commitSync from here, with one line in
each fake:

    commitSync = fake_commit_sync.commit_sync

commit_sync runs the SAME planner with the same arguments as
Channels.commitSync (test_imports69_sync_commit.py checks both calls) on
what the fake holds now, then makes the fake's own two store calls:
setChannels with the planner's writer list and its modified and deleted ids,
and setImports with the planner's import list. Only those two calls differ
from production; the real commit is tested against the real Channels
(test_imports69_channels_commit.py, test_imports69_sync_commit.py).

Not collected by pytest (no test_ prefix). Synthetic data only.
"""
import inspect

import importcfg_helpers


def planner_arguments(store, start_by_id, results, channels, modified_ids, deleted_ids):
    """The positional arguments commit_sync passes to plan_sync_commit:
    the fake's import list and the str ids of its channel records (as
    Channels.commitSync takes them from its fresh read), then the sync's
    own values unchanged."""
    fresh_imports = list(store.getImports() or [])
    fresh_ids = {c.get('id') for c in (store.getChannels() or [])
                 if isinstance(c, dict) and isinstance(c.get('id'), str)}
    return (fresh_imports, fresh_ids, start_by_id, results, channels, modified_ids, deleted_ids)


def commit_sync(store, start_by_id, results, channels, modified_ids, deleted_ids):
    """Channels.commitSync for a fake store. Returns the planner's info plus
    'status': 'written', or 'failed' when a store call returned False."""
    new_imports, writer, mods, dels, info = importcfg_helpers.plan_sync_commit(
        *planner_arguments(store, start_by_id, results, channels, modified_ids, deleted_ids))
    if 'deleted_ids' in inspect.signature(store.setChannels).parameters:
        saved_channels = store.setChannels(channels=writer, modified_ids=mods, deleted_ids=dels)
    else:
        # test_imports_module.py's store predates the deleted_ids argument
        # (imports.22), as syncAll's TypeError ladder allowed before imports.69.
        saved_channels = store.setChannels(channels=writer, modified_ids=mods)
    saved_imports = store.setImports(new_imports)
    info['status'] = 'written' if saved_channels is not False and saved_imports is not False else 'failed'
    return info
