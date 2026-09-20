"""LAMBDA-DRIFT option B — what the live Lambda is actually running.

The box deploys itself from git (`scripts/deploy.sh`) and SCHEMA-DRIFT already
watches migrations. The Lambda has no such loop: `api/deploy.sh` is run by hand
from a Mac, there is no CI in either repo, and nothing has ever compared the
deployed package with the repo. #114 and #106 sat merged-but-undeployed for a
day before anyone noticed; that is the failure this closes.

It compares CONTENTS, not hashes. A code hash (`CodeSha256`) covers the whole
zip including pinned dependencies, so it changes when nothing about our code
changed and tells you nothing about WHICH file drifted. This downloads the live
package and diffs every `app/**` and `knowledge/**` member against
`git show <ref>:<path>`, and names the files.

Read-only, and read-only in IAM terms too — see IAM_STATEMENT below. It never
deploys, never writes, and posts nothing when the package matches the repo.

Cost: the package is ~24 MB. Once a week on the Monday job is the whole budget.
"""

from __future__ import annotations

import logging
import subprocess
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parent.parent
FUNCTION_NAME = "rdmis-crm-api"
REGION = "us-east-1"

# Zip member prefix -> repo path prefix. api/deploy.sh zips `app/` from inside
# api/, and `../knowledge/` from there — the `../` is stored verbatim in the
# archive and Lambda strips it on extraction, so both spellings are accepted.
PREFIXES = (("app/", "api/app/"),
            ("../knowledge/", "knowledge/"),
            ("knowledge/", "knowledge/"))

# The two read-only permissions the box role needs. PROPOSED, not applied.
IAM_STATEMENT = {
    "Version": "2012-10-17",
    "Statement": [{
        "Sid": "ReadTheLiveLambdaForDriftCheck",
        "Effect": "Allow",
        "Action": ["lambda:GetFunction", "lambda:GetFunctionConfiguration"],
        "Resource": "arn:aws:lambda:us-east-1:095652687153:function:rdmis-crm-api",
    }],
}


def _is_code(member: str) -> bool:
    return (member.endswith(".py")
            and "__pycache__" not in member
            and not member.endswith("/"))


def repo_path_for(member: str) -> str | None:
    """`app/routers/health.py` -> `api/app/routers/health.py`; None when the
    member is not one of ours (a dependency, a migration, a test)."""
    for zip_prefix, repo_prefix in PREFIXES:
        if member.startswith(zip_prefix):
            return repo_prefix + member[len(zip_prefix):]
    return None


def git_show(path: str, ref: str = "HEAD", repo: Path = REPO) -> bytes | None:
    """The file's contents at `ref`, or None when it isn't tracked there."""
    out = subprocess.run(["git", "-C", str(repo), "show", f"{ref}:{path}"],
                         capture_output=True)
    return out.stdout if out.returncode == 0 else None


def git_files(ref: str = "HEAD", repo: Path = REPO) -> set[str]:
    """Every tracked .py under the two roots at `ref` — so a file that exists
    in the repo but never made it into the package is visible too."""
    out = subprocess.run(["git", "-C", str(repo), "ls-tree", "-r", "--name-only",
                          ref, "api/app", "knowledge"], capture_output=True, text=True)
    if out.returncode != 0:
        return set()
    return {p for p in out.stdout.split()
            if p.endswith(".py") and "__pycache__" not in p}


def compare(zip_path: str | Path, ref: str = "HEAD", repo: Path = REPO,
            read_repo=None, tracked=None) -> dict:
    """Live package vs the repo at `ref`, file by file.

    differs         same path both sides, different bytes — the live API is
                    running code that is not what `ref` says it is.
    only_in_lambda  shipped but no longer tracked (a file deleted since).
    only_in_repo    tracked but absent from the package — never deployed.
    """
    read_repo = read_repo or (lambda p: git_show(p, ref, repo))
    tracked = git_files(ref, repo) if tracked is None else set(tracked)

    differs, only_in_lambda, seen = [], [], set()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.namelist():
            if not _is_code(member):
                continue
            path = repo_path_for(member)
            if path is None:
                continue
            seen.add(path)
            live = zf.read(member)
            mine = read_repo(path)
            if mine is None:
                only_in_lambda.append(path)
            elif live != mine:
                differs.append(path)

    return {"differs": sorted(differs),
            "only_in_lambda": sorted(only_in_lambda),
            "only_in_repo": sorted(tracked - seen),
            "checked": len(seen)}


def message(result: dict, *, ref_sha: str = "", modified: str = "") -> str | None:
    """The ops post, or None when the package matches the repo."""
    if not (result["differs"] or result["only_in_lambda"] or result["only_in_repo"]):
        return None

    def names(paths: list[str], cap: int = 6) -> str:
        shown = ", ".join(paths[:cap])
        return shown + (f" (+{len(paths) - cap} more)" if len(paths) > cap else "")

    lines = []
    if result["differs"]:
        lines.append(f"{len(result['differs'])} file(s) differ: {names(result['differs'])}")
    if result["only_in_repo"]:
        lines.append(f"{len(result['only_in_repo'])} file(s) in the repo were never "
                     f"deployed: {names(result['only_in_repo'])}")
    if result["only_in_lambda"]:
        lines.append(f"{len(result['only_in_lambda'])} file(s) deployed but no longer "
                     f"in the repo: {names(result['only_in_lambda'])}")
    stamp = f" Live package built {modified}." if modified else ""
    head = f" Repo at {ref_sha[:7]}." if ref_sha else ""
    return ("⚠️ Lambda drift — the live API is not running this code. "
            + " ".join(lines) + "."
            + head + stamp
            + " Redeploy: `cd api && AWS_PROFILE=rdmis-admin bash deploy.sh`.")


def download_live(dest: Path, *, client=None) -> tuple[Path, str]:
    """Fetch the live package to `dest`. Returns (path, LastModified).

    Needs lambda:GetFunction — the response carries a presigned S3 URL, and the
    download itself needs no further permission.
    """
    import urllib.request

    if client is None:
        import boto3
        client = boto3.client("lambda", region_name=REGION)
    info = client.get_function(FunctionName=FUNCTION_NAME)
    url = info["Code"]["Location"]
    with urllib.request.urlopen(url, timeout=120) as resp, open(dest, "wb") as fh:
        while chunk := resp.read(1 << 20):
            fh.write(chunk)
    return dest, info["Configuration"].get("LastModified", "")


def check(ref: str = "HEAD", repo: Path = REPO, *, client=None) -> str | None:
    """Monday's check. Returns the ops post, or None when there is no drift.

    A missing permission is reported rather than swallowed: silence has to mean
    "the package matches", not "the check never ran".
    """
    import tempfile

    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", ref],
                         capture_output=True, text=True).stdout.strip()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            path, modified = download_live(Path(tmp) / "live.zip", client=client)
            result = compare(path, ref=ref, repo=repo)
    except Exception as exc:
        name = type(exc).__name__
        if "AccessDenied" in str(exc) or name in ("ClientError", "NoCredentialsError"):
            return ("⚠️ Lambda drift check could not run — the box role is missing "
                    "lambda:GetFunction / lambda:GetFunctionConfiguration on "
                    f"{FUNCTION_NAME}. ({exc})")
        logger.exception("Lambda drift check failed")
        return f"⚠️ Lambda drift check failed: {name}: {exc}"

    logger.info("Lambda drift: %d file(s) checked, %d differ",
                result["checked"], len(result["differs"]))
    return message(result, ref_sha=sha, modified=modified)
