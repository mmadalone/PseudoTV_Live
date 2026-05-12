# -*- coding: utf-8 -*-
# Step 6 / Phase 1 — settings.xml + strings.po → JSON-ready schema.
#
# Pure-Python module. No Kodi imports. Caller injects:
#   - settings_reader  : object with getSetting / getSettingBool / getSettingInt
#                        / getSettingString (the SETTINGS singleton in this
#                        codebase satisfies the duck-type).
#   - infobool_resolver: callable(list[str]) -> dict[str, bool]
#                        (e.g. JSONRPC().getInfoBooleans). Called ONCE per
#                        get_schema invocation with all unfresh InfoBool
#                        expressions. Batched to one JSON-RPC roundtrip on
#                        Kodi (XBMC.GetInfoBooleans accepts a list).
#                        Results are cached for INFOBOOL_TTL_SECONDS.
#
# get_schema(...) returns a dict ready for json.dumps:
#   {
#     "section_id": "plugin.video.pseudotv.imports",
#     "parsed_at": <unix ts>,
#     "categories": [
#       { "id": "...", "label": "...", "help": "...",
#         "groups": [
#           { "id": "1", "label": "...",
#             "settings": [<setting dict>, ...] }, ...] }, ...] }
#
# Setting dict shape:
#   { "id", "type", "label", "help", "level", "parent",
#     "default", "constraints" (dict), "control" (dict),
#     "data" (str|None for action/button-action),
#     "dependencies": [ { "kind": "visible"|"enable", "tree": <cond> }, ...],
#     "current": <type-coerced current value> }
#
# Condition tree shape (recursive):
#   leaf  : { "kind": "setting",  "operator": "is"|"!is"|"gt"|"lt",
#             "setting": "X", "value": "Y" }
#   leaf  : { "kind": "infobool", "expr": "...", "value": true|false|null }
#   inner : { "kind": "and"|"or", "children": [<cond>, ...] }

import os
import re
import time
import xml.etree.ElementTree as ET


INFOBOOL_TTL_SECONDS = 30
_SCHEMA_CACHE   = {}   # xml_path -> (mtime, parsed dict)
_STRINGS_CACHE  = {}   # po_path  -> (mtime, dict[int, str])
_INFOBOOL_CACHE = {}   # expr     -> (timestamp, bool)

_PO_RE = re.compile(
    r'msgctxt\s+"#(\d+)"\s*\n\s*msgid\s+"((?:\\.|[^"\\])*)"',
    re.MULTILINE,
)


def clear_caches():
    _SCHEMA_CACHE.clear()
    _STRINGS_CACHE.clear()
    _INFOBOOL_CACHE.clear()


# ---------------------------------------------------------------- strings.po

def parse_strings(po_path):
    """Returns {numeric_id (int): translated text (str)}. Cached by mtime."""
    try:
        mtime = os.path.getmtime(po_path)
    except OSError:
        return {}
    cached = _STRINGS_CACHE.get(po_path)
    if cached and cached[0] == mtime:
        return cached[1]
    out = {}
    try:
        with open(po_path, 'r', encoding='utf-8') as f:
            raw = f.read()
    except OSError:
        return {}
    for m in _PO_RE.finditer(raw):
        sid = int(m.group(1))
        text = m.group(2).replace('\\"', '"').replace('\\n', '\n').replace('\\\\', '\\')
        out[sid] = text
    _STRINGS_CACHE[po_path] = (mtime, out)
    return out


def _resolve_label(strings, ref):
    """A label ref is usually a numeric string id ("30012"). Pass non-numeric through."""
    if ref is None or ref == '':
        return ''
    try:
        return strings.get(int(ref), ref)
    except (TypeError, ValueError):
        return ref


# ---------------------------------------------------------------- settings.xml

def _text(elem):
    if elem is None:
        return None
    t = elem.text
    return t.strip() if t else None


def _parse_constraints(elem, strings):
    """elem = <constraints> subtree or None."""
    out = {}
    if elem is None:
        return out
    for child in elem:
        tag = child.tag
        if tag in ('minimum', 'maximum', 'step'):
            try:    out[tag] = int(_text(child))
            except (TypeError, ValueError):
                out[tag] = _text(child)
        elif tag == 'allowempty':
            out['allowempty'] = (_text(child) or '').lower() == 'true'
        elif tag == 'writable':
            out['writable']   = (_text(child) or '').lower() == 'true'
        elif tag == 'addontype':
            out['addontype']  = _text(child) or ''
        elif tag == 'sources':
            out['sources']    = [(_text(s) or '') for s in child.findall('source')]
        elif tag == 'options':
            opts = []
            for opt in child.findall('option'):
                opts.append({
                    'label': _resolve_label(strings, opt.get('label')),
                    'value': _text(opt),
                })
            out['options'] = opts
    return out


