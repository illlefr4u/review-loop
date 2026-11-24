"""Tests: codex-review names a review refused by OpenAI's cyber_policy filter and prints a manual Daybreak Blue command.

The wrapper never runs Daybreak itself: a Daybreak session is less safeguarded and may be used for training, so whether to run
one is a person's decision, and such a run does not count as a completed review for the deploy gate.

Contract of `codex-review` (extensionless script, loaded as a module):
- constants `DAYBREAK_MODEL == "gpt-6-sol"`, `DAYBREAK_EFFORT == "max"`, `DAYBREAK_PROGRAM == "daybreak_blue"`;
- `daybreak_model_args(env=None)` has the shape of `model_args` for the Daybreak model: ["-c", 'model="<m>"', "-c",
  'review_model="<m>"', "-c", 'model_reasoning_effort="<e>"']; `CODEX_REVIEW_DAYBREAK_MODEL` / `CODEX_REVIEW_DAYBREAK_EFFORT`
  override it, an empty string means no override;
- `build_cmd(mode, *, commit=None, base=None, prompt=None, env=None, mcp_args=())`: as before the Daybreak work except that the
  model arguments are followed by "-c", "daybreak=false" (a saved `daybreak = true` must not turn a review into a Daybreak
  session) and then by `mcp_args` (test_codex_review_no_mcp.py);
- `daybreak_review_prompt(commit)` names the exact commit, shown with `git show -m --first-parent` (a merge against its first
  parent, in old and new git), asks for findings labelled [P0]–[P3] and says not to modify any files;
- `daybreak_command(mode, *, commit=None, prompt=None, env=None, mcp_args=())` = ["codex", "exec", *daybreak_model_args(env),
  *mcp_args, "--cyber-access-program", "daybreak_blue", "-s", "read-only", PROMPT] for "commit" (PROMPT = daybreak_review_prompt(commit))
  and "arch" (the given prompt); "unpushed" → ValueError;
- `is_cyber_policy_refusal(returncode, stderr_text, codex_home=None)`: true only for a non-zero exit whose own session journal
  has a `task_complete` event with `error.codex_error_info == "cyber_policy"`; the session is the FIRST `session id: <id>` line of
  stderr (ANSI colour codes ignored), the journal `<codex_home or $CODEX_HOME or ~/.codex>/sessions/**/rollout-*<id>.jsonl`;
  stderr text itself never decides, and without the journal the answer is false;
- an unpinned commit review resolves its commit to a full SHA before the first run (an unresolvable ref is passed on as given);
- after a refused run, one stderr line names cyber_policy and the session id; for a commit or --arch review it also prints how to run the
  review manually: the command (`shlex.join(daybreak_command(...))`), for a commit — and for a pinned --arch review — inside a
  fresh detached worktree at its SHA created with the pinned review's protections; every printed git and codex command of the
  recipe — the worktree creation (`git -c core.hooksPath=/dev/null worktree add --detach`), the clean-tree check and the review
  — starts with `env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR -u GIT_OBJECT_DIRECTORY
  GIT_NO_REPLACE_OBJECTS=1`, so an exported index or work tree of the original shell is never touched; an unpinned --arch review
  gets the command alone (it reviewed the current checkout);
  with warnings that OpenAI may use a Daybreak session for training, that its agent can read any file on this machine, that Codex
  starts only if `codex mcp list` with the command's switches shows no enabled server (test_codex_review_no_mcp.py), and that the manual run does not count
  for the deploy gate; the printed command carries those switches (here none are configured: `MCP_BASE_ARGS`); for --unpushed there is no
  command; codex runs exactly once, no record is written, and the wrapper exits 1 even if the refused run printed output;
- the completion record is as before the Daybreak work: no "daybreak" and no "model" fields.
"""
import importlib.machinery
import importlib.util
import json
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "codex-review"

FIRST = "00000000-1111-7222-8333-444444444444"
OTHER = "00000000-5555-7666-8777-888888888888"
REFUSAL_ERR = ["OpenAI Codex v0.171.0\n", "--------\n", f"session id: {FIRST}\n", "--------\n",
               "ERROR: This content was flagged for possible cybersecurity risk. (codex_error_info: cyber_policy)\n",
               "tokens used\n", "48,422\n"]
QUOTA_ERR = ["OpenAI Codex v0.171.0\n", f"session id: {FIRST}\n", "ERROR: You've hit your usage limit. Try again later.\n"]
CLEAN = ["No actionable defects were found in the commit.\n"]


