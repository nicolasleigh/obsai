"""Keep all CLI tests away from the user's configuration directory."""

import os

import pytest


@pytest.fixture(autouse=True)
def isolated_user_environment(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    for name in list(os.environ):
        if name.startswith("OBSAI_"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def stub_llm(monkeypatch: pytest.MonkeyPatch):
    """Replace the answer provider with one that returns a fixed string.

    Returns an installer rather than a value, because each test wants a different
    answer: a well-cited one, one with no citation, one citing an ID that does not
    exist.

    Patched at the module attribute because the provider is constructed *inside*
    :func:`obsai.application.answering.ask` — there is no dependency to override,
    and adding one purely for tests would leave the real assembly path uncovered.
    Two test modules need this, which is why it lives here instead of in one of
    them.
    """

    def install(text: str) -> None:
        class _Provider:
            def __init__(self, config) -> None:
                self.config = config

            async def generate(
                self, system_prompt: str, user_prompt: str, *, max_output_tokens: int
            ) -> str:
                return text

        monkeypatch.setattr("obsai.application.answering.OpenAILLMProvider", _Provider)

    return install
