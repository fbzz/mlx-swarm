"""Structural checks for the public MLX Swarm landing page."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website"


def _read(name: str) -> str:
    return (WEBSITE / name).read_text(encoding="utf-8")


def test_required_website_files_exist() -> None:
    for name in (
        "index.html",
        "styles.css",
        "app.js",
        "404.html",
        "privacy.html",
        "terms.html",
        "favicon.svg",
    ):
        assert (WEBSITE / name).is_file(), name


def test_index_has_conversion_and_ship_surface() -> None:
    html = _read("index.html")
    assert "<title>MLX Swarm, local coding agents for Apple silicon</title>" in html
    assert 'name="description"' in html
    assert "<nav" in html
    assert "<main" in html
    assert "<footer" in html
    assert html.count("Install on your Mac") >= 2
    assert "400 regression tests" in html
    assert 'rel="icon"' in html
    assert "Geist" in html
    assert "privacy.html" in html
    assert "terms.html" in html
    assert "Lorem" not in html
    assert "Inter" not in html
    assert "data-tagline" in html
    assert "IntersectionObserver" not in html


def test_motion_uses_observer_and_fluid_easing() -> None:
    js = _read("app.js")
    css = _read("styles.css")
    assert "IntersectionObserver" in js
    assert "addEventListener('scroll'" not in js
    assert 'addEventListener("scroll"' not in js
    assert "cubic-bezier(0.32, 0.72, 0, 1)" in css
    assert "tagline-word" in css


def test_legal_and_404_have_a_way_back() -> None:
    for name in ("404.html", "privacy.html", "terms.html"):
        html = _read(name)
        assert "Back to MLX Swarm" in html
        assert "./index.html" in html
