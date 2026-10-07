"""Publish test fixture bytes through the real immutable snapshot contract."""

from tempfile import TemporaryFile


def snapshot_bytes(store, name, data, *, artifact_id=None):
    with TemporaryFile() as stream:
        stream.write(data)
        stream.flush()
        return store.snapshot(stream.fileno(), name, artifact_id=artifact_id)
