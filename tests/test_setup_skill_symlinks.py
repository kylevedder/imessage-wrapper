from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup_skill_symlinks.sh"
SKILL_NAMES = (
    "search-imessages",
    "search-imessage-contacts",
    "add-imessage-contact",
    "send-imessage",
)


def run_setup(codex_home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    return subprocess.run(
        [str(SETUP_SCRIPT), *args],
        check=False,
        capture_output=True,
        env=env,
        text=True,
    )


def test_setup_backs_up_existing_skills_and_is_idempotent(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    target_root = codex_home / "skills"
    target_root.mkdir(parents=True)

    for skill in SKILL_NAMES:
        existing = target_root / skill
        existing.mkdir()
        (existing / "marker.txt").write_text(skill, encoding="utf-8")

    first = run_setup(codex_home)
    assert first.returncode == 0, first.stderr

    for skill in SKILL_NAMES:
        target = target_root / skill
        assert target.is_symlink()
        assert target.resolve() == REPO_ROOT / "skills" / skill
        discovered_repo = subprocess.run(
            ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        assert Path(discovered_repo.stdout.strip()) == REPO_ROOT

    backup_dirs = list((codex_home / "skill-backups").iterdir())
    assert len(backup_dirs) == 1
    for skill in SKILL_NAMES:
        marker = backup_dirs[0] / skill / "marker.txt"
        assert marker.read_text(encoding="utf-8") == skill

    second = run_setup(codex_home)
    assert second.returncode == 0, second.stderr
    assert list((codex_home / "skill-backups").iterdir()) == backup_dirs

    checked = run_setup(codex_home, "--check")
    assert checked.returncode == 0, checked.stderr


def test_check_reports_missing_links_without_writing(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"

    checked = run_setup(codex_home, "--check")

    assert checked.returncode == 1
    assert not codex_home.exists()
    for skill in SKILL_NAMES:
        assert skill in checked.stderr
