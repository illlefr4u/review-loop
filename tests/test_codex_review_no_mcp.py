"""Tests: a review never hands the reviewer Codex's MCP servers, plugins or app connectors.

The reviewer runs without approval prompts. An MCP server or app connector would give it ready-made authenticated tools outside
the reviewed checkout (push to GitHub, drive the browser or desktop, post to Slack). Codex configuration is layered (user,
profile, a trusted project's config, system, plugins), so the wrapper does not parse it: Codex's own resolver lists the servers,
the wrapper switches each one off, and the same resolver confirms that none is left. The review keeps the user's own Codex home
and configuration.

Contract of `codex-review` (extensionless script, loaded as a module):
- `MCP_BASE_ARGS == ["-c", "features.plugins=false", "-c", "features.apps=false", "-c",
  "features.agent_message_board_mcp=false", "-c", "features.skill_mcp_dependency_install=false"]`: plugin servers, app
  connectors, the message-board server Codex creates at session start (with `multi_agent_v2`; `codex mcp list` does not show
  it) and MCP servers a mentioned skill would install into the user's configuration during the review go off as features (a
  per-server switch on a plugin's server makes Codex fail with "invalid transport"); written as `-c features.<name>=false`
  because `--disable` rejects a feature the installed Codex does not know, while `-c` ignores it;
- `McpIsolationError(Exception)`;
- `mcp_off_args(cwd=None, env=None)` returns the arguments that switch every MCP server off:
  1. runs `["codex", "mcp", "list", "--json", *MCP_BASE_ARGS]` with `subprocess.run`, passing `cwd` and `env` as keyword
     arguments, capturing text output, with `timeout=MCP_LIST_TIMEOUT` (`MCP_LIST_TIMEOUT == 120`: an enabled HTTP server
     without stored credentials makes the listing run OAuth discovery against it first, which a slow endpoint stretches);
     a timeout's message says that an HTTP MCP server may be slow to answer OAuth discovery;
  2. the output must be a JSON list of objects whose `name` is a string and `enabled` a boolean;
  3. the result is `MCP_BASE_ARGS`, followed, when any server is enabled, by ONE pair
     `"-c", "mcp_servers={<k1>={enabled=false},<k2>={enabled=false},...}"` covering every enabled server in listed order
     (separate `-c mcp_servers=` overrides replace each other, so there is exactly one; Codex merges the table into the
     configured servers); each key is a TOML basic string: the name between double quotes, with `"` and backslash each
     preceded by a backslash, U+0000-U+001F and U+007F written as `\\uXXXX` with four uppercase hex digits, every other
     character as is; there are no spaces outside keys; every name can be expressed, so no name is refused;
  4. runs `["codex", "mcp", "list", "--json", *result]` the same way and requires that no server is enabled;
  5. runs `["codex", "features", "list", *result]` the same way (exit 0 required); the output must have at least one non-blank
     line and every non-blank line must end in `true` or `false` (its first word is the feature's name; the stage between them
     may be two words); each feature MCP_BASE_ARGS
     switches off must be listed `false` or not listed at all (a Codex without that feature); one still listed `true`, for
     example pinned by managed requirements, means the override was not applied;
  a non-zero exit, a timeout, an OSError, output of another shape, a server still enabled or a feature still on raise
  McpIsolationError naming the cause (the server's or feature's name for the last two, the listing's stderr for a non-zero
  exit);
- `build_cmd(..., mcp_args=())` puts `mcp_args` right after "-c", "daybreak=false"; `daybreak_command(..., mcp_args=())` puts
  them right after the Daybreak model arguments;
- every review computes `mcp_off_args()` in the directory it reviews (inside the pinned worktree for --pin) before Codex starts
  and passes the result to `build_cmd`; if that raises, Codex is not started, the wrapper exits 1 with the cause on stderr and
  writes no review record;
- the manual Daybreak command printed after a cyber_policy refusal carries the review's own switches (computed in the directory
  the review ran in; a fresh worktree of the same repository inherits its trust and loads the same project config), and no
  other listing is made when printing; its launch line is guarded:
  `<P>python3 -I -c <Q> <switches> && <P>codex exec <switches> ...`, where <P> is DAYBREAK_GIT_ENV_PREFIX, <Q> is
  `shlex.quote(MCP_GUARD_CHECK)`, <switches> is `shlex.join(<switches>)` and the part after `&& <P>` is
  `shlex.join(daybreak_command(..., mcp_args=<switches>))`; `MCP_GUARD_CHECK` is Python code that runs
  `["codex", "mcp", "list", "--json", *sys.argv[1:]]` itself and exits 0 only if that listing exits 0 and prints a JSON list of
  objects none of which has `enabled` other than false, and then `["codex", "features", "list", *sys.argv[1:]]` exits 0 and
  lists every feature named by a `-c features.<name>=false` argument as `false` or not at all, in a feature listing with at
  least one recognised line; both runs use `timeout=MCP_LIST_TIMEOUT`; it
  names the enabled servers or features (or the failure) on stderr otherwise; so Codex starts only if, in the directory where it runs, the listing succeeds and shows no enabled server (no shell
  pipeline, whose status would be the check's alone); the warning names `codex mcp list`, says Codex starts only if that
  check passes, and says that an `invalid transport` error means the directory lacks a server the review switched off (for
  example one from an uncommitted project config) and that the review should then be rerun with --pin.
"""
import importlib.machinery
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "codex-review"
BASE = ["-c", "features.plugins=false", "-c", "features.apps=false", "-c", "features.agent_message_board_mcp=false",
        "-c", "features.skill_mcp_dependency_install=false"]
