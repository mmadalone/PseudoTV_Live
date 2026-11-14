#   Copyright (C) 2025 Lunatixz
#
# This file is part of PseudoTV Live.
#
# PseudoTV Live is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# PseudoTV Live is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with PseudoTV Live.  If not, see <http://www.gnu.org/licenses/>.

# -*- coding: utf-8 -*-

# madteevee (imports fork, imports.69): who may write each field of an import
# entry (the "imports" list in channels.json), and the merges that apply a
# dashboard save and a sync's results onto the entry as it is on disk.
#
# Import-light pure module (same pattern as replay_helpers.py /
# picture_helpers.py): standard library only (copy, hashlib, json, re), no
# Kodi imports, no I/O, Python 3.8 syntax and no '|' operator at all (a dict
# '|' needs Python 3.9; sets use .union() / .difference()). channels.py,
# imports.py and server.py share one definition, and the tests run it
# without Kodi stubs.
#
# Background (the "never synced" bug): before imports.69 a dashboard Save
# posted the whole import list back and the add-on stored it as sent, so a
# Save wiped every import's last-sync time, its error and retry state and its
# deleted-channel list, and a sync that was running wrote back the list it had
# read before the Save, undoing it.
#
# One owner per field (the Kubernetes spec/status split; Google AIP-203:
# output-only fields sent by a client are ignored, not an error):
#   DASHBOARD_FIELDS  your settings, written only by a dashboard save
#                     (FORM_FIELDS from the Edit-import window, plus the three
#                     hand-set catchup_* fields, which have no form box)
#   SYNC_FIELDS       sync results, written only by the sync, as a change list
#                     (sync_delta) applied onto the entry as it is now on disk
#   ACTION_FIELDS     tombstones (deleted channels): Orphans adds ids and
#                     Re-discover removes the ids it re-created; nothing else
#   DERIVED_FIELDS    computed for the GET reply only, never stored
#
# config_key follows Kubernetes metadata.generation /
# status.observedGeneration: a dashboard change stores the marker 'pending';
# each sync attempt stores the fingerprint of the settings it acted on, so a
# settings change made while a sync runs still forces the next one.
import copy
import hashlib
import json
import re


class _Missing(object):
    """The MISSING marker: "this key is absent", distinct from None (a key
    stored as null). It is its own copy, so a copied delta still compares
    with `is MISSING`."""
    __slots__ = ()

    def __repr__(self):
        return 'MISSING'

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self


MISSING = _Missing()

# ---------------------------------------------------------------- tables

# The dashboard's own check (manager.html gatherImportForm). is_valid_id uses
# fullmatch: Python's '$' also matches before a trailing newline, the
# JavaScript one doesn't.
ID_RE = re.compile(r'^[a-z0-9_-]+$')

FORM_FIELDS = ('id', 'name', 'm3u_url', 'm3u_path', 'epg_url', 'epg_path', 'start_num',
               'refresh_interval_min', 'respect_source_numbers', 'rediscover_deleted', 'enabled',
               'detect_placeholder_pictures')
CATCHUP_FIELDS = ('catchup_mode', 'catchup_query_format', 'catchup_days')    # hand-set, no form box
DASHBOARD_FIELDS = frozenset(FORM_FIELDS + CATCHUP_FIELDS)
SYNC_FIELDS = frozenset(('last_sync_at', 'last_status', 'last_error', 'etag', 'last_modified', 'playlist_rev',
                         'links_mtime_ns', 'fail_count', 'failed_since', 'epg_etag', 'epg_last_modified',
                         'epg_fail_count', 'm3u_source_key', 'epg_source_key', 'config_key'))
ACTION_FIELDS = frozenset(('tombstones',))
DERIVED_FIELDS = frozenset(('next_retry_at', 'display_status'))

BOOL_FIELDS = ('respect_source_numbers', 'rediscover_deleted', 'enabled', 'detect_placeholder_pictures')
ADDRESS_FIELDS = ('m3u_url', 'm3u_path', 'epg_url', 'epg_path')

# What a Save drops when that address changes. Never last_sync_at (so "never
# synced" can't come back), playlist_rev (a record-schema marker), the
# *_source_key (they tell the next sync that the address changed) or
# tombstones.
PLAYLIST_RESET = ('etag', 'last_modified', 'links_mtime_ns', 'fail_count', 'failed_since', 'last_status',
                  'last_error')
