"""The public name on every surface, the package's declarations and the announcement.

The module's name is `release.MODULE`; the harness and the org are the core's
(`moxi.names`). `pyproject.toml` cannot import either, so these tests hold it to them.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

from moxi import names

from moxi_comms import module, release

REPO = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text())
SOURCE = REPO / "src" / "moxi_comms"

#: The internal entry points (design §4.5): one name per entry point, moxi-comms-<part>.
SCRIPTS = {
    "moxi": "moxi.cli:main",
    "moxi-comms-daemon": "moxi_comms.daemon:main",
    "moxi-comms-shim": "moxi_comms.adapters.claude_shim:main",
    "moxi-comms-claude": "moxi_comms.adapters.session:main",
    "moxi-comms-codex": "moxi_comms.adapters.codex:main",
    "moxi-comms-opencode": "moxi_comms.adapters.opencode:main",
    "moxi-comms-provision": "moxi_comms.provision:main",
}


# ------------------------------------------------------------------ one spelling

def test_every_name_is_built_from_moxi_names():
    assert release.MODULE == "comms"
    assert release.DISTRIBUTION == names.distribution("comms") == "moxi-comms"
    assert release.DISPLAY_NAME == "Moxi comms"
    assert release.UNIT == "moxi-comms-daemon.service"
    assert release.REPO_URL == f"https://github.com/{names.repo('comms')}" == \
        "https://github.com/moxihq/comms"


def _code_strings(path: Path) -> list[str]:
    """Every string constant in a module's code, docstrings left out."""
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and \
                    isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [node.value for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and id(node) not in docstrings]


def test_no_module_spells_the_org_or_the_repository():
    """The org and the repository URL come from `moxi.names` through `release`, which
    alone builds the URL."""
    spelled = sorted(str(path.relative_to(SOURCE)) for path in SOURCE.rglob("*.py")
                     if path != SOURCE / "release.py" and any(names.ORG in text or "github.com" in text
                            for text in _code_strings(path)))
    assert spelled == []


def test_pyproject_spells_it_the_same():
    project = PYPROJECT["project"]
    assert project["name"] == release.DISTRIBUTION
    assert project["entry-points"]["moxi.modules"] == {"comms": "moxi_comms.module"}
    for url in project["urls"].values():
        assert url.startswith(release.REPO_URL)


def test_the_public_identity_is_one_everywhere():
    """MIT, CodeManEthan and codemanethan@gmail.com."""
    project = PYPROJECT["project"]
    assert project["license"] == "MIT"
    assert project["authors"] == [{"name": "CodeManEthan",
                                   "email": "codemanethan@gmail.com"}]


# ------------------------------------------------------------------ the package

def test_the_package_requires_the_core_and_turns_and_codex_is_an_extra():
    """turns is required: it is the one way into a harness session."""
    project = PYPROJECT["project"]
    assert project["dependencies"] == ["moxi>=0.1,<0.2", "moxi-turns>=0.1,<0.2"]
    assert [d.split(">")[0] for d in project["optional-dependencies"]["codex"]] == \
        ["moxi-turns[codex]"]
    dev = PYPROJECT["dependency-groups"]["dev"]
    assert "pytest" in dev and any(d.startswith("websockets") for d in dev)
    assert "sources" not in PYPROJECT.get("tool", {}).get("uv", {}), \
        "members declare no sources: a clone of one member resolves its siblings"


def test_the_scripts_are_the_umbrella_and_the_internal_entry_points():
    """There is no bare `comms` script ([C3]): the module runs as `moxi comms`."""
    scripts = PYPROJECT["project"]["scripts"]
    assert scripts == SCRIPTS
    assert "comms" not in scripts


def test_comms_registers_its_listener_with_turns():
    """comms' context reaches a session through turns' listener group, never a hook."""
    group = PYPROJECT["project"]["entry-points"]["moxi.turns.listeners"]
    assert group == {"comms": "moxi_comms.listeners"}


def test_the_config_schema_ships_in_the_package():
    assert PYPROJECT["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == \
        ["src/moxi_comms"]
    assert (SOURCE / module.SETTINGS).is_file()


# ------------------------------------------------------------------ the announcement

def test_the_announcement_offers_codex_and_gates_its_script():
    assert module.SETTINGS == "settings.toml"
    assert set(module.OPTIONAL) == set(PYPROJECT["project"]["optional-dependencies"])
    assert module.GATED == {"moxi-comms-codex": "codex"}
    assert module.API == "moxi_comms.api" and module.INTERFACES == frozenset({1})


def test_internal_env_is_the_launcher_wiring_the_secrets_step_and_the_test_seam():
    """Every other variable comms reads is a config key's `env` or a root locator
    (design §4.7, §7.4)."""
    assert set(module.INTERNAL_ENV) == {
        "MOXI_COMMS_NAME", "MOXI_COMMS_SLOT", "MOXI_COMMS_SCOPE", "MOXI_COMMS_GLOBAL",
        "MOXI_COMMS_DIRECTORS", "MOXI_COMMS_SOCKET", "MOXI_COMMS_CLAIM_SOCKET",
        "MOXI_COMMS_PROJECT", "MOXI_COMMS_OPERATOR", "MOXI_COMMS_SECRET_",
        "MOXI_COMMS_MOCK_GATEWAY",
    }
    assert all(why.strip() for why in module.INTERNAL_ENV.values())