def _parse_control(elem):
    """elem = <control> subtree or None."""
    if elem is None:
        return {}
    out = {
        'type'  : elem.get('type'),
        'format': elem.get('format'),
    }
    # Optional sub-tags that some controls carry.
    for tag in ('popup', 'close', 'heading', 'multiselect'):
        sub = elem.find(tag)
        if sub is not None:
            txt = _text(sub)
            if txt is None:
                continue
            if txt.lower() in ('true', 'false'):
                out[tag] = (txt.lower() == 'true')
            else:
                out[tag] = txt
    # <show more="true" details="true">installed</show> — capture for list[addon] UIs.
    show = elem.find('show')
    if show is not None:
        out['show'] = {
            'more'   : (show.get('more')    or '').lower() == 'true',
            'details': (show.get('details') or '').lower() == 'true',
            'filter' : _text(show) or '',
        }
    return out


def _parse_condition(cond):
    """cond = <condition> element → leaf dict."""
    on = (cond.get('on') or '').lower()
    if on == 'property' and (cond.get('name') or '').lower() == 'infobool':
        return {
            'kind' : 'infobool',
            'expr' : (_text(cond) or ''),
            'value': None,   # filled by _resolve_infobools_in_tree
        }
    return {
        'kind'    : 'setting',
        'operator': cond.get('operator') or 'is',
        'setting' : cond.get('setting')  or '',
        'value'   : _text(cond) if _text(cond) is not None else '',
    }


def _parse_logical(elem):
    """elem = <and>/<or>/<condition>; returns normalized cond tree."""
    if elem.tag == 'condition':
        return _parse_condition(elem)
    if elem.tag in ('and', 'or'):
        children = [_parse_logical(c) for c in elem if c.tag in ('and', 'or', 'condition')]
        return {'kind': elem.tag, 'children': children}
    return None


def _parse_dependencies(elem):
    """elem = <dependencies> subtree; returns list[{kind, tree}]."""
    if elem is None:
        return []
    out = []
    for dep in elem.findall('dependency'):
        kind = dep.get('type') or 'visible'
        # A <dependency> has exactly one of: <and>, <or>, or one direct <condition>.
        child_tree = None
        for sub in dep:
            if sub.tag in ('and', 'or', 'condition'):
                child_tree = _parse_logical(sub)
                break
        if child_tree is not None:
            out.append({'kind': kind, 'tree': child_tree})
    return out


def _parse_setting(elem, strings):
    out = {
        'id'    : elem.get('id'),
        'type'  : elem.get('type'),
        'label' : _resolve_label(strings, elem.get('label')),
        'help'  : _resolve_label(strings, elem.get('help')),
        'parent': elem.get('parent'),
    }
    level_text = _text(elem.find('level'))
    try:    out['level'] = int(level_text) if level_text is not None else 0
    except ValueError:
        out['level'] = 0
    # <default/> (self-closing) → None; <default>X</default> → "X".
    default_elem = elem.find('default')
    out['default'] = _text(default_elem) if default_elem is not None else None
    out['constraints']  = _parse_constraints(elem.find('constraints'), strings)
    out['control']      = _parse_control(elem.find('control'))
    out['dependencies'] = _parse_dependencies(elem.find('dependencies'))
    # <data> can live as a direct child (type="action") OR under <control> (button format="action").
    data_elem = elem.find('data')
    if data_elem is None:
        ctl = elem.find('control')
        if ctl is not None:
            data_elem = ctl.find('data')
    out['data'] = _text(data_elem) if data_elem is not None else None
    return out


