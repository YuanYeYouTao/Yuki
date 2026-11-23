"""The group command grammar shared with the pre-ingress recovery adapter."""

GROUP_USAGE = "/ai admin group <current|Space ID> enable|disable"
# `/ai off` and `/ai on` are the in-group spelling of the same Space switch.
GROUP_SWITCH = {"off": "group current disable", "on": "group current enable"}


def parse_group_command(argument: str) -> tuple[str, str] | None:
    parts = argument.split()
    if (
        len(parts) != 3
        or parts[0].casefold() != "group"
        or parts[2].casefold() not in {"enable", "disable"}
    ):
        return None
    return parts[1], parts[2].casefold()
