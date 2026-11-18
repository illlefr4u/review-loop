"""Tests: codex-review records that a pinned code review of a commit ran to the end, so a deploy gate can refuse a commit that was never
reviewed. The record says only that the review completed; whether it was clean is read by a person, so findings are not parsed or counted.

A record is evidence that exactly this commit was reviewed to the end, so it is written only when that holds:

- only a code review run with `--pin REF` (a fresh detached worktree at the full SHA REF resolves to, resolved before the worktree is
  created); never for `--arch`, never for an unpinned review, never when `--commit` names another commit than the pin;
- for `--unpushed` the base is resolved in the original checkout before the worktree is detached;
- only when Codex exits with code 0, its output was read to the end without an error, and the output is not empty; in each refused case
  of a pinned code review a `WARNING: review record not written: ...` line on stderr;
- a relative `$CODEX_REVIEW_VERDICTS` is resolved against the directory codex-review was started in.

The record is one JSON line appended to `$CODEX_REVIEW_VERDICTS` (default `~/.local/state/codex-review/verdicts.jsonl`, parent
directory created): {"sha", "mode": "commit"|"unpushed", "repo": top level of the original repository, "at": UTC time,
"completed": true}.
"""
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "codex-review"


def load():
    loader = importlib.machinery.SourceFileLoader("codex_review_verdicts_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("codex_review_verdicts_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class FakeCodex:
    lines, code, cmd = [], 0, None

    def __init__(self, cmd, **kw):
        FakeCodex.cmd = cmd
        self.stdout = iter(FakeCodex.lines)
        self.stderr = iter([])
        self.returncode = FakeCodex.code

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        pass


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
    heads = run("rev-list", "HEAD").stdout.split()
    return path, heads          # heads[0] = HEAD, heads[1] = HEAD~1


def review(monkeypatch, tmp_path, argv, lines, code=0, prepare=None):
    m = load()
    if prepare is not None:
        prepare(m)
    monkeypatch.setattr(m, "OUTPUT_FILE", str(tmp_path / "codex-review.txt"))
    real_popen = subprocess.Popen
    # only the codex process is faked; git runs for real in the temporary repository
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: FakeCodex(cmd, **kw) if cmd and cmd[0] == "codex" else real_popen(cmd, **kw))
    FakeCodex.lines, FakeCodex.code = lines, code
    monkeypatch.setattr(sys, "argv", ["codex-review", *argv])
    try:
        m.main()
    except SystemExit:
        pass
    path = tmp_path / "state" / "verdicts.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


FINDINGS = ["The change can lose data.\n", "\n", "Full review comments:\n", "\n",
            "- [P0] Release blocker — /a.py:1\n", "- [P1] Broken invariant — /b.py:2\n", "- [P2] Risk — /c.py:3\n"]
CLEAN = ["No actionable defects were found in the commit. The targeted tests passed.\n"]


def broken_after(lines, error):
    def stream():
        yield from lines
        raise error
    return stream()


def test_a_pinned_review_with_findings_records_its_completion_without_counting_them(repo, monkeypatch, tmp_path):
    path, heads = repo
    recs = review(monkeypatch, tmp_path, ["--pin", heads[1][:7]], FINDINGS)
    assert len(recs) == 1
    r = recs[0]
    assert (r["sha"], r["mode"], r["completed"]) == (heads[1], "commit", True)
    assert "p1" not in r and "p2" not in r, "findings are read by a person, not counted"
    assert Path(r["repo"]).resolve() == path.resolve(), "the repository it was started in, not the temporary worktree"
    assert len(r["at"]) == 20 and r["at"].endswith("Z")


def test_a_pinned_clean_review_records_its_completion(repo, monkeypatch, tmp_path):
    path, heads = repo
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD"], CLEAN)
    assert [(r["sha"], r["mode"], r["completed"]) for r in recs] == [(heads[0], "commit", True)]


def test_a_review_that_says_it_could_not_review_is_still_a_completed_run(repo, monkeypatch, tmp_path):
    """The record proves only that the pinned review ran to the end; what it says is read by a person."""
    path, heads = repo
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD"], ["No issues could be verified because the diff was inaccessible.\n"])
    assert [r["sha"] for r in recs] == [heads[0]]


