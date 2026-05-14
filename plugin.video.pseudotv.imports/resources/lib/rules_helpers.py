# -*- coding: utf-8 -*-
"""Rule-payload normalization + validation (imports.40).

Extracted from `server.py:/api/channels/rules.json` so the same logic can
be reused by `server.py:/channels/add.json` when an operator creates a
new Custom channel with rules pre-configured via the Add modal's Rules
button (the imports.40 transient-buffer flow).

The function-local `RulesList` import keeps the module cheap to load in
tests — tests can either pass an explicit `catalog` dict (no Kodi
context needed) or let the helper auto-build it from `RulesList().allRules()`
in the live server context. Mirrors the imports.29 filter_helpers /
imports.32 sysinfo_helpers / imports.33 cleanup_helpers / imports.36
logo_helpers precedent.
"""


def _normalizeRules(rules, catalog=None):
    """Normalize + validate a rules payload from an untrusted client.

    Args:
        rules : dict from the request body, shaped like
                ``{rule_id: {'values': {idx: value}}}``. JSON wire
                format means rule_id and idx arrive as strings; this
                helper coerces them to int.
        catalog : optional dict ``{int: rule_instance}`` of known rules.
                  When None, builds the catalog by calling
                  ``RulesList().allRules()`` (live-server path).
                  Tests can inject a stub catalog to avoid Kodi context.

    Returns:
        ``(normalized, rejected)`` tuple.

        * ``normalized`` is a dict ``{int_rule_id: {'values': {int_idx: value}}}``
          ready for ``citem['rules'] = normalized``.
        * ``rejected`` is a list of dicts describing each entry that
          failed validation, with ``rule_id`` (best-effort) and
          ``reason`` keys; the endpoint surfaces this list to the
          client so the dashboard can show which rules were dropped.

    Empty / non-dict ``rules`` input returns ``({}, [])`` — no-op for
    "operator didn't configure any rules" path.

    Mirrors the EXACT validation behavior at server.py:924-942
    (imports.39 baseline): unknown rule_ids rejected, non-int rule_ids
    rejected, non-dict blocks rejected, non-int idx within values
    rejected. Other-keys-besides-'values' inside a rule block are
    silently dropped (matches pre-extraction behavior).
    """
    if not isinstance(rules, dict) or not rules:
        return {}, []

    if catalog is None:
        from rules import RulesList
        catalog = {r.myId: r for r in RulesList().allRules()}

    rejected = []
    normalized = {}

    for raw_rid, raw_block in rules.items():
        try:
            rid = int(raw_rid)
        except (TypeError, ValueError):
            rejected.append({'rule_id': raw_rid, 'reason': 'rule_id must be int'})
            continue
        if rid not in catalog:
            rejected.append({'rule_id': rid, 'reason': 'unknown rule id'})
            continue
        if not isinstance(raw_block, dict):
            rejected.append({'rule_id': rid, 'reason': 'rule block must be object'})
            continue

        raw_values = raw_block.get('values')
        values = raw_values if isinstance(raw_values, dict) else {}

        norm_values = {}
        for raw_idx, v in values.items():
            try:
                norm_values[int(raw_idx)] = v
            except (TypeError, ValueError):
                rejected.append({'rule_id': rid, 'idx': raw_idx, 'reason': 'idx must be int'})
                continue

        normalized[rid] = {'values': norm_values}

    return normalized, rejected
