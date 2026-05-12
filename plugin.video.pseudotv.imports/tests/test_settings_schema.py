"""Unit tests for settings_schema (Step 6 / Phase 1).

Covers:
- strings.po regex parsing (msgctxt+msgid pairs, escaped quotes)
- settings.xml → category/group/setting tree shape
- Label/help numeric-id resolution + passthrough for missing strings
- Type-specific shapes: boolean, integer (with options vs min/max/step),
  string, path, action, list[addon]
- Dependency normalization (visible/enable, AND/OR, setting vs InfoBool conds)
- Implicit AND when one setting has multiple <dependency> blocks
- <data> capture from both direct-child and under-<control> positions
- Current-value enrichment via injected settings_reader (typed dispatch)
- InfoBool resolver wiring + 30s TTL cache (deduplicated by expr)
- File-mtime cache invalidation
"""
import json
import os
import time

import pytest

# conftest.py adds KODI_STUBS + ADDON_LIB to sys.path before collection.
import settings_schema
from conftest import FIXTURES_DIR


XML_PATH = os.path.join(FIXTURES_DIR, 'sample_settings.xml')
PO_PATH  = os.path.join(FIXTURES_DIR, 'sample_strings.po')


@pytest.fixture(autouse=True)
def _clear_caches():
    settings_schema.clear_caches()
    yield
    settings_schema.clear_caches()


# -------------------------------------------------------------- strings.po

def test_strings_parser_returns_dict_keyed_by_int():
    strings = settings_schema.parse_strings(PO_PATH)
    assert isinstance(strings, dict)
    assert strings[30011] == 'Enable Foo'
    assert strings[30014] == 'Off'
    assert strings[33000] == 'General category help'


def test_strings_parser_handles_escaped_quotes():
    strings = settings_schema.parse_strings(PO_PATH)
    assert '"test"' in strings[33032]


def test_strings_parser_missing_file_returns_empty_dict():
    assert settings_schema.parse_strings('/nonexistent/strings.po') == {}


# -------------------------------------------------------------- shape

def test_parse_returns_top_level_keys():
    schema = settings_schema.get_schema(XML_PATH, PO_PATH)
    assert schema['section_id'] == 'test.section'
    assert 'categories' in schema
    assert 'parsed_at' in schema


def test_two_categories_in_fixture():
    schema = settings_schema.get_schema(XML_PATH, PO_PATH)
    ids = [c['id'] for c in schema['categories']]
    assert ids == ['general', 'paths']


def test_category_label_resolved_from_strings_po():
    schema = settings_schema.get_schema(XML_PATH, PO_PATH)
    general = next(c for c in schema['categories'] if c['id'] == 'general')
    assert general['label'] == 'General'
    assert general['help']  == 'General category help'


def test_category_with_empty_help_attribute_returns_empty_string():
    schema = settings_schema.get_schema(XML_PATH, PO_PATH)
    paths = next(c for c in schema['categories'] if c['id'] == 'paths')
    assert paths['help'] == ''


def _setting(schema, sid):
    for cat in schema['categories']:
        for grp in cat['groups']:
            for s in grp['settings']:
                if s['id'] == sid:
                    return s
    raise KeyError(sid)


# -------------------------------------------------------------- types

def test_boolean_setting_shape():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Enable_Foo')
    assert s['type']    == 'boolean'
    assert s['label']   == 'Enable Foo'
    assert s['help']    == 'Enable foo help'
    assert s['level']   == 0
    assert s['parent']  is None
    assert s['default'] == 'true'
    assert s['control']['type'] == 'toggle'
    assert s['dependencies'] == []
    assert s['data'] is None


def test_integer_with_min_max_step_constraints():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Foo_Limit')
    assert s['type']    == 'integer'
    assert s['parent']  == 'Enable_Foo'
    assert s['level']   == 1
    assert s['constraints'] == {'minimum': 1, 'step': 1, 'maximum': 25}
    assert s['control'] == {'type': 'slider', 'format': 'integer', 'popup': False}


