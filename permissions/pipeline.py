"""
权限管线 — 三级门控

Gate 1: DENY_LIST 硬拒绝 (rm -rf /, sudo, ...)
Gate 2: RULES 规则匹配 (写工作区外? 破坏性命令?)
Gate 3: 用户审批 (命中规则时问用户 Allow? [y/N])
"""

from pathlib import Path


class PermissionPipeline:
    def __init__(self, workdir: Path):
        self.workdir = workdir

        # Gate 1: 硬拒绝列表，命中即阻断，无需用户确认
        self.deny_list = [
            "rm -rf /", "sudo", "shutdown", "reboot",
            "mkfs", "dd if=", "> /dev/sda",
        ]

        # Gate 2: 规则列表，命中后进入 Gate 3 问用户
        self.rules = [
            {
                "tools": ["write_file", "edit_file"],
                "check": lambda args: not (self.workdir / args.get("path", "")).resolve()
                    .is_relative_to(self.workdir.resolve()),
                "message": "Writing outside workspace",
            },
            {
                "tools": ["bash"],
                "check": lambda args: any(
                    kw in args.get("command", "") for kw in ["rm ", "> /etc/", "chmod 777"]
                ),
                "message": "Potentially destructive command",
            },
        ]

    def check(self, tool_name: str, tool_input: dict) -> str | None:
        """
        检查权限。
        返回 None 表示允许，返回字符串表示拒绝原因。
        """
        # Gate 1: 硬拒绝
        if tool_name == "bash":
            for pattern in self.deny_list:
                if pattern in tool_input.get("command", ""):
                    return f"Blocked: '{pattern}' is on the deny list"

        # Gate 2: 规则匹配
        for rule in self.rules:
            if tool_name in rule["tools"] and rule["check"](tool_input):
                # Gate 3: 用户审批
                return self._ask_user(tool_name, tool_input, rule["message"])

        return None

    def _ask_user(self, tool_name: str, args: dict, reason: str) -> str | None:
        """Gate 3: 暂停执行，询问用户是否允许"""
        print(f"\n\033[33m⚠  {reason}\033[0m")
        print(f"   Tool: {tool_name}({args})")
        choice = input("   Allow? [y/N] ").strip().lower()
        if choice in ("y", "yes"):
            return None  # 用户允许
        return "Permission denied by user"
