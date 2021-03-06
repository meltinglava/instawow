from pathlib import Path

import pytest

from instawow.manager import find_base_dirs, should_extract
from instawow.utils import TocReader, bucketise, merge_intersecting_sets, tabulate


@pytest.fixture
def fake_addon():
    yield Path(__file__).parent / 'fixtures' / 'FakeAddon'


def test_find_base_dirs_can_find_explicit_dirs():
    assert find_base_dirs(['b/', 'b/b.toc']) == {'b'}


def test_find_base_dirs_can_find_implicit_dirs():
    assert find_base_dirs(['b/b.toc']) == {'b'}


def test_find_base_dirs_discards_resource_forks():
    assert find_base_dirs(['b/', '__MACOSX/']) == {'b'}


def test_find_base_dirs_discards_files_in_root():
    assert find_base_dirs(['a', 'b/b.toc', 'c/']) == {'b', 'c'}


def test_should_extract_discards_names_with_prefix_not_in_dirs():
    is_member = should_extract({'b'})
    assert list(map(is_member, ['a/', 'b/', 'aa/', 'bb/'])) == [False, True, False, False]


def test_loading_toc_from_path(fake_addon):
    TocReader.from_path(fake_addon / 'FakeAddon.toc')
    with pytest.raises(FileNotFoundError):
        TocReader.from_path(fake_addon / 'MissingToc.toc')

    TocReader.from_path_name(fake_addon)
    with pytest.raises(FileNotFoundError):
        TocReader.from_path_name(fake_addon.parent / 'MissingAddon')


def test_parsing_toc_entries(fake_addon):
    toc_reader = TocReader.from_path_name(fake_addon)
    assert toc_reader.entries == {'Normal': 'Normal entry',
                                  'Compact': 'Compact entry',}


def test_indexing_toc_entries(fake_addon):
    toc_reader = TocReader.from_path_name(fake_addon)
    assert toc_reader['Normal'] == ('Normal', 'Normal entry')
    assert toc_reader['Compact'] == ('Compact', 'Compact entry')
    assert toc_reader['Indented'] == ('Indented', '')
    assert toc_reader['Comment'] == ('Comment', '')
    assert toc_reader['Nonexistent'] == ('Nonexistent', '')


def test_multiindexing_toc_entries(fake_addon):
    toc_reader = TocReader.from_path_name(fake_addon)
    assert toc_reader['Normal', 'Compact'] == ('Normal', 'Normal entry')
    assert toc_reader['Compact', 'Normal'] == ('Compact', 'Compact entry')
    assert toc_reader['Indented', 'Normal'] == ('Normal', 'Normal entry')
    assert toc_reader['Nonexistent', 'Indented', 'Normal'] == ('Normal', 'Normal entry')


def test_bucketise_bucketises_by_putting_things_in_a_bucketing_bucket():
    assert bucketise(iter([1, 1, 0, 1]), bool) == {True: [1, 1, 1], False: [0]}


def test_tabulate(fake_addon):
    toc_reader = TocReader.from_path_name(fake_addon)
    data = [('key', 'value'), *toc_reader.entries.items()]
    assert tabulate(data) == '''\
  key       value    \n\
------- -------------
Normal  Normal entry \n\
Compact Compact entry\
'''


def test_merge_intersecting_sets_in_noncontiguous_collection():
    collection = [{'a'},
                  {'b', 'c'},
                  {'a', 'd'},
                  {'e'},
                  {'c', 'f'},
                  {'g', 'a'},]
    output = [{'a', 'd', 'g'},
              {'b', 'c', 'f'},
              {'e'},]
    assert sorted(merge_intersecting_sets(collection)) == output
