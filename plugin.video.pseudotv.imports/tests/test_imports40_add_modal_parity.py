# -*- coding: utf-8 -*-
"""imports.40: Add PseudoTV channel modal — full Edit-dialog parity.

Source-scan invariants for:
  * server.py /channels/add.json widened to accept optional logo, group,
    catchup, radio, favorite, enabled, rules fields.
  * server.py /api/channels/rules.json POST now delegates rule validation
    to rules_helpers._normalizeRules (extracted from inline block).
  * manager.html pseudo-modal grew to 9 fields + Set rules… button +
    transient draft state. cf-modal markup refactored to use the shared
    renderChannelFieldEditor helper (regression-guarded by source-scan
    asserting all cf-* IDs still emit from the helper).

Behavioral test for the wire-format reaches into rules_helpers (separate
file). Driving the HTTP class with full Kodi context is impractical;
source-scan is the convention (mirrors test_http_server_close.py,
test_imports34_manager_classification.py).
"""
import os
import re


HERE       = os.path.dirname(os.path.abspath(__file__))
ADDON_ROOT = os.path.dirname(HERE)
LIB        = os.path.join(ADDON_ROOT, 'resources', 'lib')
HTML_PATH  = os.path.join(ADDON_ROOT, 'remotes', 'manager.html')
SERVER_PY  = os.path.join(LIB, 'server.py')
HELPERS_PY = os.path.join(LIB, 'rules_helpers.py')


def _read(path):
    with open(path, 'r', encoding='utf-8') as fh:
        return fh.read()


# ============================================================
# A. rules_helpers.py — file exists, exposes _normalizeRules
# ============================================================

def test_rules_helpers_module_present():
    src = _read(HELPERS_PY)
    assert 'def _normalizeRules' in src, "rules_helpers.py must define _normalizeRules"


def test_rules_helpers_function_local_RulesList_import():
    """Mirrors the imports.29/.32/.33/.36 helpers convention — `from rules import RulesList`
    happens inside the function so tests don't need Kodi stubs."""
    src = _read(HELPERS_PY)
    # The import must appear AFTER `def _normalizeRules` (function-local).
    fn = src.find('def _normalizeRules')
    assert fn != -1
    body = src[fn:]
    assert 'from rules import RulesList' in body, \
        "rules_helpers._normalizeRules must use function-local RulesList import"


# ============================================================
# B. server.py /channels/add.json — widened fields
# ============================================================

def _add_json_block(src):
    """Locate the actual /channels/add.json route handler body in server.py,
    not the descriptive comment above it. Returns the source slice from
    `elif self.path... == '/channels/add.json':` until the next `elif self.path`
    or `# POST /channels/` boundary."""
    route_re = re.compile(
        r"elif self\.path\.split\('\?', 1\)\[0\]\.lower\(\) == '/channels/add\.json':",
    )
    m = route_re.search(src)
    assert m, "/channels/add.json route handler not found in server.py"
    start = m.start()
    # Bound: next elif self.path branch (or end of file).
    end_m = re.search(
        r"\n\s+elif self\.path\.split\('\?', 1\)\[0\]\.lower\(\) ==",
        src[start + 100:],
    )
    end = start + 100 + (end_m.start() if end_m else len(src))
    return src[start:end]


def test_add_json_imports_normalizeRules():
    block = _add_json_block(_read(SERVER_PY))
    assert 'from rules_helpers import _normalizeRules' in block, \
        "/channels/add.json must import _normalizeRules"


def test_add_json_reads_optional_fields():
    block = _add_json_block(_read(SERVER_PY))
    for field in ('logo', 'group', 'catchup', 'radio', 'favorite', 'enabled', 'rules'):
        assert "incoming.get('%s')" % field in block, \
            "/channels/add.json must read incoming.%s" % field


def test_add_json_catchup_allow_list():
    block = _add_json_block(_read(SERVER_PY))
    assert "'default'" in block and "'vod'" in block and "'timeshift'" in block, \
        "/channels/add.json catchup allow-list missing one of default/vod/timeshift"


def test_add_json_calls_copyToLogoLoc_when_logo_supplied():
    block = _add_json_block(_read(SERVER_PY))
    assert 'copyToLogoLoc' in block, \
        "/channels/add.json must run operator-supplied logo through copyToLogoLoc"


