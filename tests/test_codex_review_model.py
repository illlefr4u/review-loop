"""RED-тесты: codex-review закрепляет модель и усилие ревью (решение Al 24.09.2026: gpt-6-sol, xhigh).

Контракт `~/bin/codex-review` (скрипт без расширения, грузится как модуль):
- константы `REVIEW_MODEL == "gpt-6-sol"`, `REVIEW_EFFORT == "xhigh"`;
- `model_args(env=None) -> list[str]` = ["-c", 'model="<m>"', "-c", 'model_reasoning_effort="<e>"']; переменные окружения
  `CODEX_REVIEW_MODEL` / `CODEX_REVIEW_EFFORT` переопределяют (пустая строка = нет переопределения);
- `build_cmd(mode, *, commit=None, base=None, prompt=None, env=None)`: "commit" → codex review <model_args> --commit SHA,
  "unpushed" → codex review <model_args> --base BASE, "arch" → codex exec <model_args> PROMPT; `main()` запускает ровно его;
- флаг `--print-model` печатает «<модель> <усилие>» и выходит с кодом 0, ничего не трогая (ни git, ни файл вывода);
- перед запуском ревью печатается строка `Model: <модель> / <усилие>`.
"""
import importlib.machinery
import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "codex-review"


def load():
    loader = importlib.machinery.SourceFileLoader("codex_review_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("codex_review_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_pinned_constants():
    m = load()
    assert m.REVIEW_MODEL == "gpt-6-sol" and m.REVIEW_EFFORT == "xhigh"


def test_model_args_default_and_env_override():
    m = load()
    assert m.model_args({}) == ["-c", 'model="gpt-6-sol"', "-c", 'model_reasoning_effort="xhigh"']
    assert m.model_args({"CODEX_REVIEW_MODEL": "gpt-6-astra", "CODEX_REVIEW_EFFORT": "high"}) == \
        ["-c", 'model="gpt-6-astra"', "-c", 'model_reasoning_effort="high"']
    assert m.model_args({"CODEX_REVIEW_MODEL": "", "CODEX_REVIEW_EFFORT": ""}) == m.model_args({})


def test_build_cmd_all_modes_carry_the_model():
    m = load()
    ma = m.model_args({})
    assert m.build_cmd("commit", commit="abc1234", env={}) == ["codex", "review", *ma, "--commit", "abc1234"]
    assert m.build_cmd("unpushed", base="origin/main", env={}) == ["codex", "review", *ma, "--base", "origin/main"]
    assert m.build_cmd("arch", prompt="P", env={}) == ["codex", "exec", *ma, "P"]


def test_print_model_exits_zero_and_touches_nothing(monkeypatch, capsys, tmp_path):
    m = load()
    out = tmp_path / "codex-review.txt"
    out.write_text("previous review")
    monkeypatch.setattr(m, "OUTPUT_FILE", str(out))
    monkeypatch.setattr(sys, "argv", ["codex-review", "--print-model"])
    monkeypatch.setenv("CODEX_REVIEW_MODEL", "gpt-6-astra")
    monkeypatch.delenv("CODEX_REVIEW_EFFORT", raising=False)
    boom = lambda *a, **k: pytest.fail("--print-model must not run git or codex")   # noqa: E731
    monkeypatch.setattr(m.subprocess, "Popen", boom)
    monkeypatch.setattr(m.subprocess, "check_output", boom)
    with pytest.raises(SystemExit) as e:
        m.main()
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == "gpt-6-astra xhigh"
    assert out.read_text() == "previous review"


class FakeProc:
    def __init__(self, cmd, **kw):
        FakeProc.cmd = cmd
        self.stdout = iter(["review text\n"])
        self.stderr = iter([])
        self.returncode = 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def test_main_runs_build_cmd_and_announces_the_model(monkeypatch, capsys, tmp_path):
    m = load()
    monkeypatch.setattr(m, "OUTPUT_FILE", str(tmp_path / "codex-review.txt"))
    monkeypatch.setattr(m, "git_changed_files", lambda commit: ["a.py"])
    monkeypatch.setattr(m.subprocess, "Popen", FakeProc)
    monkeypatch.setattr(sys, "argv", ["codex-review", "--commit", "abc1234"])
    monkeypatch.delenv("CODEX_REVIEW_MODEL", raising=False)
    monkeypatch.delenv("CODEX_REVIEW_EFFORT", raising=False)
    try:
        m.main()
    except SystemExit as e:
        assert e.code in (0, None)
    assert FakeProc.cmd == m.build_cmd("commit", commit="abc1234", env={})
    assert "Model: gpt-6-sol / xhigh" in capsys.readouterr().out
