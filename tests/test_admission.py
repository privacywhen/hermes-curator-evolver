"""Package/admission metadata contracts that do not require live Hermes state."""

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_distribution_manifest_and_runtime_version_agree():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["version"] == "0.10.0"
    assert '0.10.0' in (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    assert "__version__ = \"0.10.0\"" in (ROOT / "hermes_curator_evolver" / "__init__.py").read_text(encoding="utf-8")


def test_semantic_extra_is_limited_to_supported_desktop_platforms():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = metadata["project"]["optional-dependencies"]["semantic"]

    assert len(requirements) == 1
    assert requirements[0] == (
        "sentence-transformers>=3; sys_platform == 'win32' or "
        "sys_platform == 'linux' or sys_platform == 'darwin'"
    )


def test_install_docs_frame_bootstrap_as_consequential_and_preserve_data():
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()
    after_install = (ROOT / "docs" / "after-install.md").read_text(encoding="utf-8").lower()

    assert "bootstrap" in readme and "consequential" in readme
    assert "preserve" in after_install and "evidence" in after_install
    assert "rm -rf ~/.hermes/plugin-data/curator-evolver" not in readme
    assert "rm -rf ~/.hermes/plugin-data/curator-evolver" not in after_install


def test_metadata_keeps_upstream_credit_and_fork_support_urls():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert metadata["project"]["authors"][0]["name"] == "pingchesu"
    assert metadata["project"]["urls"]["Homepage"] == "https://github.com/privacywhen/hermes-curator-evolver"
    assert metadata["project"]["urls"]["Issues"] == "https://github.com/privacywhen/hermes-curator-evolver/issues"
    assert "SkillClaw" in (ROOT / "README.md").read_text(encoding="utf-8")
