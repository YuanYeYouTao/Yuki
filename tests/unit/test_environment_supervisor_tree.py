import pytest

pytest.importorskip("termios")

from yuki.sandbox.environment_supervisor import children


def test_process_tree_keeps_parent_and_all_descendants_from_one_proc_view(monkeypatch):
    class Entry:
        def __init__(self, identity, parent):
            self.name, self.parent = str(identity), parent

        def __truediv__(self, _name):
            return self

        def read_text(self):
            return f"{self.name} (process with spaces) S {self.parent} 0 0"

    entries = [Entry(10, 1), Entry(11, 10), Entry(12, 10), Entry(13, 11)]
    entries.extend(Entry(i, i - 1) for i in range(100, 2100))
    monkeypatch.setattr("pathlib.Path.iterdir", lambda _path: iter(entries))
    assert children(10) == {10, 11, 12, 13}
    assert children(99) == set(range(99, 2100))
    assert children(4040) == {4040}
