"""P04 local metadata is not authorship or write authorization."""
from pathlib import Path
from types import SimpleNamespace
import json
import pytest
from hermes_curator_evolver.skill_sources import build_skill_source_context, classify_skill_source


def test_autorun_resolves_unambiguous_directory_alias_without_inventing_authorship(tmp_path):
    from hermes_curator_evolver.auto_evolve import AutoEvolveConfig, run_auto_evolve
    from hermes_curator_evolver.storage import EvidenceStore
    root = tmp_path / "skills"
    path = root / "directory" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: declared\n---\n# Body", encoding="utf-8")
    store = EvidenceStore(tmp_path / "aliases.sqlite")
    store.record_tool_call(tool_name="skill_view", args={"skill": "directory"}, result="synthetic")
    result = run_auto_evolve(AutoEvolveConfig(db_path=store.db_path, skills_dir=root, min_evidence=1))
    candidate = result["candidates"][0]
    assert candidate["skill_name"] == "declared" and candidate["target_path"] == str(path)
    assert candidate["status"] == "planned" and candidate["source_writable"] is False
    assert result["selection"]["evidence_aggregate_limit"] == 20
    assert result["selection"]["complete_inventory_ranking"] is False



def test_local_placement_does_not_infer_authorship_or_permission(tmp_path):
    root = tmp_path / "skills"
    skill = root / "synthetic" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# User-authored synthetic skill", encoding="utf-8")
    info = classify_skill_source(skill_name="synthetic", skill_file=skill, context=build_skill_source_context(root))
    assert info.writable is False
    assert info.source != "local-agent-created"


def test_auto_discovery_preserves_distinct_packages_with_same_declared_name(tmp_path):
    from hermes_curator_evolver.auto_evolve import discover_skill_files
    files = []
    for category in ("one", "two"):
        path = tmp_path / category / "package" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: shared\n---\n# {category}", encoding="utf-8")
        files.append(path)
    discovered = discover_skill_files(tmp_path)
    assert set(discovered.values()) == set(files)
    assert len(discovered) == 2


def test_catalog_delegates_identity_and_preserves_hidden_disabled_rows(tmp_path):
    from hermes_curator_evolver.skill_catalog import read_skill_catalog, review_rows
    path = tmp_path / "directory" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: declared\nplatforms: [unsupported]\n---\nBody", encoding="utf-8")
    calls = []
    def resolve(entries):
        calls.append(entries)
        return [dict(e, status="unique", load_name="declared", relative_path="directory") for e in entries]
    api = SimpleNamespace(resolve_skill_catalog=resolve,
        iter_skill_index_files=lambda root, filename: [path],
        is_disabled_entry=lambda e, disabled: e["name"] in disabled,
        skill_matches_platform=lambda meta: False)
    result = read_skill_catalog(tmp_path, native_api=api, disabled_names={"declared"})
    assert result["native_status"] == "supported"
    assert len(calls) == 1 and calls[0][0]["name"] == "declared"
    entry = result["entries"][0]
    assert entry["load_name"] == "declared" and entry["status"] == "unique"
    assert entry["visible"] is False and entry["disabled"] is True
    assert entry["permissions"]["auto_apply"] is False
    assert review_rows(result) == []