def test_a_pinned_unpushed_review_records_the_pin(repo, monkeypatch, tmp_path):
    path, heads = repo
    recs = review(monkeypatch, tmp_path, ["--pin", heads[0][:9], "--unpushed"], CLEAN,
                  prepare=lambda m: monkeypatch.setattr(m, "git_unpushed_base", lambda: "HEAD~1"))
    assert [(r["sha"], r["mode"]) for r in recs] == [(heads[0], "unpushed")]


def test_a_pin_given_as_a_ref_records_the_commit_it_resolved_to(repo, monkeypatch, tmp_path):
    path, heads = repo
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD~1"], CLEAN)
    assert [r["sha"] for r in recs] == [heads[1]]


@pytest.mark.parametrize("argv", [["--commit", "HEAD"], ["--arch", "--pin", "HEAD"], ["--arch", "--commit", "HEAD"]])
def test_unpinned_and_architecture_reviews_record_nothing(repo, monkeypatch, tmp_path, argv):
    assert review(monkeypatch, tmp_path, argv, CLEAN) == []


def test_a_commit_other_than_the_pin_records_nothing(repo, monkeypatch, tmp_path, capsys):
    path, heads = repo
    assert review(monkeypatch, tmp_path, ["--pin", heads[0], "--commit", heads[1]], CLEAN) == []
    assert "review record not written" in capsys.readouterr().err
    assert len(review(monkeypatch, tmp_path, ["--pin", heads[0], "--commit", heads[0][:8]], CLEAN)) == 1, "the same commit is fine"


def test_an_empty_review_records_nothing_and_says_so(repo, monkeypatch, tmp_path, capsys):
    assert review(monkeypatch, tmp_path, ["--pin", "HEAD"], ["   \n", "\n"]) == []
    assert "WARNING: review record not written" in capsys.readouterr().err


def test_a_failed_review_records_nothing(repo, monkeypatch, tmp_path):
    assert review(monkeypatch, tmp_path, ["--pin", "HEAD"], ["- [P2] partial\n"], code=1) == []


def test_a_review_whose_output_breaks_off_records_nothing_and_says_so(repo, monkeypatch, tmp_path, capsys):
    """A stream cut in the middle can look like a short clean review; only output read to its end is a completed review."""
    lines = broken_after(["Reviewing the change…\n"], OSError("connection reset by peer"))
    assert review(monkeypatch, tmp_path, ["--pin", "HEAD"], lines) == []
    assert "WARNING: review record not written" in capsys.readouterr().err


def test_records_accumulate(repo, monkeypatch, tmp_path):
    review(monkeypatch, tmp_path, ["--pin", "HEAD"], FINDINGS)
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD~1"], CLEAN)
    assert len(recs) == 2, "appended, never overwritten"


def test_a_relative_verdict_path_is_kept_in_the_start_directory(repo, monkeypatch, tmp_path):
    path, heads = repo
    monkeypatch.setenv("CODEX_REVIEW_VERDICTS", "state-rel/verdicts.jsonl")
    review(monkeypatch, tmp_path, ["--pin", "HEAD"], CLEAN)
    target = path / "state-rel" / "verdicts.jsonl"
    assert target.exists() and json.loads(target.read_text().splitlines()[0])["sha"] == heads[0]


def test_a_pinned_unpushed_review_uses_the_base_of_the_original_checkout(repo, monkeypatch, tmp_path):
    path, heads = repo
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True)   # noqa: E731
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True, capture_output=True)
    run("checkout", "-q", "-b", "feature")
    run("remote", "add", "origin", str(origin))
    # origin's default branch is already at HEAD, the feature branch's push target is one commit behind:
    # a base taken inside the detached worktree (origin/HEAD) sees nothing to review
    run("push", "-q", "origin", f"{heads[0]}:refs/heads/master", f"{heads[1]}:refs/heads/feature")
    run("fetch", "-q", "origin")
    run("remote", "set-head", "origin", "master")
    run("branch", "-u", "origin/feature")
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD", "--unpushed"], CLEAN)
    assert [(r["sha"], r["mode"]) for r in recs] == [(heads[0], "unpushed")], "the detached worktree has no push target of its own"
    base = FakeCodex.cmd[FakeCodex.cmd.index("--base") + 1]
    assert run("rev-parse", base + "^{commit}").stdout.strip() == heads[1], "the branch's push target, not origin/HEAD"


