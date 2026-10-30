"""Edits profile.toml from the Skills page, keeping its comments and layout."""

from __future__ import annotations

import os
from pathlib import Path

import tomlkit

from jobfinder.score import canonical_skill

LEVELS = (0, 1, 2, 3)  # 0 takes the skill out of the profile


def set_skill_level(path: Path, name: str, level: int) -> str:
    """Set a skill's level and return the name it is stored under. An entry already in the file
    under any spelling ("JS" for JavaScript) is changed in place, keeping its comment."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}, not {level!r}")
    name = name.strip().lower()
    if not name:
        raise ValueError("skill name is empty")

    with open(path, encoding="utf-8", newline="") as f:  # Path.read_text(newline=) needs Python 3.13
        doc = tomlkit.parse(f.read())
    skills = doc.setdefault("skills", tomlkit.table())
    wanted = canonical_skill(name)
    existing = next((key for key in skills if canonical_skill(key) == wanted), None)
    stored = existing or name
    if level == 0:
        if existing is not None:
            del skills[existing]
    else:
        skills[stored] = level

    # Written aside, then swapped in, so a process reading the file never sees half of it.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(tomlkit.dumps(doc), encoding="utf-8", newline="")
    os.replace(temp, path)
    return stored
