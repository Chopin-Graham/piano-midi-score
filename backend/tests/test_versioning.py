from __future__ import annotations

import json
import re
from pathlib import Path

from app import __version__

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_release_versions_stay_synchronized() -> None:
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    project_version = re.search(
        r'^version = "([^"]+)"$',
        pyproject,
        flags=re.MULTILINE,
    )
    frontend = json.loads(
        (PROJECT_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )
    frontend_lock = json.loads(
        (PROJECT_ROOT / "frontend" / "package-lock.json").read_text(encoding="utf-8")
    )
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert project_version is not None
    assert project_version.group(1) == __version__
    assert frontend["version"] == __version__
    assert frontend_lock["version"] == __version__
    assert frontend_lock["packages"][""]["version"] == __version__
    assert f"## [{__version__}]" in changelog
    assert f"当前版本：`v{__version__}`" in readme
