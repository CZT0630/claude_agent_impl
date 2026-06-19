"""
沙箱模块 — 跨平台命令隔离执行

s21: 在 run_bash 层面插入进程级隔离，防止 prompt injection 破坏宿主机

自动选择策略（优先级从高到低）:
  1. bwrap (Linux)        → namespace 隔离，文件只读，网络隔离
  2. sandbox-exec (macOS) → 文件写入受控，syscall 过滤
  3. Job Object (Windows) → 内存/进程数限制
  4. Docker (跨平台)      → 容器完全隔离
  5. resource (Linux/macOS) → 资源限制
  6. 兜底                 → 环境清理 + 命令黑名单
"""

import os
import sys
import shutil
import platform
import subprocess
from pathlib import Path

from tools.result import ToolResult


# ─── 敏感环境变量 ──────────────────────────────────────────────
# 执行命令时从环境中清除，防止泄露密钥
SENSITIVE_VARS: set[str] = {
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "GITHUB_TOKEN", "GITLAB_TOKEN",
    "SSH_KEY", "SSH_AUTH_SOCK", "NPM_TOKEN", "PYPI_TOKEN",
    "DOCKER_PASSWORD", "DATABASE_URL", "REDIS_URL", "ANTHROPIC_AUTH_TOKEN",
    "SECRET_KEY", "JWT_SECRET", "API_KEY",
}

SENSITIVE_SUFFIXES: tuple[str, ...] = (
    "_API_KEY",
    "_AUTH_TOKEN",
    "_ACCESS_TOKEN",
    "_SECRET",
    "_PASSWORD",
    "_TOKEN",
    "_KEY",
)

# ─── 危险命令黑名单 ─────────────────────────────────────────────
DENY_PATTERNS: list[str] = [
    "rm -rf /", "rm -rf /*", "mkfs", "dd if=", "> /dev/sd",
    "shutdown", "reboot", "halt", "poweroff",
    "chmod 777 /", "chown root",
    ":(){ :|:& };:",  # fork bomb
]


def detect_backend() -> str:
    """
    自动检测当前平台可用的最优沙箱方案。

    Returns:
        方案名称: "bwrap" | "sandbox-exec" | "job-object" | "docker" | "resource" | "none"
    """
    # 1. bwrap (Linux 最优)
    if shutil.which("bwrap"):
        return "bwrap"

    # 2. sandbox-exec (macOS)
    if platform.system() == "Darwin":
        return "sandbox-exec"

    # 3. Job Object (Windows 原生)
    if sys.platform == "win32":
        return "job-object"

    # 4. Docker (跨平台备选)
    if shutil.which("docker"):
        return "docker"

    # 5. resource 模块 (Linux/macOS 兜底)
    if sys.platform != "win32":
        try:
            import resource  # noqa: F401
            return "resource"
        except ImportError:
            pass

    # 6. 啥都没有
    return "none"


def clean_env() -> dict[str, str]:
    """清理环境变量，移除敏感信息"""
    sensitive_names = {name.upper() for name in SENSITIVE_VARS}
    return {
        k: v
        for k, v in os.environ.items()
        if k.upper() not in sensitive_names
        and not k.upper().endswith(SENSITIVE_SUFFIXES)
    }


def check_deny_list(command: str) -> str | None:
    """
    检查命令是否命中黑名单。

    Returns:
        命中原因字符串，或 None（安全）
    """
    for pattern in DENY_PATTERNS:
        if pattern in command:
            return f"Blocked: matches deny pattern '{pattern}'"
    return None


