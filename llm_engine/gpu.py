"""The GPU policy of ADR-0017: one tenant at a time, and short jobs.

- /tmp/gpu.lock is an advisory flock. A tenant holds it for its full life:
  llama-server holds it through its systemd unit (`flock /tmp/gpu.lock ...`).
  A short job (the text parse, the reranker) holds it while it uses the GPU.
- /tmp/gpu.cleared: while it exists, no phdcode GPU user starts. The user
  runs `phdcode gpu clear` before a training run and `phdcode gpu restore`
  after it. Training itself takes no lock.

llama-server starts on demand: systemd listens on its port, and the first
request starts it (phdcode-llm.socket). It stops after 30 minutes with no
connection (--exit-idle-time in phdcode-llm-proxy.service). So it runs around the sweep, and while a chat on a local model
sends requests.

A short job uses the GPU only if that is faster than the CPU, waits included
(short_job()).
"""
import contextlib
import errno
import fcntl
import logging
import os
import subprocess
import sys
import time
from typing import Iterator

log = logging.getLogger(__name__)

LOCK_PATH = "/tmp/gpu.lock"
GPU_CLEARED = "/tmp/gpu.cleared"
# The units of llama-server (llm_engine/systemd/). `phdcode gpu clear` stops
# all three: with the socket stopped, a request fails at once.
LLM_SOCKET = "phdcode-llm-proxy.socket"
LLM_UNITS = (LLM_SOCKET, "phdcode-llm-proxy.service", "phdcode-llm.service")

_TORCH_FREE_MIB = (
    "import torch; print(torch.cuda.mem_get_info()[0] >> 20 if torch.cuda.is_available() else 0)"
)


def free_vram_mib() -> int:
    """The free VRAM in MiB; 0 if there is no usable GPU. nvidia-smi first;
    if NVML fails (a driver update before the reboot), a short PyTorch child
    process, which takes about 3 s. Never in this process: a process that
    then runs on the CPU must hold no CUDA context (about 300 MB of VRAM)."""
    for command in (
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            [sys.executable, "-c", _TORCH_FREE_MIB],
    ):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=60, check=True)
            return int(result.stdout.split()[0])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            continue
    return 0


def llm_active() -> bool:
    """True if llama-server runs or starts now (it holds the GPU until it is idle)."""
    result = subprocess.run(["systemctl", "--user", "is-active", "--quiet", LLM_UNITS[2]], check=False)
    return result.returncode == 0


def _try_lock(fd: int, wait_seconds: float) -> bool:
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError as e:
            if e.errno not in (errno.EAGAIN, errno.EACCES):
                raise
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.5)


@contextlib.contextmanager
def short_job(need_mib: int, gpu_seconds: float, cpu_seconds: float, label: str) -> Iterator[str]:
    """Yields "cuda" with the lock held, or "cpu" with no lock.

    The GPU, only if it is faster than the CPU, waits included:
    - cleared (training), or llama-server holds it: the CPU at once. Neither
      ends soon: training runs for hours, llama-server after 30 idle minutes.
    - another short job holds the lock: wait for it, but at most as long as
      the GPU saves (cpu_seconds - gpu_seconds); then the CPU.
    - less than need_mib of VRAM is free (for example, a training run
      without `phdcode gpu clear`): the CPU.
    """
    if os.path.exists(GPU_CLEARED) or llm_active() or cpu_seconds <= gpu_seconds:
        yield "cpu"
        return
    # The lockfile is never deleted: a waiter on the old inode and a new
    # process on a new file would both hold "the" lock.
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        locked = _try_lock(fd, cpu_seconds - gpu_seconds)
        if locked and not os.path.exists(GPU_CLEARED) and free_vram_mib() >= need_mib:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {label}\n".encode())  # for `cat /tmp/gpu.lock`
            log.info("%s on the GPU", label)
            yield "cuda"
            return
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
        log.info("%s on the CPU (%s)", label, "the GPU is busy" if locked else "the lock is held")
    finally:
        os.close(fd)  # also releases the lock
    yield "cpu"


def clear() -> None:
    """`phdcode gpu clear`: no phdcode GPU user starts after this; llama-server
    and its socket stop; a short job on the GPU ends first (this waits for
    the lock)."""
    open(GPU_CLEARED, "a").close()
    subprocess.run(["systemctl", "--user", "stop", *LLM_UNITS], check=False)
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o666)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)  # waits for a short job to end
    finally:
        os.close(fd)


def restore() -> None:
    """`phdcode gpu restore`: phdcode may use the GPU again. The socket starts
    again, so llama-server starts at the next request. Also the fix if the
    socket failed (for example, llama-server could not start)."""
    with contextlib.suppress(FileNotFoundError):
        os.remove(GPU_CLEARED)
    subprocess.run(["systemctl", "--user", "reset-failed", *LLM_UNITS], check=False, capture_output=True)
    subprocess.run(["systemctl", "--user", "start", LLM_SOCKET], check=False)
