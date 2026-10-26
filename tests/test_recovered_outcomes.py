"""Recovered P02 outcome gaps; effectful scheduler calls are synthetic fakes."""
import json
import pytest
from hermes_curator_evolver.__main__ import build_parser
from hermes_curator_evolver import cli


@pytest.mark.parametrize("argv,result,expected", [
    (["install-auto", "--enable"], {"enabled": False, "systemctl": {"exit_code": 1}}, 1),
    (["install-auto", "--enable"], {"enabled": True, "systemctl": {"exit_code": 0}}, 0),
    (["install-auto"], {"enabled": False}, 0),
    (["uninstall-auto"], {"removed": [], "missing": ["synthetic.timer"]}, 0),
    (["uninstall-auto"], {"removed": [], "systemctl": {"exit_code": 1}}, 1),
    (["uninstall-auto", "--keep-enabled"], {"removed": [], "missing": []}, 0),
])
def test_scheduler_cli_status_matches_requested_outcome(monkeypatch, capsys, argv, result, expected):
    monkeypatch.setattr(cli, "install_auto_timer", lambda **kwargs: result)
    monkeypatch.setattr(cli, "uninstall_auto_timer", lambda **kwargs: result)
    assert cli.handle_cli(build_parser().parse_args(argv)) == expected
    assert json.loads(capsys.readouterr().out) == result


def test_bootstrap_activation_failure_is_nonzero_and_not_a_success_message(monkeypatch, capsys):
    result = {"backfill": {}, "auto_timer": {"enabled": False, "systemctl": {"exit_code": 1}},
              "next_steps": []}
    monkeypatch.setattr(cli, "_run_bootstrap", lambda values: result)
    assert cli.handle_cli(build_parser().parse_args(["bootstrap"])) == 1
    output = capsys.readouterr().out
    assert "activation failed" in output.lower()
    assert "✓ Scheduler installed" not in output
