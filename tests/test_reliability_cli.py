import json
import os
import subprocess
import sys


def test_reliability_cli_quick_ci_exit_code_hold(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TRADING_RELIABILITY_TEST_MODE": "true",
            "TRADING_SEND_ORDER_ALLOWED": "false",
            "TRADING_ORDER_MANAGER_OBSERVE_ONLY": "true",
            "TRADING_ORDER_MANAGER_ENQUEUE_GATEWAY_COMMAND": "false",
            "TRADING_ORDER_INTENT_ENABLED": "false",
            "TRADING_ALLOW_LIVE_SIM_ORDERS": "false",
            "TRADING_BROKER_ENV": "SIMULATION",
            "TRADING_ACCOUNT_MODE": "SIMULATION",
        }
    )
    completed = subprocess.run(
        [
            sys.executable,
            "tools/runtime_reliability_qualification.py",
            "--profile",
            "quick-ci",
            "--duration-sec",
            "1",
            "--code-count",
            "2",
            "--output-dir",
            str(tmp_path),
            "--json",
        ],
        check=False,
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert completed.returncode == 2
    summary = json.loads(completed.stdout)
    report_path = tmp_path / summary["run_id"] / "qualification.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["status"] == "HOLD"
    assert report["metrics"]["counters"]["order_command_count"] == 0