GUIDE_RESET = ('epg_etag', 'epg_last_modified', 'epg_fail_count', 'last_error')
# What a sync's results may not write when that address changed while it ran:
# they describe the old address.
PLAYLIST_GROUP = ('etag', 'last_modified', 'playlist_rev', 'links_mtime_ns', 'm3u_source_key',
                  'fail_count', 'failed_since', 'last_status', 'last_error')
GUIDE_GROUP = ('epg_etag', 'epg_last_modified', 'epg_fail_count', 'epg_source_key', 'last_error')

CONFIG_KEY_PENDING = 'pending'
START_NUM_RANGE = (1, 9999)
REFRESH_RANGE = (0, 1440)        # 0 = no per-import gate, as syncAll reads it
CATCHUP_DAYS_RANGE = (0, 365)

# syncAll's results that never reached syncOne: copies of the start entry.
SKIPPED_STATUSES = ('skipped', 'abandoned')

_FORM_SET = frozenset(FORM_FIELDS)
_SOURCE_KEYS = {'playlist': ('m3u_url', 'm3u_path'), 'guide': ('epg_url', 'epg_path')}
_DIGITS_RE = re.compile(r'[0-9]+')
_CACHE_PREFIX, _CACHE_SUFFIX = 'epg_', '.xml'

# Names the dashboard user sees, for the error messages.
_LABELS = {
    'm3u_url': 'playlist URL', 'm3u_path': 'playlist file path',
    'epg_url': 'guide URL', 'epg_path': 'guide file path',
    'respect_source_numbers': '"Respect source channel numbers"',
    'rediscover_deleted': '"Re-discover deleted channels"',
    'enabled': '"Enabled"',
    'detect_placeholder_pictures': '"Hide placeholder show pictures automatically"',
}

# Error messages (ASCII; see ImportRequestError).
MSG_BAD_REQUEST     = "This import request isn't understood. Reload the page and try again."
MSG_ENTRY           = 'The import settings are missing from the request.'
MSG_NO_ID           = 'The request names no import Identifier.'
MSG_BAD_ID          = 'Identifier must be lowercase a-z, 0-9, _ or -'
MSG_ID_TAKEN        = 'Identifier "%s" is already used by another import'
MSG_ID_LOCKED       = "The Identifier of an existing import can't be changed"
MSG_GONE            = 'This import no longer exists (deleted elsewhere). Reload the page.'
MSG_DUPLICATE_ID    = 'Two imports use this Identifier; delete one of them first'
MSG_LIST_ITEM       = 'Every import in the list needs an Identifier.'
MSG_LIST_DUPLICATE  = 'The same Identifier is used twice in the list.'
MSG_LIST_ID_CHANGE  = "The Identifier of an import can't be changed (or this page is out of date). Reload the page."
MSG_OUT_OF_DATE     = 'This page is out of date (imports were changed elsewhere). Reload the page and try again.'
MSG_NAME            = 'The display name must be text.'
MSG_START_NUM       = 'Start channel number must be a whole number from 1 to 9999.'
MSG_REFRESH         = 'Refresh interval must be a whole number of minutes from 0 to 1440.'
MSG_CATCHUP_TEXT    = '%s must be text or empty.'
MSG_CATCHUP_DAYS    = 'catchup_days must be a whole number from 0 to 365, or empty.'


class ImportRequestError(ValueError):
    """A refused dashboard request: .status is the HTTP code, .message the
    text the page shows ("Save failed: <message>").

    Messages are ASCII and never contain a raw import id: server.py's
    send_error writes the message into the HTTP status line, which
    BaseHTTPRequestHandler encodes as latin-1 (strict), and legacy ids outside
    ID_RE stay allowed, so an echoed id could raise inside the error path. The
    only message naming an id is the Add duplicate, after that id passed
    ID_RE. Non-ASCII characters and line breaks are replaced as a last guard.
    A ValueError, so callers that catch ValueError keep working."""

    def __init__(self, status, message):
        text = str(message).replace('\r', ' ').replace('\n', ' ')
        text = text.encode('ascii', 'replace').decode('ascii')
        super(ImportRequestError, self).__init__(text)
        self.status = status
        self.message = text


