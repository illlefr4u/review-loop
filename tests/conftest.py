"""Shared test fixture: a scripted `codex mcp list` so no test depends on the Codex CLI or configuration of the machine."""
import json
import os
import subprocess
from pathlib import Path

import pytest


ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}


def parse_off_table(text):
    """Strictly parse '{"<k>"={enabled=false},...}' (TOML basic-string keys, no spaces) into the list of keys."""
    names, i = [], 0

    def expect(token):
        nonlocal i
        if not text.startswith(token, i):
            raise ValueError(f"expected {token!r} at {i}")
        i += len(token)

    expect("{")
    if text == "{}":
        return names
    while True:
        expect('"')
        chars = []
        while True:
            ch = text[i]
            if ch == '"':
                i += 1
                break
            if ch == "\\":
                code = text[i + 1]
                if code in ESCAPES:
                    chars.append(ESCAPES[code])
                    i += 2
                elif code in "uU":
                    width = 4 if code == "u" else 8
                    chars.append(chr(int(text[i + 2:i + 2 + width], 16)))
                    i += 2 + width
                else:
                    raise ValueError(f"bad escape at {i}")
            elif ord(ch) < 0x20 or ord(ch) == 0x7F:
                raise ValueError(f"unescaped control character at {i}")
            else:
                chars.append(ch)
                i += 1
        names.append("".join(chars))
        expect("={enabled=false}")
        if text.startswith(",", i):
            i += 1
            continue
        expect("}")
        if i != len(text):
            raise ValueError("trailing text")
        return names


class McpList:
    """Answers `codex mcp list --json ...` calls made through subprocess.run.

    `servers(cwd)` returns [(name, enabled), ...] for the directory the call runs in. Like Codex, only the last
    `-c mcp_servers=<inline table>` override counts (separate overrides of one key replace each other); the table
    `{"<name>"={enabled=false},...}` switches those servers off, and one that does not parse fails the listing.
    `script(argv, cwd)`, when set, answers instead.

    It also answers `codex features list ...` in the format of Codex (name, stage, true/false): `features` maps a feature
    to its configured value, `-c features.<name>=false` turns it off unless the name is in `pinned` (as managed
    requirements would), and `features_script(argv, cwd)`, when set, answers instead.
    """

    FEATURES = {"plugins": True, "apps": True, "agent_message_board_mcp": False, "skill_mcp_dependency_install": True,
                "multi_agent_v2": False, "shell_tool": True}
    STAGES = {"agent_message_board_mcp": "under development"}

    def __init__(self):
        self.servers = lambda cwd: []
        self.script = None
        self.calls = []
        self.features = dict(self.FEATURES)
        self.pinned = set()
        self.features_script = None
        self.feature_calls = []

    def answer_features(self, argv, kw):
        cwd = str(Path(kw.get("cwd") or os.getcwd()).resolve())
        self.feature_calls.append({"argv": list(argv), "cwd": cwd, "env": kw.get("env"), "timeout": kw.get("timeout")})
        if self.features_script is not None:
            return self.features_script(list(argv), cwd)
        values = dict(self.features)
        for flag, value in zip(argv, argv[1:]):
            if flag == "-c" and value.startswith("features.") and value.endswith("=false"):
                name = value[len("features."):-len("=false")]
                if name in values and name not in self.pinned:
                    values[name] = False
        lines = [f"{name:<41}{self.STAGES.get(name, 'stable'):<19}{'true' if on else 'false'}"
                 for name, on in sorted(values.items())]
        return subprocess.CompletedProcess(list(argv), 0, stdout="\n".join(lines) + "\n", stderr="")

    def answer(self, argv, kw):
        cwd = str(Path(kw.get("cwd") or os.getcwd()).resolve())
        self.calls.append({"argv": list(argv), "cwd": cwd, "env": kw.get("env"), "timeout": kw.get("timeout")})
        if self.script is not None:
            return self.script(list(argv), cwd)
        table = None
        for flag, value in zip(argv, argv[1:]):
            if flag == "-c" and value.startswith("mcp_servers="):
                table = value[len("mcp_servers="):]
        try:
            off = set(parse_off_table(table)) if table is not None else set()
        except (ValueError, IndexError) as exc:
            return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr=f"Error: invalid -c mcp_servers: {exc}")
        data = [{"name": name, "enabled": bool(enabled) and name not in off, "disabled_reason": None,
                 "transport": {"type": "stdio"}} for name, enabled in self.servers(cwd)]
        return subprocess.CompletedProcess(list(argv), 0, stdout=json.dumps(data), stderr="")


@pytest.fixture(autouse=True)
def mcp_list(monkeypatch):
    fake = McpList()
    real_run = subprocess.run

    def run(cmd, *args, **kw):
        if isinstance(cmd, (list, tuple)) and list(cmd[:3]) == ["codex", "mcp", "list"]:
            return fake.answer(cmd, kw)
        if isinstance(cmd, (list, tuple)) and list(cmd[:3]) == ["codex", "features", "list"]:
            return fake.answer_features(cmd, kw)
        return real_run(cmd, *args, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    return fake
