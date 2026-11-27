"""Bounded child processes shared by Git, checks, GitHub, and Pi."""

from __future__ import annotations

from contextlib import contextmanager, suppress
import os
import re
import selectors
import signal
import subprocess
import time


class ProcessError(RuntimeError):
    pass


class Interrupted(ProcessError):
    pass


def redact(text):
    text = re.sub(r"(?i)(authorization:\s*(?:bearer|token)\s+)\S+", r"\1[redacted]", text)
    return re.sub(r"\b(?:gh[pousr]_|github_pat_|sk-)[A-Za-z0-9_-]{12,}", "[redacted]", text)


def redact_value(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    return value


def finish(process):
    if process.stdin:
        with suppress(BrokenPipeError, OSError):
            process.stdin.close()
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait()


@contextmanager
def child(argv, *, cwd, env, pipe_input=False):
    try:
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, start_new_session=True, bufsize=0,
            stdin=subprocess.PIPE if pipe_input else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise ProcessError(f"Could not start {argv[0]}: {error}") from error
    try:
        yield process
    finally:
        if process.poll() is None:
            finish(process)
        elif process.stdin:
            with suppress(OSError):
                process.stdin.close()
        process.stdout.close()
        process.stderr.close()


def chunks(process, timeout, max_bytes, stop=None, pending_input=None):
    deadline = time.monotonic() + timeout
    used = 0
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        if pending_input is not None:
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        while selector.get_map():
            if stop is not None and stop.is_set():
                raise Interrupted("Execution was interrupted.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProcessError(f"Process exceeded its {timeout:g}s deadline.")
            for key, _ in selector.select(min(remaining, 0.25)):
                if key.data == "stdin":
                    count = os.write(key.fd, pending_input[:4096])
                    pending_input = pending_input[count:]
                    if not pending_input:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                block = os.read(key.fd, 32768)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                used += len(block)
                if used > max_bytes:
                    raise ProcessError("Process output exceeded its capture limit.")
                yield key.data, block
        try:
            process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise ProcessError(f"Process exceeded its {timeout:g}s deadline.") from error


def run(argv, *, cwd, env=None, timeout=60, limit=2_000_000, input_bytes=None, stop=None):
    with child(argv, cwd=cwd, env=env, pipe_input=input_bytes is not None) as process:
        captured = {"stdout": bytearray(), "stderr": bytearray()}
        try:
            for stream, block in chunks(process, timeout, limit, stop, input_bytes):
                captured[stream].extend(block)
        except BrokenPipeError:
            finish(process)
        return subprocess.CompletedProcess(argv, process.returncode,
                                           bytes(captured["stdout"]), bytes(captured["stderr"]))


def text(output):
    return redact(output.decode("utf-8", errors="replace").strip())
