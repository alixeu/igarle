"""Operator-owned configuration and task specifications."""

from __future__ import annotations

from dataclasses import dataclass, replace
from fnmatch import fnmatchcase
from hashlib import sha256
import json
from pathlib import Path, PurePosixPath
import re
import tomllib


HEADINGS = ("Objective", "Constraints", "Acceptance criteria")
SPEC_KEYS = ("objective", "constraints", "acceptance_criteria")
SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
IDENTIFIER = re.compile(r"[a-zA-Z0-9_.-]+\Z")
PROTECTED = {".git", ".agents", ".github", ".pi", ".aws", ".ssh", ".codex",
             "agents.md", "claude.md", ".gitmodules", ".gitattributes", ".loop.toml"}


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return sha256(json_text(value).encode()).hexdigest()


def runtime_policy(config, bundle):
    sources = sorted([*bundle.glob("*.py"), *bundle.glob("*.ts")])
    return digest({"config": config, "runtime": [
        [source.name, sha256(source.read_bytes()).hexdigest()] for source in sources
    ]})


def clean_path(path):
    if not isinstance(path, str) or not path or len(path) > 500:
        raise ValueError("Expected a repository-relative file path.")
    posix = PurePosixPath(path)
    if not posix.parts or posix.is_absolute() or posix.as_posix() != path or any(
        part == ".." or "\\" in part or any(ord(char) < 32 for char in part)
        for part in posix.parts
    ):
        raise ValueError(f"Invalid repository path: {path!r}")
    return path


def protected(path):
    for part in PurePosixPath(path).parts:
        name = part.casefold()
        if (name in PROTECTED or name.startswith(".env") or name.endswith((".pem", ".key"))
                or name in {"auth.json", "credentials.json", ".netrc", ".npmrc", ".pypirc"}):
            return True
    return path == "outputs" or path.startswith("outputs/")


def permitted(path, patterns):
    return not protected(clean_path(path)) and any(fnmatchcase(path, pattern) for pattern in patterns)