# ---------------------------------------------------------------- small helpers


def _entry_id(entry):
    """The entry's id; None for a non-dict entry or an id that can't be a
    dict key (a list or dict from a hand-edited file)."""
    if not isinstance(entry, dict):
        return None
    iid = entry.get('id')
    try:
        hash(iid)
    except TypeError:
        return None
    return iid


def _member(value, collection):
    """`value in collection`, False for an unhashable value."""
    try:
        return value in collection
    except TypeError:
        return False


def _text_ids(values):
    """The str items of a list/tuple/set/frozenset (tombstones, re-discovered
    ids); anything else gives []. Channel ids are always text."""
    if not isinstance(values, (list, tuple, set, frozenset)):
        return []
    return [v for v in values if isinstance(v, str)]


def _as_int(value):
    """Compare times as integers (last_sync_at); None when not a number."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def effective_source(entry, kind):
    """The address the sync reads for `kind` ('playlist' or 'guide'): the URL
    when set, else the file path (imports.py syncOne, replay_helpers), or ''.
    The value is returned as stored, so source_key gives what syncOne
    computes."""
    url_key, path_key = _SOURCE_KEYS[kind]
    if not isinstance(entry, dict):
        return ''
    return entry.get(url_key) or entry.get(path_key) or ''


def source_key(address):
    """A fingerprint of an address: the first 16 hex digits of its sha256,
    '' for no address. A hash, so no copy of an address that carries a
    credential is stored (sha256 rather than md5: ruff S flags md5, and
    usedforsecurity needs Python 3.9)."""
    if not address:
        return ''
    if not isinstance(address, str):
        address = str(address)
    return hashlib.sha256(address.encode('utf-8', 'surrogatepass')).hexdigest()[:16]


def source_changed(stored, new):
    """True only when both keys exist and differ: an entry from before
    imports.69 has no key, and a missing key never counts as a change."""
    return bool(stored) and bool(new) and stored != new


def config_fingerprint(entry):
    """The fingerprint of an import's settings: every DASHBOARD_FIELDS key
    except id that is present in the entry, as sorted compact JSON, sha256,
    16 hex digits. Key order and the id don't change it."""
    if not isinstance(entry, dict):
        entry = {}
    view = {}
    for key in sorted(DASHBOARD_FIELDS):
        if key != 'id' and key in entry:
            view[key] = entry[key]
    # default=str: settings come from JSON, so this never fires; it only keeps
    # a hand-made value from stopping the sync with an exception.
    text = json.dumps(view, sort_keys=True, separators=(',', ':'), ensure_ascii=True, default=str)
    return hashlib.sha256(text.encode('ascii')).hexdigest()[:16]


def config_changed(entry):
    """True when the settings changed since the last sync attempt recorded
    its fingerprint. A missing config_key (entries from before imports.69)
    means unchanged; 'pending' (a dashboard change) always means changed."""
    if not isinstance(entry, dict):
        return False
    stored = entry.get('config_key')
    return bool(stored) and stored != config_fingerprint(entry)


def is_valid_id(value):
    """A new import's Identifier: lowercase a-z, 0-9, _ or - only."""
    return isinstance(value, str) and ID_RE.fullmatch(value) is not None


def stale_guide_caches(names, keep_ids):
    """The guide cache files (epg_<id>.xml) in a directory listing whose <id>
    is not one of `keep_ids` (the imports still listed). Names come from the
    listing, never from an import id, and are compared as plain text, so odd
    legacy ids need no escaping. Other files are never returned."""
    keep = {str(i) for i in (keep_ids or ()) if i is not None and i != ''}
    stale = []
    for name in names or ():
        if not isinstance(name, str) or '/' in name or '\\' in name:
            continue
        if not (name.startswith(_CACHE_PREFIX) and name.endswith(_CACHE_SUFFIX)):
            continue
        if name[len(_CACHE_PREFIX):len(name) - len(_CACHE_SUFFIX)] not in keep:
            stale.append(name)
    return sorted(stale)


