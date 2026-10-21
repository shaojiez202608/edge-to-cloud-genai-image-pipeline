"""Repository text rules: no local paths, personal mail domains, internal wording or account ids in any
tracked text file, and the standard documents exist."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", "cdk.out", "build", "node_modules", ".pytest_cache", ".ruff_cache", "out", ".deploy"}


def _files() -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
        return [ROOT / p for p in out if (ROOT / p).is_file()]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in ROOT.rglob("*") if p.is_file() and not SKIP_DIRS & set(p.relative_to(ROOT).parts)]


TEXT = [p for p in _files() if p.suffix in {".md", ".py", ".json", ".yml", ".yaml", ".toml", ".sh", ".txt", ""}
        and p.name not in ("uv.lock", ".forbidden-terms")]
FORBIDDEN = re.compile(r"/Users/|163\.com|employee account|员工账号|负责人|客户要求|补充材料|档位", re.I)
ACCOUNT = re.compile(r"(?<![0-9A-Za-z])\d{12}(?![0-9A-Za-z])")
# Project-specific names that must never appear are not listed here (that would publish them): put one regular
# expression per line in the git-ignored file .forbidden-terms; the check is skipped when the file is absent.
LOCAL_TERMS_FILE = ROOT / ".forbidden-terms"
# This file defines the generic patterns above, so it is excluded from the generic check only.
SELF = Path(__file__).resolve()


def _local_terms() -> re.Pattern | None:
    if not LOCAL_TERMS_FILE.is_file():
        return None
    terms = [t.strip() for t in LOCAL_TERMS_FILE.read_text(encoding="utf-8").splitlines()]
    terms = [t for t in terms if t and not t.startswith("#")]
    return re.compile("|".join(f"(?:{t})" for t in terms), re.I) if terms else None


@pytest.mark.parametrize("path", TEXT, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_forbidden_text_or_account_ids(path):
    text = path.read_text(encoding="utf-8", errors="ignore")
    if path.resolve() != SELF:
        assert not FORBIDDEN.search(text), FORBIDDEN.search(text).group(0)
    bad = [m for m in ACCOUNT.findall(text) if m != "111122223333"]
    assert not bad, "12-digit number other than the placeholder"


def test_local_forbidden_terms():
    pattern = _local_terms()
    if pattern is None:
        pytest.skip("no local .forbidden-terms file")
    hits = [str(p.relative_to(ROOT)) for p in TEXT if pattern.search(p.read_text(encoding="utf-8", errors="ignore"))]
    assert not hits


@pytest.mark.parametrize("name", ["README.md", "README.zh-CN.md", "LICENSE", "CONTRIBUTING.md", "CODE_OF_CONDUCT.md",
                                  "SECURITY.md", "SECURITY-NOTES.md", "THREAT_MODEL.md", "VERIFICATION.md",
                                  "DEPLOYMENT-LOG.md", "OPEN-QUESTIONS.md", "DECISIONS.md", "THIRD-PARTY-LICENSES",
                                  "CHANGELOG.md", "VERSION", "AGENTS.md", "CLAUDE.md", "docs/prior-art.md",
                                  "docs/architecture.md", "docs/deployment-guide.md", "docs/demo-script.md",
                                  "docs/handover.md", "docs/cleanup.md", "docs/standards-review.md"])
def test_standard_files_exist(name):
    assert (ROOT / name).is_file()


def test_threat_model_is_draft():
    assert "Status: DRAFT" in (ROOT / "THREAT_MODEL.md").read_text(encoding="utf-8")
