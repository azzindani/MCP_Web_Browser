"""Verify pick_profile selects ID profile for Indonesian locale."""

from __future__ import annotations

import random

from engine.workers.fingerprint import pick_profile


def test_id_locale_returns_id_profile() -> None:
    profile = pick_profile("id-ID")
    assert profile.locale == "id-ID"
    assert profile.timezone == "Asia/Jakarta"
    assert profile.viewport == (1280, 800)


def test_default_picks_from_pool() -> None:
    rng = random.Random(0)
    profile = pick_profile("en-US", rng=rng)
    assert profile.locale.startswith("en")


def test_no_locale_picks_from_default_pool() -> None:
    rng = random.Random(7)
    profile = pick_profile(None, rng=rng)
    assert profile.locale.startswith("en")
    assert profile.viewport[0] >= 1280