def clean_stored(entry):
    """A deep copy of a stored entry without the GET-only keys, with a stored
    'abandoned' turned back into 'failed' ('abandoned' is derived from
    failed_since and the abandon setting; older versions could store it)."""
    out = {key: copy.deepcopy(value) for key, value in entry.items() if key not in DERIVED_FIELDS}
    if out.get('last_status') == 'abandoned':
        out['last_status'] = 'failed'
    return out


def without_import_records(channels, import_ids):
    """The channel list without the imported records (type 'import') of the
    given imports. Other records, and non-dict items, are kept."""
    ids = set(import_ids or ())
    return [c for c in (channels or ())
            if not (isinstance(c, dict) and c.get('type') == 'import' and _member(c.get('import_source'), ids))]


# ---------------------------------------------------------------- dashboard save


def _whole_number(value, bounds, message, digits_ok=False):
    if isinstance(value, bool):
        raise ImportRequestError(400, message)
    if digits_ok and isinstance(value, str) and _DIGITS_RE.fullmatch(value):
        value = int(value)
    if not isinstance(value, int) or not bounds[0] <= value <= bounds[1]:
        raise ImportRequestError(400, message)
    return value


def normalise_form_entry(raw, check_address=None, new_id=True):
    """The DASHBOARD_FIELDS keys present in `raw`, type-checked. Any other
    key (sync results, tombstones, GET-only or unknown keys) is dropped
    without an error (AIP-203); a missing key stays missing. Raises
    ImportRequestError(400) for a bad value.

    new_id=True (an Add): the id must match ID_RE. new_id=False (an Edit):
    the id is taken as stored, so a legacy id outside ID_RE stays editable.
    check_address(value) checks every non-empty address (server.py passes
    imports.validate_source_url with the allowed schemes); its ValueError
    becomes the 400."""
    if not isinstance(raw, dict):
        raise ImportRequestError(400, MSG_ENTRY)
    out = {}
    if new_id:
        if not is_valid_id(raw.get('id')):
            raise ImportRequestError(400, MSG_BAD_ID)
        out['id'] = raw['id']
    elif 'id' in raw:
        out['id'] = raw['id']
    if 'name' in raw:
        name = raw['name']
        if name is not None and not isinstance(name, str):
            raise ImportRequestError(400, MSG_NAME)
        # As the Edit-import window does: an empty name becomes the id.
        name = (name or '').strip()
        fallback = out.get('id')
        out['name'] = name or (fallback if isinstance(fallback, str) else '')
    for key in ADDRESS_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if value is None:
            out[key] = None
            continue
        if not isinstance(value, str):
            raise ImportRequestError(400, 'The %s must be text.' % (_LABELS[key],))
        value = value.strip()
        if value and check_address is not None:
            try:
                check_address(value)
            except ValueError as e:
                raise ImportRequestError(400, 'Invalid %s: %s' % (_LABELS[key], e)) from e
        out[key] = value
    if 'start_num' in raw:
        out['start_num'] = _whole_number(raw['start_num'], START_NUM_RANGE, MSG_START_NUM, digits_ok=True)
    if 'refresh_interval_min' in raw:
        out['refresh_interval_min'] = _whole_number(raw['refresh_interval_min'], REFRESH_RANGE, MSG_REFRESH)
    for key in BOOL_FIELDS:
        if key in raw:
            if not isinstance(raw[key], bool):
                raise ImportRequestError(400, '%s must be ticked or unticked (true or false).' % (_LABELS[key],))
            out[key] = raw[key]
    for key in ('catchup_mode', 'catchup_query_format'):
        if key in raw:
            if raw[key] is not None and not isinstance(raw[key], str):
                raise ImportRequestError(400, MSG_CATCHUP_TEXT % (key,))
            out[key] = raw[key]
    if 'catchup_days' in raw:
        days = raw['catchup_days']
        out['catchup_days'] = None if days is None else _whole_number(days, CATCHUP_DAYS_RANGE, MSG_CATCHUP_DAYS)
    return out


