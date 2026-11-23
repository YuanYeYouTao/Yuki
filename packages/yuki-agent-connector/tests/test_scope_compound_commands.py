"""Compound commands in a narrowed delegation scope (#647 D1, owner approval 2026-11-23).

A narrowed scope (``commands`` without ``*``) used to report every command containing
any of ``;&|<>`$()`` or a newline. Native agents almost always write
``cd <dir> && cmd``, so granted prefixes rarely matched. The approved rule:

- ``&&``, ``||`` and ``;`` split a command into segments (every literal occurrence,
  quoted or not); every segment must be inside the scope.
- A segment is inside when it matches a granted prefix by the existing rule
  (``segment == prefix`` or ``segment.startswith(prefix + " ")``), or is exactly
  ``cd <absolute directory>`` whose resolved target lies in ``write_paths``.
- Anything else stays outside: pipes, redirections, ``$``, backticks, newlines,
  carriage returns, background ``&``, subshells, empty segments, text shlex cannot
  parse. ``*`` keeps meaning every command.

Failure cases written before the change (all must be judged outside the scope):
U  an ungranted segment anywhere, joined by ``&&``, ``||`` or ``;``;
P  pipes and background: ``|``, ``|&``, ``&``, ``&>``, ``;&``, ``&&&``, ``|||``;
R  redirections and heredocs, ``2>&1`` included;
S  substitution and expansion: ``$()``, backticks, ``$VAR``, ``${}``, ``<()``;
G  subshells, groups, functions, shell keywords;
N  newline, carriage return, line continuation, a trailing newline;
Q  control characters inside quotes or escaped, and ``#`` comments hiding a segment;
E  empty segments: leading or trailing separators, ``;;``, ``; ;``, ``|| ||``;
X  text shlex cannot parse (unbalanced quotes, a trailing backslash);
B  prefix boundaries: ``pytestx``, ``git push``, tab or quotes around the prefix,
   leading Unicode spaces, full-width ``；``, assignments and ``env`` in front;
C  ``cd`` escapes: ``..`` components, ``/``, a directory outside ``write_paths``, a
   symlink leading out, ``~``, no target, ``-``, options, two targets, relative
   targets (CDPATH, unknown shell cwd), globs, braces, zsh extended-glob characters,
   backslashes, a quoted or escaped ``cd``, ``CDPATH=`` or ``pushd`` in front.
W  ``*`` still allows everything above, and an empty command stays outside.
I  the common shapes are inside (these fail on the 0.9.8 rule).
D  the Claude driver auto-approves inside compound commands and reports the others.
H  real shells: every command judged inside runs only granted programs (whole-word
   prefix of argv) and only in the start directory or inside ``write_paths``.
   Artifact: test-results/connector-stability/scope-compound-<platform>.json.
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_claude_phase2 import answered, response_for, started, tool_request, until

from yuki_agent_connector.scope import Scope

PREFIXES = ["pytest", "git log", "git status", "ls"]

INSIDE = [
    "pytest -q && git status",
    "pytest; ls",
    "pytest -q;ls",
    "pytest || git status",
    "  pytest -q  &&  ls  ",
    "git log --oneline -5 && git status || ls",
    "cd {W} && pytest -q",
    "cd {W}/sub && pytest tests; git status",
    "cd '{W}/dir with space' && ls",
    'cd "{W}/sub/" && ls -la',
    "cd {W}",
    "cd {W}/missing && ls",
    "git log && cd {W} && pytest",
]

OUTSIDE = {
    "U": [
        "pytest && rm -rf ~",
        "pytest; rm -rf ~",
        "pytest || rm -rf ~",
        "rm -rf ~ && pytest",
        "git log; git push",
        "ls && cd {W} && rm x",
    ],
    "P": [
        "pytest | tail",
        "pytest |& ls",
        "pytest & ls",
        "pytest &",
        "pytest &> log",
        "pytest ;& ls",
        "pytest &&& ls",
        "pytest ||| ls",
        "pytest &| ls",
        "pytest &! ls",
    ],
    "R": [
        "pytest > f",
        "pytest >> f && ls",
        "pytest 2>&1",
        "pytest < f",
        "pytest <<< x",
        "ls <<EOF\nx\nEOF",
        "cat > f <<'EOF'\nx\nEOF",
    ],
    "S": [
        "pytest $(id)",
        'pytest "$(id)"',
        "pytest '$(id)'",
        "pytest `id`",
        'pytest "`id`"',
        "pytest $HOME",
        "pytest ${X}",
        "cd $HOME && ls",
        "pytest <(id)",
    ],
    "G": [
        "(pytest)",
        "pytest; (rm -rf ~)",
        "{ rm -rf ~; }",
        "pytest() { rm -rf ~; }; pytest",
        "if pytest; then rm -rf ~; fi",
        "for f in a; do rm f; done",
        "while pytest; do ls; done",
        "! rm -rf ~",
        "case x in x) rm;; esac",
        "time rm -rf ~",
    ],
    "N": [
        "pytest\nrm -rf ~",
        "pytest -q\n",
        "\npytest",
        "pytest\r\nls",
        "pytest -q\rls",
        "pytest \\\nrm -rf ~",
    ],
    "Q": [
        'git log --grep "x; rm -rf ~"',
        "git log --grep 'a && rm -rf ~'",
        "git log --grep ';'",
        'pytest "a|b"',
        "pytest '&&' rm",
        "pytest \\; rm -rf ~",
        "pytest \\&\\& rm -rf ~",
        "pytest # && rm -rf ~",
        "pytest -q \\||git status",
        "pytest \\&&ls",
        "pytest \\;ls",
        "pytest \\\\; ls",
    ],
    "E": [
        "pytest &&",
        "&& pytest",
        "pytest ;; ls",
        "pytest; ; ls",
        ";",
        "&&",
        "pytest || || ls",
        "pytest;",
    ],
    "X": [
        'git log "x && git status',
        "git log 'x; ls",
        "pytest \\",
        "cd '{W} && ls",
    ],
    "B": [
        "pytestx && ls",
        "git logx; ls",
        "git push && ls",
        "git\tlog && ls",
        '"pytest" && ls',
        "p\\ytest && ls",
        "\u00a0pytest && ls",
        "\u3000pytest; ls",
        "pytest\u00a0&& ls",
        "pytest\uff1brm -rf ~",
        "PATH=/tmp/e pytest && ls",
        "env X=1 pytest; ls",
    ],
    "C": [
        "cd .. && pytest",
        "cd {W}/.. && ls",
        "cd {W}/sub/../.. && ls",
        "cd {W}/sub/.. && ls",
        "cd / && ls",
        "cd {O} && ls",
        "cd {W}/link && ls",
        "cd {W}/link/sub && ls",
        "cd {W}/link/.. && ls",
        "cd ~ && ls",
        "cd ~/x && ls",
        "cd && ls",
        "cd",
        "cd - && ls",
        "cd -P {W} && ls",
        "cd -- {W} && ls",
        "cd {W} {W} && ls",
        "cd sub && ls",
        "cd ./sub && ls",
        "cd {W} && cd sub && ls",
        "cd {W}/* && ls",
        "cd {W}/s?b && ls",
        "cd {W}/[s]ub && ls",
        "cd {W}/{sub,x} && ls",
        "cd {W}/^x && ls",
        "cd {W}/#x && ls",
        "cd {W}\\sub && ls",
        '"cd" {W} && ls',
        "c\\d {W} && ls",
        "cd\t{W} && ls",
        "CDPATH={O} cd sub && ls",
        "pushd {W} && ls",
        "cd '{W}/a;b' && ls",
        "cd {W}; cd {O}",
    ],
}


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    # Long, resolved names: a Windows runner's 8.3 temp path contains ``~``.
    root = tmp_path.resolve()
    work, outside, start = root / "work", root / "outside", root / "start"
    for directory in (work / "sub", work / "dir with space", outside / "sub", start):
        directory.mkdir(parents=True)
    (work / "link").symlink_to(outside, target_is_directory=True)
    return {"W": work, "O": outside, "start": start}


def _scope(tree: dict[str, Path], *commands: str) -> Scope:
    scope = Scope.parse(
        {"write_paths": [str(tree["W"])], "commands": list(commands or PREFIXES)},
        str(tree["start"]),
    )
    assert scope is not None
    return scope


def _fill(command: str, tree: dict[str, Path]) -> str:
    return command.replace("{W}", tree["W"].as_posix()).replace("{O}", tree["O"].as_posix())


@pytest.mark.parametrize("command", INSIDE)
def test_inside_compound_commands(tree: dict[str, Path], command: str) -> None:
    assert _scope(tree).runs(_fill(command, tree))


@pytest.mark.parametrize(
    "command",
    [
        pytest.param(command, id=f"{case}{index}")
        for case, rows in OUTSIDE.items()
        for index, command in enumerate(rows)
    ],
)
def test_outside_constructs(tree: dict[str, Path], command: str) -> None:
    assert not _scope(tree).runs(_fill(command, tree))


def test_lists_are_joined_and_never_split(tree: dict[str, Path]) -> None:
    scope = _scope(tree)
    assert scope.runs(["pytest", "-q", "a b"])
    assert not scope.runs(["pytest", "a;b"])
    assert not scope.runs(["git", "log", "&&", "git", "status"])
    assert not scope.runs(["bash", "-c", "pytest; ls"])


def test_wildcard_is_unchanged(tree: dict[str, Path]) -> None:
    scope = _scope(tree, "*")
    for rows in OUTSIDE.values():
        for command in rows:
            text = _fill(command, tree)
            assert scope.runs(text) is bool(text.strip()), text
    assert not scope.runs("")
    assert not scope.runs(" \t")


# --- D: the Claude driver ------------------------------------------------------------


@pytest.mark.asyncio
async def test_claude_auto_approves_inside_compound_commands_and_reports_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver, transports, _, events = await started(tmp_path, monkeypatch)
    work = (tmp_path / "work").resolve()
    work.mkdir()
    try:
        await driver.execute(
            {
                "id": "delegated",
                "action": "prompt",
                "arguments": {
                    "prompt": "run the tests",
                    "cwd": str(tmp_path),
                    "scope": {"write_paths": [str(work)], "commands": ["pytest", "ls"]},
                },
            }
        )
        await until(events, lambda kind, value: value["operation_id"] == "delegated")
        transport = transports[0]
        for key, command in [
            ("cd-test", f"cd {work.as_posix()} && pytest -q"),
            ("sequence", "pytest -q; ls"),
        ]:
            await transport.incoming.put(tool_request(key, "Bash", {"command": command}))
            assert (await answered(transport, f"request-{key}"))["behavior"] == "allow"
        for key, command in [
            ("cd-out", f"cd {work.parent.as_posix()} && pytest -q"),
            ("chained", "pytest && rm -rf ~"),
            ("piped", "pytest | tail"),
        ]:
            await transport.incoming.put(tool_request(key, "Bash", {"command": command}))
            _kind, reported = await until(events, lambda kind, value: kind == "permission")
            assert reported["payload"]["native_request_id"] == key
            assert response_for(transport, f"request-{key}") is None
    finally:
        await driver.stop()


# --- H: real shells ------------------------------------------------------------------

ARTIFACT = Path(__file__).resolve().parents[3] / "test-results" / "connector-stability"
GRANTED = ("pytest", "git", "ls")
STUBS = (*GRANTED, "rm", "id", "curl", "touch", "tail", "cat", "env", "evil", "sh", "bash")
SPLITS = re.compile(r"&&|\|\||;")
SEED = 647
SAMPLES = 4000
LIMITS = (
    "PATH holds only logging stubs, so only programs found through PATH are observed; "
    "absolute or relative program paths, user aliases, functions and chpwd hooks, "
    "PowerShell and cmd.exe are not covered. No violation here is an observation on "
    "this corpus and these shells, not a proof of safety."
)


def _shells() -> list[list[str]]:
    found: dict[str, list[str]] = {}
    for name, flags in (
        ("bash", ["--norc", "--noprofile"]),
        ("zsh", ["-f"]),
        ("dash", []),
        ("sh", []),
    ):
        for path in {shutil.which(name), f"/bin/{name}"}:
            if path and Path(path).exists():
                found.setdefault(os.path.realpath(path), [path, *flags])
    return list(found.values())


def _corpus(tree: dict[str, Path]) -> list[str]:
    """Fixed-seed joins of granted, ungranted and ``cd`` segments with noisy tails."""
    work, outside = tree["W"].as_posix(), tree["O"].as_posix()
    heads = [
        "pytest",
        "pytest -q",
        "git log",
        "git status",
        "ls",
        "cd " + work,
        f"cd {work}/sub",
        f"cd {work}/link",
        f"cd {work}/missing",
        "cd " + outside,
        f"cd '{work}/dir with space'",
        "rm",
        "evil",
        "git push",
        "cd",
        "cd ..",
        "'pytest'",
        "p\\ytest",
        "pytestx",
    ]
    tails = [
        " -q",
        " x",
        " 'a b'",
        ' "a;b"',
        " 'x && evil'",
        " \\;",
        " \\&",
        " #",
        " '",
        ' "',
        " \\",
        " ~",
        " *",
        " evil",
        " rm",
        " cd",
        "\t-q",
        " (",
        ")",
        " {",
        " }",
        " !",
        " =x",
        " $",
        " `",
        " %",
        " ^",
        " a#b",
    ]
    separators = [
        " && ",
        "&&",
        " || ",
        "||",
        "; ",
        ";",
        " ;",
        " | ",
        " & ",
        "\n",
        " ;; ",
        " &&& ",
        " |& ",
    ]
    generator = random.Random(SEED)
    corpus = []
    for _ in range(SAMPLES):
        segments = []
        for _ in range(generator.randint(1, 4)):
            segment = generator.choice(heads)
            for _ in range(generator.choices([0, 1, 2, 3], [5, 3, 1, 1])[0]):
                segment += generator.choice(tails)
            segments.append(segment)
        text = segments[0]
        for segment in segments[1:]:
            text += generator.choice(separators) + segment
        corpus.append(text)
    return corpus


@pytest.mark.skipif(os.name == "nt", reason="POSIX shells only")
def test_real_shells_run_only_granted_programs_inside_write_paths(tree: dict[str, Path]) -> None:
    stubs, home = tree["start"].parent / "stubs", tree["start"].parent / "home"
    stubs.mkdir()
    home.mkdir()
    for name in STUBS:
        stub = stubs / name
        stub.write_text(
            "#!/bin/sh\n"
            "line=\"$(pwd -P)$(printf '\\037')${0##*/}\"\n"
            'for a in "$@"; do line="$line$(printf \'\\037\')$a"; done\n'
            "# One write per call: piped stubs must not interleave.\n"
            'printf \'%s\\n\' "$line" >> "$YUKI_LOG"\n'
        )
        stub.chmod(0o755)
    scope = _scope(tree)
    commands = [_fill(command, tree) for command in INSIDE] + _corpus(tree)
    inside = [command for command in commands if scope.runs(command)]
    compound = [command for command in inside if SPLITS.search(command)]
    log = tree["start"].parent / "calls.log"
    shells = _shells()
    violations: list[dict[str, object]] = []
    runs = 0
    for shell in shells:
        for command in inside:
            log.write_text("")
            subprocess.run(
                [*shell, "-c", command],
                cwd=tree["start"],
                env={"PATH": str(stubs), "HOME": str(home), "YUKI_LOG": str(log)},
                capture_output=True,
                timeout=10,
                check=False,
            )
            runs += 1
            for line in log.read_text().splitlines():
                cwd, *argv = line.split("\x1f")
                place = Path(cwd)
                granted = any(argv[: len(p.split())] == p.split() for p in PREFIXES)
                placed = place == tree["start"] or tree["W"] in (place, *place.parents)
                if not (granted and placed):
                    violations.append(
                        {"shell": shell[0], "command": command, "call": argv, "cwd": cwd}
                    )
    versions = {
        shell[0]: subprocess.run(
            [shell[0], "--version"], capture_output=True, text=True, check=False
        ).stdout.splitlines()[:1]
        for shell in shells
    }
    ARTIFACT.mkdir(parents=True, exist_ok=True)
    (ARTIFACT / f"scope-compound-{sys.platform}.json").write_text(
        json.dumps(
            {
                "seed": SEED,
                "samples": SAMPLES,
                "fixed_inside": len(INSIDE),
                "judged_inside": len(inside),
                "judged_inside_compound": len(compound),
                "shell_runs": runs,
                "shells": versions,
                "violations": violations,
                "limits": LIMITS,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    assert shells
    assert len(compound) > len(INSIDE)
    assert violations == []