def load():
    loader = importlib.machinery.SourceFileLoader("codex_review_daybreak_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("codex_review_daybreak_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class ScriptedCodex:
    """Each codex call takes the next (stdout lines, stderr lines, exit code) from the script."""
    script, calls = [], []

    def __init__(self, cmd, **kw):
        ScriptedCodex.calls.append(list(cmd))
        out, err, code = ScriptedCodex.script[len(ScriptedCodex.calls) - 1]
        self.stdout = iter(out)
        self.stderr = iter(err)
        self.returncode = code

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


def write_refusal(codex_home, session_id, info="cyber_policy"):
    """A journal ending like a real refused review: task_complete with error.codex_error_info."""
    day = Path(codex_home) / "sessions" / "2026" / "11" / "23"
    day.mkdir(parents=True, exist_ok=True)
    lines = [
        {"timestamp": "2026-11-23T05:00:00.000Z", "type": "session_meta",
         "payload": {"id": session_id, "source": "exec", "originator": "codex_exec"}},
        {"timestamp": "2026-11-23T05:04:00.000Z", "type": "event_msg",
         "payload": {"type": "task_complete", "turn_id": "t1", "last_agent_message": None,
                     "error": {"message": "This content was flagged for possible cybersecurity risk.",
                               "codex_error_info": info}}},
    ]
    (day / f"rollout-2026-11-23T05-00-00-{session_id}.jsonl").write_text(
        "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in lines))


@pytest.fixture
def repo(tmp_path, monkeypatch):
    path = tmp_path / "repo"
    path.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True)   # noqa: E731
    run("init", "-q")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "one")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "two")
    monkeypatch.chdir(path)
    monkeypatch.setenv("CODEX_REVIEW_VERDICTS", str(tmp_path / "state" / "verdicts.jsonl"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    for name in ("CODEX_REVIEW_MODEL", "CODEX_REVIEW_EFFORT", "CODEX_REVIEW_DAYBREAK_MODEL", "CODEX_REVIEW_DAYBREAK_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    return path, run("rev-list", "HEAD").stdout.split()


def review(monkeypatch, tmp_path, argv, script, prepare=None):
    """Run main() with scripted codex calls; git runs for real. Returns (exit code, records, module)."""
    m = load()
    if prepare is not None:
        prepare(m)
    monkeypatch.setattr(m, "OUTPUT_FILE", str(tmp_path / "codex-review.txt"))
    real_popen = subprocess.Popen
    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda cmd, **kw: ScriptedCodex(cmd, **kw) if cmd and cmd[0] == "codex" else real_popen(cmd, **kw))
    ScriptedCodex.script, ScriptedCodex.calls = script, []
    monkeypatch.setattr(sys, "argv", ["codex-review", *argv])
    code = 0
    try:
        m.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    path = tmp_path / "state" / "verdicts.jsonl"
    recs = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return code, recs, m


# --- arguments ---

def test_daybreak_model_constants_and_arguments():
    m = load()
    assert (m.DAYBREAK_MODEL, m.DAYBREAK_EFFORT, m.DAYBREAK_PROGRAM) == ("gpt-6-sol", "max", "daybreak_blue")
    assert m.daybreak_model_args({}) == ["-c", 'model="gpt-6-sol"', "-c", 'review_model="gpt-6-sol"',
                                         "-c", 'model_reasoning_effort="max"']
    assert m.daybreak_model_args({"CODEX_REVIEW_DAYBREAK_MODEL": "gpt-6-luna", "CODEX_REVIEW_DAYBREAK_EFFORT": "xhigh"}) == \
        ["-c", 'model="gpt-6-luna"', "-c", 'review_model="gpt-6-luna"', "-c", 'model_reasoning_effort="xhigh"']
    assert m.daybreak_model_args({"CODEX_REVIEW_DAYBREAK_MODEL": "", "CODEX_REVIEW_DAYBREAK_EFFORT": ""}) == \
        m.daybreak_model_args({})


def test_a_normal_run_turns_daybreak_off_whatever_the_saved_configuration():
    m = load()
    ma = m.model_args({})
    off = ["-c", "daybreak=false"]
    assert m.build_cmd("commit", commit="abc1234", env={}) == ["codex", "review", *ma, *off, "--commit", "abc1234"]
    assert m.build_cmd("unpushed", base="origin/main", env={}) == ["codex", "review", *ma, *off, "--base", "origin/main"]
    assert m.build_cmd("arch", prompt="P", env={}) == ["codex", "exec", *ma, *off, "P"]


def test_the_manual_command_runs_codex_exec_read_only_with_the_daybreak_program():
    m = load()
    head = ["codex", "exec", *m.daybreak_model_args({}), "--cyber-access-program", "daybreak_blue", "-s", "read-only"]
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert m.daybreak_command("commit", commit=sha, env={}) == [*head, m.daybreak_review_prompt(sha)]
    assert m.daybreak_command("arch", prompt="P", env={}) == [*head, "P"]
    with pytest.raises(ValueError):
        m.daybreak_command("unpushed", env={})


def test_the_daybreak_prompt_names_the_commit_portably():
    m = load()
    sha = "0123456789abcdef0123456789abcdef01234567"
    text = m.daybreak_review_prompt(sha)
    assert sha in text and "-m --first-parent" in text and "--diff-merges" not in text, "works in git older than 2.31"
    assert all(label in text for label in ("[P0]", "[P1]", "[P2]", "[P3]"))
    assert "do not modify" in text.lower()


def test_a_refusal_is_recognised_only_by_its_own_session_journal(tmp_path, monkeypatch):
    m = load()
    home = tmp_path / "home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    err = "".join(REFUSAL_ERR)
    assert m.is_cyber_policy_refusal(1, err) is False, "no journal: stderr alone never decides"
    write_refusal(home, FIRST, info="usage_limit_exceeded")
    assert m.is_cyber_policy_refusal(1, err) is False, "the journal names another error"
    write_refusal(home, FIRST)
    assert m.is_cyber_policy_refusal(1, err) is True, "the usage footer after the error does not matter"
    assert m.is_cyber_policy_refusal(0, err) is False
    assert m.is_cyber_policy_refusal(1, "") is False and m.is_cyber_policy_refusal(1, None) is False
    spoofed = f"OpenAI Codex v0.171.0\nsession id: {OTHER}\nexec cat notes\nsession id: {FIRST}\n"
    assert m.is_cyber_policy_refusal(1, spoofed) is False, "only the first session id line (the header) names the run"
    coloured = err.replace(f"session id: {FIRST}", f"\x1b[1msession id:\x1b[0m \x1b[2m{FIRST}\x1b[0m")
    assert coloured != err and m.is_cyber_policy_refusal(1, coloured) is True, "FORCE_COLOR colours the header"
    other = tmp_path / "other"
    write_refusal(other, FIRST)
    monkeypatch.delenv("CODEX_HOME")
    assert m.is_cyber_policy_refusal(1, err, codex_home=str(other)) is True


# --- behaviour ---

def test_a_refused_pinned_review_prints_the_manual_daybreak_command_and_runs_codex_once(repo, monkeypatch, tmp_path, capsys):
    path, heads = repo
    write_refusal(tmp_path / "codex-home", FIRST)
    code, recs, m = review(monkeypatch, tmp_path, ["--pin", "HEAD"], [(["Reviewing…\n"], REFUSAL_ERR, 1)])
    assert code == 1 and recs == [] and len(ScriptedCodex.calls) == 1
    assert "daybreak=false" in ScriptedCodex.calls[0] and ScriptedCodex.calls[0][:2] == ["codex", "review"]
    err = capsys.readouterr().err
    assert any("cyber_policy" in line and FIRST in line for line in err.splitlines()), "one line names the refusal and its session"
    assert shlex.join(m.daybreak_command("commit", commit=heads[0], mcp_args=m.MCP_BASE_ARGS)) in err
    low = err.lower()
    assert all(word in low for word in ("training", "deploy", "mcp", "any file", "worktree")), "the risks and the fresh checkout"
    env = "env -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE -u GIT_COMMON_DIR -u GIT_OBJECT_DIRECTORY GIT_NO_REPLACE_OBJECTS=1"
    assert f"{env} git -c core.hooksPath=/dev/null worktree add --detach <dir> {heads[0]}" in err, "clean Git env, no hooks"
    assert f"{env} git -C <dir> status --porcelain" in err, "the clean-tree check uses the same clean Git environment"
    assert f"{env} {shlex.join(m.daybreak_command('commit', commit=heads[0], mcp_args=m.MCP_BASE_ARGS))}" in err


def test_a_refused_unpinned_review_names_the_commit_it_reviewed(repo, monkeypatch, tmp_path, capsys):
    path, heads = repo
    write_refusal(tmp_path / "codex-home", FIRST)
    code, recs, m = review(monkeypatch, tmp_path, ["--commit", "HEAD"], [([], REFUSAL_ERR, 1)])
    assert code == 1 and len(ScriptedCodex.calls) == 1
    assert ScriptedCodex.calls[0][ScriptedCodex.calls[0].index("--commit") + 1] == heads[0], "resolved before the run"
    assert shlex.join(m.daybreak_command("commit", commit=heads[0], mcp_args=m.MCP_BASE_ARGS)) in capsys.readouterr().err


def test_a_refused_architecture_review_prints_the_command_with_its_prompt(repo, monkeypatch, tmp_path, capsys):
    write_refusal(tmp_path / "codex-home", FIRST)
    code, recs, m = review(monkeypatch, tmp_path, ["--arch"], [([], REFUSAL_ERR, 1)])
    assert code == 1 and len(ScriptedCodex.calls) == 1
    prompt = ScriptedCodex.calls[0][-1]
    err = capsys.readouterr().err
    assert shlex.join(m.daybreak_command("arch", prompt=prompt, mcp_args=m.MCP_BASE_ARGS)) in err
    assert "worktree add" not in err, "an unpinned architecture review reviewed the current checkout"


def test_a_refused_pinned_architecture_review_recreates_the_pinned_checkout(repo, monkeypatch, tmp_path, capsys):
    path, heads = repo
    write_refusal(tmp_path / "codex-home", FIRST)
    code, recs, m = review(monkeypatch, tmp_path, ["--arch", "--pin", "HEAD~1"], [([], REFUSAL_ERR, 1)])
    assert code == 1 and len(ScriptedCodex.calls) == 1
    err = capsys.readouterr().err
    assert f"git -c core.hooksPath=/dev/null worktree add --detach <dir> {heads[1]}" in err
    assert shlex.join(m.daybreak_command("arch", prompt=ScriptedCodex.calls[0][-1], mcp_args=m.MCP_BASE_ARGS)) in err


@pytest.mark.parametrize("argv", [["--pin", "HEAD", "--unpushed"], ["--unpushed"]])
def test_a_refused_unpushed_review_is_named_without_a_command(repo, monkeypatch, tmp_path, capsys, argv):
    write_refusal(tmp_path / "codex-home", FIRST)
    code, recs, _ = review(monkeypatch, tmp_path, argv, [([], REFUSAL_ERR, 1)],
                           prepare=lambda m: monkeypatch.setattr(m, "git_unpushed_base", lambda: "HEAD~1"))
    assert code == 1 and recs == [] and len(ScriptedCodex.calls) == 1
    err = capsys.readouterr().err
    assert any("cyber_policy" in line and FIRST in line for line in err.splitlines()), "one line names the refusal and its session"
    assert "--cyber-access-program" not in err


def test_a_quota_failure_gets_no_daybreak_advice(repo, monkeypatch, tmp_path, capsys):
    write_refusal(tmp_path / "codex-home", FIRST, info="usage_limit_exceeded")
    code, recs, _ = review(monkeypatch, tmp_path, ["--pin", "HEAD"], [([], QUOTA_ERR, 1)])
    assert code == 1 and recs == [] and len(ScriptedCodex.calls) == 1
    assert "--cyber-access-program" not in capsys.readouterr().err


def test_a_refusal_without_its_journal_gets_no_daybreak_advice(repo, monkeypatch, tmp_path, capsys):
    code, recs, _ = review(monkeypatch, tmp_path, ["--pin", "HEAD"], [([], REFUSAL_ERR, 1)])
    assert code == 1 and recs == [] and len(ScriptedCodex.calls) == 1
    assert "--cyber-access-program" not in capsys.readouterr().err


def test_a_completed_review_records_as_before(repo, monkeypatch, tmp_path):
    path, heads = repo
    code, recs, _ = review(monkeypatch, tmp_path, ["--pin", "HEAD"], [(CLEAN, [], 0)])
    assert code == 0 and len(ScriptedCodex.calls) == 1 and "daybreak=false" in ScriptedCodex.calls[0]
    assert [(r["sha"], r["completed"]) for r in recs] == [(heads[0], True)]
    assert "daybreak" not in recs[0] and "model" not in recs[0]
