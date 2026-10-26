"""Public lifecycle metadata must not overstate the legacy apply flags."""
import subprocess
import pytest

from hermes_curator_evolver import auto_evolve
from hermes_curator_evolver.__main__ import build_parser


def test_scheduler_metadata_does_not_claim_native_write_permission(tmp_path, monkeypatch):
    monkeypatch.setattr(auto_evolve, "_scheduler_backend", lambda: "systemd")
    monkeypatch.setattr(auto_evolve, "_systemd_dir", lambda: tmp_path / "units")
    def forbidden(*args, **kwargs):
        raise AssertionError("no live scheduler activation")
    monkeypatch.setattr(subprocess, "run", forbidden)
    result = auto_evolve.install_auto_timer(skills_dir=tmp_path / "skills", apply_low_risk=True, enable=False)
    assert result["auto_apply_policy"] == "proposal-only-native-single-writer-handoff-required"
    assert result.get("auto_apply_available") is False
    assert result["enabled"] is False
    assert (tmp_path / "units" / "hermes-curator-evolver-auto.service").is_file()


def test_autorun_help_discloses_proposal_only_native_handoff(capsys):
    with pytest.raises(SystemExit) as exc:
        build_parser().parse_args(["auto-run", "--help"])
    assert exc.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    assert "proposal-only" in text and "single-writer handoff" in text