def merge_dashboard_entry(disk, form):
    """Apply a dashboard form onto the stored entry. Returns (merged,
    changed_fields, reset_kinds).

    Starts from clean_stored(disk) and takes from `form` only the
    DASHBOARD_FIELDS keys it carries (never the id), so the stored sync
    results and tombstones stay, and the last save wins per box.
    changed_fields lists the keys whose value differs from disk (a key
    missing on disk differs from any value). When something changed: an
    address that changed drops that address's download markers and error
    state (PLAYLIST_RESET / GUIDE_RESET, reset_kinds names them), and
    config_key becomes 'pending' so the next sync cycle syncs this import."""
    merged = clean_stored(disk)
    changed = []
    for key in sorted(DASHBOARD_FIELDS):
        if key == 'id' or key not in form:
            continue
        if disk.get(key, MISSING) != form[key]:
            changed.append(key)
        merged[key] = copy.deepcopy(form[key])
    reset = []
    if changed:
        for kind, keys in (('playlist', PLAYLIST_RESET), ('guide', GUIDE_RESET)):
            if effective_source(merged, kind) != effective_source(disk, kind):
                for key in keys:
                    merged.pop(key, None)
                reset.append(kind)
        merged['config_key'] = CONFIG_KEY_PENDING
    return merged, changed, reset


def _summary(op, iid=None, changed=False, fields=(), reset=(), removed_ids=()):
    return {'op': op, 'id': iid, 'changed': bool(changed), 'fields': list(fields),
            'reset': list(reset), 'removed_ids': list(removed_ids)}


def _op_add(fresh, entry, check_address):
    form = normalise_form_entry(entry, check_address, new_id=True)
    iid = form['id']
    if any(_entry_id(e) == iid for e in fresh):
        raise ImportRequestError(409, MSG_ID_TAKEN % (iid,))   # iid passed ID_RE: ASCII, no quotes
    # Appended: list order is sync priority. No sync fields: with no
    # last_sync_at the new import's gate is open.
    new_list = list(fresh)
    new_list.append(form)
    return new_list, _summary('add', iid, True, sorted(k for k in form if k != 'id'))


def _op_edit(fresh, orig, entry, check_address):
    if not isinstance(orig, str) or not orig:
        raise ImportRequestError(400, MSG_NO_ID)
    if not isinstance(entry, dict):
        raise ImportRequestError(400, MSG_ENTRY)
    if 'id' in entry and entry['id'] != orig:
        raise ImportRequestError(400, MSG_ID_LOCKED)        # the server-side Identifier lock
    form = normalise_form_entry(dict(entry, id=orig), check_address, new_id=False)
    where = [i for i, e in enumerate(fresh) if _entry_id(e) == orig]
    if not where:
        raise ImportRequestError(404, MSG_GONE)
    if len(where) > 1:
        raise ImportRequestError(409, MSG_DUPLICATE_ID)     # older versions / hand edits only
    merged, changed, reset = merge_dashboard_entry(fresh[where[0]], form)
    if not changed:
        return list(fresh), _summary('edit', orig)
    new_list = list(fresh)
    new_list[where[0]] = merged
    return new_list, _summary('edit', orig, True, changed, reset)


def _op_delete(fresh, iid):
    if not isinstance(iid, str) or not iid:
        raise ImportRequestError(400, MSG_NO_ID)
    kept = [e for e in fresh if _entry_id(e) != iid]
    if len(kept) == len(fresh):
        return list(fresh), _summary('delete', iid)          # already gone: a double click is harmless
    return kept, _summary('delete', iid, True, removed_ids=[iid])


def _form_built(item):
    """An item the old page's gatherImportForm built (only form keys), as
    opposed to a verbatim copy of the GET reply: a synced import's copy always
    carries last_sync_at and other sync keys."""
    return set(item).issubset(_FORM_SET)


def _differs(item, disk_entry, keys):
    return any(item.get(key, MISSING) != disk_entry.get(key, MISSING) for key in keys)


