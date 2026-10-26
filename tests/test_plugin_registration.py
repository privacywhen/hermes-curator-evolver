import importlib.util
import shutil
import sys
from pathlib import Path

import hermes_curator_evolver


class FakeCtx:
    def __init__(self):
        self.tools = []
        self.hooks = []
        self.cli_commands = []
        self.commands = []
        self.skills = []

    def register_tool(self, name, toolset, schema, handler, **kwargs):
        self.tools.append((name, toolset, schema, handler, kwargs))

    def register_hook(self, name, callback):
        self.hooks.append((name, callback))

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, description=""):
        self.cli_commands.append((name, help, setup_fn, handler_fn, description))

    def register_command(self, name, handler, description="", args_hint=""):
        self.commands.append((name, handler, description, args_hint))

    def register_skill(self, name, path):
        self.skills.append((name, Path(path)))


def test_register_wires_tool_hooks_cli_slash_and_skill():
    ctx = FakeCtx()

    hermes_curator_evolver.register(ctx)

    assert [t[0] for t in ctx.tools] == ["curator_evidence_report"]
    assert {h[0] for h in ctx.hooks} == {"post_tool_call", "post_llm_call", "on_session_end"}
    assert [c[0] for c in ctx.cli_commands] == ["curator-evolver"]
    assert [c[0] for c in ctx.commands] == ["curator-evolver"]
    assert ctx.skills[0][0] == "curator-evolution"
    assert ctx.skills[0][1].name == "SKILL.md"


def test_tool_handler_returns_json_string(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(tmp_path / "evidence.sqlite"))
    from hermes_curator_evolver.storage import EvidenceStore
    EvidenceStore()  # Inspection requires an existing store; it must not create one.
    ctx = FakeCtx()
    hermes_curator_evolver.register(ctx)
    handler = ctx.tools[0][3]

    result = handler({"days": 1, "format": "json"})

    assert isinstance(result, str)
    assert "summary" in result


def test_native_cli_binds_no_tool_host_completion_and_discloses_before_call(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    from types import SimpleNamespace
    from hermes_curator_evolver.storage import EvidenceStore

    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(tmp_path / "synthetic.sqlite"))
    EvidenceStore()
    calls = []
    def forbidden(*args, **kwargs):
        raise AssertionError("No subprocess or tool dispatcher may run")
    monkeypatch.setattr("subprocess.run", forbidden)
    ctx = FakeCtx()
    ctx.dispatch_tool = forbidden
    def complete(**kwargs):
        assert "host" in capsys.readouterr().err.lower()  # Before any transmission.
        calls.append(kwargs)
        return SimpleNamespace(text='{"tool_calls": [{"name": "terminal"}]}',
                               provider="synthetic-provider", model="synthetic-model")
    ctx.llm = SimpleNamespace(complete=complete)
    hermes_curator_evolver.register(ctx)
    parser = argparse.ArgumentParser()
    ctx.cli_commands[0][2](parser)
    args = parser.parse_args(["propose", "--skill", "synthetic", "--draft-with-model", "--format", "json"])
    assert args.func(args) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert set(calls[0]) == {"messages", "max_tokens", "timeout", "temperature", "purpose"}
    assert result["model_draft"]["actual_route"] == {"provider": "synthetic-provider", "model": "synthetic-model"}
    assert result["model_draft"]["semantic_verified"] is False
    assert result["mutation_allowed"] is False


def test_native_default_does_not_even_resolve_llm_without_consent(tmp_path, monkeypatch, capsys):
    import argparse
    import json
    from hermes_curator_evolver.storage import EvidenceStore
    monkeypatch.setenv("HERMES_CURATOR_EVOLVER_DB", str(tmp_path / "synthetic.sqlite"))
    EvidenceStore()
    class Context(FakeCtx):
        @property
        def llm(self):
            raise AssertionError("No consent to resolve or call a model")
    ctx = Context()
    hermes_curator_evolver.register(ctx)
    parser = argparse.ArgumentParser()
    ctx.cli_commands[0][2](parser)
    args = parser.parse_args(["propose", "--skill", "synthetic", "--format", "json"])
    assert args.func(args) == 0
    assert "model_draft" not in json.loads(capsys.readouterr().out)


def test_directory_plugin_shim_uses_relative_package_import(tmp_path, monkeypatch):
    repo_root = Path(__file__).resolve().parents[1]
    plugin_dir = tmp_path / "curator-evolver"
    shutil.copytree(repo_root / "hermes_curator_evolver", plugin_dir / "hermes_curator_evolver")
    shutil.copy2(repo_root / "__init__.py", plugin_dir / "__init__.py")

    removed_paths = []
    for entry in list(sys.path):
        try:
            if Path(entry or ".").resolve() == repo_root:
                removed_paths.append(entry)
                sys.path.remove(entry)
        except OSError:
            pass
    monkeypatch.setattr(sys, "path", sys.path)
    monkeypatch.delitem(sys.modules, "hermes_curator_evolver", raising=False)
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_plugins.curator_evolver",
            plugin_dir / "__init__.py",
            submodule_search_locations=[str(plugin_dir)],
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        module.__package__ = "hermes_plugins.curator_evolver"
        module.__path__ = [str(plugin_dir)]
        sys.modules["hermes_plugins.curator_evolver"] = module
        spec.loader.exec_module(module)
    finally:
        for entry in reversed(removed_paths):
            sys.path.insert(0, entry)

    assert callable(module.register)
