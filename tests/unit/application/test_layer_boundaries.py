import ast
from pathlib import Path

FORBIDDEN_IMPORTS = {
    "dishka",
    "fastapi",
    "httpx",
    "planning.entrypoint",
    "planning.infrastructure",
    "planning.presentation",
    "sqlalchemy",
    "starlette",
}


def test_domain_and_application_do_not_depend_on_outer_layers() -> None:
    source_root = Path(__file__).parents[3] / "src" / "planning"
    violations: list[str] = []

    for layer in ("domain", "application"):
        for path in (source_root / layer).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names: list[str] = []
                if isinstance(node, ast.Import):
                    names.extend(alias.name for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module)
                for name in names:
                    if any(
                        name == forbidden or name.startswith(f"{forbidden}.")
                        for forbidden in FORBIDDEN_IMPORTS
                    ):
                        violations.append(f"{path}:{node.lineno}: {name}")

    assert violations == []


def test_presentation_does_not_access_persistence_directly() -> None:
    presentation = (
        Path(__file__).parents[3]
        / "src"
        / "planning"
        / "presentation"
        / "http"
    )
    forbidden_imports = {
        "planning.infrastructure",
        "sqlalchemy",
    }
    forbidden_names = {"AsyncSession"}
    violations: list[str] = []

    for path in presentation.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                names = []
            for name in names:
                if any(
                    name == forbidden or name.startswith(f"{forbidden}.")
                    for forbidden in forbidden_imports
                ):
                    violations.append(f"{path}:{node.lineno}: {name}")
            if isinstance(node, ast.Name) and node.id in forbidden_names:
                violations.append(f"{path}:{node.lineno}: {node.id}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"execute", "scalar", "scalars", "commit"}
            ):
                violations.append(f"{path}:{node.lineno}: {node.func.attr}()")

    assert violations == []


def test_legacy_management_routers_are_removed() -> None:
    http = (
        Path(__file__).parents[3]
        / "src"
        / "planning"
        / "presentation"
        / "http"
    )
    legacy_files = {
        http / "admin_router.py",
        http / "engineer_router.py",
        http / "project_router.py",
    }

    assert not any(path.exists() for path in legacy_files)
