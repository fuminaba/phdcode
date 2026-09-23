"""The GPU arbiter: one advisory lock that every GPU consumer takes.

See docs/adr/0007-gpu-arbitration-via-os-level-lock.md.

One RTX 5070 Ti with 16,303 MiB serves every local workload, and no two of
them fit together -- Bonsai 2 27B holds 14,424 MiB, MedGemma 4B peaks at
8,343 MiB, and marker-pdf needs room of its own. Contention here is not
theoretical: two unqueued GPU paths once cost a 352-second reply delay.

Why a file lock rather than an in-process semaphore or a task queue: the
agent writes and runs its own scripts. A script dcode authored is a process
that imports none of our Python, so no in-process mechanism can see it.
`flock` is the only choke point every consumer can reach, in any language.

The lock is ADVISORY. A process that does not take it is not stopped. The
mitigation is structural rather than technical: every GPU-using capability
is a library (ADR-0006) and the lock lives inside those libraries, so the
correct path is also the easy one. Tools never take it.

From the shell, including from a script the agent wrote:

    flock /tmp/phdcode-gpu.lock -c 'python batch_job.py'
    flock /tmp/phdcode-gpu.lock python -m llm_engine.vision --glob 'p/*.png'

Print the path with `python -m llm_engine.gpu` so a generated script can
find it without hardcoding.
"""
import contextlib
import errno
import fcntl
import logging
import os
import threading
import time
from typing import Iterator, Optional

log = logging.getLogger(__name__)

# Deliberately outside the repository and outside any per-user cache: every
# consumer on this machine must agree on one path, including scripts written
# by the agent in a working directory of their own choosing. Overridable so a
# second GPU (or a test) can have its own.
LOCK_PATH = os.getenv("PHDCODE_GPU_LOCK", "/tmp/phdcode-gpu.lock")

# Generous: the holder may be a document ingestion or a patch batch running
# for minutes. Waiting is correct; failing is not. None means wait forever.
DEFAULT_TIMEOUT: Optional[float] = 1800.0


class GpuLockTimeout(RuntimeError):
    """Raised when the lock could not be acquired within the timeout."""


# Re-entrancy depth for the current thread. flock associates a lock with the
# open file description, so a nested acquisition opens a second fd and blocks
# against the first -- a self-deadlock, with no other process involved. This
# is the same trap tasks/dispatch.in_celery_worker() existed to dodge: work
# already inside the critical section must not queue for the slot it holds.
# Generalised here so that a library holding the lock for a batch can call
# another that takes it per item (embeddings inside an ingestion, say)
# without either knowing about the other.
_held = threading.local()


@contextlib.contextmanager
def gpu_lock(
        timeout: Optional[float] = DEFAULT_TIMEOUT,
        enabled: bool = True,
        label: str = "",
) -> Iterator[None]:
    """Holds the GPU lock for the duration of the block.

    Args:
        timeout: seconds to wait before raising GpuLockTimeout. None waits
            forever.
        enabled: False makes this a no-op. Pass `route.local` so a hosted
            model -- which consumes no VRAM here -- does not queue behind
            local work. A queue that protects a resource must be skipped by
            work that does not use that resource, or the protection becomes
            unrelated head-of-line blocking.
        label: recorded in the debug log, to identify the holder.

    Hold it for the whole batch, not per item: one 8.2GB model load for a
    thousand patches, not a thousand loads.
    """
    if not enabled:
        yield
        return

    depth = getattr(_held, "depth", 0)
    if depth:
        # Already held by this thread; the outermost holder owns release.
        _held.depth = depth + 1
        try:
            yield
        finally:
            _held.depth -= 1
        return

    # 0o666 so a second account on this machine can take the same lock; the
    # umask still applies, which is intended -- this is arbitration, not a
    # security boundary.
    fd = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o666)
    deadline = None if timeout is None else time.monotonic() + timeout
    waited_from = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise GpuLockTimeout(
                        f"GPU lock held by another process for more than "
                        f"{timeout}s ({LOCK_PATH})"
                    ) from e
                time.sleep(0.25)

        waited = time.monotonic() - waited_from
        if waited > 1.0:
            log.info("Waited %.1fs for the GPU lock%s", waited,
                     f" ({label})" if label else "")
        try:
            # Best-effort provenance for whoever runs `cat` on the lockfile
            # while wondering what is holding the card.
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {label}\n".encode())
        except OSError:
            pass

        _held.depth = 1
        yield
    finally:
        _held.depth = 0
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
    # The lockfile itself is never unlinked: deleting it would let a waiter
    # holding the old inode and a new arrival creating a fresh one both
    # believe they hold the lock.


if __name__ == "__main__":
    print(LOCK_PATH)
