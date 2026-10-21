"""User-facing syntax owned by the finite chat command groups."""

SELF_HELP = """Yuki 命令：
/ai help | ping | status | whoami | capabilities [类别]
/ai new | compact [重点] | stop（当前会话；群内 new/compact 需要管理员资格）
/ai link [code]（私聊配对或关联本人账号）
/ai forgetme（删除本人数据）
/ai memory list|add|update|delete|evidence|show|explain|history|conflicts|correct|invalidate|restore
/ai preference list|set|delete
/ai automation list|completed|show|pause|resume|cancel|run|history
/ai plugin run <plugin_id> <command> [参数]
/ai admin [领域]（人物管理员操作）"""

# This is syntax data consumed by the finite parsers and help; it grants no authority.
ADMIN_ACTIONS = {
    "person": (
        "list",
        "show",
        "create",
        "enable",
        "disable",
        "grant",
        "revoke",
        "rename",
        "merge",
        "forget",
    ),
    "binding": ("list", "show", "attach", "enable", "disable", "unbind", "transfer"),
    "presence": (
        "list",
        "show",
        "register",
        "unregister",
        "transfer",
        "enable",
        "disable",
        "ingest-enable",
        "ingest-disable",
    ),
    "config": ("list", "get", "set", "unset", "history", "rollback"),
    "plugin": (
        "list",
        "show",
        "permissions",
        "discover",
        "approve",
        "enable",
        "disable",
        "doctor",
        "configuration",
        "configure",
    ),
    "work": ("list", "show", "cancel", "resume"),
    "model": ("show", "stats"),
    "memory": (
        "list",
        "add",
        "update",
        "delete",
        "evidence",
        "search",
        "show",
        "explain",
        "history",
        "conflicts",
        "correct",
        "invalidate",
        "restore",
        "merge",
        "resolve",
        "doctor",
        "maintenance",
        "index",
        "embedding",
        "self-reflection",
        "dream",
        "rebuild",
    ),
    "preference": ("list", "set", "delete"),
}

# Presentation only. Domain authorization and writes do not consult this catalog.
ADMIN_READ_ACTIONS = {
    "person": frozenset({"list", "show"}),
    "binding": frozenset({"list", "show"}),
    "presence": frozenset({"list", "show"}),
    "config": frozenset({"list", "get", "history"}),
    "plugin": frozenset({"list", "show", "permissions", "doctor", "configuration"}),
    "work": frozenset({"list", "show"}),
    "model": frozenset({"show", "stats"}),
    "memory": frozenset(
        {"list", "evidence", "search", "show", "explain", "history", "conflicts", "doctor"}
    ),
    "preference": frozenset({"list"}),
}

# Memory's long-running domains have concrete subcommands with different effects.
ADMIN_SUBACTIONS = {
    "memory.index": {"status": False, "rebuild": True},
    "memory.embedding": {
        "status": False,
        "doctor": False,
        "retry": True,
        "rebuild": True,
        "purge-old": True,
    },
    "memory.maintenance": {"status": False, "run": True},
    "memory.self-reflection": {"status": False, "run": True, "retry": True},
    "memory.dream": {
        "plan": True,
        "list": False,
        "show": False,
        "status": False,
        "preview": True,
        "start": True,
        "cancel": True,
        "resume": True,
        "retry": True,
        "rollback": True,
    },
    "memory.rebuild": {
        "plan": True,
        "list": False,
        "status": False,
        "review": False,
        "start": True,
        "pause": True,
        "resume": True,
        "cancel": True,
        "approve": True,
        "reject": True,
        "commit": True,
        "retry": True,
        "purge": True,
    },
}


def admin_usage(family: str) -> str:
    actions = ADMIN_ACTIONS[family]
    return f"/ai admin {family} " + "|".join(actions)


ADMIN_HELP = "管理员命令：\n" + "\n".join(admin_usage(family) for family in ADMIN_ACTIONS)
ADMIN_HELP += "\n/ai admin group <current|Space ID> enable|disable"
ADMIN_HELP += "\n/ai admin operation <operation ID>（查询原操作回执）"
ADMIN_HELP += "\n/ai admin work list [state|all] [cursor]（下一页沿用 next_cursor）"
ADMIN_HELP += "\n/ai admin binding list [Person ID|all] [cursor]"
ADMIN_HELP += "\n/ai admin presence register <platform> <account> [Assignment ID revision]"
ADMIN_HELP += (
    "\n/ai admin presence transfer <Presence ID> <Person ID>；unregister 结束归属，disable 只暂停。"
)
ADMIN_HELP += "\n人物/账号/Work 使用内部 ID；模型文件和密钥使用管理 CLI。"
