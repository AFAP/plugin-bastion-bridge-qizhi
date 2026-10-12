"""危险操作兜底（Dangerous Command Guard）。

桥接器收到指令后先经过本模块判定：命中危险规则时**不下发到目标服务器**，
只向调用方返回一句「危险操作，请手动处理」的提示。

**对外只暴露一句话**：响应与普通执行结果同形（``stdout``/``stderr``/``exit_code``，
其中 ``exit_code`` 为 126），不返回规则名、原因等内部策略信息——规则细节只留在本模块。

纯标准库实现，可被 ``ssh_client`` 与 ``api_server`` 共用。三类规则互补：
程序名（解析出真正执行的程序后比对，``sudo rm``、``sh -c "rm"`` 都能识别）、
子命令（``systemctl stop``，不误伤 ``systemctl status``）、整条命令正则（裸设备重定向、fork 炸弹等）。

尽力而为的字面特征兜底，不是沙箱；安全边界仍是操作授权与堡垒机审计。
可配置项见 ``config.py``（``dangerous_command_guard_*``，只在源码中说明，不写入对外文档）。
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Pattern, Sequence, Tuple

logger = logging.getLogger("QizhiSSHBridge.command_guard")

#: 拒绝执行时返回的退出码：沿用 shell「命令存在但不可执行」的约定（126）。
BLOCKED_EXIT_CODE = 126

#: 默认提示文案（唯一对外可见的策略信息）。
DEFAULT_MESSAGE = (
    "⛔ 危险操作，请手动处理。\n"
    "该命令未在目标服务器上执行；如确需执行，请由授权人员手动完成。"
)

# ----------------------------------------------------------------------
# 规则定义
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class DangerousRule:
    """一条危险操作规则。

    Attributes:
        name: 规则标识（仅内部使用，可被配置停用）。
        description: 中文说明（仅内部使用）。
        programs: 程序名正则（对程序 basename 做 ``fullmatch``）。
        patterns: 整条命令正则（对原命令做 ``search``）。
        subcommands: 仅当命令中出现这些子命令之一时才拦截。
        subcommand_match: ``first`` 只看程序后第一个非选项参数，``any`` 检查所有非选项参数。
    """

    name: str
    description: str
    programs: Tuple[Pattern[str], ...] = ()
    patterns: Tuple[Pattern[str], ...] = ()
    subcommands: Tuple[str, ...] = field(default_factory=tuple)
    subcommand_match: str = "first"

    def matches_program(self, name: str) -> bool:
        """程序 basename 是否命中本规则的程序名正则。"""
        return bool(name) and any(pattern.fullmatch(name) for pattern in self.programs)

    def matches_subcommand(self, tokens: Sequence[str]) -> bool:
        """命令中是否出现被拦截的子命令。"""
        positional = [token for token in tokens if not token.startswith("-")]
        if not positional:
            return False
        if self.subcommand_match == "any":
            return any(token in self.subcommands for token in positional)
        return positional[0] in self.subcommands


def _compile(*expressions: str) -> Tuple[Pattern[str], ...]:
    """把正则字符串编译成元组（编译失败直接抛出，属于代码缺陷）。"""
    return tuple(re.compile(expression) for expression in expressions)


def _compile_optional(expressions: Iterable[str], source: str) -> Tuple[Pattern[str], ...]:
    """编译用户配置的正则；非法表达式只记日志、不阻断启动。"""
    compiled: List[Pattern[str]] = []
    for expression in expressions:
        text = str(expression).strip()
        if not text:
            continue
        try:
            compiled.append(re.compile(text))
        except re.error as exc:
            logger.warning("忽略非法正则（%s）：%r -> %s", source, text, exc)
    return tuple(compiled)


#: 内置规则。默认只拦截「不可逆 / 数据丢失 / 主机与服务中断 / 安全策略变更」类操作，
#: 只读查询类命令（``systemctl status``、``df``、``free``、``crontab -l``）不在其中。
_DEFAULT_RULES: Tuple[DangerousRule, ...] = (
    DangerousRule(
        "delete",
        "删除类命令，可能造成数据丢失",
        programs=_compile(r"rm", r"rmdir", r"shred", r"unlink", r"srm", r"wipe"),
        patterns=_compile(r"(?:^|\s)-delete(?:\s|$)", r"\bkubectl\b[^\n]*\bdelete\b"),
    ),
    DangerousRule(
        "disk",
        "磁盘 / 分区 / 文件系统破坏性操作",
        programs=_compile(
            r"dd",
            r"mkfs(?:\..+)?",
            r"mke2fs",
            r"mkdosfs",
            r"mkswap",
            r"fdisk",
            r"sfdisk",
            r"cfdisk",
            r"gdisk",
            r"sgdisk",
            r"parted",
            r"partprobe",
            r"wipefs",
            r"blkdiscard",
            r"badblocks",
            r"mdadm",
            r"cryptsetup",
            r"pvremove",
            r"vgremove",
            r"lvremove",
            r"zpool",
            r"zfs",
        ),
    ),
    DangerousRule(
        "power",
        "关机 / 重启 / 停机",
        programs=_compile(r"shutdown", r"reboot", r"halt", r"poweroff", r"telinit", r"init"),
    ),
    DangerousRule(
        "kill",
        "结束进程",
        programs=_compile(r"kill", r"killall", r"killall5", r"pkill", r"fuser"),
    ),
    DangerousRule(
        "service",
        "停止 / 禁用服务",
        programs=_compile(r"systemctl", r"service"),
        subcommands=(
            "stop",
            "restart",
            "disable",
            "mask",
            "kill",
            "isolate",
            "poweroff",
            "reboot",
            "halt",
            "emergency",
            "rescue",
            "daemon-reexec",
            "set-property",
        ),
        subcommand_match="any",
    ),
    DangerousRule(
        "container",
        "停止 / 删除容器",
        programs=_compile(r"docker", r"podman", r"nerdctl"),
        subcommands=("rm", "rmi", "kill", "stop", "restart", "prune", "down", "pause"),
        subcommand_match="any",
        patterns=_compile(
            r"\b(?:docker|podman|nerdctl)\s+volume\s+rm\b",
            r"\b(?:docker|podman|nerdctl)\b[^\n]*\bcompose\b[^\n]*\b(?:down|rm)\b",
        ),
    ),
    DangerousRule(
        "firewall",
        "防火墙 / 安全策略变更",
        programs=_compile(
            r"iptables",
            r"ip6tables",
            r"iptables-restore",
            r"ip6tables-restore",
            r"nft",
            r"ufw",
            r"firewall-cmd",
            r"setenforce",
        ),
    ),
    DangerousRule(
        "account",
        "账户 / 权限变更",
        programs=_compile(
            r"userdel",
            r"groupdel",
            r"usermod",
            r"groupmod",
            r"gpasswd",
            r"chpasswd",
            r"passwd",
            r"chsh",
            r"visudo",
        ),
    ),
    DangerousRule(
        "schedule",
        "计划任务变更",
        patterns=_compile(r"\bcrontab\b[^\n]*?\s-[re](?:\s|$)"),
    ),
    DangerousRule(
        "unmount",
        "卸载文件系统 / 关闭交换分区",
        programs=_compile(r"umount", r"swapoff"),
    ),
    DangerousRule(
        "truncate",
        "清空文件内容",
        programs=_compile(r"truncate"),
    ),
    DangerousRule(
        "device-write",
        "直接写入裸设备",
        patterns=_compile(r">\s*/dev/(?:sd|hd|vd|xvd|nvme|mmcblk|loop|md|disk)"),
    ),
    DangerousRule(
        "critical-file",
        "覆盖系统关键文件或放宽根目录权限",
        patterns=_compile(
            r">\s*/etc/(?:passwd|shadow|gshadow|sudoers|fstab|hosts)\b",
            r"\bchmod\s+(?:-[A-Za-z-]+\s+)*(?:777|a\+rwx)\s+/(?:\s|$)",
            r"\bchown\s+(?:-[A-Za-z-]+\s+)*\S+\s+/(?:\s|$)",
        ),
    ),
    DangerousRule(
        "fork-bomb",
        "fork 炸弹，会耗尽主机资源",
        patterns=_compile(r":\s*\(\s*\)\s*\{[^\n]*\}\s*;\s*:"),
    ),
    DangerousRule(
        "database",
        "破坏性数据库语句",
        patterns=_compile(
            r"(?i)\bdrop\s+(?:database|table|schema)\b",
            r"(?i)\btruncate\s+table\b",
        ),
    ),
)

@dataclass(frozen=True)
class GuardDecision:
    """一次拒绝判定的结果；``rule``/``description``/``label`` 仅内部使用，调用方只能看到 ``message``。"""

    rule: str
    description: str
    label: str
    message: str


# ----------------------------------------------------------------------
# 命令解析辅助
# ----------------------------------------------------------------------

#: 简单命令之间的分隔符：``;`` ``&&`` ``||`` ``|`` ``|&`` ``&`` 与换行。
_SEPARATOR_RE = re.compile(r"\|\||&&|\|&|[;|&\n]")
#: 前置环境变量赋值（``FOO=bar cmd``）。
_ASSIGNMENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
#: 命令替换：``$(...)`` 与反引号。
_SUBSTITUTION_RES = (
    re.compile(r"\$\(([^()]*)\)"),
    re.compile(r"`([^`]*)`"),
)
#: ``timeout 30 cmd`` 中的时长参数。
_DURATION_RE = re.compile(r"\d+(?:\.\d+)?[smhd]?")

#: 调用包装器：跳过它们才能拿到真正被执行的程序（``sudo rm``、``timeout 30 rm``）。
#: ``su`` / ``runuser`` 不在此列：它们的 ``-c`` 参数本身就是一条命令，由
#: :meth:`CommandGuard._scan_inline_program` 单独处理。
_WRAPPER_PROGRAMS = frozenset(
    {
        "sudo", "doas", "env", "nohup", "nice", "ionice", "stdbuf", "setsid",
        "time", "timeout", "command", "exec", "chroot",
        "unshare", "nsenter", "flock", "busybox", "proot", "systemd-run",
        "start-stop-daemon", "daemonize", "parallel",
    }
)
#: 这些包装器的第一个非选项参数是「目录 / 用户」等前置参数，程序在其后（``chroot /mnt rm``）。
_WRAPPER_LEADING_ARG = frozenset({"chroot"})
#: shell 关键字：``then rm x``、``do rm x`` 等写法需要跳过关键字再取程序。
_SHELL_KEYWORDS = frozenset(
    {
        "if", "then", "elif", "else", "fi", "for", "while", "until", "do", "done",
        "case", "esac", "in", "select", "function", "!", "{", "}", "(",
    }
)
#: shell 解释器：``sh -c "…"`` 里的字符串要递归检查。
_SHELLS = frozenset(
    {"sh", "bash", "dash", "zsh", "ksh", "ash", "mksh", "pdksh", "csh", "tcsh", "fish"}
)
#: 参数中携带内嵌命令的包装器（``su -c "…"``）。
_INLINE_COMMAND_WRAPPERS = frozenset({"su", "runuser"})
#: 通过远程方式执行命令的程序（``ssh host "…"``）。
_REMOTE_EXEC_PROGRAMS = frozenset({"ssh", "pdsh", "pssh"})
#: 会把「第一个非选项参数」当作程序来执行的包装器。
_PROGRAM_ARG_WRAPPERS = frozenset({"xargs", "watch", "parallel", "nice", "ionice", "stdbuf", "timeout", "time"})
#: ``find -exec rm {} +`` 等写法。
_FIND_EXEC_FLAGS = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
#: 会吞掉下一个参数（值）的选项，按包装器分别声明；声明不全只影响识别能力，
#: 不会把真实程序当成值跳过（避免「跳过即漏检」）。
_WRAPPER_VALUE_OPTIONS: Dict[str, frozenset] = {
    "sudo": frozenset({"-u", "-g", "-p", "-C", "-T", "-r", "-t", "-h", "-U"}),
    "doas": frozenset({"-u", "-C"}),
    "env": frozenset({"-u", "-C", "-S"}),
    "nice": frozenset({"-n"}),
    "ionice": frozenset({"-c", "-n", "-p"}),
    "stdbuf": frozenset({"-i", "-o", "-e"}),
    "timeout": frozenset({"-s", "-k"}),
    "su": frozenset({"-c", "-s", "-g", "-G"}),
    "runuser": frozenset({"-u", "-g", "-G", "-c", "-s"}),
    "chroot": frozenset({"--userspec", "--groups"}),
    "flock": frozenset({"-w", "-E"}),
    "nsenter": frozenset({"-t", "-S", "-G", "-w", "-r"}),
    "unshare": frozenset({"--setgroups", "--map-user", "--map-group", "--kill-child"}),
    "xargs": frozenset({"-I", "-n", "-P", "-s", "-E", "-d", "-a", "-L", "-e"}),
    "watch": frozenset({"-n", "-d", "-p"}),
    "systemd-run": frozenset({"-u", "-p", "--unit", "--property", "--working-directory"}),
    "start-stop-daemon": frozenset({"-p", "-x", "-u", "-c", "-S", "-a", "-N"}),
    "ssh": frozenset(
        {"-p", "-i", "-l", "-o", "-F", "-E", "-J", "-b", "-c", "-D", "-L", "-R", "-W", "-m", "-S", "-Q", "-e"}
    ),
    "pdsh": frozenset({"-w", "-x", "-R", "-l", "-N", "-f", "-t", "-u"}),
    "pssh": frozenset({"-h", "-H", "-l", "-p", "-o", "-e", "-t", "-x", "-A", "-i"}),
}


def _is_command_flag(token: str, allow_bundle: bool) -> bool:
    """判断 token 是否是「后跟内嵌命令」的 ``-c`` 选项。"""
    if not token.startswith("-") or token.startswith("--"):
        return False
    if token == "-c":
        return True
    return allow_bundle and "c" in token[1:]


def _basename(token: str) -> str:
    """取程序 basename：去掉引号、目录前缀与 Windows 风格路径。"""
    text = token.strip().strip("'\"")
    if not text:
        return ""
    text = text.replace("\\", "/").rsplit("/", 1)[-1]
    return text.strip("'\"")


def _split_simple_commands(command: str) -> List[str]:
    """按 shell 控制运算符把命令行切成若干「简单命令」。"""
    return [segment for segment in _SEPARATOR_RE.split(command) if segment and segment.strip()]


def _tokenize(segment: str) -> List[str]:
    """把简单命令切成 token；引号不配对时退回按空白切分。"""
    try:
        return shlex.split(segment, comments=False, posix=True)
    except ValueError:
        return segment.split()


def _extract_substitutions(command: str) -> List[str]:
    """取出 ``$(...)`` 与反引号中的内嵌命令。"""
    found: List[str] = []
    for pattern in _SUBSTITUTION_RES:
        found.extend(match.group(1) for match in pattern.finditer(command))
    return [item for item in found if item.strip()]


def _skip_options(tokens: Sequence[str], index: int, wrapper: str, skip_duration: bool = False) -> int:
    """跳过包装器后面的选项与它们的值，返回新的下标。"""
    value_options = _WRAPPER_VALUE_OPTIONS.get(wrapper, frozenset())
    consumed_duration = False
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT_RE.match(token):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            if token in value_options and index < len(tokens):
                index += 1
            continue
        if skip_duration and not consumed_duration and _DURATION_RE.fullmatch(token):
            consumed_duration = True
            index += 1
            continue
        break
    return index


# ----------------------------------------------------------------------
# 拦截器
# ----------------------------------------------------------------------


class CommandGuard:
    """危险操作拦截器：判定命令是否命中危险规则，并生成给调用方的提示。"""

    def __init__(
        self,
        rules: Optional[Sequence[DangerousRule]] = None,
        message_template: str = DEFAULT_MESSAGE,
        enabled: bool = True,
        allow_patterns: Sequence[Pattern[str]] = (),
    ) -> None:
        self.rules: Tuple[DangerousRule, ...] = tuple(_DEFAULT_RULES if rules is None else rules)
        self.message_template = message_template or DEFAULT_MESSAGE
        self.enabled = bool(enabled)
        self.allow_patterns: Tuple[Pattern[str], ...] = tuple(allow_patterns)

    # -- 构造 ---------------------------------------------------------
    @classmethod
    def default(cls) -> "CommandGuard":
        """返回启用全部内置规则的拦截器。"""
        return cls()

    @classmethod
    def disabled(cls) -> "CommandGuard":
        """返回不做任何拦截的实例（显式关闭时使用）。"""
        return cls(enabled=False)

    @classmethod
    def from_config(cls, config: Optional[Mapping[str, Any]]) -> "CommandGuard":
        """按 ``config.json`` 构造拦截器；缺少配置项时使用内置默认值。"""
        data: Mapping[str, Any] = config or {}
        enabled = bool(data.get("dangerous_command_guard_enabled", True))

        template = str(data.get("dangerous_command_guard_message") or "").strip() or DEFAULT_MESSAGE
        disabled_rules = {str(item).strip() for item in _as_list(data.get("dangerous_command_guard_disabled_rules"))}
        rules: List[DangerousRule] = [rule for rule in _DEFAULT_RULES if rule.name not in disabled_rules]

        # 用户自定义规则单独成一条，不改动内置规则，避免相互干扰。
        custom_programs = _compile_optional(
            _as_list(data.get("dangerous_command_guard_programs")),
            "dangerous_command_guard_programs",
        )
        custom_patterns = _compile_optional(
            _as_list(data.get("dangerous_command_guard_patterns")),
            "dangerous_command_guard_patterns",
        )
        if custom_programs or custom_patterns:
            rules.append(
                DangerousRule(
                    name="custom",
                    description="自定义危险操作规则",
                    programs=custom_programs,
                    patterns=custom_patterns,
                )
            )

        allow_patterns = _compile_optional(
            _as_list(data.get("dangerous_command_guard_allow_patterns")),
            "dangerous_command_guard_allow_patterns",
        )
        guard = cls(rules=rules, message_template=template, enabled=enabled, allow_patterns=allow_patterns)
        if not enabled:
            logger.info("危险操作拦截已在配置中关闭")
        return guard

    # -- 判定 ---------------------------------------------------------
    def check(self, command: Optional[str]) -> Optional[GuardDecision]:
        """检查一条命令；命中危险规则时返回判定结果，否则返回 ``None``。"""
        if not self.enabled:
            return None
        raw = (command or "").strip()
        if not raw:
            return None
        if self._is_allowed(raw):
            return None
        decision = self._scan(raw, depth=0)
        if decision is not None:
            # 只记录元数据：规则细节留在内存里，不写日志、不回传调用方。
            logger.warning("危险操作已拒绝（未下发到服务器）| 命令长度=%d", len(raw))
        return decision

    def check_input(self, data: Optional[str]) -> Optional[GuardDecision]:
        """检查交互式输入（``POST /input``）的每一行，避免用它绕过兜底。"""
        if not self.enabled or not data:
            return None
        for line in re.split(r"[\r\n]+", data):
            decision = self.check(line)
            if decision is not None:
                return decision
        return None

    # -- 内部实现 -----------------------------------------------------
    def _is_allowed(self, command: str) -> bool:
        return any(pattern.search(command) for pattern in self.allow_patterns)

    def _build_decision(self, rule: DangerousRule) -> GuardDecision:
        """构造判定结果；提示文案默认不含规则信息（除非部署者自定义模板引用 ``{rule}``）。"""
        label = f"{rule.name}（{rule.description}）"
        try:
            message = self.message_template.format(rule=label)
        except (KeyError, IndexError, ValueError):
            message = self.message_template
        return GuardDecision(rule=rule.name, description=rule.description, label=label, message=message)

    def _scan(self, command: str, depth: int) -> Optional[GuardDecision]:
        """对整条命令做一次完整扫描（程序名 / 子命令 / 正则 / 嵌套命令）。"""
        if depth > 4:
            return None

        for rule in self.rules:
            for pattern in rule.patterns:
                if pattern.search(command):
                    return self._build_decision(rule)

        for inner in _extract_substitutions(command):
            decision = self.check(inner)
            if decision is not None:
                return decision

        for segment in _split_simple_commands(command):
            decision = self._scan_segment(_tokenize(segment), depth)
            if decision is not None:
                return decision
        return None

    def _scan_segment(self, tokens: Sequence[str], depth: int) -> Optional[GuardDecision]:
        """检查单个简单命令：解析出真正的程序，再看是否命中规则。"""
        if not tokens:
            return None

        index = 0
        while index < len(tokens) and _ASSIGNMENT_RE.match(tokens[index]):
            index += 1

        # 跳过 shell 关键字与调用包装器，定位真正被执行的程序。
        while index < len(tokens):
            name = _basename(tokens[index])
            if name in _SHELL_KEYWORDS:
                index += 1
                continue
            if name in _WRAPPER_PROGRAMS:
                index += 1
                index = _skip_options(tokens, index, name, skip_duration=(name == "timeout"))
                if name in _WRAPPER_LEADING_ARG and index < len(tokens) and not tokens[index].startswith("-"):
                    index += 1
                continue
            break

        if index >= len(tokens):
            return None

        program = _basename(tokens[index])
        rest = list(tokens[index + 1:])

        # 1) 程序本身 + 子命令
        for rule in self.rules:
            if not rule.matches_program(program):
                continue
            if rule.subcommands:
                if rule.matches_subcommand(rest):
                    return self._build_decision(rule)
                continue
            return self._build_decision(rule)

        # 2) 程序以字符串形式再执行一次命令（sh -c / eval / xargs / find -exec）
        decision = self._scan_inline_program(program, rest, depth)
        if decision is not None:
            return decision

        return None

    def _scan_inline_program(
        self, program: str, rest: Sequence[str], depth: int
    ) -> Optional[GuardDecision]:
        """检查「参数里又带了一条命令」的写法（``sh -c``、``eval``、``su -c``、``ssh``、``xargs``、``find -exec``）。"""
        if program in _SHELLS:
            return self._check_flag_command(rest, allow_bundle=True)

        if program == "eval":
            joined = " ".join(rest).strip()
            return self.check(joined) if joined else None

        if program in _INLINE_COMMAND_WRAPPERS:  # su / runuser
            decision = self._check_flag_command(rest, allow_bundle=False)
            if decision is not None:
                return decision
            # ``su - root rm -rf /``：没有 -c 时跳过目标用户，其余 token 继续按命令检查。
            index = _skip_options(rest, 0, program)
            if index < len(rest):
                return self._scan_segment(rest[index + 1:], depth + 1)
            return None

        if program in _REMOTE_EXEC_PROGRAMS:  # ssh / pdsh / pssh
            return self._check_remote_command(rest, program)

        if program in _PROGRAM_ARG_WRAPPERS:
            index = _skip_options(rest, 0, program, skip_duration=(program == "timeout"))
            if index < len(rest):
                return self._scan_segment(rest[index:], depth + 1)
            return None

        if program == "find":
            for position, token in enumerate(rest):
                if token in _FIND_EXEC_FLAGS and position + 1 < len(rest):
                    decision = self._scan_segment(rest[position + 1:], depth + 1)
                    if decision is not None:
                        return decision
            return None

        return None

    def _check_flag_command(self, tokens: Sequence[str], allow_bundle: bool) -> Optional[GuardDecision]:
        """检查 ``-c <命令>`` 中的内嵌命令（``allow_bundle`` 兼容 ``bash -lc "…"``）。"""
        for index, token in enumerate(tokens):
            if not _is_command_flag(token, allow_bundle):
                continue
            if index + 1 >= len(tokens):
                return None
            script = tokens[index + 1].strip()
            return self.check(script) if script else None
        return None

    def _check_remote_command(self, tokens: Sequence[str], program: str) -> Optional[GuardDecision]:
        """检查 ``ssh [-选项] 主机 <远程命令>`` 中的远程命令。"""
        index = _skip_options(tokens, 0, program)
        if index >= len(tokens):
            return None
        remote = " ".join(tokens[index + 1:]).strip()
        return self.check(remote) if remote else None


def _as_list(value: Any) -> List[Any]:
    """把配置项统一成列表（支持单个字符串）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


# ----------------------------------------------------------------------
# 响应构造（供 api_server / ssh_client 复用，保证各入口返回一致）
#
# 对外只给一句话：响应字段与正常结果完全相同，只有 exit_code=126 与提示文案不同。
# ----------------------------------------------------------------------


def build_blocked_result(decision: GuardDecision) -> Dict[str, Any]:
    """构造 ``/execute`` 形状的拒绝响应（与正常执行结果同形）。"""
    return {
        "stdout": "",
        "stderr": decision.message,
        "exit_code": BLOCKED_EXIT_CODE,
    }


def build_blocked_input_result(decision: GuardDecision) -> Dict[str, Any]:
    """构造 ``/input`` 形状的拒绝响应。

    刻意不回显 ``data``：交互输入可能是口令等敏感内容。
    """
    return {
        "status": "refused",
        "message": decision.message,
    }