def test_add_json_calls_markOverrides():
    block = _add_json_block(_read(SERVER_PY))
    assert 'markOverrides(citem' in block, \
        "/channels/add.json must call markOverrides on operator-supplied optional fields (imports.20 pattern)"


def test_add_json_radio_auto_derive_preserved():
    """imports.40 must not regress the musicdb:// auto-derive for radio
    when the operator doesn't supply the field."""
    block = _add_json_block(_read(SERVER_PY))
    assert "musicdb://" in block, \
        "/channels/add.json must preserve the musicdb:// → radio=True auto-derive fallback"


def test_add_json_response_carries_rules_applied_rejected():
    block = _add_json_block(_read(SERVER_PY))
    assert "'applied'" in block and "'rejected'" in block, \
        "/channels/add.json response must include rules.applied + rules.rejected"


def test_add_json_backward_compat_required_fields_unchanged():
    """The original 3 required fields (name, number, path) and their
    validation messages remain — backward compat for the 3-field POST."""
    block = _add_json_block(_read(SERVER_PY))
    assert 'name required' in block
    assert 'number must be int 1..%d' in block
    assert 'path required' in block


# ============================================================
# C. server.py /api/channels/rules.json POST — uses helper now
# ============================================================

def test_rules_json_post_uses_normalizeRules():
    src = _read(SERVER_PY)
    # Find the POST handler block (not the GET at line ~2148).
    m = re.search(r"/api/channels/rules\.json'\s*:\s*$", src, flags=re.MULTILINE)
    # Fallback: just find the first occurrence after a `do_POST` context.
    if not m:
        m = re.search(r"/api/channels/rules\.json", src)
    assert m, "/api/channels/rules.json route not found"
    block = src[m.start(): m.start() + 3000]
    assert 'from rules_helpers import _normalizeRules' in block, \
        "/api/channels/rules.json POST must import _normalizeRules"
    assert '_normalizeRules(rules)' in block, \
        "/api/channels/rules.json POST must call _normalizeRules(rules)"


def test_rules_json_post_inline_validation_removed():
    """The OLD inline catalog-building + per-rule loop should be gone from
    the POST handler. Specifically, the line `catalog = {r.myId: r for r in
    RulesList().allRules()}` must NOT appear inside the POST block (it's
    now in rules_helpers)."""
    src = _read(SERVER_PY)
    m = re.search(r"/api/channels/rules\.json", src)
    block = src[m.start(): m.start() + 2000]
    assert 'catalog = {r.myId: r for r in RulesList().allRules()}' not in block, \
        "/api/channels/rules.json POST still has inline catalog-build — imports.40 must extract"


# ============================================================
# D. server.py /channels/logo/upload.json — pending_id branch
# ============================================================

def test_upload_json_accepts_pending_id():
    src = _read(SERVER_PY)
    m = re.search(r"/channels/logo/upload\.json", src)
    assert m
    block = src[m.start(): m.start() + 3000]
    assert "incoming.get('pending_id')" in block, \
        "/channels/logo/upload.json must read pending_id from incoming"


def test_upload_json_either_channel_id_or_pending_id_required():
    src = _read(SERVER_PY)
    m = re.search(r"/channels/logo/upload\.json", src)
    block = src[m.start(): m.start() + 3000]
    assert 'channel_id or pending_id required' in block, \
        "/channels/logo/upload.json must reject when neither channel_id nor pending_id provided"


def test_upload_json_writeUploadedLogo_uses_name_hint_kw():
    """writeUploadedLogo's first param renamed chname→name_hint. The
    upload endpoint passes either pending_id OR target.get('name','')
    via the positional arg — same call shape, different source per branch."""
    src = _read(LIB + '/logo_helpers.py')
    assert 'def writeUploadedLogo(file_bytes, name_hint' in src, \
        "writeUploadedLogo must accept name_hint param"


# ============================================================
# E. manager.html — pseudo-modal full parity + renderChannelFieldEditor
# ============================================================

def test_renderChannelFieldEditor_defined():
    src = _read(HTML_PATH)
    assert 'function renderChannelFieldEditor(idPrefix' in src, \
        "manager.html must define the renderChannelFieldEditor helper"


