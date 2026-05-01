"""Progress record helpers — pure dict constructors, zero I/O."""

from __future__ import annotations


def ok(label: str, detail: str = "") -> dict:
    return {"status": "ok", "label": label, "detail": detail}


def fail(label: str, detail: str = "") -> dict:
    return {"status": "fail", "label": label, "detail": detail}


def info(label: str, detail: str = "") -> dict:
    return {"status": "info", "label": label, "detail": detail}


def warn(label: str, detail: str = "") -> dict:
    return {"status": "warn", "label": label, "detail": detail}


def undo(label: str, detail: str = "") -> dict:
    return {"status": "undo", "label": label, "detail": detail}