LIST = ["codex", "mcp", "list", "--json"]


def load():
    loader = importlib.machinery.SourceFileLoader("codex_review_no_mcp_mod", str(SCRIPT))
    spec = importlib.util.spec_from_loader("codex_review_no_mcp_mod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def toml_key(name):
    out = []
    for ch in name:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append("\\u%04X" % ord(ch))
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def off(*names):
    if not names:
        return list(BASE)
    return BASE + ["-c", "mcp_servers={" + ",".join(toml_key(n) + "={enabled=false}" for n in names) + "}"]


def guarded(m, sw, command):
    p = m.DAYBREAK_GIT_ENV_PREFIX
    return f"{p}python3 -I -c {shlex.quote(m.MCP_GUARD_CHECK)} {shlex.join(sw)} && {p}{shlex.join(command)}"


# --- mcp_off_args ---

def test_the_base_arguments_turn_plugins_and_apps_off():
    assert load().MCP_BASE_ARGS == BASE


def test_every_enabled_server_is_switched_off_and_the_result_is_confirmed(mcp_list, tmp_path):
    m = load()
    mcp_list.servers = lambda cwd: [("github", True), ("computer-use", False), ("node_repl", True)]
    env = {"CODEX_HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "")}
    args = m.mcp_off_args(cwd=str(tmp_path), env=env)
    assert args == off("github", "node_repl")
    assert [c["argv"] for c in mcp_list.calls] == [LIST + BASE, LIST + args], "listed, then confirmed with the switches"
    assert [c["argv"] for c in mcp_list.feature_calls] == [["codex", "features", "list", *args]], "features confirmed too"
    assert m.MCP_LIST_TIMEOUT == 120
    for call in mcp_list.calls + mcp_list.feature_calls:
        assert call["cwd"] == str(tmp_path.resolve()) and call["env"] is env
        assert call["timeout"] == m.MCP_LIST_TIMEOUT


def test_no_enabled_server_gives_the_base_arguments_and_is_still_confirmed(mcp_list):
    m = load()
    mcp_list.servers = lambda cwd: [("computer-use", False), ("dot.name", False)]
    assert m.mcp_off_args() == BASE
    assert [c["argv"] for c in mcp_list.calls] == [LIST + BASE, LIST + BASE]


@pytest.mark.parametrize("name,key", [
    ("io.github.example/service", '"io.github.example/service"'),
    ("with space", '"with space"'),
    ('quo"te', '"quo\\"te"'),
    ("quo'te", '"quo\'te"'),
    ("eq=x", '"eq=x"'),
    ("br[0]", '"br[0]"'),
    ("back\\slash", '"back\\\\slash"'),
    ("tab\tname", '"tab\\u0009name"'),
    ("del\x7fname", '"del\\u007Fname"'),
    ("\u00fcn\u00ef", '"\u00fcn\u00ef"'),
    ("\U0001F600", '"\U0001F600"'),
    ("", '""'),
], ids=["dots", "space", "double-quote", "single-quote", "equals", "brackets", "backslash", "tab", "del", "unicode", "emoji",
        "empty"])
def test_every_name_is_switched_off_as_a_quoted_key(mcp_list, name, key):
    m = load()
    mcp_list.servers = lambda cwd: [("github", True), (name, True)]
    args = m.mcp_off_args()
    assert args == BASE + ["-c", 'mcp_servers={"github"={enabled=false},' + key + "={enabled=false}}"]
    assert len(mcp_list.calls) == 2


def test_one_override_carries_every_enabled_server(mcp_list):
    m = load()
    mcp_list.servers = lambda cwd: [("a", True), ("b", False), ("c", True), ("d", True)]
    args = m.mcp_off_args()
    overrides = [v for f, v in zip(args, args[1:]) if f == "-c" and v.startswith("mcp_servers")]
    assert overrides == ['mcp_servers={"a"={enabled=false},"c"={enabled=false},"d"={enabled=false}}']


def test_names_the_switch_can_express_are_accepted(mcp_list):
    m = load()
    mcp_list.servers = lambda cwd: [("foo:bar", True), ("@scope/srv", True), ("computer-use", True), ("node_repl", True)]
    assert m.mcp_off_args() == off("foo:bar", "@scope/srv", "computer-use", "node_repl")


@pytest.mark.parametrize("feature", ["plugins", "apps", "agent_message_board_mcp", "skill_mcp_dependency_install"])
def test_a_feature_the_override_did_not_turn_off_is_refused(mcp_list, feature):
    m = load()
    mcp_list.features[feature] = True
    mcp_list.pinned = {feature}
    with pytest.raises(m.McpIsolationError) as e:
        m.mcp_off_args()
    assert feature in str(e.value)


def test_a_feature_this_codex_does_not_have_is_fine(mcp_list):
    m = load()
    del mcp_list.features["agent_message_board_mcp"], mcp_list.features["skill_mcp_dependency_install"]
    assert m.mcp_off_args() == BASE


@pytest.mark.parametrize("script", [
    lambda argv, cwd: subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: features broke"),
    lambda argv, cwd: subprocess.CompletedProcess(argv, 0, stdout="apps   stable   maybe\n", stderr=""),
    lambda argv, cwd: (_ for _ in ()).throw(subprocess.TimeoutExpired(argv, 5)),
    lambda argv, cwd: subprocess.CompletedProcess(argv, 0, stdout="", stderr=""),
    lambda argv, cwd: subprocess.CompletedProcess(argv, 0, stdout=" \n\n", stderr=""),
], ids=["exit", "unparsed-line", "timeout", "empty", "blank"])
def test_a_feature_listing_that_cannot_be_trusted_is_refused(mcp_list, script):
    m = load()
    mcp_list.features_script = script
    with pytest.raises(m.McpIsolationError):
        m.mcp_off_args()


def test_a_server_still_enabled_after_the_switches_is_refused(mcp_list):
    m = load()
    answers = iter([[{"name": "github", "enabled": True}], [{"name": "github", "enabled": True}]])
    mcp_list.script = lambda argv, cwd: subprocess.CompletedProcess(argv, 0, stdout=json.dumps(next(answers)), stderr="")
    with pytest.raises(m.McpIsolationError) as e:
        m.mcp_off_args()
    assert "github" in str(e.value)


def _completed(stdout, code=0, stderr=""):
    return lambda argv, cwd: subprocess.CompletedProcess(argv, code, stdout=stdout, stderr=stderr)


def _raise(exc):
    def script(argv, cwd):
        raise exc
    return script


@pytest.mark.parametrize("script", [
    _completed("", code=2, stderr="Error: failed to load configuration"),
    _completed("not json"),
    _completed('{"name": "github", "enabled": true}'),
    _completed('[{"name": "github"}]'),
    _completed('[{"name": "github", "enabled": "yes"}]'),
    _completed('[{"name": 3, "enabled": true}]'),
    _completed('["github"]'),
    _raise(subprocess.TimeoutExpired(LIST, 5)),
    _raise(FileNotFoundError(2, "No such file or directory", "codex")),
], ids=["exit", "not-json", "not-a-list", "no-enabled", "enabled-not-bool", "name-not-str", "not-objects", "timeout",
        "no-codex"])
def test_a_listing_that_cannot_be_trusted_is_refused(mcp_list, script):
    m = load()
    mcp_list.script = script
    with pytest.raises(m.McpIsolationError) as e:
        m.mcp_off_args()
    assert str(e.value)


def test_a_listing_timeout_points_at_oauth_discovery(mcp_list):
    m = load()
    mcp_list.script = _raise(subprocess.TimeoutExpired(LIST, 120))
    with pytest.raises(m.McpIsolationError) as e:
        m.mcp_off_args()
    assert "timed out" in str(e.value) and "OAuth" in str(e.value)


def test_a_failed_listing_names_its_stderr(mcp_list):
    m = load()
    mcp_list.script = _completed("", code=1, stderr="Error: failed to load bootstrap configuration\ninvalid transport")
    with pytest.raises(m.McpIsolationError) as e:
        m.mcp_off_args()
    assert "invalid transport" in str(e.value)


# --- command builders ---

def test_build_cmd_puts_the_switches_right_after_daybreak_false():
    m = load()
    sw = off("github")
    for cmd in (m.build_cmd("commit", commit="abc", env={}, mcp_args=sw),
                m.build_cmd("unpushed", base="origin/main", env={}, mcp_args=sw),
                m.build_cmd("arch", prompt="P", env={}, mcp_args=sw)):
        i = cmd.index("daybreak=false")
        assert cmd[i + 1:i + 1 + len(sw)] == sw
    assert m.build_cmd("commit", commit="abc", env={}) == m.build_cmd("commit", commit="abc", env={}, mcp_args=())


def test_the_daybreak_command_puts_the_switches_right_after_its_model_arguments():
    m = load()
    sw = off("github")
    assert m.daybreak_command("commit", commit="abc", env={}, mcp_args=sw) == [
        "codex", "exec", *m.daybreak_model_args({}), *sw, "--cyber-access-program", "daybreak_blue", "-s", "read-only",
        m.daybreak_review_prompt("abc")]
    assert m.daybreak_command("arch", prompt="P", env={}, mcp_args=sw)[-1] == "P"


# --- end to end ---

class FakeCodex:
    calls = []
    returncode = 0
    stderr_lines = []

    def __init__(self, cmd, **kw):
        FakeCodex.calls.append(list(cmd))
        self.stdout = iter(["No actionable defects were found.\n"] if FakeCodex.returncode == 0 else [])
        self.stderr = iter(FakeCodex.stderr_lines)
        self.returncode = FakeCodex.returncode

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
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    for name in ("CODEX_REVIEW_MODEL", "CODEX_REVIEW_EFFORT", "CODEX_REVIEW_DAYBREAK_MODEL", "CODEX_REVIEW_DAYBREAK_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    return path.resolve(), run("rev-list", "HEAD").stdout.split()


def review(monkeypatch, tmp_path, argv, *, returncode=0, refused=False):
    m = load()
    monkeypatch.setattr(m, "OUTPUT_FILE", str(tmp_path / "codex-review.txt"))
    monkeypatch.setattr(m, "git_unpushed_base", lambda: "HEAD~1")
    if refused:
        monkeypatch.setattr(m, "is_cyber_policy_refusal", lambda *a, **k: True)
    real_popen = subprocess.Popen
    monkeypatch.setattr(m.subprocess, "Popen",
                        lambda cmd, **kw: FakeCodex(cmd, **kw) if cmd and cmd[0] == "codex" else real_popen(cmd, **kw))
    FakeCodex.calls, FakeCodex.returncode = [], returncode
    FakeCodex.stderr_lines = ["session id: 00000000-0000-0000-0000-000000000001\n"] if refused else []
    monkeypatch.setattr(sys, "argv", ["codex-review", *argv])
    code = 0
    try:
        m.main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    path = tmp_path / "state" / "verdicts.jsonl"
    recs = [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []
    return code, recs, m


ARGVS = [["--commit", "HEAD"], ["--pin", "HEAD"], ["--arch"], ["--unpushed"]]


@pytest.mark.parametrize("argv", ARGVS, ids=["commit", "pin", "arch", "unpushed"])
def test_a_review_switches_off_the_servers_listed_in_the_directory_it_reviews(repo, mcp_list, monkeypatch, tmp_path, argv):
    path, heads = repo
    mcp_list.servers = lambda cwd: [("github", True), ("computer-use", False), ("projsrv", True)]
    code, recs, m = review(monkeypatch, tmp_path, argv)
    assert code in (0, None) and len(FakeCodex.calls) == 1
    cmd = FakeCodex.calls[0]
    i = cmd.index("daybreak=false")
    assert cmd[i + 1:i + 1 + len(off("github", "projsrv"))] == off("github", "projsrv")
    first = mcp_list.calls[0]["cwd"]
    if "--pin" in argv:
        assert Path(first).name.startswith("codex-pin-") and Path(first) != path, "listed inside the pinned worktree"
        assert [r["sha"] for r in recs] == [heads[0]], "a pinned review still records its completion"
    else:
        assert Path(first) == path, "listed in the reviewed checkout"
    assert len(mcp_list.calls) == 2


@pytest.mark.parametrize("argv", ARGVS, ids=["commit", "pin", "arch", "unpushed"])
def test_a_review_whose_feature_override_was_not_applied_does_not_start(repo, mcp_list, monkeypatch, tmp_path, capsys, argv):
    mcp_list.pinned = {"apps"}
    code, recs, m = review(monkeypatch, tmp_path, argv)
    assert code == 1 and FakeCodex.calls == [] and recs == []
    assert "apps" in capsys.readouterr().err


@pytest.mark.parametrize("argv", ARGVS, ids=["commit", "pin", "arch", "unpushed"])
def test_a_review_whose_servers_cannot_be_switched_off_does_not_start(repo, mcp_list, monkeypatch, tmp_path, capsys, argv):
    still_on = json.dumps([{"name": "github", "enabled": True}, {"name": "dot.name", "enabled": True}])
    mcp_list.script = lambda argv_, cwd: subprocess.CompletedProcess(argv_, 0, stdout=still_on, stderr="")
    code, recs, m = review(monkeypatch, tmp_path, argv)
    assert code == 1 and FakeCodex.calls == [] and recs == []
    assert "dot.name" in capsys.readouterr().err


FAKE_CODEX = """#!/bin/sh
if [ "$1 $2" = "features list" ]; then
  printf '%s\\n' "$*" > "$FAKE_ARGV.features"
  printf '%s' "$FAKE_FEATURES"
  exit "${FAKE_FEATURES_CODE:-0}"
fi
printf '%s\\n' "$*" > "$FAKE_ARGV"
printf '%s' "$FAKE_LISTING"
printf '%s' "$FAKE_STDERR" >&2
exit "$FAKE_CODE"
"""
FEATURES_OFF = "apps   stable   false\nplugins   stable   false\nshell_tool   stable   true\n"


@pytest.mark.parametrize("listing,code,ok", [
    ("[]", 0, True),
    ('[{"name": "a", "enabled": false}]', 0, True),
    ('[{"name": "a", "enabled": false}, {"name": "bsrv", "enabled": true}]', 0, False),
    ("[]", 3, False),
    ('[{"name": "a", "enabled": false}]', 1, False),
    ('[{"name": "a"}]', 0, False),
    ('[{"name": "a", "enabled": null}]', 0, False),
    ('["a"]', 0, False),
    ('{"name": "a", "enabled": false}', 0, False),
    ("not json", 0, False),
    ("", 0, False),
], ids=["empty", "all-off", "one-on", "empty-but-failed", "all-off-but-failed", "no-enabled", "enabled-null", "not-objects",
        "not-a-list", "not-json", "no-output"])
def test_the_guard_runs_the_listing_and_passes_only_a_successful_one_with_no_enabled_server(tmp_path, listing, code, ok):
    m = load()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(FAKE_CODEX)
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""), FAKE_LISTING=listing,
               FAKE_CODE=str(code), FAKE_STDERR="listing broke" if code else "", FAKE_ARGV=str(tmp_path / "argv"),
               FAKE_FEATURES=FEATURES_OFF)
    sw = off("github")
    r = subprocess.run([sys.executable, "-I", "-c", m.MCP_GUARD_CHECK, *sw], capture_output=True, text=True, env=env)
    assert (r.returncode == 0) is ok, r.stderr
    assert (tmp_path / "argv").read_text().split() == ["mcp", "list", "--json", *sw], "the guard runs the listing itself"
    if "bsrv" in listing:
        assert "bsrv" in r.stderr, "the enabled server is named"


@pytest.mark.parametrize("features,code,ok", [
    (FEATURES_OFF, 0, True),
    ("plugins   stable   false\nshell_tool   stable   true\n", 0, True),
    ("apps   stable   true\nplugins   stable   false\n", 0, False),
    ("agent_message_board_mcp   under development   true\n", 0, False),
    (FEATURES_OFF, 2, False),
    ("", 0, False),
    (" \n\n", 0, False),
], ids=["all-off", "apps-absent", "apps-on", "two-word-stage-on", "failed", "empty", "blank"])
def test_the_guard_also_requires_the_switched_features_off(tmp_path, features, code, ok):
    m = load()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "codex"
    fake.write_text(FAKE_CODEX)
    fake.chmod(0o755)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ.get("PATH", ""), FAKE_LISTING="[]", FAKE_CODE="0",
               FAKE_STDERR="", FAKE_ARGV=str(tmp_path / "argv"), FAKE_FEATURES=features, FAKE_FEATURES_CODE=str(code))
    sw = BASE + ["-c", 'mcp_servers={"github"={enabled=false}}']
    r = subprocess.run([sys.executable, "-I", "-c", m.MCP_GUARD_CHECK, *sw], capture_output=True, text=True, env=env)
    assert (r.returncode == 0) is ok, r.stderr
    assert (tmp_path / "argv.features").read_text().split() == ["features", "list", *sw]
    if "true" in features and "apps   stable   true" in features:
        assert "apps" in r.stderr


def test_the_guard_bounds_its_listings_in_time(monkeypatch):
    m = load()
    seen = []

    class FakeSubprocess:
        TimeoutExpired = subprocess.TimeoutExpired

        @staticmethod
        def run(cmd, **kw):
            seen.append((cmd[:3], kw.get("timeout")))
            out = "[]" if cmd[1] == "mcp" else "apps   stable   false\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setitem(sys.modules, "subprocess", FakeSubprocess)
    monkeypatch.setattr(sys, "argv", ["-c", *BASE])
    try:
        exec(m.MCP_GUARD_CHECK, {"__name__": "__main__"})
    except SystemExit as e:
        assert e.code in (None, 0), e.code
    assert [c for c, _ in seen] == [["codex", "mcp", "list"], ["codex", "features", "list"]]
    assert all(t == m.MCP_LIST_TIMEOUT for _, t in seen), "both runs of the manual guard use the listing timeout"


@pytest.mark.parametrize("argv", [["--commit", "HEAD"], ["--pin", "HEAD"]], ids=["commit", "pin"])
def test_a_refused_commit_review_prints_its_own_switches_behind_a_guard(repo, mcp_list, monkeypatch, tmp_path, capsys, argv):
    path, heads = repo
    trusted = lambda cwd: Path(cwd) == path or Path(cwd).name.startswith("codex-pin-")   # noqa: E731
    mcp_list.servers = lambda cwd: [("github", True)] + ([("projsrv", True)] if trusted(cwd) else [])
    code, recs, m = review(monkeypatch, tmp_path, argv, returncode=1, refused=True)
    assert code == 1 and len(FakeCodex.calls) == 1 and recs == []
    sw = off("github", "projsrv")
    err = capsys.readouterr().err
    assert guarded(m, sw, m.daybreak_command("commit", commit=heads[0], mcp_args=sw)) in err
    assert len(mcp_list.calls) == 2, "the review's own switches; no listing when printing"
    low = err.lower()
    assert "codex mcp list" in err and "any file" in low and "training" in low and "deploy" in low
    assert "call any mcp tool configured for codex" not in low
    assert "invalid transport" in err and "--pin" in err, "how to recover when the fresh worktree lacks a server"


def test_a_refused_architecture_review_reuses_its_own_switches_behind_a_guard(repo, mcp_list, monkeypatch, tmp_path, capsys):
    path, heads = repo
    mcp_list.servers = lambda cwd: [("github", True), ("projsrv", True)]
    code, recs, m = review(monkeypatch, tmp_path, ["--arch"], returncode=1, refused=True)
    assert code == 1 and len(FakeCodex.calls) == 1
    prompt = FakeCodex.calls[0][-1]
    sw = off("github", "projsrv")
    err = capsys.readouterr().err
    assert guarded(m, sw, m.daybreak_command("arch", prompt=prompt, mcp_args=sw)) in err
    assert "worktree add" not in err and len(mcp_list.calls) == 2