def test_pseudo_modal_uses_grid_container():
    src = _read(HTML_PATH)
    # The Add modal grid is now injected via the helper, not inline.
    m = re.search(r'id="pseudo-modal-overlay".*?id="pf-grid-container"', src, flags=re.DOTALL)
    assert m, "pseudo-modal-overlay must contain a #pf-grid-container placeholder"


def test_cf_modal_uses_grid_container():
    src = _read(HTML_PATH)
    m = re.search(r'id="chfields-modal-overlay".*?id="cf-grid-container"', src, flags=re.DOTALL)
    assert m, "chfields-modal-overlay must contain a #cf-grid-container placeholder"


def test_cf_modal_static_fields_removed():
    """Regression guard: the OLD hardcoded cf-name/cf-number/etc. inputs
    inside the modal markup must be gone — they're now generated by the
    helper at openCustomEditModal time."""
    src = _read(HTML_PATH)
    # Find the chfields-modal-overlay block boundaries.
    start = src.find('id="chfields-modal-overlay"')
    end   = src.find('</div>', src.find('chfields-modal-title', start)) + 1000
    # Bound search to just the modal markup (not the helper output).
    modal_markup = src[start:end]
    # The helper string-template lives OUTSIDE this modal block; if cf-name
    # appears in the modal HTML itself (not via the helper), the refactor
    # didn't take. Loosened: assert the modal block does NOT contain an
    # `<input type="text" id="cf-name">` literal.
    assert '<input type="text" id="cf-name">' not in modal_markup, \
        "cf-modal still has hardcoded <input id=cf-name>; imports.40 refactor incomplete"


def test_helper_generates_all_9_field_ids():
    """The renderChannelFieldEditor template must emit every cf-* ID the
    existing cf-modal code (openCustomEditModal + stageCustomEdit) reads."""
    src = _read(HTML_PATH)
    fn = src.find('function renderChannelFieldEditor(')
    assert fn != -1
    # Look ahead ~3000 chars for the template body.
    body = src[fn: fn + 3000]
    # Every input ID is `${idPrefix}-<suffix>`. Suffix names must be present.
    for suffix in ('name', 'number', 'group', 'logo', 'path', 'catchup', 'radio', 'favorite', 'enabled'):
        assert "${idPrefix}-" + suffix in body, \
            "renderChannelFieldEditor missing ${idPrefix}-%s output" % suffix


def test_openCustomEditModal_calls_helper():
    src = _read(HTML_PATH)
    fn = src.find('function openCustomEditModal(')
    assert fn != -1
    body = src[fn: fn + 1500]
    assert "renderChannelFieldEditor('cf')" in body, \
        "openCustomEditModal must inject cf-grid via renderChannelFieldEditor('cf')"


def test_openPseudoModal_calls_helper_with_add_opts():
    src = _read(HTML_PATH)
    fn = src.find('function openPseudoModal(')
    assert fn != -1
    body = src[fn: fn + 2000]
    assert "renderChannelFieldEditor('pf'" in body, \
        "openPseudoModal must inject pf-grid via renderChannelFieldEditor('pf', ...)"
    assert 'showEnabledHelp' in body or 'showEnabledHelp: true' in body, \
        "openPseudoModal should pass showEnabledHelp:true for the disable-warning help text"
    assert 'uploadByPendingId' in body, \
        "openPseudoModal should pass uploadByPendingId:true so the 📤 button routes to /upload.json pending_id branch"


def test_openPseudoModal_resets_draft_and_pending_id():
    src = _read(HTML_PATH)
    fn = src.find('function openPseudoModal(')
    body = src[fn: fn + 2000]
    assert 'PENDING_NEW_CHANNEL_DRAFT' in body
    assert 'CURRENT_PENDING_ID' in body
    assert "'add_'" in body, "pending_id must use the 'add_<ts>_<rand>' shape per the plan"


