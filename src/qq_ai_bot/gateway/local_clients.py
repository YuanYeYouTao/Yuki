"""Transport facts shared by first-party HTTP clients."""

LOCAL_CLIENT_PLATFORMS = frozenset({"app", "acp"})


def local_account_id(platform: str) -> str:
    return f"yuki-{platform}"