def test_integer_with_options_constraints():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Foo_Mode')
    assert s['constraints']['options'] == [
        {'label': 'Off', 'value': '0'},
        {'label': 'On',  'value': '1'},
    ]


def test_path_setting_captures_sources_and_writable():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Foo_Folder')
    assert s['type'] == 'path'
    assert s['constraints']['sources']  == ['files']
    assert s['constraints']['writable'] is True


def test_list_addon_setting_captures_addontype_and_show_block():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Foo_Resources')
    assert s['type'] == 'list[addon]'
    assert s['constraints']['addontype'] == 'kodi.resource.images'
    show = s['control'].get('show')
    assert show == {'more': True, 'details': True, 'filter': 'installed'}
    assert s['control'].get('multiselect') is True


# -------------------------------------------------------------- <data>

def test_action_setting_captures_data_as_direct_child():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Launch_Foo')
    assert s['type'] == 'action'
    assert s['data'] == 'RunScript(special://home/addons/test/foo.py, run)'


def test_button_format_action_captures_data_under_control():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Foo_Gated')
    # <data> lives inside <control>, not as direct child.
    assert s['data'] == 'RunScript(special://home/addons/test/foo.py, select)'


# -------------------------------------------------------------- dependencies

def test_simple_setting_condition_normalizes_to_leaf():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Foo_Limit')
    assert len(s['dependencies']) == 1
    dep = s['dependencies'][0]
    assert dep['kind'] == 'visible'
    assert dep['tree'] == {
        'kind': 'setting',
        'operator': 'is',
        'setting': 'Enable_Foo',
        'value': 'true',
    }


def test_and_block_normalizes_to_inner_node_with_infobool_leaves():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Launch_Foo')
    assert len(s['dependencies']) == 1
    dep = s['dependencies'][0]
    assert dep['kind'] == 'enable'
    assert dep['tree']['kind'] == 'and'
    leaves = dep['tree']['children']
    assert len(leaves) == 2
    assert all(leaf['kind'] == 'infobool' for leaf in leaves)
    assert leaves[0]['expr'] == 'System.HasAddon(test.addon)'
    assert leaves[0]['value'] is None   # unresolved (no resolver passed)


def test_or_block_and_implicit_and_when_multiple_dependency_blocks():
    s = _setting(settings_schema.get_schema(XML_PATH, PO_PATH), 'Foo_Gated')
    assert len(s['dependencies']) == 2
    visible = next(d for d in s['dependencies'] if d['kind'] == 'visible')
    enable  = next(d for d in s['dependencies'] if d['kind'] == 'enable')
    assert visible['tree']['kind'] == 'or'
    assert len(visible['tree']['children']) == 2
    # Independent 'enable' dependency block — implicit AND with the 'visible' one.
    assert enable['tree']['kind'] == 'setting'
    assert enable['tree']['operator'] == '!is'


# -------------------------------------------------------------- enrichment

def test_current_value_dispatches_per_type():
    class Reader:
        def getSettingBool(self, k):   return True
        def getSettingInt(self, k):    return 13
        def getSettingString(self, k): return 'val:%s' % k
        def getSetting(self, k):       return 'res.one|res.two|%s' % k
    schema = settings_schema.get_schema(XML_PATH, PO_PATH, settings_reader=Reader())
    assert _setting(schema, 'Enable_Foo')['current']    is True
    assert _setting(schema, 'Foo_Limit')['current']     == 13
    assert _setting(schema, 'Foo_Gated')['current']     == 'val:Foo_Gated'
    assert _setting(schema, 'Foo_Folder')['current']    == 'val:Foo_Folder'
    # list[addon] → plain getSetting + split on '|' (typed getSettingString
    # returns '' for list-flavored addon settings on Kodi 21)
    assert _setting(schema, 'Foo_Resources')['current'] == ['res.one', 'res.two', 'Foo_Resources']
    # action settings have no stored value
    assert _setting(schema, 'Launch_Foo')['current']    is None


