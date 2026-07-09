"""Tests del análisis de drawdown (puro, offline)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from umbra.research.drawdown import (
    drawdown_series,
    find_drawdown_episodes,
    summarize_drawdown,
)
from umbra.research.series import PricePoint

BASE = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)


def _series(values: list[float]) -> list[PricePoint]:
    return [PricePoint(ts=BASE + timedelta(minutes=i), value=v) for i, v in enumerate(values)]


def test_drawdown_series_basic():
    # sube a 1.0, cae a 0.6 (40% dd), recupera
    dd = drawdown_series(_series([0.5, 1.0, 0.8, 0.6, 1.0]))
    assert dd[0] == pytest.approx(0.0)
    assert dd[1] == pytest.approx(0.0)  # nuevo pico
    assert dd[2] == pytest.approx(0.2)  # 0.8 vs pico 1.0
    assert dd[3] == pytest.approx(0.4)  # 0.6 vs pico 1.0
    assert dd[4] == pytest.approx(0.0)  # recuperó


def test_single_episode_recovers():
    eps = find_drawdown_episodes(_series([1.0, 0.9, 0.7, 0.85, 1.1]), min_depth=0.05)
    assert len(eps) == 1
    e = eps[0]
    assert e.peak_value == pytest.approx(1.0)
    assert e.trough_value == pytest.approx(0.7)
    assert e.depth == pytest.approx(0.30)
    assert e.recovered is True
    assert e.time_to_recovery_s == pytest.approx(4 * 60)  # pico idx0 → recovery idx4


def test_unrecovered_episode_at_end():
    eps = find_drawdown_episodes(_series([1.0, 0.8, 0.6]), min_depth=0.05)
    assert len(eps) == 1
    assert eps[0].recovered is False
    assert eps[0].time_to_recovery_s is None
    assert eps[0].depth == pytest.approx(0.40)


def test_min_depth_filters_noise():
    # caída de 2% no debe contar como episodio con umbral 5%
    eps = find_drawdown_episodes(_series([1.0, 0.98, 1.0, 0.99]), min_depth=0.05)
    assert eps == []


def test_summary_picks_worst():
    pts = _series([1.0, 0.9, 1.0, 0.5, 1.0])  # dos caídas: 10% y 50%
    stats = summarize_drawdown(pts, min_depth=0.05)
    assert stats.n_episodes == 2
    assert stats.max_drawdown == pytest.approx(0.5)
    assert stats.worst_episode is not None
    assert stats.worst_episode.depth == pytest.approx(0.5)


def test_empty_and_flat_series():
    assert drawdown_series([]) == []
    assert find_drawdown_episodes(_series([0.5]), min_depth=0.05) == []
    flat = summarize_drawdown(_series([0.5, 0.5, 0.5]))
    assert flat.n_episodes == 0
    assert flat.max_drawdown == pytest.approx(0.0)
