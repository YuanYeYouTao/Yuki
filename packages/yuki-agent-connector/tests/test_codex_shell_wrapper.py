"""Codex shows a command approval as the shell wrapper it runs (#647 D1).

Real codex-cli 0.169.0 / 0.171.0 send ``command`` as ``/bin/zsh -lc '<script>'``
(#720), so a narrowed scope's prefixes never matched and every Codex command
approval was reported. The driver now judges the script inside a recognised wrapper:
exactly ``<shell> -lc <script>`` where the shell is ``bash``, ``zsh`` or ``sh``,
named bare or by its absolute path in a system directory. A command that starts with
such a shell in any other shape is outside (fail closed); a command that does not
start with one is judged as it is. ``*`` is unchanged.

Failure cases written before the change:
- W1 a wrapped script whose segments are all inside (``pytest``, ``cd <write path>
  && pytest``) is still reported (red on 0.9.9 before this change);
- W2 a wrapped script with an ungranted segment, a ``cd`` out of ``write_paths``,
  ``$()`` or an empty script is auto-approved;
- W3 a wrapper of another shape is auto-approved: ``-c`` or ``-l -c`` instead of
  ``-lc``, no script, extra words after the script, words outside the quotes
  (``; rm``), unbalanced quotes, an unknown shell (``fish``), a relative shell path,
  or an absolute path outside the system directories (an agent-written ``zsh`` in
  the write path or ``/tmp``);
- W4 the same in list form (``["/bin/zsh", "-lc", script]``);
- W5 an unwrapped command changes meaning (``pytest -q`` stays inside, ``rm`` out);
- W6 ``*`` stops approving a malformed wrapper.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from test_codex_phase2 import reported, scoped_turn

INSIDE: list[Any] = [
    "/bin/zsh -lc 'pytest -q'",
    "/bin/zsh -lc 'cd {W} && pytest -q'",
    "/bin/bash -lc 'pytest -q; pytest'",
    "/usr/bin/bash -lc 'pytest'",
    "/opt/homebrew/bin/zsh -lc 'pytest'",
    "zsh -lc 'cd {W} && pytest -q'",
    "sh -lc pytest",
    ["/bin/zsh", "-lc", "cd {W} && pytest -q"],
    "pytest -q",
]

OUTSIDE: list[Any] = [
    "/bin/zsh -lc 'pytest && rm -rf ~'",
    "/bin/zsh -lc 'cd /etc && pytest'",
    "/bin/zsh -lc 'pytest $(id)'",
    "/bin/zsh -lc ''",
    "/bin/zsh -c 'pytest'",
    "/bin/zsh -l -c 'pytest'",
    "/bin/zsh -lc",
    "/bin/zsh 'pytest'",
    "/bin/zsh -lc 'pytest' extra",
    "/bin/zsh -lc 'pytest' ; rm -rf ~",
    "/bin/zsh -lc 'pytest",
    "/bin/fish -lc 'pytest'",
    "bin/zsh -lc 'pytest'",
    "./zsh -lc 'pytest'",
    "{W}/zsh -lc 'pytest'",
    "/tmp/zsh -lc 'pytest'",
    ["/bin/zsh", "-lc", "pytest", "extra"],
    ["/bin/zsh", "-c", "pytest"],
    ["/bin/zsh", "-lc", "pytest | tail"],
    "rm -rf ~",
]


def _fill(command: Any, work: Path) -> Any:
    if isinstance(command, list):
        return [item.replace("{W}", str(work)) for item in command]
    return command.replace("{W}", str(work))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "auto"), [(c, True) for c in INSIDE] + [(c, False) for c in OUTSIDE]
)
async def test_codex_wrapped_command_is_judged_by_its_script(
    tmp_path: Path, command: Any, auto: bool
) -> None:
    async with scoped_turn(tmp_path, ["pytest"]) as (owner, _driver, events, work):
        request = {"command": _fill(command, work), "kind": "command"}
        await owner.request(31, "item/commandExecution/requestApproval", "turn-1", **request)
        if auto:
            assert await asyncio.wait_for(owner.answers.get(), 3) == {
                "id": 31,
                "result": {"decision": "accept"},
            }
        else:
            payload = await reported(owner, events, 31)
            assert payload["native_request"]["command"] == request["command"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/bin/zsh -c 'rm -rf ~'", "/bin/zsh -lc 'x' ; rm"])
async def test_wildcard_still_approves_any_wrapper_w6(tmp_path: Path, command: str) -> None:
    async with scoped_turn(tmp_path, ["*"]) as (owner, _driver, _events, _work):
        await owner.request(32, "item/commandExecution/requestApproval", "turn-1", command=command)
        assert await asyncio.wait_for(owner.answers.get(), 3) == {
            "id": 32,
            "result": {"decision": "accept"},
        }
