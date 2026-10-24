"""Stable Manager keys shared by personal commands and resource revocation."""

from uuid import NAMESPACE_URL, uuid5

CONTROL_NAMESPACE = uuid5(NAMESPACE_URL, "https://yuki.life/computer-control/v1")


def manager_key(scope: str, request_id: str) -> str:
    return str(uuid5(CONTROL_NAMESPACE, "client:" + scope + ":" + request_id))


def release_key(lease_id: str) -> str:
    return str(uuid5(CONTROL_NAMESPACE, "release:" + lease_id))
