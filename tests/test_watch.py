"""Watch daemon: pure decision logic, the single-instance lock, and a live
single pass on a real (upstream-less) git repo."""
from __future__ import annotations

import fcntl
import subprocess
from pathlib import Path

from groundgraph.watch import plan_repo, run_pass, watch


def _fresh(repo="r", behind=None, dirty=0) -> dict:
    return {"repo": repo, "branch": "main", "behind": behind,
            "ahead": 0, "dirty": dirty, "head": "abc1234"}


def test_plan_repo_decisions() -> None:
    assert plan_repo(_fresh(behind=None)).action == "skip-unknown"
    assert plan_repo(_fresh(behind=0)).action == "skip-fresh"
    # the sacred rule: behind but DIRTY -> never pulled
    assert plan_repo(_fresh(behind=5, dirty=2)).action == "skip-dirty"
    assert plan_repo(_fresh(behind=5, dirty=0)).action == "pull"
    # not-a-repo: dirty is -1 and behind None -> unknown, untouched
    assert plan_repo(_fresh(behind=None, dirty=-1)).action == "skip-unknown"


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "src"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "core.py").write_text("def parse(t):\n    return t\n")
    for cmd in (["init", "-q"], ["add", "-A"],
                ["-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(repo), *cmd], check=True,
                       capture_output=True)
    return repo


def test_run_pass_no_op_when_nothing_behind(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)          # no upstream -> behind unknown
    db = str(tmp_path / "g.db")
    s = run_pass(db, [str(repo)], docs=None, fetch=False)
    assert s["rebuilt"] is False and s["pulled"] == []
    assert s["plans"][0]["action"] == "skip-unknown"
    assert not Path(db).exists()        # no-op wrote nothing


def test_run_pass_force_rebuilds(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    db = str(tmp_path / "g.db")
    s = run_pass(db, [str(repo)], docs=None, force=True, fetch=False)
    assert s["rebuilt"] is True
    assert s["written"]["code"] > 0
    assert Path(db).exists()


def test_watch_lock_makes_second_instance_skip(tmp_path: Path) -> None:
    repo = _git_repo(tmp_path)
    db = str(tmp_path / "g.db")
    lock = tmp_path / "g.db.lock"
    # hold the lock as "another pass"
    fh = lock.open("w")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        rc = watch(db, [str(repo)], force=True, lock_path=str(lock))
        assert rc == 0                      # clean skip, not an error
        assert not Path(db).exists()        # and it did NOT rebuild
    finally:
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    # lock released -> a pass proceeds
    rc = watch(db, [str(repo)], force=True, lock_path=str(lock))
    assert rc == 0 and Path(db).exists()