class Sandbox:
    """
    跨平台命令沙箱。

    用法:
        sandbox = Sandbox(workdir=Path("/project"), level="auto")
        result = sandbox.execute("ls -la")
    """

    def __init__(self, workdir: Path, level: str = "auto"):
        """
        Args:
            workdir: 工作目录
            level: "auto" 自动检测 | "off" 关闭沙箱
        """
        self.workdir = workdir

        if level == "off":
            self.backend = "none"
        else:
            self.backend = detect_backend()

    def execute_result(self, command: str, timeout: int = 120) -> ToolResult:
        """Execute a command and return the minimal structured s22 result."""
        deny_reason = check_deny_list(command)
        if deny_reason:
            return ToolResult.failure(
                "SANDBOX_DENY_PATTERN",
                stderr=deny_reason,
                metadata={"backend": self.backend, "command": command},
            )

        output = self.execute(command, timeout)
        metadata = {"backend": self.backend, "command": command}
        if output.startswith("Error: Timeout"):
            return ToolResult.failure("SANDBOX_TIMEOUT", stderr=output, metadata=metadata)
        if output.startswith("Error:"):
            return ToolResult.failure("SANDBOX_EXEC_ERROR", stderr=output, metadata=metadata)
        return ToolResult.success(stdout=output, metadata=metadata)

    def execute(self, command: str, timeout: int = 120) -> str:
        """
        在沙箱中执行命令。

        Args:
            command: shell 命令
            timeout: 超时秒数

        Returns:
            命令输出（截断到 50000 字符）
        """
        # 黑名单检查（所有方案共享）
        deny_reason = check_deny_list(command)
        if deny_reason:
            return f"Error: {deny_reason}"

        # 分发到具体方案
        handler = {
            "bwrap":         self._bwrap,
            "sandbox-exec":  self._sandbox_exec,
            "job-object":    self._job_object,
            "docker":        self._docker,
            "resource":      self._resource,
            "none":          self._raw,
        }.get(self.backend, self._raw)

        try:
            out = handler(command, timeout)
            return out[:50000] if out else "(no output)"
        except subprocess.TimeoutExpired:
            return f"Error: Timeout ({timeout}s)"
        except (FileNotFoundError, OSError) as e:
            return f"Error: {e}"

    # ─── bwrap (Linux) ──────────────────────────────────────────
    def _bwrap(self, command: str, timeout: int) -> str:
        """
        Bubblewrap namespace 隔离。
        - 整个根目录只读挂载
        - 只有工作目录可读写
        - 可选网络隔离
        """
        cmd = [
            "bwrap",
            "--ro-bind", "/", "/",           # 根目录只读
            "--dev", "/dev",                  # 设备目录
            "--tmpfs", "/tmp",                # /tmp 可写
            "--bind", str(self.workdir), str(self.workdir),  # 工作目录可读写
            "--unshare-net",                  # 网络隔离
            "--die-with-parent",              # 父进程死了子进程一起死
            "--", "sh", "-c", command,
        ]
        r = subprocess.run(
            cmd, cwd=self.workdir, capture_output=True, text=True,
            timeout=timeout, env=clean_env(),
        )
        return (r.stdout + r.stderr).strip()

    # ─── sandbox-exec (macOS) ───────────────────────────────────
    def _sandbox_exec(self, command: str, timeout: int) -> str:
        """
        macOS sandbox-exec (Seatbelt) 隔离。
        - 通过 profile 文件控制文件写入权限
        """
        # 生成沙箱配置：允许写工作目录，禁止写系统目录
        workdir = str(self.workdir)
        profile = (
            "(version 1)\n"
            "(allow default)\n"
            "(deny file-write*\n"
            '  (subpath "/System")\n'
            '  (subpath "/usr")\n'
            '  (subpath "/Library")\n'
            '  (subpath "/private/etc"))\n'
            "(allow file-write*\n"
            f'  (subpath "{workdir}")\n'
            '  (subpath "/tmp"))\n'
        )
        cmd = ["sandbox-exec", "-p", profile, "sh", "-c", command]
        r = subprocess.run(
            cmd, cwd=self.workdir, capture_output=True, text=True,
            timeout=timeout, env=clean_env(),
        )
        return (r.stdout + r.stderr).strip()

    # ─── Job Object (Windows) ───────────────────────────────────
    def _job_object(self, command: str, timeout: int) -> str:
        """
        Windows Job Object 进程级资源限制。
        - 内存限制 512MB
        - 最多 10 个进程（防 fork 炸弹）
        - 清理敏感环境变量
        """
        if sys.platform != "win32":  # pragma: no cover — 跨平台兼容
            return self._raw(command, timeout)
        try:
            return self._job_object_impl(command, timeout)
        except Exception:
            # Job Object 失败时降级到环境清理
            return self._resource_fallback(command, timeout)

    @staticmethod
    def _build_clean_env_block() -> str:
        """构建 Windows 环境变量块（Unicode，双 null 结尾）用于 CreateProcessW 的 lpEnvironment"""
        env = clean_env()
        # CreateProcessW 的 lpEnvironment 要求: key=value\0key=value\0\0
        parts = []
        for k, v in env.items():
            if k.upper().startswith(("=", "(", ")")):
                continue  # 跳过非法变量名
            parts.append(f"{k}={v}")
        return "\0".join(parts) + "\0\0"

    def _job_object_impl(self, command: str, timeout: int) -> str:
        """Job Object 实际实现（仅 Windows）

        使用 CreateProcessW 直接创建挂起的进程，获取线程句柄，
        分配到 Job Object 后再恢复线程。
        """
        import ctypes
        import ctypes.wintypes as wt

        # Win32 常量
        JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
        JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
        JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
        JobObjectExtendedLimitInformation = 9
        CREATE_SUSPENDED = 0x00000004
        CREATE_UNICODE_ENVIRONMENT = 0x00000400
        STARTF_USESTDHANDLES = 0x00000100
        HANDLE_FLAG_INHERIT = 0x00000001
        MB = 1024 * 1024

        # 定义结构体
        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", wt.LARGE_INTEGER),
                ("PerJobUserTimeLimit", wt.LARGE_INTEGER),
                ("LimitFlags", wt.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wt.DWORD),
                ("Affinity", ctypes.POINTER(wt.ULONG)),
                ("PriorityClass", wt.DWORD),
                ("SchedulingClass", wt.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", wt.ULARGE_INTEGER),
                ("WriteOperationCount", wt.ULARGE_INTEGER),
                ("OtherOperationCount", wt.ULARGE_INTEGER),
                ("ReadTransferCount", wt.ULARGE_INTEGER),
                ("WriteTransferCount", wt.ULARGE_INTEGER),
                ("OtherTransferCount", wt.ULARGE_INTEGER),
            ]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wt.DWORD),
                ("lpReserved", wt.LPWSTR),
                ("lpDesktop", wt.LPWSTR),
                ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD),
                ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD),
                ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD),
                ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD),
                ("dwFlags", wt.DWORD),
                ("wShowControl", wt.WORD),
                ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wt.HANDLE),
                ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE),
            ]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", wt.HANDLE),
                ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD),
                ("dwThreadId", wt.DWORD),
            ]

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

        # 定义 SECURITY_ATTRIBUTES（ctypes.wintypes 不导出，需手动定义）
        class SECURITY_ATTRIBUTES(ctypes.Structure):
            _fields_ = [
                ("nLength", wt.DWORD),
                ("lpSecurityDescriptor", wt.LPVOID),
                ("bInheritHandle", wt.BOOL),
            ]

        # 创建匿名管道用于捕获 stdout/stderr
        sa = SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(sa)
        sa.bInheritHandle = True
        sa.lpSecurityDescriptor = None

        h_stdout_read = wt.HANDLE()
        h_stdout_write = wt.HANDLE()
        h_stderr_read = wt.HANDLE()
        h_stderr_write = wt.HANDLE()
        if not kernel32.CreatePipe(ctypes.byref(h_stdout_read), ctypes.byref(h_stdout_write), ctypes.byref(sa), 0):
            raise OSError("CreatePipe stdout failed")
        if not kernel32.CreatePipe(ctypes.byref(h_stderr_read), ctypes.byref(h_stderr_write), ctypes.byref(sa), 0):
            raise OSError("CreatePipe stderr failed")
        # 读端不继承
        kernel32.SetHandleInformation(h_stdout_read, HANDLE_FLAG_INHERIT, 0)
        kernel32.SetHandleInformation(h_stderr_read, HANDLE_FLAG_INHERIT, 0)

        # 创建 Job Object
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObjectW failed")

        try:
            # 设置限制
            ext_info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            ext_info.BasicLimitInformation.LimitFlags = (
                JOB_OBJECT_LIMIT_PROCESS_MEMORY
                | JOB_OBJECT_LIMIT_JOB_MEMORY
                | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            )
            ext_info.ProcessMemoryLimit = 512 * MB    # 单进程 512MB
            ext_info.JobMemoryLimit = 1024 * MB        # 总共 1GB
            ext_info.BasicLimitInformation.ActiveProcessLimit = 10  # 最多 10 进程

            result = kernel32.SetInformationJobObject(
                job, JobObjectExtendedLimitInformation,
                ctypes.byref(ext_info), ctypes.sizeof(ext_info),
            )
            if not result:
                raise OSError("SetInformationJobObject failed")

            # 用 cmd.exe 包装命令（shell=True 语义）
            shell_cmd = f'cmd.exe /c "{command}"'
            cmdline = ctypes.create_unicode_buffer(shell_cmd)
            env_block = ctypes.create_unicode_buffer(self._build_clean_env_block())

            # 构建 STARTUPINFO — 将管道写端重定向为子进程的 stdout/stderr
            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(si)
            si.dwFlags = STARTF_USESTDHANDLES
            si.hStdOutput = h_stdout_write
            si.hStdError = h_stderr_write
            # stdin 用父进程的
            si.hStdInput = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE

            pi = PROCESS_INFORMATION()

            # 创建挂起的进程 — 返回线程句柄（关键！）
            success = kernel32.CreateProcessW(
                None,           # lpApplicationName
                cmdline,        # lpCommandLine
                None,           # lpProcessAttributes
                None,           # lpThreadAttributes
                True,           # bInheritHandles
                CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT,  # dwCreationFlags
                env_block,      # lpEnvironment (清理敏感变量)
                str(self.workdir),  # lpCurrentDirectory
                ctypes.byref(si),   # lpStartupInfo
                ctypes.byref(pi),   # lpProcessInformation → 包含 hThread
            )
            if not success:
                raise OSError(f"CreateProcessW failed: {ctypes.GetLastError()}")

            # 关闭写端（父进程不需要，否则读端永远不 EOF）
            kernel32.CloseHandle(h_stdout_write)
            kernel32.CloseHandle(h_stderr_write)
            h_stdout_write = None
            h_stderr_write = None

            try:
                # 把进程放进 Job
                kernel32.AssignProcessToJobObject(job, pi.hProcess)
                # 恢复线程（pi.hThread 才是线程句柄！）
                kernel32.ResumeThread(pi.hThread)

                # 读取输出
                stdout_chunks = []
                stderr_chunks = []
                buf = ctypes.create_string_buffer(4096)
                bytes_read = wt.DWORD(0)

                # 非阻塞读取两个管道
                import time as _time
                deadline = _time.monotonic() + timeout

                for handle, chunks in [(h_stdout_read, stdout_chunks), (h_stderr_read, stderr_chunks)]:
                    while _time.monotonic() < deadline:
                        # PeekNamedPipe 检查是否有数据
                        avail = wt.DWORD(0)
                        kernel32.PeekNamedPipe(handle, None, 0, None, ctypes.byref(avail), None)
                        if avail.value > 0:
                            ok = kernel32.ReadFile(handle, buf, 4096, ctypes.byref(bytes_read), None)
                            if ok and bytes_read.value > 0:
                                chunks.append(buf.raw[:bytes_read.value].decode("utf-8", errors="replace"))
                        else:
                            # 检查进程是否已退出
                            exit_code = wt.DWORD(0)
                            kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
                            if exit_code.value != 259:  # STILL_ACTIVE
                                # 读完剩余数据
                                while True:
                                    ok = kernel32.ReadFile(handle, buf, 4096, ctypes.byref(bytes_read), None)
                                    if not ok or bytes_read.value == 0:
                                        break
                                    chunks.append(buf.raw[:bytes_read.value].decode("utf-8", errors="replace"))
                                break
                            _time.sleep(0.01)

                stdout = "".join(stdout_chunks)
                stderr = "".join(stderr_chunks)
                return (stdout + stderr).strip()
            finally:
                # 确保进程被终止
                kernel32.TerminateProcess(pi.hProcess, 1)
                kernel32.CloseHandle(pi.hProcess)
                kernel32.CloseHandle(pi.hThread)
                if h_stdout_write:
                    kernel32.CloseHandle(h_stdout_write)
                if h_stderr_write:
                    kernel32.CloseHandle(h_stderr_write)
                kernel32.CloseHandle(h_stdout_read)
                kernel32.CloseHandle(h_stderr_read)
        finally:
            kernel32.CloseHandle(job)

    def _resource_fallback(self, command: str, timeout: int) -> str:
        """Job Object 不可用时的降级方案：环境清理 + 黑名单"""
        r = subprocess.run(
            command, shell=True, cwd=self.workdir,
            capture_output=True, text=True, timeout=timeout,
            env=clean_env(),
        )
        return (r.stdout + r.stderr).strip()

    # ─── Docker ─────────────────────────────────────────────────
    def _docker(self, command: str, timeout: int) -> str:
        """
        Docker 容器隔离。
        - 完全独立的文件系统
        - 网络可选隔离
        - 资源硬限制
        """
        workdir = str(self.workdir)
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{workdir}:/work",
            "-w", "/work",
            "--network=none",            # 禁网
            "--memory=512m",             # 内存限制
            "--cpus=1",                  # CPU 限制
            "--pids-limit=256",          # 进程数限制
            "ubuntu:22.04",
            "sh", "-c", command,
        ]
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
        )
        return (r.stdout + r.stderr).strip()

    # ─── resource 限制 (Linux/macOS) ────────────────────────────
    def _resource(self, command: str, timeout: int) -> str:
        """
        resource 模块限制（Linux/macOS）。
        - CPU 时间 30 秒
        - 内存 512MB
        - 文件大小 50MB
        - 进程数 10
        """
        import resource  # type: ignore[import-not-found]

        MB = 1024 * 1024

        def set_limits() -> None:
            resource.setrlimit(resource.RLIMIT_CPU, (30, 30))  # type: ignore[attr-defined]
            resource.setrlimit(resource.RLIMIT_AS, (512 * MB, 512 * MB))  # type: ignore[attr-defined]
            resource.setrlimit(resource.RLIMIT_FSIZE, (50 * MB, 50 * MB))  # type: ignore[attr-defined]
            resource.setrlimit(resource.RLIMIT_NPROC, (10, 10))  # type: ignore[attr-defined]

        r = subprocess.run(
            command, shell=True, cwd=self.workdir,
            capture_output=True, text=True, timeout=timeout,
            env=clean_env(),
            preexec_fn=set_limits,
        )
        return (r.stdout + r.stderr).strip()

    # ─── 兜底：无沙箱 ──────────────────────────────────────────
    def _raw(self, command: str, timeout: int) -> str:
        """无沙箱，仅环境清理 + 黑名单保护"""
        r = subprocess.run(
            command, shell=True, cwd=self.workdir,
            capture_output=True, text=True, timeout=timeout,
            env=clean_env(),
        )
        return (r.stdout + r.stderr).strip()