def test_addPseudoChannel_reads_all_9_fields():
    src = _read(HTML_PATH)
    fn = src.find('function addPseudoChannel(')
    if fn == -1:
        fn = src.find('async function addPseudoChannel(')
    assert fn != -1
    body = src[fn: fn + 2500]
    # Every pf-* input must be read; rules buffer must be spread-copied.
    for suffix in ('name', 'number', 'group', 'logo', 'path', 'catchup', 'radio', 'favorite', 'enabled'):
        assert "$('pf-%s')" % suffix in body, \
            "addPseudoChannel must read $('pf-%s')" % suffix
    assert 'rules:' in body and 'PENDING_NEW_CHANNEL_DRAFT' in body, \
        "addPseudoChannel must include rules: { ...PENDING_NEW_CHANNEL_DRAFT.rules } in the staged entry"


def test_pseudo_modal_has_rules_button():
    src = _read(HTML_PATH)
    assert 'id="pf-rules-btn"' in src, "pseudo-modal must contain a #pf-rules-btn button"


def test_pseudo_modal_has_pending_id_hidden_input():
    src = _read(HTML_PATH)
    assert 'id="pf-pending-id"' in src, "pseudo-modal must contain a #pf-pending-id hidden input"


def test_pf_rules_btn_opens_transient_rules_modal():
    src = _read(HTML_PATH)
    # The click handler must call openRulesModal(null, PENDING_NEW_CHANNEL_DRAFT).
    assert re.search(
        r"\$\('pf-rules-btn'\)\?.addEventListener\('click',\s*\(\)\s*=>\s*\{\s*openRulesModal\(null,\s*PENDING_NEW_CHANNEL_DRAFT\)",
        src,
    ), "pf-rules-btn must wire to openRulesModal(null, PENDING_NEW_CHANNEL_DRAFT)"


def test_openRulesModal_accepts_transient_buffer():
    src = _read(HTML_PATH)
    fn = src.find('async function openRulesModal(')
    assert fn != -1
    sig = src[fn: fn + 100]
    assert 'transientBuffer' in sig, \
        "openRulesModal signature must accept a transientBuffer param"


def test_stageRulesEdit_writes_to_transient_buffer():
    src = _read(HTML_PATH)
    fn = src.find('function stageRulesEdit(')
    assert fn != -1
    body = src[fn: fn + 2500]
    assert 'RULES_TRANSIENT_BUFFER' in body, \
        "stageRulesEdit must check RULES_TRANSIENT_BUFFER for the Add-flow branch"
    assert 'updateRulesButtonLabel' in body, \
        "stageRulesEdit's transient branch must refresh the Add modal's button label"


def test_rules_modal_overlay_zindex_bumped():
    src = _read(HTML_PATH)
    assert re.search(r'#rules-modal-overlay\s*\{\s*z-index:\s*9100\b', src), \
        "rules-modal-overlay must have z-index:9100 so it stacks above other modals"


def test_escape_handler_closes_rules_first_returns():
    """When rules-modal is stacked over the Add modal (transient mode),
    Escape should close ONLY the rules-modal and leave Add open. The
    handler must early-return after closing rules-modal."""
    src = _read(HTML_PATH)
    # Find the keydown Escape handler.
    fn = src.find("if (ev.key !== 'Escape') return;")
    assert fn != -1
    body = src[fn: fn + 1500]
    # The rules-modal close must appear FIRST + have an early return.
    rules_pos  = body.find("rules-modal-overlay')")
    pseudo_pos = body.find("pseudo-modal-overlay')")
    assert rules_pos != -1 and pseudo_pos != -1
    assert rules_pos < pseudo_pos, \
        "Escape handler must check rules-modal BEFORE pseudo-modal so stacked rules closes first"
    # Confirm the early return is in the rules branch.
    rules_block = body[rules_pos: pseudo_pos]
    assert 'return' in rules_block, \
        "Escape handler's rules-modal branch must early-return to leave Add modal open"


def test_upload_logo_handler_accepts_pending_id_source():
    src = _read(HTML_PATH)
    # The handler reads btn.dataset.pendingIdSource.
    fn = src.find("data-action=\"upload-logo\"")
    # find the JS handler block (not the HTML markup) by searching for the listener
    fn = src.find("addEventListener('click', async (ev) => {", fn)
    assert fn != -1
    body = src[fn: fn + 4000]
    assert 'pendingIdSource' in body, \
        "upload-logo click handler must read data-pending-id-source"
    assert 'pending_id' in body, \
        "upload-logo click handler must POST pending_id in the body when present"
