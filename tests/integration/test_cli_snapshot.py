"""阶段 A 的回归护栏：CLI 输出必须与重构前逐字节一致。

基线由 `scripts/cli_snapshot.py --write` 在改造前采集，覆盖 43 条离线可用的
CLI 调用（含退出码、stdout、stderr、抛出的异常类型）。任何行为漂移都会在这里失败，
并逐条打印差异。

若某次改动**有意**改变输出，重新采集基线即可，但必须在提交信息里说明原因：

    .venv/bin/python scripts/cli_snapshot.py --write tests/fixtures/cli_snapshot_baseline.json
"""

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
BASELINE = Path(__file__).resolve().parent.parent / "fixtures" / "cli_snapshot_baseline.json"


def test_cli_output_matches_baseline() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/cli_snapshot.py", "--compare", str(BASELINE)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert "对比通过" in result.stdout
