# -*- coding: utf-8 -*-
"""imports.69: the Save fix -- the dashboard page (remotes/manager.html).

Before imports.69 the Edit-import window posted the whole import list (the
GET copy it had loaded, with the edited import replaced by the form's 12
boxes), so a Save wiped every import's sync info and could put back what
another tab or a running sync had changed; Delete posted the list minus one.
Now:
  - the Identifier is read-only when editing (the server refuses a change
    too), and only a new Identifier is checked against the pattern;
  - saveImport sends one request: {op: 'add', entry} for a new import, or
    {op: 'edit', id, entry} with the Identifier the window opened with and
    only the boxes that differ from how it opened (IMP_OPENED);
  - Delete sends {op: 'delete', id}, with a confirm text that says what
    happens; 🗑 Orphans' confirm text says when the channels come back;
  - help texts under the start number and the Re-discover box;
  - 🔨 Rebuild all and 🧹 Cleanup stranded check the reply (the add-on answers
    500 when their save is refused);
  - the status badge reads display_status for 'abandoned';
  - the imports.66 extra refreshOne after a picture-switch change is gone:
    the add-on syncs a changed import itself (config_key + render kick).
Source scans, with the helpers of test_imports66_show_pictures.py; the
page's script is also syntax-checked with node when it is installed.
"""
import re
import shutil
import subprocess  # noqa: S404 -- runs `node --check` on the page's own script

import pytest

from test_imports66_show_pictures import _html, _js_code, _js_function


def _squash(src):
    """The code without whole-line comments, one statement per line, spaces
    collapsed: what a scan compares."""
    return [re.sub(r'\s+', ' ', ln).strip() for ln in _js_code(src).splitlines() if ln.strip()]


def _modal():
    src = _html()
    return src[src.index('id="import-modal-overlay"'):src.index('id="import-save-btn"')]


def _handler_block(button_id):
    """The page's click handler for one button: from its addEventListener line
    to the next one."""
    src = _html()
    needle = "$('%s')?.addEventListener('click'" % (button_id,)
    start = src.index(needle)
    end = src.index('?.addEventListener(', start + len(needle))
    return src[start:end]


# ================================================================ the Identifier


def test_identifier_is_read_only_when_editing():
    opened = _js_code(_js_function('openImportModal'))
    assert re.search(r"\$\('f-id'\)\.readOnly\s*=\s*!!imp;", opened)
    assert re.search(r"\$\('f-id-help'\)\.textContent\s*=\s*imp \? \"\(can't be changed after the import is created\)\""
                     r" : '\(unique slug, lowercase a-z/0-9/_-\)';", opened)
    assert '<span class="help" id="f-id-help">(unique slug, lowercase a-z/0-9/_-)</span>' in _modal()
    # greyed out, so it reads as locked
    assert '.imp-modal input[type=text][readonly] { opacity: 0.55; cursor: not-allowed; }' in _html()


def test_the_pattern_check_is_only_for_a_new_identifier():
    """An existing import with an older id outside the pattern stays editable
    (the field is read-only and taken as stored)."""
    form = _js_code(_js_function('gatherImportForm'))
    assert ("if (!$('f-id').readOnly && !/^[a-z0-9_-]+$/.test(id)) "
            "{ toast('Identifier must be lowercase a-z/0-9/_-', 'err'); return null; }") in form
    assert form.count('/^[a-z0-9_-]+$/') == 1
    for box in ('id, name, m3u_path, m3u_url, epg_path, epg_url, start_num,', 'refresh_interval_min: refresh,',
                "respect_source_numbers: $('f-respect').checked,", "rediscover_deleted: $('f-rediscover').checked,",
                "enabled: $('f-enabled').checked,", "detect_placeholder_pictures: $('f-detect-pictures').checked,"):
        assert box in form, box


# ================================================================ saving: one request, only the changed boxes


def test_the_window_keeps_the_boxes_as_they_opened():
    """IMP_OPENED is gatherImportForm's view right after every box is filled
    (and after the read-only switch, which gatherImportForm reads), so the
    diff is against what you saw; null when adding."""
    src = _js_code(_html())
    assert re.search(r'\n    let IMP_OPENED = null;', src)
    opened = _js_code(_js_function('openImportModal'))
    assert opened.count('IMP_OPENED = imp ? gatherImportForm() : null;') == 1
    at = opened.index('IMP_OPENED = ')
    assert opened.index("$('f-id').readOnly") < at
    for box in ('f-id', 'f-name', 'f-m3u-path', 'f-m3u-url', 'f-epg-path', 'f-epg-url', 'f-start-num', 'f-refresh',
                'f-respect', 'f-rediscover', 'f-enabled', 'f-detect-pictures'):
        fill = re.search(r"\$\('%s'\)\.(?:value|checked)\s*=" % (re.escape(box),), opened)
        assert fill and fill.start() < at, box
    assert at < opened.index("$('import-modal-overlay').classList.add('open');")
    # the imports.66 data-original of the picture switch is gone
    assert "$('f-detect-pictures').dataset.original" not in src


def test_save_import_sends_one_request_with_only_the_changed_boxes():
    assert _squash(_js_function('saveImport')) == [
        'async function saveImport() {',
        'const item = gatherImportForm();',
        'if (!item) return;',
        "if (IMP_EDITING_INDEX < 0 || !IMP_OPENED) { await postImports({op: 'add', entry: item}); return; }",
        'const entry = {id: IMP_OPENED.id};',
        'for (const k of Object.keys(item)) if (item[k] !== IMP_OPENED[k]) entry[k] = item[k];',
        "await postImports({op: 'edit', id: IMP_OPENED.id, entry: entry});",
        '}',
    ]


