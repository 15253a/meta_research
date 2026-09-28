"""Checkpoint discovery retains each producer's exact declared directory."""
from collections import Counter

from test_target_subject_artifact_boundaries import _discover


def test_nested_fold_checkpoints_remain_individually_addressable(tmp_path):
    paths = [
        f'outputs/checkpoints/ahepa_pair/seed_{seed}_fold_{fold}'
        for seed in (15, 42, 100) for fold in range(1, 6)
    ]
    for path in paths:
        directory = tmp_path / path
        directory.mkdir(parents=True)
        (directory / 'model.bin').write_bytes(path.encode())
        (directory / 'state.json').write_text('{"completed": true}')
    sibling = tmp_path / 'outputs/checkpoints/ahepa_pair/README.txt'
    sibling.write_text('Shared provenance, not one fold checkpoint.')
    document = {'metrics': {}, 'formal_runs': [
        {'run_key': f'fold-{index}', 'checkpoint_paths': [path]}
        for index, path in enumerate(paths)
    ]}

    handoff = _discover(tmp_path, document)

    selected = [a.relative_path for a in handoff.artifacts if a.role == 'checkpoint']
    assert set(paths) <= set(selected)
    assert 'outputs/checkpoints/ahepa_pair' not in selected
    actual_files = Counter(
        str(file.relative_to(tmp_path))
        for path in selected
        for file in ((tmp_path / path).rglob('*') if (tmp_path / path).is_dir() else [tmp_path / path])
        if file.is_file()
    )
    expected_files = Counter(str(file.relative_to(tmp_path))
        for file in (tmp_path / 'outputs/checkpoints').rglob('*') if file.is_file())
    assert actual_files == expected_files


def test_unselected_checkpoint_collection_retains_existing_boundary(tmp_path):
    directory = tmp_path / 'outputs/checkpoints/collection'
    directory.mkdir(parents=True)
    (directory / 'model.bin').write_bytes(b'checkpoint')
    document = {'metrics': {}, 'formal_runs': [{'run_key': 'stateless', 'checkpoint_paths': []}]}

    handoff = _discover(tmp_path, document)

    assert [a.relative_path for a in handoff.artifacts if a.role == 'checkpoint'] == [
        'outputs/checkpoints/collection']
