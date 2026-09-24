import hashlib
import json

import pytest

from meta_research import bundle_target_contract as contract


@pytest.fixture(autouse=True)
def clear_success_cache():
    cached = getattr(contract, '_validate_small_frozen_domain_document', None)
    if cached is not None:
        cached.cache_clear()
    yield
    if cached is not None:
        cached.cache_clear()


def count_parses(monkeypatch):
    calls = []
    original = contract.json.loads

    def counted(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    monkeypatch.setattr(contract.json, 'loads', counted)
    return calls


def validate(canonical, name='domain'):
    return contract._validate_frozen_domain_document(contract.FrozenJsonObject(canonical), name)


def test_repeat_small_values_reuse_success_by_full_value_and_name(monkeypatch):
    calls = count_parses(monkeypatch)
    validate('{"safe":1}')
    validate('{"safe":1}')
    assert len(calls) == 1
    validate('{"safe":2}')
    validate('{"safe":1}', 'another domain')
    assert len(calls) == 3


def test_canonical_hash_serializes_once_and_preserves_digest(monkeypatch):
    document = {'unicode': '研究', 'nested': [True, None, 1, 1.5, {'z': 'é'}]}
    expected = hashlib.sha256(json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
    calls = []
    original = contract._canonical_json

    def counted(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(contract, '_canonical_json', counted)
    assert contract._canonical_hash(document, 'document') == expected
    assert len(calls) == 1


@pytest.mark.parametrize('canonical', [
    '{bad', '[]', '{}', '{"safe": 1}', '{"command":"run"}',
    '{"nested":[{"Runtime-Binding":1}]}', '{"safe":NaN}',
    '{"safe":"\\ud800"}', [], 1, b'{"safe":1}',
])
def test_invalid_frozen_values_are_rejected_each_time_without_caching_failure(canonical, monkeypatch):
    calls = count_parses(monkeypatch)
    for _ in range(2):
        with pytest.raises(contract.BundleTargetContractError):
            validate(canonical)
    assert len(calls) == 2
    assert contract._validate_small_frozen_domain_document.cache_info().currsize == 0


def test_exact_frozen_type_is_checked_even_after_same_value_succeeded():
    validate('{"safe":1}')

    class FrozenSubclass(contract.FrozenJsonObject):
        pass

    for value in (object(), FrozenSubclass('{"safe":1}')):
        with pytest.raises(contract.BundleTargetContractError, match='domain_invalid'):
            contract._validate_frozen_domain_document(value, 'domain')


@pytest.mark.parametrize('bad_value', ['{"command":"run"}', '{bad', []])
def test_forced_mutation_of_successful_object_still_revalidates(bad_value):
    value = contract.FrozenJsonObject('{"safe":1}')
    contract._validate_frozen_domain_document(value, 'domain')
    object.__setattr__(value, 'canonical_json', bad_value)
    with pytest.raises(contract.BundleTargetContractError):
        contract._validate_frozen_domain_document(value, 'domain')


def test_as_dict_always_returns_fresh_nested_values():
    value = contract.FrozenJsonObject('{"nested":{"items":[1]}}')
    contract._validate_frozen_domain_document(value, 'domain')
    first = value.as_dict()
    first['nested']['items'].append(2)
    first['command'] = 'not-in-original'
    assert value.as_dict() == {'nested': {'items': [1]}}
    contract._validate_frozen_domain_document(value, 'domain')


def sized_document(length):
    chunks = ['x' * 4000] * 4 + ['']
    baseline = json.dumps({'chunks': chunks}, separators=(',', ':'))
    chunks[-1] = 'x' * (length - len(baseline))
    value = json.dumps({'chunks': chunks}, separators=(',', ':'))
    assert len(value) == length
    return value


@pytest.mark.parametrize(('length', 'name_length', 'expected_parses'), [
    (16384, 128, 1), (16385, 128, 2), (16384, 129, 2),
])
def test_cache_size_gates_bypass_large_values_and_names(length, name_length, expected_parses, monkeypatch):
    calls = count_parses(monkeypatch)
    value = sized_document(length)
    validate(value, 'n' * name_length)
    validate(value, 'n' * name_length)
    assert len(calls) == expected_parses


def test_non_exact_strings_and_unhashable_names_follow_uncached_path(monkeypatch):
    class UnhashableString(str):
        __hash__ = None

    calls = count_parses(monkeypatch)
    for value, name in [
        ('{"safe":1}', []), ('{"safe":1}', UnhashableString('domain')),
        (UnhashableString('{"safe":1}'), 'domain'),
    ]:
        validate(value, name)
        validate(value, name)
    assert len(calls) == 6
    assert contract._validate_small_frozen_domain_document.cache_info().currsize == 0


def test_success_cache_evicts_old_values_at_256_entries(monkeypatch):
    calls = count_parses(monkeypatch)
    for number in range(258):
        validate('{"safe":' + str(number) + '}')
    assert len(calls) == 258
    assert contract._validate_small_frozen_domain_document.cache_info().currsize == 256
    validate('{"safe":257}')
    assert len(calls) == 258
    validate('{"safe":0}')
    assert len(calls) == 259


@pytest.mark.parametrize('limit', ['depth', 'string', 'nodes', 'bytes'])
def test_existing_json_limits_remain_enforced(limit):
    if limit == 'depth':
        value = 0
        for _ in range(66):
            value = {'nested': value}
    elif limit == 'string':
        value = {'text': 'x' * (contract.BUNDLE_PROJECTION_STRING_MAX_UTF8_BYTES + 1)}
    elif limit == 'nodes':
        value = {'rows': [[0] * 1024] * 64}
    else:
        value = {'rows': [['x' * 4000] * 1024] * 3}
    with pytest.raises(contract.BundleTargetContractError):
        validate(json.dumps(value, separators=(',', ':')))


@pytest.mark.parametrize('value', [{1: 'bad'}, {'x': float('inf')}, {'x': b'bad'}, {'x': object()}])
def test_digest_reuse_keeps_canonical_validation(value):
    with pytest.raises(contract.BundleTargetContractError):
        contract._canonical_hash(value, 'document')