def test_the_pinned_worktree_is_created_without_repository_hooks(repo, monkeypatch, tmp_path):
    """A post-checkout hook could rewrite a tracked file in the pinned worktree before Codex reads it; the review then sees another tree
    than the pin while a completed record is written. The worktree is created with hooks off."""
    path, heads = repo
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True)   # noqa: E731
    (path / "guarded.txt").write_text("committed\n")
    run("add", "guarded.txt")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "three")
    hook = path / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\necho rewritten-by-hook > guarded.txt\n")
    hook.chmod(0o755)
    seen = []
    real_init = FakeCodex.__init__

    def init(self, cmd, **kw):
        seen.append(Path("guarded.txt").read_text())
        real_init(self, cmd, **kw)
    monkeypatch.setattr(FakeCodex, "__init__", init)
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD"], CLEAN)
    assert seen == ["committed\n"], "Codex reads the committed tree, not a hook's rewrite"
    assert len(recs) == 1


def test_an_unpushed_record_carries_the_base_it_was_compared_with(repo, monkeypatch, tmp_path):
    """The reviewed diff of --unpushed is base..pin; the record names that base so a reader can check it against the deployed revision."""
    path, heads = repo
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD", "--unpushed"], CLEAN,
                  prepare=lambda m: monkeypatch.setattr(m, "git_unpushed_base", lambda: "HEAD~1"))
    assert [(r["mode"], r.get("base")) for r in recs] == [("unpushed", heads[1])], "the full SHA of the base"


def test_a_pinned_worktree_that_does_not_match_the_pin_records_nothing(repo, monkeypatch, tmp_path, capsys):
    """Defence in depth: whatever changed the worktree (a filter, a hook that slipped through), a tree other than the pin is not evidence."""
    def prepare(m):
        real = m.subprocess.check_output

        def check_output(cmd, *a, **kw):
            if "status" in cmd and "--porcelain" in cmd:
                return b" M guarded.txt\n" if not kw.get("text") else " M guarded.txt\n"
            return real(cmd, *a, **kw)
        monkeypatch.setattr(m.subprocess, "check_output", check_output)
    assert review(monkeypatch, tmp_path, ["--pin", "HEAD"], CLEAN, prepare=prepare) == []
    assert "does not match the pin" in capsys.readouterr().err


def _remote(path, tmp_path):
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True)   # noqa: E731
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(origin)], check=True, capture_output=True)
    run("remote", "add", "origin", str(origin))
    return run


def test_a_new_branch_without_a_push_target_falls_back_to_the_remote_default(repo, monkeypatch, tmp_path):
    """As before the pinned base resolution: a branch never pushed has no push target, the review compares with origin/HEAD."""
    path, heads = repo
    run = _remote(path, tmp_path)
    run("push", "-q", "origin", f"{heads[1]}:refs/heads/master")
    run("fetch", "-q", "origin")
    run("remote", "set-head", "origin", "master")
    run("checkout", "-q", "-b", "never-pushed")
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD", "--unpushed"], CLEAN)
    assert [(r["sha"], r["mode"], r["base"]) for r in recs] == [(heads[0], "unpushed", heads[1])]


def test_an_unpushed_record_names_the_merge_base_the_review_compared_with(repo, monkeypatch, tmp_path):
    """`codex review --base` compares from the merge base; with a diverged push target the record names that merge base, not the target."""
    path, heads = repo
    run = _remote(path, tmp_path)
    run("checkout", "-q", "-b", "feature", heads[1])
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "upstream only")
    upstream_only = run("rev-parse", "HEAD").stdout.strip()
    run("push", "-q", "origin", f"{upstream_only}:refs/heads/feature")
    run("fetch", "-q", "origin")
    run("reset", "-q", "--hard", heads[1])
    run("branch", "-u", "origin/feature")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "local only")
    local_only = run("rev-parse", "HEAD").stdout.strip()
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD", "--unpushed"], CLEAN)
    assert [(r["sha"], r["base"]) for r in recs] == [(local_only, heads[1])], "the merge base, not the diverged push target"