def _apply_list(fresh, items, check_address):
    """A whole list posted by a dashboard tab opened before imports.69: one
    change at a time, and only from an up-to-date list.

    The old page posts the list it got from GET (every item a verbatim copy,
    V) with the edited or new import replaced by the form's 12 keys (F), on
    every Save, also when nothing changed; a Delete posts the list minus one
    item. Verbatim items are never applied, so their old sync results,
    tombstones and GET-only keys never reach storage. A verbatim item that no
    longer matches disk, or more than one change, means the page is out of
    date: 409. Leftovers (only such tabs; the page carries no version): an
    Edit re-sends every box of that import, and an Edit of an import deleted
    elsewhere looks like an Add and re-adds it; a never-synced import's copy
    looks form-built, so a Delete is refused while one exists."""
    ids = []
    for item in items:
        iid = item.get('id') if isinstance(item, dict) else None
        if not isinstance(iid, str) or not iid:
            raise ImportRequestError(400, MSG_LIST_ITEM)
        ids.append(iid)
    if len(set(ids)) != len(ids):
        raise ImportRequestError(400, MSG_LIST_DUPLICATE)
    disk = {}                                                  # first entry per id, disk order
    for entry in fresh:
        iid = _entry_id(entry)
        if isinstance(iid, str) and iid and iid not in disk:
            disk[iid] = entry
    listed = set(ids)
    removed = [iid for iid in disk if iid not in listed]
    added = [item for item in items if item['id'] not in disk]
    form_built = [item for item in items if _form_built(item)]
    stale = [item for item in items if item['id'] in disk and not _form_built(item)
             and _differs(item, disk[item['id']], DASHBOARD_FIELDS)]
    edits = [item for item in items if item['id'] in disk and _form_built(item)
             and _differs(item, disk[item['id']], item)]
    if removed and added:
        # An Identifier changed in the old form, a page whose list failed to
        # load, or an Add from a page that misses an import added elsewhere.
        raise ImportRequestError(400, MSG_LIST_ID_CHANGE)
    if [item for item in added if not _form_built(item)]:
        raise ImportRequestError(409, MSG_OUT_OF_DATE)         # a copy of an import deleted elsewhere
    if stale:
        raise ImportRequestError(409, MSG_OUT_OF_DATE)         # something changed elsewhere since the page loaded
    if removed:
        # A Delete only from a list of verbatim copies: an old tab's no-change
        # Save always carries a form-built item and must never delete an import
        # added elsewhere.
        if len(removed) == 1 and not form_built:
            return _op_delete(fresh, removed[0])
        raise ImportRequestError(409, MSG_OUT_OF_DATE)
    if added:
        if len(added) == 1 and not edits:
            return _op_add(fresh, added[0], check_address)
        raise ImportRequestError(409, MSG_OUT_OF_DATE)
    if edits:
        if len(edits) == 1:
            # Every form key of the item; catch-up keys it lacks are kept.
            return _op_edit(fresh, edits[0]['id'], edits[0], check_address)
        raise ImportRequestError(409, MSG_OUT_OF_DATE)
    return list(fresh), _summary('none')


def apply_dashboard_request(fresh_imports, payload, check_address=None):
    """Apply one POST /imports.json request to the import list as it is on
    disk now. Returns (new_list, summary) with summary = {'op', 'id',
    'changed', 'fields', 'reset', 'removed_ids'}; changed False means
    nothing to write. Raises ImportRequestError (400/404/409).

    payload is the new page's one operation:
      {'op': 'add', 'entry': {...}}            Identifier must match ID_RE; 409 if taken
      {'op': 'edit', 'id': orig, 'entry': {}}  only the changed boxes; the id can't change (400);
                                               404 when deleted elsewhere; 409 for a duplicated id
      {'op': 'delete', 'id': x}                every entry with that id; nothing to do if none
    or a whole list from a page opened before imports.69 (_apply_list).
    Unchanged entries are passed through as they are (not copied)."""
    fresh = list(fresh_imports or [])
    if isinstance(payload, dict):
        op = payload.get('op')
        if op == 'add':
            return _op_add(fresh, payload.get('entry'), check_address)
        if op == 'edit':
            return _op_edit(fresh, payload.get('id'), payload.get('entry'), check_address)
        if op == 'delete':
            return _op_delete(fresh, payload.get('id'))
        raise ImportRequestError(400, MSG_BAD_REQUEST)
    if isinstance(payload, list):
        return _apply_list(fresh, payload, check_address)
    raise ImportRequestError(400, MSG_BAD_REQUEST)


# ---------------------------------------------------------------- sync commit