def parse_settings(xml_path, po_path=None):
    """Returns {section_id, categories: [...]} skeleton. Cached by (xml mtime, po mtime).

    Current value + InfoBool resolution NOT included — get_schema() enriches.

    If po_path is None, attempts the conventional location next to xml_path:
        <xml_path dir>/language/resource.language.en_gb/strings.po
    """
    try:
        xml_mtime = os.path.getmtime(xml_path)
    except OSError:
        return {'section_id': '', 'categories': []}
    if po_path is None:
        po_path = os.path.join(os.path.dirname(xml_path),
                               'language', 'resource.language.en_gb', 'strings.po')
    try:    po_mtime = os.path.getmtime(po_path)
    except OSError:
        po_mtime = 0
    cache_key = (xml_path, po_path)
    cached = _SCHEMA_CACHE.get(cache_key)
    if cached and cached[0] == (xml_mtime, po_mtime):
        return cached[1]
    strings = parse_strings(po_path)
    try:
        root = ET.parse(xml_path).getroot()
    except (ET.ParseError, OSError):
        return {'section_id': '', 'categories': []}
    section = root.find('section')
    if section is None:
        return {'section_id': '', 'categories': []}
    categories = []
    for cat in section.findall('category'):
        cat_out = {
            'id'    : cat.get('id'),
            'label' : _resolve_label(strings, cat.get('label')),
            'help'  : _resolve_label(strings, cat.get('help')),
            'groups': [],
        }
        for grp in cat.findall('group'):
            grp_out = {
                'id'      : grp.get('id'),
                'label'   : _resolve_label(strings, grp.get('label')),
                'settings': [],
            }
            for s in grp.findall('setting'):
                grp_out['settings'].append(_parse_setting(s, strings))
            cat_out['groups'].append(grp_out)
        categories.append(cat_out)
    parsed = {
        'section_id': section.get('id') or '',
        'categories': categories,
    }
    _SCHEMA_CACHE[cache_key] = ((xml_mtime, po_mtime), parsed)
    return parsed


# ---------------------------------------------------------------- enrichment

def _walk_infobools(tree, sink):
    """Collect every infobool leaf in `tree` into list `sink` (mutates in place)."""
    if tree is None:
        return
    if tree.get('kind') == 'infobool':
        sink.append(tree)
    elif tree.get('kind') in ('and', 'or'):
        for child in tree.get('children') or []:
            _walk_infobools(child, sink)


def _resolve_infobools_batch(exprs, resolver):
    """Returns {expr: bool} for every expr in `exprs`. Honors a 30-second TTL
    cache; only exprs whose cache is empty/stale are passed to `resolver` in a
    single batched call. `resolver` signature: list[str] -> dict[str, bool].
    """
    now = time.time()
    out = {}
    stale = []
    for expr in exprs:
        cached = _INFOBOOL_CACHE.get(expr)
        if cached and (now - cached[0]) < INFOBOOL_TTL_SECONDS:
            out[expr] = cached[1]
        else:
            stale.append(expr)
    if stale:
        try:
            resolved = resolver(stale) or {}
        except Exception:
            resolved = {}
        for expr in stale:
            val = bool(resolved.get(expr, False))
            out[expr] = val
            _INFOBOOL_CACHE[expr] = (now, val)
    return out


def _current_value(settings_reader, setting):
    """Use the right typed getter per setting.type. None for actions."""
    sid, stype = setting['id'], setting['type']
    if stype == 'action' or not sid:
        return None
    try:
        if stype == 'boolean':
            return settings_reader.getSettingBool(sid)
        if stype == 'integer':
            return settings_reader.getSettingInt(sid)
        if stype and stype.startswith('list'):
            # Kodi's typed getSettingString returns '' for list-flavored addon
            # settings (e.g. list[addon] panels). Plain getSetting reads the raw
            # pipe-joined string the addon actually stores — matches existing
            # callers (resources.py:189, kodi.py:286, builder.py:121).
            raw = settings_reader.getSetting(sid) or ''
            return [s for s in raw.split('|') if s]
        return settings_reader.getSettingString(sid) or ''
    except Exception:
        return None


def get_schema(xml_path, po_path=None, settings_reader=None, infobool_resolver=None):
    """Returns the parse_settings() tree with each setting enriched with
    `current` and every infobool leaf resolved to True/False.

    Either dependency parameter may be omitted (useful for tests): in that
    case `current` stays None and infobool `value` stays None.
    """
    schema = parse_settings(xml_path, po_path)
    bool_sinks = []
    for cat in schema.get('categories', []):
        for grp in cat.get('groups', []):
            for setting in grp.get('settings', []):
                if settings_reader is not None:
                    setting['current'] = _current_value(settings_reader, setting)
                else:
                    setting.setdefault('current', None)
                for dep in setting.get('dependencies', []):
                    _walk_infobools(dep.get('tree'), bool_sinks)
    if infobool_resolver is not None:
        # Batched: one JSON-RPC call per get_schema, not per unique expr.
        unique = sorted({leaf['expr'] for leaf in bool_sinks if leaf.get('expr')})
        resolved = _resolve_infobools_batch(unique, infobool_resolver)
        for leaf in bool_sinks:
            leaf['value'] = resolved.get(leaf.get('expr'))
    schema['parsed_at'] = int(time.time())
    return schema
