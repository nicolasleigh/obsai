"""Keep all CLI tests away from the user's configuration directory."""

import pytest


@pytest.fixture(autouse=True)
def isolated_user_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for name in ("OBSAI_VAULT", "OBSAI_INDEX", "OBSAI_VAULT__PATH", "OBSAI_INDEX__DATABASE"):
        monkeypatch.delenv(name, raising=False)
