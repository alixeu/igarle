"""Git snapshots and unattended checks, with separate process permissions."""

from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit
from uuid import uuid4

from processes import ProcessError, run, text
from settings import SHA, clean_path, protected


def git_env():
    return {key: value for key, value in os.environ.items() if not key.startswith("GIT_")} | {
        "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C",
    }


def git(root, *args, timeout=60, limit=4_000_000, allow_failure=False, input_bytes=None):
    command = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
               "-c", "submodule.recurse=false", "-c", "commit.gpgSign=false",
               "-c", "diff.renames=false", *args]
    result = run(command, cwd=root, env=git_env(), timeout=timeout, limit=limit, input_bytes=input_bytes)
    if result.returncode and not allow_failure:
        raise ProcessError(f"Git {args[0]} failed: {(text(result.stderr) or text(result.stdout))[-2000:]}")
    return result


def git_text(root, *args, **kwargs):
    return git(root, *args, **kwargs).stdout.decode("utf-8").strip()


def locate_root(directory):
    root = Path(git_text(directory, "rev-parse", "--show-toplevel")).resolve()
    git_dir = git_text(root, "rev-parse", "--absolute-git-dir")
    common_dir = git_text(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if Path(common_dir).resolve() != Path(git_dir).resolve():
        raise ValueError("Run Loop from the main repository checkout.")
    return root


def github_origin(url):
    if url.startswith("git@github.com:"):
        name = url.removeprefix("git@github.com:")
    else:
        parsed = urlsplit(url)
        if parsed.netloc not in {"github.com", "git@github.com"} or parsed.scheme not in {"https", "ssh"}:
            return None
        if parsed.query or parsed.fragment:
            return None
        name = parsed.path.lstrip("/")
    return name.rstrip("/").removesuffix(".git").lower()


def verify_origin(root, repository):
    for flags in ((), ("--push",)):
        urls = git_text(root, "remote", "get-url", "--all", *flags, "origin").splitlines()
        if len(urls) != 1 or github_origin(urls[0]) != repository:
            raise ValueError(f"Both origin URLs must designate {repository} on GitHub.")


def head_sha(root, ref="HEAD"):
    result = git(root, "rev-parse", "--verify", f"{ref}^{{commit}}", allow_failure=True)
    head = result.stdout.decode().strip()
    return head if result.returncode == 0 and SHA.fullmatch(head) else None


def changed_paths(root):
    raw = git(root, "status", "--porcelain=v1", "--no-renames", "-z", "--untracked-files=all").stdout
    try:
        return {clean_path(item[3:].decode("utf-8")) for item in raw.split(b"\x00") if item}
    except UnicodeDecodeError as error:
        raise ValueError("Changed paths must be UTF-8.") from error


def is_ancestor(root, base, head):
    return git(root, "merge-base", "--is-ancestor", base, head, allow_failure=True).returncode == 0


def safe_ref(ref):
    if not re.fullmatch(r"refs/(?:heads|pi-loop)/[a-zA-Z0-9_./-]+", ref) or ".." in ref:
        raise ValueError("Invalid Loop ref.")
    return ref


class Workspaces:
    def __init__(self, root, directory, settings):
        self.root = root
        self.directory = directory
        self.settings = settings
        self.trees = directory / "worktrees"
        if self.trees.resolve() != self.trees:
            raise ValueError("The worktree directory cannot traverse a symlink.")
        self.trees.mkdir(mode=0o700, exist_ok=True)

    def local_base(self, recorded=None):
        return next((sha for ref in (recorded, f"refs/remotes/origin/{self.settings.branch}",
                                     f"refs/heads/{self.settings.branch}")
                     if ref and (sha := head_sha(self.root, ref))), None)

    def fetch_base(self, number):
        if not SHA.fullmatch(number):
            raise ValueError("GitHub returned an invalid baseline SHA.")
        if head_sha(self.root, number) == number:
            return number
        git(self.root, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
            "fetch", "--no-tags", "--no-recurse-submodules", "origin",
            f"+refs/heads/{self.settings.branch}:refs/pi-loop/base")
        if head_sha(self.root, number) != number:
            raise ProcessError("The GitHub baseline moved during fetch.")
        return number

    @contextmanager
    def at(self, sha):
        if not SHA.fullmatch(sha):
            raise ValueError("Invalid checkout SHA.")
        tree = self.trees / uuid4().hex
        git(self.root, "worktree", "add", "--detach", str(tree), sha)
        try:
            yield tree
        finally:
            git(self.root, "worktree", "remove", "--force", str(tree), allow_failure=True)

    def checkpoint(self, tree, parent, allowed, run_id):
        if head_sha(tree) != parent:
            raise ValueError("The checkout did not stay on the assigned base.")
        paths = changed_paths(tree)
        if not paths:
            return None
        if paths - set(allowed) or any(protected(path) for path in paths):
            raise ValueError("The candidate changed files outside the frozen plan.")
        for name in paths:
            path = tree / name
            if path.resolve() != path or (path.exists() and not path.is_file()):
                raise ValueError(f"Cannot commit a symlink or special file: {name}")
        git(tree, "add", "--all", "--", *sorted(paths))
        result = git(tree, "diff", "--cached", "--check", allow_failure=True)
        if result.returncode:
            raise ValueError(f"Candidate diff check failed: {text(result.stdout)[-1200:]}")
        staged = git(tree, "diff", "--cached", "--name-only", "-z").stdout.split(b"\0")
        if {name.decode() for name in staged if name} != paths:
            raise ValueError("The staged files differ from the authorized changes.")
        git(tree, "commit", "--quiet", "-m", f"Pi Loop checkpoint {run_id}")
        head = head_sha(tree)
        ref = safe_ref(f"refs/pi-loop/checkpoints/{run_id}")
        git(self.root, "update-ref", ref, head, "0" * len(head))
        return head

    def verify_diff(self, base, head, allowed, *, worktree=None):
        local = worktree or self.root
        if base == head or not is_ancestor(self.root, base, head):
            raise ValueError("Candidate must change and descend from the frozen base.")
        names = git(local, "diff", "--name-only", "-z", base, head, "--").stdout
        files = {clean_path(name.decode()) for name in names.split(b"\0") if name}
        if files - set(allowed) or any(protected(path) for path in files):
            raise ValueError("Candidate includes files outside the frozen plan.")
        return files

    def transplant(self, previous, checkpoint, base, allowed, run_id):
        if not self.verify_diff(previous, checkpoint, allowed):
            return None
        diff = git(self.root, "diff", "--binary", "--full-index", previous, checkpoint).stdout
        with self.at(base) as tree:
            applied = git(tree, "apply", "--3way", "--index", "-", input_bytes=diff, allow_failure=True)
            if applied.returncode:
                return None
            return self.checkpoint(tree, base, allowed, run_id)

    def checks(self, tree, selected=None, stop=None):
        names = set(selected) if selected else None
        return {check.name: self.run_check(tree, check.argv, stop)
                for check in self.settings.checks if names is None or check.name in names}

    def run_check(self, tree, argv, stop):
        if sys.platform != "darwin":
            raise ProcessError("Project checks require the macOS process sandbox.")
        def selector(operation, paths, mode="subpath"):
            specs = " ".join(f"({mode} {json.dumps(str(path))})" for path in paths)
            return f"(allow {operation} {specs})"

        scratch = self.directory / f"check-{uuid4().hex}"
        scratch.mkdir(mode=0o700)
        try:
            executable = sys.executable if argv[0] in {"python3", "python"} else shutil.which(argv[0])
            if executable is None and argv[0].startswith("./"):
                candidate = tree / argv[0]
                if candidate.is_file() and candidate.resolve().is_relative_to(tree):
                    executable = str(candidate)
            if executable is None:
                raise ProcessError(f"Check executable is unavailable: {argv[0]}")
            executable = Path(executable).resolve()
            system_roots = ["/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/lib",
                            "/usr/libexec", "/usr/share", "/System", "/Library/Apple",
                            "/private/var/db/dyld", "/private/var/db/timezone"]
            tool_dirs = [Path(path).expanduser() for path in self.settings.tool_dirs]
            if any(not path.is_absolute() or not path.is_dir() or path.resolve() in {
                Path("/"), Path.home(), self.root, self.directory
            } for path in tool_dirs):
                raise ValueError("tool_dirs must name existing tool installations outside the runtime.")
            reads = [*system_roots, tree, scratch, *(path.resolve() for path in tool_dirs)]
            if executable.name.startswith("python") and not executable.is_relative_to(tree):
                prefix = Path(sys.base_prefix).resolve()
                if executable.is_relative_to(prefix):
                    reads.append(prefix)
            # macOS can locate its shared dyld cache through private paths that
            # change across releases. Start with system reads, then close user,
            # removable-volume and temporary directories before reopening only
            # the checkout, scratch area and explicitly selected toolchains.
            profile = "\n".join([
                "(version 1)", "(deny default)", "(allow process-exec process-fork sysctl-read)",
                "(allow file-read*)",
                "(deny file-read* (subpath \"/Users\") (subpath \"/Volumes\") "
                "(subpath \"/private/tmp\") (subpath \"/private/var/folders\"))",
                "(allow file-read-metadata)",
                selector("file-read*", reads),
                selector("file-read*", [executable, "/dev/null", "/dev/random", "/dev/urandom"], "literal"),
                selector("file-write*", [scratch]),
                selector("file-write*", ["/dev/null"], "literal"),
            ])
            profile_path = scratch / "sandbox.sb"
            profile_path.write_text(profile)
            env = {
                "PATH": os.pathsep.join([str(executable.parent), "/usr/bin", "/bin", "/usr/sbin",
                                         *(str(path / "bin") for path in tool_dirs)]),
                "HOME": str(scratch), "TMPDIR": str(scratch), "XDG_CACHE_HOME": str(scratch),
                "LC_ALL": "C", "PYTHONDONTWRITEBYTECODE": "1", "PI_LOOP_SANDBOXED": "1",
            }
            result = run(["/usr/bin/sandbox-exec", "-f", str(profile_path), str(executable), *argv[1:]],
                         cwd=tree, env=env, timeout=self.settings.check_timeout, limit=1_000_000, stop=stop)
            return self.check_result(tree, result)
        finally:
            shutil.rmtree(scratch)

    @staticmethod
    def check_result(tree, result):
        output = "\n".join(part for part in (text(result.stdout), text(result.stderr)) if part)
        output = output.replace(str(tree), "<worktree>")
        return {"exit": result.returncode, "output": output[-8000:],
                "output_digest": sha256(output.encode()).hexdigest()}

    def remote_head(self, branch):
        ref = safe_ref(f"refs/heads/{branch}")
        output = git_text(self.root, "-c", "credential.helper=", "-c",
                          "credential.helper=!gh auth git-credential",
                          "ls-remote", "--refs", "origin", ref, timeout=30)
        if not output:
            return None
        sha, observed = output.split("\t")
        if observed != ref or not SHA.fullmatch(sha):
            raise ValueError("Unexpected remote branch response.")
        return sha

    def push(self, branch, head, expected):
        ref = safe_ref(f"refs/heads/{branch}")
        if not SHA.fullmatch(head) or (expected is not None and not SHA.fullmatch(expected)):
            raise ValueError("Invalid publication SHA.")
        git(self.root, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential",
            "push", "--porcelain", f"--force-with-lease={ref}:{expected or ''}",
            "origin", f"{head}:{ref}", timeout=60)
