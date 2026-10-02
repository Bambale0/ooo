"""Code-only releases into an existing host Compose stack; no DB/proxy replacement."""

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from pathlib import Path

SERVICES = ("app", "worker", "webhook_worker", "telegram")
REVISION_CHECK = "import os,sys; assert os.environ.get('APP_REVISION') == sys.argv[1]"
READINESS_CHECK = (
    "import json,sys,urllib.request; "
    "r=json.load(urllib.request.urlopen('http://localhost:8000/api/v1/readiness',timeout=5)); "
    "assert r.get('status') == 'ready' and r.get('revision') == sys.argv[1]"
)


class DeployError(RuntimeError):
    """Operator-safe error. Never include environment values or raw Docker output."""


def command(args: list[str], *, env: dict | None = None, timeout: int = 60) -> str:
    try:
        result = subprocess.run(args, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError("Command unavailable or timed out; inspect the host privately") from None
    if result.returncode:
        raise DeployError(f"Command failed with exit {result.returncode}; inspect the host privately")
    return result.stdout.strip()


def updated_env(content: bytes, image: str, revision: str) -> bytes:
    """Replace only release selectors. Do not parse/re-serialize any secret value."""
    values = {b"NEIRONYCH_IMAGE": image.encode("ascii"), b"APP_REVISION": revision.encode("ascii")}
    seen = set()
    lines = []
    newline = b"\r\n" if b"\r\n" in content else b"\n"
    for line in content.splitlines(keepends=True):
        match = re.match(rb"^[ \t]*(?:export[ \t]+)?(NEIRONYCH_IMAGE|APP_REVISION)[ \t]*=", line)
        if match:
            key = match[1]
            if key in seen:
                raise DeployError("Duplicate release selector in host .env")
            seen.add(key)
            line = key + b"=" + values[key] + newline
        lines.append(line)
    result = b"".join(lines)
    for key, value in values.items():
        if key not in seen:
            if result and not result.endswith(b"\n"):
                result += newline
            result += key + b"=" + value + newline
    return result


def atomic_write(path: Path, content: bytes) -> None:
    path = path.resolve()
    previous = path.stat() if path.exists() else None
    fd, name = tempfile.mkstemp(prefix=".release-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            if previous:
                if hasattr(os, "geteuid") and os.geteuid() == 0:
                    os.fchown(stream.fileno(), previous.st_uid, previous.st_gid)
                os.fchmod(stream.fileno(), stat.S_IMODE(previous.st_mode))
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def set_current(root: Path, target: str | None) -> None:
    current = root / "current"
    if target is None:
        current.unlink(missing_ok=True)
        return
    fd, temporary = tempfile.mkstemp(prefix=".current-", dir=root)
    os.close(fd)
    os.unlink(temporary)
    try:
        os.symlink(target, temporary)
        os.replace(temporary, current)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


class HostRelease:
    def __init__(self, root: Path, revision: str, image: str, *, attempts: int = 12):
        self.root = root.resolve()
        self.revision = revision
        self.image = image
        self.attempts = attempts
        self.env = dict(os.environ)
        self.compose_args = [
            "docker", "compose", "--project-directory", str(self.root),
            "--env-file", str(self.root / ".env"),
            "-f", str(self.root / "docker-compose.prod.yml"),
            "-f", str(self.root / "compose.host.yml"), "--profile", "telegram",
        ]

    def compose(self, *args: str, timeout: int = 60) -> str:
        return command([*self.compose_args, *args], env=self.env, timeout=timeout)

    def runtime_image(self) -> str:
        images = set()
        for service in SERVICES:
            ids = self.compose("ps", "--all", "--quiet", service).split()
            if len(ids) != 1:
                raise DeployError(f"Expected exactly one existing {service} container")
            details = command(["docker", "inspect", "--format", "{{.Image}} {{.State.Running}}", ids[0]]).split()
            if len(details) != 2 or details[1] != "true" or not re.fullmatch(r"sha256:[0-9a-f]{64}", details[0]):
                raise DeployError(f"Existing {service} container is not running")
            images.add(details[0])
        if len(images) != 1:
            raise DeployError("Mixed runtime images; reconcile the current release first")
        return images.pop()

    def verify(self, revision: str, image: str) -> None:
        for attempt in range(self.attempts):
            try:
                if self.runtime_image() != image:
                    raise DeployError("Runtime image mismatch")
                for service in SERVICES:
                    self.compose("exec", "-T", service, "python", "-c", REVISION_CHECK, revision, timeout=15)
                self.compose("exec", "-T", "app", "python", "-c", READINESS_CHECK, revision, timeout=15)
                return
            except DeployError:
                if attempt + 1 == self.attempts:
                    raise
                time.sleep(5)

    def execute(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{40}", self.revision) or not self.image.endswith(":" + self.revision):
            raise DeployError("A full commit SHA and its image tag are required")
        if not 1 <= self.attempts <= 60:
            raise DeployError("Readiness attempts must be between 1 and 60")
        for name in (".env", "REVISION", "docker-compose.prod.yml", "compose.host.yml"):
            if not (self.root / name).is_file():
                raise DeployError(f"Required host file is missing: {name}")
        if not (self.root / "releases" / self.revision).is_dir():
            raise DeployError("Verified release bundle is missing")
        current = self.root / "current"
        if current.exists() and not current.is_symlink():
            raise DeployError("current must be a symlink, not a directory")
        previous_link = os.readlink(current) if current.is_symlink() else None
        original_env = (self.root / ".env").read_bytes()
        original_revision = (self.root / "REVISION").read_bytes()
        previous_revision = original_revision.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", previous_revision):
            raise DeployError("Invalid previous revision; refusing an unverified rollback target")
        # Fail before any runtime change on ambiguous .env selectors.
        updated_env(original_env, self.image, self.revision)
        configuration = json.loads(self.compose("config", "--format", "json"))
        project = configuration.get("name", "")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", project):
            raise DeployError("Host Compose project name is missing or invalid")
        for service in SERVICES:
            settings = configuration.get("services", {}).get(service)
            if settings is None or settings.get("scale", 1) != 1 or settings.get("deploy", {}).get("replicas", 1) != 1:
                raise DeployError("Each mandatory runtime service must have exactly one replica")
        self.compose_args.extend(["--project-name", project])
        previous_image = self.runtime_image()
        self.verify(previous_revision, previous_image)
        if previous_revision == self.revision:
            print("DEPLOY_ALREADY_VERIFIED " + self.revision)
            return

        print("DEPLOY_PREPARE " + self.revision, flush=True)
        command(["docker", "pull", self.image], timeout=600)
        image_id = command(["docker", "image", "inspect", "--format", "{{.Id}}", self.image])
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
            raise DeployError("Invalid pulled image identity")
        label = command([
            "docker", "image", "inspect", "--format",
            '{{index .Config.Labels "org.opencontainers.image.revision"}}', self.image,
        ])
        if label != self.revision:
            raise DeployError("Pulled image revision label does not match verified CI revision")
        self.env.update(NEIRONYCH_IMAGE=image_id, APP_REVISION=self.revision)
        # Host backups/PITR differ from standalone deployment. Never invent a
        # migration/backup procedure: stop before replacing containers if needed.
        self.compose("run", "--rm", "--no-deps", "app", "alembic", "current", "--check-heads", timeout=120)
        parent = self.root / "release-backups"
        parent.mkdir(mode=0o700, exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix=self.revision[:12] + "-", dir=parent))
        for name in (".env", "REVISION", "docker-compose.prod.yml", "compose.host.yml"):
            shutil.copy2(self.root / name, backup / name)
            (backup / name).chmod(0o600)
        (backup / "rollback.json").write_text(json.dumps({
            "image": previous_image, "revision": previous_revision, "current": previous_link,
        }), encoding="utf-8")
        (backup / "rollback.json").chmod(0o600)
        try:
            print("DEPLOY_SWITCH " + self.revision, flush=True)
            self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", *SERVICES, timeout=300)
            self.verify(self.revision, image_id)
            time.sleep(5)
            self.verify(self.revision, image_id)
            # Persist selectors for compose.sh/systemd restarts, not only the current invocation.
            atomic_write(self.root / ".env", updated_env(original_env, image_id, self.revision))
            set_current(self.root, "releases/" + self.revision)
            atomic_write(self.root / "REVISION", (self.revision + "\n").encode("ascii"))
        except BaseException:
            print("DEPLOY_ROLLBACK " + previous_revision, flush=True)
            self.env.update(NEIRONYCH_IMAGE=previous_image, APP_REVISION=previous_revision)
            atomic_write(self.root / ".env", original_env)
            atomic_write(self.root / "REVISION", original_revision)
            set_current(self.root, previous_link)
            # The previous image can be local-only; no registry pull during rollback.
            self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", *SERVICES, timeout=300)
            self.verify(previous_revision, previous_image)
            print("ROLLBACK_VERIFIED " + previous_revision, flush=True)
            raise
        print("DEPLOY_VERIFIED " + self.revision, flush=True)


def main() -> None:
    import fcntl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    try:
        with (args.root / ".deploy.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            HostRelease(args.root, args.revision, args.image).execute()
    except (DeployError, OSError) as error:
        # File errors can include sensitive local paths; report only the safe domain error.
        message = str(error) if isinstance(error, DeployError) else "Host access/lock failure"
        parser.exit(1, message + "\n")


if __name__ == "__main__":
    main()