def test_current_value_swallows_reader_exceptions():
    class Reader:
        def getSettingBool(self, k):   raise RuntimeError('boom')
        def getSettingInt(self, k):    raise RuntimeError('boom')
        def getSettingString(self, k): raise RuntimeError('boom')
        def getSetting(self, k):       raise RuntimeError('boom')
    schema = settings_schema.get_schema(XML_PATH, PO_PATH, settings_reader=Reader())
    assert _setting(schema, 'Enable_Foo')['current'] is None


def test_infobool_resolver_is_called_once_with_all_unique_exprs():
    calls = []
    def resolver(exprs):
        calls.append(list(exprs))
        return {e: ('HasAddon' in e) for e in exprs}
    settings_schema.get_schema(XML_PATH, PO_PATH, infobool_resolver=resolver)
    # Single batched call with both distinct exprs from Launch_Foo.
    assert len(calls) == 1
    assert sorted(calls[0]) == sorted([
        'System.HasAddon(test.addon)',
        'System.AddonIsEnabled(test.addon)',
    ])


def test_infobool_values_attach_to_each_leaf_after_resolve():
    def resolver(exprs):
        return {e: ('HasAddon' in e) for e in exprs}
    schema = settings_schema.get_schema(XML_PATH, PO_PATH, infobool_resolver=resolver)
    deps = _setting(schema, 'Launch_Foo')['dependencies'][0]['tree']['children']
    by_expr = {leaf['expr']: leaf['value'] for leaf in deps}
    assert by_expr['System.HasAddon(test.addon)']        is True
    assert by_expr['System.AddonIsEnabled(test.addon)']  is False


def test_infobool_resolver_failure_falls_back_to_false():
    def resolver(exprs):
        raise RuntimeError('jsonrpc down')
    schema = settings_schema.get_schema(XML_PATH, PO_PATH, infobool_resolver=resolver)
    deps = _setting(schema, 'Launch_Foo')['dependencies'][0]['tree']['children']
    assert all(leaf['value'] is False for leaf in deps)


def test_infobool_results_cached_for_30s_across_calls():
    calls = []
    def resolver(exprs):
        calls.append(list(exprs))
        return {e: True for e in exprs}
    settings_schema.get_schema(XML_PATH, PO_PATH, infobool_resolver=resolver)
    n_first = len(calls)
    settings_schema.get_schema(XML_PATH, PO_PATH, infobool_resolver=resolver)
    # 2nd run: every expr is in cache, so resolver is not invoked again at all.
    assert len(calls) == n_first


def test_schema_cached_by_xml_mtime(tmp_path):
    xml_copy = tmp_path / 'settings.xml'
    po_copy  = tmp_path / 'strings.po'
    with open(XML_PATH, 'rb') as f: xml_copy.write_bytes(f.read())
    with open(PO_PATH,  'rb') as f: po_copy.write_bytes(f.read())
    first  = settings_schema.parse_settings(str(xml_copy), str(po_copy))
    second = settings_schema.parse_settings(str(xml_copy), str(po_copy))
    assert first is second   # same object — cached

    # Touching mtime forces re-parse.
    os.utime(str(xml_copy), (time.time() + 10, time.time() + 10))
    third = settings_schema.parse_settings(str(xml_copy), str(po_copy))
    assert third is not first


# -------------------------------------------------------------- safety

def test_missing_xml_returns_empty_schema():
    schema = settings_schema.get_schema('/nonexistent/settings.xml')
    assert schema['categories'] == []
    assert schema['section_id'] == ''


def test_output_is_json_serializable():
    schema = settings_schema.get_schema(XML_PATH, PO_PATH)
    # Will raise TypeError if any value isn't json-friendly.
    json.dumps(schema)