def literal(value, label, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{label} must be nonempty text of at most {limit} characters.")
    return value.strip()


def task_spec(raw):
    if not isinstance(raw, dict):
        raise ValueError("A task specification must be an object.")
    spec = {key: literal(raw.get(key), key, 180 if key == "title" else 4000)
            for key in ("title", *SPEC_KEYS)}
    if "\n" in spec["title"] or "\r" in spec["title"]:
        raise ValueError("The task title must fit on one line.")
    return spec


def issue_spec(issue):
    if issue.get("pull_request") or issue.get("state") != "open":
        raise ValueError("Expected an open GitHub Issue.")
    body = literal(issue.get("body"), "Issue body", 14000).replace("\r\n", "\n")
    headings = "|".join(HEADINGS)
    sections = {}
    for name, content in re.findall(rf"^## ({headings})[ \t]*$\n(.*?)(?=^## |^\s*<!-- pi-loop:|\Z)",
                                    body, re.MULTILINE | re.DOTALL | re.IGNORECASE):
        key = name.lower().replace(" ", "_")
        if key in sections:
            raise ValueError("Each task section must occur once.")
        sections[key] = content.strip()
    return task_spec({"title": issue.get("title"), **sections})


def issue_body(spec, marker):
    return "\n\n".join([*(f"## {title}\n\n{spec[key]}" for title, key in zip(HEADINGS, SPEC_KEYS)), marker])


def positive(value, name, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}.")
    return value


@dataclass(frozen=True)
class Check:
    name: str
    argv: tuple[str, ...]
    scan_failure: str = ""


@dataclass(frozen=True)
class Settings:
    repository: str
    branch: str
    operators: tuple[int, ...]
    workflow: str
    required_job: str
    ci_source: str
    status_actor: int | None
    pi_bin: str
    model: str
    pi_profile: str
    provider_env: tuple[str, ...]
    timeout: int
    check_timeout: int
    scan_interval: int
    poll_interval: int
    retries: int
    max_steps: int
    stall_timeout: int
    review_diff_limit: int
    allowed_paths: tuple[str, ...]
    auto_paths: tuple[str, ...]
    checks: tuple[Check, ...]
    tool_dirs: tuple[str, ...]
    policy: str

    @classmethod
    def load(cls, root):
        path = root / ".loop.toml"
        if path.is_symlink():
            raise ValueError("The operator configuration must be a regular file.")
        config = tomllib.loads(path.read_text(encoding="utf-8"))
        return replace(cls.from_dict(config), policy=runtime_policy(config, Path(__file__).resolve().parent))

    @classmethod
    def from_dict(cls, doc):
        if doc.get("version") != 1:
            raise ValueError("Expected Loop configuration version 1.")
        github = doc["github"]
        pi = doc.get("pi", {})
        run = doc.get("run", {})
        project = doc["project"]
        repo = github["repository"]
        branch = github["branch"]
        if not re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", repo):
            raise ValueError("Set github.repository to OWNER/REPO.")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_./-]*", branch) or ".." in branch:
            raise ValueError("Invalid GitHub branch.")
        workflow = clean_path(github["workflow"])
        if not re.fullmatch(r"\.github/workflows/[a-zA-Z0-9_.-]+\.ya?ml", workflow):
            raise ValueError("Set github.workflow to one GitHub Actions workflow file.")
        job = literal(github["required_job"], "required_job", 100)
        ci_source = github.get("ci_source", "actions")
        if ci_source not in {"actions", "status"}:
            raise ValueError("github.ci_source must be actions or status.")
        status_actor = github.get("status_actor")
        if ci_source == "status":
            positive(status_actor, "status_actor", 2**63 - 1)
        elif status_actor is not None:
            raise ValueError("status_actor is only valid for commit-status CI.")
        operators = github.get("operator_ids", [])
        if not isinstance(operators, list):
            raise ValueError("operator_ids must be an array of numeric GitHub IDs.")
        operators = tuple(positive(item, "operator ID", 2**63 - 1) for item in operators)
        checks = []
        for name, value in doc.get("checks", {}).items():
            if not IDENTIFIER.fullmatch(name):
                raise ValueError(f"Invalid check name: {name!r}")
            argv = value["argv"]
            if not isinstance(argv, list) or not argv or any(
                not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv
            ):
                raise ValueError(f"Check {name} needs an argument array.")
            marker = value.get("scan_failure", "")
            if not isinstance(marker, str) or "\n" in marker or len(marker) > 200:
                raise ValueError(f"Invalid scan_failure for {name}.")
            checks.append(Check(name, tuple(argv), marker))
        if not checks:
            raise ValueError("Register at least one project check.")
        provider_env = pi.get("provider_env", [])
        if not isinstance(provider_env, list) or any(
            not isinstance(name, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", name)
            or name.startswith(("GH_", "GITHUB_", "GIT_", "NODE_", "LD_", "DYLD_", "PI_LOOP_"))
            or name in {"PATH", "HOME", "TMPDIR", "BASH_ENV", "ENV", "PYTHONPATH"}
            for name in provider_env
        ):
            raise ValueError("provider_env needs model credential environment variable names.")
        allowed = project["allowed_paths"]
        auto = project.get("auto_paths", [])
        if any(not isinstance(paths, list) for paths in (allowed, auto)) or not allowed:
            raise ValueError("Set project.allowed_paths and optional project.auto_paths.")
        for path in [*allowed, *auto]:
            clean_path(path)
        policy = digest(doc)
        return cls(
            repo.lower(), branch, operators, workflow, job, ci_source, status_actor, pi.get("binary", "pi"),
            pi.get("model", ""), pi.get("profile", ""), tuple(provider_env),
            positive(run.get("step_timeout", 900), "step_timeout", 7200),
            positive(run.get("check_timeout", 180), "check_timeout", 1800),
            positive(run.get("scan_interval", 3600), "scan_interval", 86400),
            positive(run.get("poll_interval", 30), "poll_interval", 300),
            positive(run.get("retries", 3), "retries", 10),
            positive(run.get("max_steps", 24), "max_steps", 1000),
            positive(run.get("stall_timeout", 3600), "stall_timeout", 86400),
            positive(run.get("review_diff_limit", 23000), "review_diff_limit", 4_000_000),
            tuple(allowed), tuple(auto), tuple(checks), tuple(project.get("tool_dirs", [])), policy,
        )

    def paths(self, proposed, autonomous=False):
        if not isinstance(proposed, list) or not 1 <= len(proposed) <= 80:
            raise ValueError("A plan must name between 1 and 80 files.")
        for path in proposed:
            if not permitted(path, self.allowed_paths) or (
                autonomous and not permitted(path, self.auto_paths)
            ):
                raise ValueError(f"File outside the authorized project scope: {path!r}")
        return sorted(set(proposed))