def sync_delta(start, updated):
    """{key: new value, or MISSING for a removed key} for the SYNC_FIELDS a
    sync changed between its start copy of the entry and its result."""
    start = start if isinstance(start, dict) else {}
    updated = updated if isinstance(updated, dict) else {}
    delta = {}
    for key in sorted(SYNC_FIELDS):
        new = updated.get(key, MISSING)
        if start.get(key, MISSING) != new:
            delta[key] = new
    return delta


def _superseded(fresh, start, updated):
    """Another sync committed this import while this one ran (a Save never
    changes last_sync_at) and this result is older: drop it whole, so the
    newest attempt wins and last_sync_at never goes backwards. An equal time
    is applied, and so is any result while the entry has no last_sync_at
    (deleted and re-added with the same Identifier during the sync)."""
    fresh_at = _as_int(fresh.get('last_sync_at'))
    if fresh_at is None or fresh_at == _as_int(start.get('last_sync_at')):
        return False
    result_at = _as_int(updated.get('last_sync_at'))
    return result_at is None or result_at < fresh_at


def _drop_tombstones(entry, ids):
    tombstones = entry.get('tombstones')
    if not isinstance(tombstones, list):
        return
    left = [t for t in tombstones if not _member(t, ids)]
    if left:
        entry['tombstones'] = left
    else:
        entry.pop('tombstones', None)


def apply_sync_results(fresh_imports, start_by_id, updated_by_id, rediscovered_by_id, cache_written_ids=()):
    """Apply a sync's results onto the import list as it is on disk now.
    Returns (new_list, info).

    start_by_id: deep copies of the entries as the sync read them.
    updated_by_id: the updated_cfg of the results that went through syncOne
    only (a skipped or abandoned result is a copy of its start entry and must
    not overwrite a Save made meanwhile). rediscovered_by_id: the tombstoned
    channel ids each sync re-created. cache_written_ids: the imports whose
    cache/epg_<id>.xml this sync replaced.

    Per disk entry, in disk order (every entry through clean_stored):
      - no start or no result (added during the sync, gate-skipped): as it is;
      - superseded by a newer sync (_superseded): unchanged;
      - else exactly sync_delta(start, updated) is applied. When the playlist
        (guide) address changed while the sync ran, PLAYLIST_GROUP
        (GUIDE_GROUP) is left out: those results describe the old address.
        If such a result replaced the guide cache file, the stored guide
        validators go and epg_source_key becomes this result's, so the key
        always names the address whose guide is in the file;
      - last_sync_at and config_key are in no group and always apply.
    Re-discovered ids are kept only while the entry exists, the result wasn't
    superseded, Re-discover is still ticked on the disk entry and the playlist
    didn't move; kept ids leave the entry's tombstones (the key goes when it
    is empty), the others are 'dropped'.

    info: 'gone', 'playlist_moved', 'guide_moved', 'superseded', 'updated'
    (sets of import ids), 'kept', 'dropped' ({import id: set of channel ids})
    and 'tombstones' (every tombstone left on disk)."""
    start_by_id = start_by_id or {}
    updated_by_id = updated_by_id or {}
    rediscovered_by_id = rediscovered_by_id or {}
    cache_written = set(cache_written_ids or ())
    info = {'gone': set(), 'playlist_moved': set(), 'guide_moved': set(), 'superseded': set(),
            'kept': {}, 'dropped': {}, 'tombstones': set(), 'updated': set()}
    fresh = list(fresh_imports or [])
    on_disk = {_entry_id(e) for e in fresh if isinstance(e, dict)}
    for iid in start_by_id:
        if iid in updated_by_id and iid not in on_disk:
            info['gone'].add(iid)                    # deleted while the sync ran: never re-created
    new_list = []
    for entry in fresh:
        if not isinstance(entry, dict):
            new_list.append(copy.deepcopy(entry))
            continue
        iid = _entry_id(entry)
        merged = clean_stored(entry)
        new_list.append(merged)
        start = start_by_id.get(iid) if iid is not None else None
        updated = updated_by_id.get(iid) if iid is not None else None
        if not isinstance(start, dict) or not isinstance(updated, dict):
            continue
        if _superseded(entry, start, updated):
            info['superseded'].add(iid)
            continue
        delta = sync_delta(start, updated)
        if effective_source(entry, 'playlist') != effective_source(start, 'playlist'):
            for key in PLAYLIST_GROUP:
                delta.pop(key, None)
            info['playlist_moved'].add(iid)
        if effective_source(entry, 'guide') != effective_source(start, 'guide'):
            for key in GUIDE_GROUP:
                delta.pop(key, None)
            info['guide_moved'].add(iid)
            if iid in cache_written:
                # The file now holds this sync's download of the old address:
                # no stored validator may describe another download, and the
                # key names the address the file came from.
                merged.pop('epg_etag', None)
                merged.pop('epg_last_modified', None)
                key = updated.get('epg_source_key')
                if key:
                    merged['epg_source_key'] = key
                else:
                    merged.pop('epg_source_key', None)
        for key, value in delta.items():
            if value is MISSING:
                merged.pop(key, None)
            else:
                merged[key] = copy.deepcopy(value)
        info['updated'].add(iid)
        ids = _text_ids(rediscovered_by_id.get(iid))
        if ids and entry.get('rediscover_deleted') and iid not in info['playlist_moved']:
            info['kept'].setdefault(iid, set()).update(ids)
            _drop_tombstones(merged, set(ids))
    for iid, ids in rediscovered_by_id.items():
        left = set(_text_ids(ids)).difference(info['kept'].get(iid, ()))
        if left:
            info['dropped'][iid] = left
    for entry in new_list:
        if isinstance(entry, dict):
            info['tombstones'].update(_text_ids(entry.get('tombstones')))
    return new_list, info


