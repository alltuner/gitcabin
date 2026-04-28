# ABOUTME: Tests for the monotonic id counter backed by refs/meta/counters.
# ABOUTME: Allocation uses CAS on git update-ref so concurrent writers are safe.

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gitcabin.storage.counter import Counter
from gitcabin.storage.repo import BareRepo


@pytest.fixture
def repo(tmp_path: Path) -> BareRepo:
    return BareRepo.open_or_init(tmp_path / "owner" / "name.git")


def test_first_allocation_returns_one(repo: BareRepo) -> None:
    counter = Counter(repo, "issues")
    assert counter.next() == 1


def test_allocations_are_monotonic(repo: BareRepo) -> None:
    counter = Counter(repo, "issues")
    assert [counter.next() for _ in range(5)] == [1, 2, 3, 4, 5]


def test_separate_counters_dont_collide(repo: BareRepo) -> None:
    issues = Counter(repo, "issues")
    prs = Counter(repo, "prs")
    assert issues.next() == 1
    assert prs.next() == 1
    assert issues.next() == 2


def test_counter_state_persists_in_refs_meta_counters(repo: BareRepo) -> None:
    counter = Counter(repo, "issues")
    counter.next()
    counter.next()
    # The ref must exist and the counter's value must be readable from the
    # tree at that ref's commit. We're not just keeping state in memory.
    out = repo.run_git("cat-file", "-p", "refs/meta/counters:issues").strip()
    assert out == "2"


def test_concurrent_allocations_are_unique(repo: BareRepo) -> None:
    # Eight threads each allocating ten ids; CAS must guarantee no duplicates
    # and no gaps. If the retry loop is wrong we'd see either repeats (bad)
    # or fewer than 80 successful allocations (also bad).
    counter = Counter(repo, "issues")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: counter.next(), range(80)))
    assert sorted(results) == list(range(1, 81))