def test_catalog_passes_explicit_tiers_and_never_scans_project_or_plugins(tmp_path):
    from hermes_curator_evolver.skill_catalog import read_skill_catalog, review_rows
    local, external, project = (tmp_path / p for p in ("local", "external", "project"))
    files = []
    for root in (local, external, project):
        path = root / "same" / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: shared\n---\nBody", encoding="utf-8")
        files.append(path)
    seen = []
    def index(root, filename):
        assert root != project
        seen.append(root)
        return [root / "same" / filename]
    def resolve(entries):
        assert [e["tier"] for e in entries] == [1, 3]
        return [dict(e, status="unique" if e["tier"] == 1 else "shadowed",
                     load_name="shared" if e["tier"] == 1 else None) for e in entries]
    def forbidden(*args, **kwargs):
        raise AssertionError("discovery/project scan is forbidden")
    api = SimpleNamespace(iter_skill_index_files=index, resolve_skill_catalog=resolve,
        is_disabled_entry=lambda e, disabled: False, skill_matches_platform=lambda meta: True,
        iter_project_skill_files=forbidden, get_skill_search_roots=forbidden,
        discover_plugins=forbidden, skills_list=forbidden)
    result = read_skill_catalog(local, native_api=api, roots=[(1, local), (3, external), (0, project)])
    assert result["native_status"] == "supported"
    assert seen == [local, external]
    assert len(result["entries"]) == 2 and len(review_rows(result)) == 1
    assert "trusted-project" in result["unavailable_scopes"]
    assert "plugins" in result["unavailable_scopes"]


@pytest.mark.parametrize("record,managed,pinned", [
    ({"created_by": "agent", "pinned": True}, True, True),
    ({"agent_created": True, "pinned": False}, True, False),
    ({"created_by": "learn"}, False, None),
    ({"uses": 42}, False, None),
])
def test_native_management_marker_is_not_authorship_or_write_permission(tmp_path, record, managed, pinned):
    from hermes_curator_evolver.skill_catalog import read_skill_catalog
    path = tmp_path / "demo" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: demo\n---\nBody", encoding="utf-8")
    (tmp_path / ".usage.json").write_text(json.dumps({"demo": record}), encoding="utf-8")
    api = SimpleNamespace(iter_skill_index_files=lambda *args: [path],
        resolve_skill_catalog=lambda entries: [dict(e, status="unique", load_name="demo") for e in entries],
        is_disabled_entry=lambda *args: False)
    entry = read_skill_catalog(tmp_path, native_api=api)["entries"][0]
    assert entry.get("native_managed") is managed
    assert entry.get("native_pinned") is pinned
    assert entry.get("management_status") == "valid"
    assert entry.get("authorship") is None
    assert entry["permissions"] == {"propose": True, "auto_apply": False}


def test_native_catalog_output_cannot_grant_plugin_write_permission(tmp_path):
    from hermes_curator_evolver.skill_catalog import read_skill_catalog
    path = tmp_path / "SKILL.md"
    path.write_text("# Body", encoding="utf-8")
    api = SimpleNamespace(iter_skill_index_files=lambda *args: [path],
        resolve_skill_catalog=lambda entries: [dict(e, status="unique", load_name="demo",
            permissions={"propose": True, "auto_apply": True}) for e in entries],
        is_disabled_entry=lambda *args: False)
    assert read_skill_catalog(tmp_path, native_api=api)["entries"][0]["permissions"]["auto_apply"] is False


def test_structural_audit_labels_physical_scope_and_shared_identity(tmp_path):
    from hermes_curator_evolver.skill_audit import audit_skill_library
    path = tmp_path / "directory" / "SKILL.md"
    path.parent.mkdir()
    path.write_text("---\nname: declared\n---\n# Body", encoding="utf-8")
    report = audit_skill_library(tmp_path)
    assert report.get("scope") == "physical-structural-audit"
    assert report.get("canonical_inventory") is False
    identity = report["skills"][0].get("catalog", {})
    assert identity.get("name") == "declared"
    assert identity.get("status") == "unknown"
    assert "text" not in identity


