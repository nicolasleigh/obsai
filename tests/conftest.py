"""Keep all CLI tests away from the user's configuration directory."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_user_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for name in list(os.environ):
        if name.startswith("OBSAI_"):
            monkeypatch.delenv(name, raising=False)