def plan_sync_commit(fresh_imports, fresh_channel_ids, start_by_id, results, writer_channels,
                     modified_ids, deleted_ids):
    """The whole sync commit as data, shared by Channels.commitSync and the
    tests' fake channel stores. Returns (new_imports, writer, mods, dels,
    info).

    results: syncAll's per_import_results ({import id: result}); only results
    that reached syncOne count (status not 'skipped'/'abandoned'), with their
    'rediscovered' and 'epg_cache_written' keys. writer_channels,
    modified_ids, deleted_ids: the sync's channel list and id sets.
      - new_imports, info = apply_sync_results(...);
      - writer: writer_channels without the imported records of the imports
        deleted, moved (playlist) or superseded during the sync, and without
        the dropped re-discovered ids;
      - mods: modified_ids limited to the writer list;
      - dels: deleted_ids plus every tombstone on disk now (so an Orphans made
        during the sync stays), minus the kept re-discoveries.
    info gains 'drop_imports', 'dropped_ids', 'deleted_ids' (= dels) and
    'applied_deletes' (the deletes that remove a record that exists on disk
    or in the writer list: the PVR refresh signal)."""
    updated_by_id, rediscovered_by_id, cache_written = {}, {}, set()
    for iid, res in (results or {}).items():
        if not isinstance(res, dict) or res.get('status') in SKIPPED_STATUSES:
            continue
        cfg = res.get('updated_cfg')
        if not isinstance(cfg, dict):
            continue
        updated_by_id[iid] = cfg
        if res.get('rediscovered'):
            rediscovered_by_id[iid] = _text_ids(res.get('rediscovered'))
        if res.get('epg_cache_written'):
            cache_written.add(iid)
    new_imports, info = apply_sync_results(fresh_imports, start_by_id, updated_by_id,
                                           rediscovered_by_id, cache_written)
    drop_imports = set(info['gone']).union(info['playlist_moved'], info['superseded'])
    drop_ids, kept = set(), set()
    for ids in info['dropped'].values():
        drop_ids.update(ids)
    for ids in info['kept'].values():
        kept.update(ids)
    writer = [c for c in without_import_records(writer_channels, drop_imports)
              if not (isinstance(c, dict) and _member(c.get('id'), drop_ids))]
    writer_ids = set()
    for c in writer:
        cid = _entry_id(c)
        if cid is not None:
            writer_ids.add(cid)
    mods = set(modified_ids or ()).intersection(writer_ids)
    dels = set(deleted_ids or ()).union(info['tombstones']).difference(kept)
    info['drop_imports'] = drop_imports
    info['dropped_ids'] = drop_ids
    info['deleted_ids'] = set(dels)
    info['applied_deletes'] = dels.intersection(set(fresh_channel_ids or ()).union(writer_ids))
    return new_imports, writer, mods, dels, info