def test_apply_flags_only_produce_proposal_until_native_single_writer_handoff(tmp_path, monkeypatch):
    from hermes_curator_evolver import auto_evolve
    from hermes_curator_evolver.storage import EvidenceStore
    root = tmp_path / "skills"
    path = root / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: demo\ndescription: synthetic\n---\n# Demo", encoding="utf-8")
    before = path.read_bytes()
    store = EvidenceStore(tmp_path / "evidence.db")
    store.record_tool_call(session_id="s", task_id="t", tool_name="skill_view", args={"skill": "demo"}, result="synthetic")
    def forbidden(**kwargs):
        raise AssertionError("no native writer handoff exists")
    monkeypatch.setattr(auto_evolve, "apply_guarded_patch", forbidden)
    result = auto_evolve.run_auto_evolve(auto_evolve.AutoEvolveConfig(db_path=store.db_path,
        skills_dir=root, backup_dir=tmp_path / "backups", min_evidence=1,
        apply_low_risk=True, approve_auto_apply=True, auto_apply_allowlist=("demo",)))
    candidate = result["candidates"][0]
    assert candidate["status"] == "proposal-only"
    assert candidate["apply_result"]["reason"] == "native-single-writer-handoff-required"
    assert candidate["new_content_changed"] is True
    assert path.read_bytes() == before and not (tmp_path / "backups").exists()


def test_catalog_keeps_native_update_ownership_separate_from_management(tmp_path):
    from hermes_curator_evolver.skill_catalog import read_skill_catalog
    root, external = tmp_path / "skills", tmp_path / "external"
    for folder, name in ((root, "bundled"), (root, "hub"), (external, "shared")):
        path = folder / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\n---\nBody", encoding="utf-8")
    (root / ".bundled_manifest").write_text("bundled:fixturehash\n", encoding="utf-8")
    (root / ".hub").mkdir()
    (root / ".hub" / "lock.json").write_text(json.dumps({"installed": {"hub": {}}}), encoding="utf-8")
    (root / ".usage.json").write_text(json.dumps({n: {"created_by": "agent"} for n in ("bundled", "hub", "shared")}), encoding="utf-8")
    api = SimpleNamespace(iter_skill_index_files=lambda folder, filename: sorted(folder.rglob(filename)),
        resolve_skill_catalog=lambda entries: [dict(e, status="unique", load_name=e["name"]) for e in entries],
        is_disabled_entry=lambda *args: False)
    result = read_skill_catalog(root, native_api=api, roots=[(1, root), (3, external)])
    assert {e.get("source") for e in result["entries"]} == {"bundled", "hub-installed", "external-dir"}
    assert all(e["native_managed"] is True and not e["permissions"]["auto_apply"] for e in result["entries"])


def test_fallback_alias_cannot_overwrite_another_declared_name(tmp_path):
    from hermes_curator_evolver.auto_evolve import discover_skill_files
    files = []
    for directory, name in (("cat/one", "shared"), ("other/one", "shared"), ("special", "cat/one")):
        path = tmp_path / directory / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(f"---\nname: {name}\n---\nBody", encoding="utf-8")
        files.append(path)
    assert set(discover_skill_files(tmp_path).values()) == set(files)


def test_corrupt_protection_metadata_is_unknown_and_never_mutates_tree(tmp_path):
    from hermes_curator_evolver.skill_catalog import read_skill_catalog
    root = tmp_path / "skills"
    path = root / "demo" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\nname: demo\n---\nBody", encoding="utf-8")
    (root / ".hub").mkdir()
    for bad in (root / ".bundled_manifest", root / ".hub" / "lock.json", root / ".usage.json", tmp_path / "config.yaml"):
        bad.write_bytes(b"\xff")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    entry = read_skill_catalog(root)["entries"][0]
    assert entry["management_status"] == "invalid" and entry["native_managed"] is None
    assert entry["source"] == "unknown" and entry["permissions"]["auto_apply"] is False
    assert before == {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}


def test_semantic_reader_uses_same_review_identity_as_autorun(tmp_path):
    from hermes_curator_evolver.semantic import find_skill_candidates
    from hermes_curator_evolver.auto_evolve import discover_skill_files
    for directory in ("one/package", "two/package"):
        path = tmp_path / directory / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text("---\nname: shared\n---\nBody", encoding="utf-8")
    result = find_skill_candidates(query="Body", skills_dir=tmp_path)
    assert {e["skill_name"] for e in result["candidates"]} == set(discover_skill_files(tmp_path))
    assert all(e["permissions"]["auto_apply"] is False for e in result["candidates"])