def test_a_worktree_changed_during_the_review_records_nothing(repo, monkeypatch, tmp_path, capsys):
    """The pin is checked again after the review: a run that changed the worktree under the reviewer is not evidence for the pin."""
    path, heads = repo
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True)   # noqa: E731
    (path / "guarded.txt").write_text("committed\n")
    run("add", "guarded.txt")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "three")
    real_wait = FakeCodex.wait

    def wait(self, timeout=None):
        Path("guarded.txt").write_text("changed during the review\n")
        return real_wait(self, timeout)
    monkeypatch.setattr(FakeCodex, "wait", wait)
    assert review(monkeypatch, tmp_path, ["--pin", "HEAD"], CLEAN) == []
    assert "does not match the pin" in capsys.readouterr().err


def test_replacement_refs_do_not_change_what_is_reviewed(repo, monkeypatch, tmp_path):
    """With refs/replace Git can show another tree under the pin's SHA; the pinned checkout and Codex run with replacement objects off."""
    path, heads = repo
    run = lambda *a: subprocess.run(["git", "-C", str(path), *a], check=True, capture_output=True, text=True)   # noqa: E731
    (path / "x.txt").write_text("original\n")
    run("add", "x.txt")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "original")
    original = run("rev-parse", "HEAD").stdout.strip()
    (path / "x.txt").write_text("replacement\n")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-am", "replacement")
    replacement = run("rev-parse", "HEAD").stdout.strip()
    run("replace", original, replacement)
    seen, envs = [], []
    real_init = FakeCodex.__init__

    def init(self, cmd, **kw):
        seen.append(Path("x.txt").read_text())
        envs.append((kw.get("env") or {}).get("GIT_NO_REPLACE_OBJECTS"))
        real_init(self, cmd, **kw)
    monkeypatch.setattr(FakeCodex, "__init__", init)
    recs = review(monkeypatch, tmp_path, ["--pin", original], CLEAN)
    assert seen == ["original\n"], "the pin's own tree"
    assert envs == ["1"], "Codex's own git commands run with replacement objects off"
    assert [r["sha"] for r in recs] == [original]


def test_a_descendant_holding_stderr_open_does_not_fail_a_finished_review(repo, monkeypatch, tmp_path):
    """Codex can exit after the full review while a background child keeps the stderr pipe open; the review is finished when its stdout is
    read to the end, as before the record existed."""
    import time as _time
    path, heads = repo

    def lingering():
        _time.sleep(8)
        yield ""
    real_init = FakeCodex.__init__

    def init(self, cmd, **kw):
        real_init(self, cmd, **kw)
        self.stderr = lingering()
    monkeypatch.setattr(FakeCodex, "__init__", init)
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD"], CLEAN)
    assert [r["sha"] for r in recs] == [heads[0]]


def test_untracked_files_count_whatever_the_status_configuration(repo, monkeypatch, tmp_path, capsys):
    path, heads = repo
    subprocess.run(["git", "-C", str(path), "config", "status.showUntrackedFiles", "no"], check=True)
    real_wait = FakeCodex.wait

    def wait(self, timeout=None):
        Path("generated.py").write_text("print('made during the review')\n")
        return real_wait(self, timeout)
    monkeypatch.setattr(FakeCodex, "wait", wait)
    assert review(monkeypatch, tmp_path, ["--pin", "HEAD"], CLEAN) == []
    assert "does not match the pin" in capsys.readouterr().err


def test_repository_git_environment_does_not_follow_into_the_pin(repo, monkeypatch, tmp_path):
    """GIT_DIR and GIT_WORK_TREE of the caller would keep git and Codex on the original checkout; inside the pin they are dropped."""
    path, heads = repo
    monkeypatch.setenv("GIT_DIR", str(path / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(path))
    envs = []
    real_init = FakeCodex.__init__

    def init(self, cmd, **kw):
        envs.append(kw.get("env") or {})
        real_init(self, cmd, **kw)
    monkeypatch.setattr(FakeCodex, "__init__", init)
    recs = review(monkeypatch, tmp_path, ["--pin", "HEAD"], CLEAN)
    assert envs and "GIT_DIR" not in envs[0] and "GIT_WORK_TREE" not in envs[0]
    assert envs[0].get("GIT_NO_REPLACE_OBJECTS") == "1"
    assert [r["sha"] for r in recs] == [heads[0]]
