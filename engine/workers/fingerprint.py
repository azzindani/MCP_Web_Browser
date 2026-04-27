"""Coherent browser identity profiles.

Bundles UA, viewport, timezone, locale, platform, screen size, and
WebGL strings as a single believable set. The browser worker picks one
profile per session and uses it for every page in that session so a
fingerprint scanner sees a consistent identity, not mismatched values.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    user_agent: str
    viewport: tuple[int, int]  # (width, height)
    locale: str
    timezone: str
    platform: str
    screen_width: int
    screen_height: int
    color_depth: int
    hardware_concurrency: int
    device_memory: int
    max_touch_points: int
    canvas_noise: float
    webgl_vendor: str
    webgl_renderer: str
    sec_ch_ua_full: str
    accept_language: str


_PROFILES: tuple[BrowserProfile, ...] = (
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport=(1920, 1040),
        locale="en-US",
        timezone="America/New_York",
        platform="Win32",
        screen_width=1920,
        screen_height=1080,
        color_depth=24,
        hardware_concurrency=8,
        device_memory=8,
        max_touch_points=0,
        canvas_noise=0.000012,
        webgl_vendor="Google Inc. (Intel)",
        webgl_renderer=(
            "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
        sec_ch_ua_full=(
            '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
        ),
        accept_language="en-US,en;q=0.9",
    ),
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport=(1440, 860),
        locale="en-US",
        timezone="America/Los_Angeles",
        platform="MacIntel",
        screen_width=1440,
        screen_height=900,
        color_depth=30,
        hardware_concurrency=10,
        device_memory=8,
        max_touch_points=0,
        canvas_noise=0.000021,
        webgl_vendor="Google Inc. (Apple)",
        webgl_renderer="ANGLE (Apple, Apple M1 Pro, OpenGL 4.1 Metal - 88)",
        sec_ch_ua_full=(
            '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
        ),
        accept_language="en-US,en;q=0.9",
    ),
    BrowserProfile(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport=(1366, 728),
        locale="en-US",
        timezone="Europe/London",
        platform="Win32",
        screen_width=1366,
        screen_height=768,
        color_depth=24,
        hardware_concurrency=4,
        device_memory=4,
        max_touch_points=0,
        canvas_noise=0.000008,
        webgl_vendor="Google Inc. (Intel)",
        webgl_renderer=(
            "ANGLE (Intel, Intel(R) HD Graphics 620 Direct3D11 vs_5_0 ps_5_0, D3D11)"
        ),
        sec_ch_ua_full=(
            '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
        ),
        accept_language="en-GB,en;q=0.9,en-US;q=0.8",
    ),
)


_ID_PROFILE: BrowserProfile = BrowserProfile(
    user_agent=(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    viewport=(1280, 800),
    locale="id-ID",
    timezone="Asia/Jakarta",
    platform="Win32",
    screen_width=1280,
    screen_height=800,
    color_depth=24,
    hardware_concurrency=4,
    device_memory=4,
    max_touch_points=0,
    canvas_noise=0.000015,
    webgl_vendor="Google Inc. (Intel)",
    webgl_renderer=(
        "ANGLE (Intel, Intel(R) HD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    ),
    sec_ch_ua_full=(
        '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"'
    ),
    accept_language="id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7",
)


def pick_profile(
    locale: str | None = None, *, rng: random.Random | None = None
) -> BrowserProfile:
    if locale and locale.startswith("id"):
        return _ID_PROFILE
    r = rng if rng is not None else random
    return r.choice(_PROFILES)