def test_the_extra_refresh_after_a_save_is_gone():
    """The add-on now syncs a changed import itself (config_key 'pending' and
    a render kick); the imports.66 follow-up would only duplicate that."""
    assert 'refreshOne(' not in _js_code(_js_function('saveImport'))
    assert 'detectChanged' not in _html() and 'detectOpened' not in _html()


def test_post_imports_posts_the_request_as_given():
    post = _js_function('postImports')
    assert post.startswith('\n    async function postImports(payload) {')
    assert 'body: JSON.stringify({uuid: UUID, payload: payload}),' in post
    # the error text both pages show ("Save failed: <message>")
    assert "const msg = (t.match(/<p>Message: ([^<]+)/) || [, 'HTTP ' + res.status])[1];" in post
    assert "toast('Save failed: ' + msg, 'err');" in post


def test_no_whole_list_is_posted_any_more():
    """Every postImports call sends one operation."""
    calls = re.findall(r'postImports\(([^)]*)\)', _js_code(_html()))
    args = [c for c in calls if c != 'payload']                     # the definition itself
    assert sorted(args) == sorted(["{op: 'add', entry: item}", "{op: 'edit', id: IMP_OPENED.id, entry: entry}",
                                   "{op: 'delete', id: imp.id}"])


# ================================================================ Delete and Orphans


def test_delete_is_its_own_request_with_the_new_confirm_text():
    click = _js_code(_js_function('_onImportsClick'))
    branch = click[click.index("const delBtn = "):click.index("const refBtn = ")]
    assert ('if (!confirm(`Delete import "${imp.name || imp.id}"?\\n\\nIts channels leave Kodi within about '
            '5 minutes (after any sync or channel build that is running).`)) return;') in branch
    assert "postImports({op: 'delete', id: imp.id});" in branch
    assert 'splice' not in branch and 'IMPORTS_DATA.imports.slice()' not in branch
    assert 'Channels stay on disk until you run Cleanup stranded' not in _html()


def test_orphans_confirm_says_when_they_come_back():
    orphans = _js_function('removeOrphans')
    assert ('if (!confirm(`Delete all orphan channels for import "${id}"?\\n\\nThey come back only if '
            '"Re-discover deleted channels" is ticked for this import and the provider lists them again.`)) '
            'return;') in orphans
    assert "Tombstoned so they won't reappear on next sync." not in _html()


# ================================================================ help texts


def test_help_under_the_start_number():
    modal = _modal()
    text = ('<p class="help">A new start number applies to channels added later. '
            'To move existing channels, press ⟳ Renumber.</p>')
    assert modal.count(text) == 1
    assert modal.index('id="f-start-num"') < modal.index(text) < modal.index('id="f-respect"')


def test_help_under_the_rediscover_box():
    modal = _modal()
    text = ('<p class="help">Ticked: a channel you deleted with 🗑 Orphans comes back (switched on, as a new '
            'channel) when the provider lists it again. Unticked: deleted channels stay deleted. A channel that '
            "came back can't be deleted again while the provider lists it; switch it off instead.</p>")
    assert modal.count(text) == 1
    assert modal.index('id="f-rediscover"') < modal.index(text) < modal.index('id="f-detect-pictures"')


# ================================================================ Rebuild all / Cleanup stranded / badge


@pytest.mark.parametrize('button, route, failed, queued', [
    ('cleanup-stranded-btn', 'imports/cleanup_stranded.json', "toast('Cleanup failed: HTTP ' + res.status, 'err');",
     "toast('Cleanup queued', 'ok');"),
    ('rebuild-all-pseudo-btn', 'channels/rebuild.json', "toast('Rebuild failed: HTTP ' + res.status, 'err');",
     "toast('Rebuild all queued', 'ok');"),
])
def test_rebuild_all_and_cleanup_stranded_check_the_reply(button, route, failed, queued):
    block = _js_code(_handler_block(button))
    assert 'const res = await fetch(`http://${REMOTE_HOST}/%s`' % (route,) in block
    check = 'if (!res.ok) { %s return; }' % (failed,)
    assert block.count(check) == 1
    assert block.index(check) < block.index(queued)


def test_status_badge_reads_display_status():
    badge = _js_code(_js_function('statusBadge'))
    assert "if (imp.display_status === 'abandoned') {" in badge
    assert "imp.last_status === 'abandoned'" not in badge
    assert 'const synced = imp.last_sync_at;' in badge
    assert ("if (imp.enabled === false) return '<span class=\"warn\">switched off (channels hidden from the TV)"
            "</span>';") in badge


# ================================================================ the page still parses


def test_the_page_script_parses(tmp_path):
    """A syntax error would break the whole dashboard; checked with node when
    it is installed (skipped otherwise)."""
    node = shutil.which('node')
    if not node:
        pytest.skip('node is not installed')
    scripts = re.findall(r'<script(?:\s[^>]*)?>(.*?)</script>', _html(), re.S)
    assert scripts
    page = tmp_path / 'page.js'
    page.write_text('\n;\n'.join(s for s in scripts if s.strip()), encoding='utf-8')
    done = subprocess.run([node, '--check', str(page)], capture_output=True, text=True, timeout=60)  # noqa: S603
    assert done.returncode == 0, done.stderr
