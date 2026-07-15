"""Project structure mapper — builds a textual overview for the system prompt.

Parses the project directory tree (respecting ``.gitignore`` rules), detects
key configuration files, and extracts the Python import graph to produce a
compact structural summary that helps the LLM understand the codebase layout.
"""

from __future__ import annotations

import ast
import fnmatch
import logging
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Recognised config / project-definition files
# ---------------------------------------------------------------------------

_KNOWN_CONFIG_FILES: set[str] = {
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "requirements.txt",
    "requirements-dev.txt",
    "Makefile",
    "Dockerfile",
    "docker-compose.yml",
    "docker-compose.yaml",
    ".env",
    ".env.example",
    ".editorconfig",
    ".pre-commit-config.yaml",
    "tox.ini",
    "noxfile.py",
    "CLAUDE.md",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
}

# Files and directories that are always excluded from the tree.
_ALWAYS_EXCLUDE: set[str] = {
    ".git",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "venv",
    ".eggs",
    "*.egg-info",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "node_modules",
    ".idea",
    ".vscode",
    "*.swp",
    "*.swo",
    "*~",
    "build",
    "dist",
    ".coverage",
    "htmlcov",
}

# Maximum depth for the rendered directory tree.
_MAX_TREE_DEPTH = 5

# Maximum number of files to list per directory before collapsing.
_MAX_FILES_PER_DIR = 30

# Maximum number of import graph entries to include.
_MAX_IMPORT_ENTRIES = 40


# ======================================================================
# Gitignore parser
# ======================================================================


class GitignoreMatcher:
    """Matches file paths against ``.gitignore`` rules.

    Parses one or more ``.gitignore`` files and provides an
    :meth:`is_ignored` method that respects the usual gitignore semantics
    (directory anchoring, negation, ``**`` globs).

    Parameters
    ----------
    patterns:
        Raw gitignore lines (one pattern per item, ``#``-comments and
        blank lines are skipped).
    root_dir:
        The repository root — used to resolve anchored patterns (those
        starting with ``/``).
    """

    def __init__(
        self, patterns: list[str], root_dir: Path
    ) -> None:
        self._root = root_dir.resolve()
        # Each rule is (pattern, is_negation, dirs_only, anchored)
        self._rules: list[tuple[str, bool, bool, bool]] = []
        for line in patterns:
            stripped = line.rstrip("\r\n")
            # Strip trailing spaces (but not escaped spaces in patterns).
            stripped = stripped.rstrip()
            if not stripped or stripped.startswith("#"):
                continue

            is_negation = stripped.startswith("!")
            if is_negation:
                stripped = stripped[1:]

            # Trailing slash means "directories only".
            dirs_only = stripped.endswith("/")
            if dirs_only:
                stripped = stripped.rstrip("/")

            # Leading slash means anchored to root.
            anchored = stripped.startswith("/")
            if anchored:
                stripped = stripped[1:]

            if stripped:
                self._rules.append(
                    (stripped, is_negation, dirs_only, anchored)
                )

    def is_ignored(self, path: Path, *, is_dir: bool = False) -> bool:
        """Check whether *path* should be ignored.

        Parameters
        ----------
        path:
            Absolute or relative path to test.
        is_dir:
            ``True`` when *path* represents a directory (relevant for
            trailing-slash directory-only patterns).
        """
        try:
            rel = path.resolve().relative_to(self._root)
        except ValueError:
            # Path is outside the root — don't ignore (shouldn't happen).
            return False

        rel_str = rel.as_posix()
        ignored = False

        for pattern, is_negation, dirs_only, anchored in self._rules:
            if dirs_only and not is_dir:
                continue

            target = rel_str if not anchored else rel_str
            if self._match(pattern, target, is_dir=is_dir):
                ignored = not is_negation

        return ignored

    # ------------------------------------------------------------------
    # Internal pattern matching
    # ------------------------------------------------------------------

    @staticmethod
    def _match(pattern: str, path_str: str, *, is_dir: bool) -> bool:
        """Return ``True`` when *pattern* matches *path_str*.

        Supports gitignore-style ``**`` (cross-directory glob) in addition
        to standard :func:`fnmatch` wildcards.
        """
        # If pattern contains **, split and match segments.
        if "**" in pattern:
            return GitignoreMatcher._match_globstar(
                pattern, path_str, is_dir=is_dir
            )

        # A pattern without a slash matches the basename at any depth.
        if "/" not in pattern:
            basename = path_str.rsplit("/", 1)[-1] if "/" in path_str else path_str
            if is_dir:
                return fnmatch.fnmatch(basename, pattern) or fnmatch.fnmatch(
                    path_str, pattern
                )
            return fnmatch.fnmatch(basename, pattern)

        # Pattern with slash — match against the full relative path.
        if is_dir:
            return fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(
                path_str + "/", pattern
            )
        return fnmatch.fnmatch(path_str, pattern)

    @staticmethod
    def _match_globstar(
        pattern: str, path_str: str, *, is_dir: bool
    ) -> bool:
        """Handle ``**`` in patterns by matching against every suffix."""
        parts = path_str.split("/")
        for i in range(len(parts) + 1):
            candidate = "/".join(parts[i:])
            simple = pattern.replace("**/", "").replace("/**", "")
            if simple:
                if fnmatch.fnmatch(candidate, simple):
                    return True
            else:
                return True
        return False


def _load_gitignore(project_root: Path) -> GitignoreMatcher:
    """Load and parse ``.gitignore`` at *project_root* (if it exists)."""
    ignore_file = project_root / ".gitignore"
    if not ignore_file.is_file():
        return GitignoreMatcher([], project_root)

    lines = ignore_file.read_text(encoding="utf-8").splitlines()
    return GitignoreMatcher(lines, project_root)


# ======================================================================
# Import graph extraction
# ======================================================================


class _ImportVisitor(ast.NodeVisitor):
    """AST visitor that collects import statements."""

    def __init__(self) -> None:
        self.imports: list[str] = []
        self.from_imports: list[tuple[str, list[str]]] = []

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.imports.append(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        names = [alias.name for alias in node.names]
        self.from_imports.append((module, names))


def _extract_imports(file_path: Path) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Parse a Python file and return (direct_imports, from_imports)."""
    try:
        source = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], []

    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError:
        return [], []

    visitor = _ImportVisitor()
    visitor.visit(tree)
    return visitor.imports, visitor.from_imports


# ======================================================================
# ProjectMapper
# ======================================================================


class ProjectMapper:
    """Builds a compact structural map of a project directory.

    The map includes a filtered directory tree, detected configuration
    files, and a module import graph summary — everything needed to give
    the LLM a high-level understanding of the codebase layout.

    Parameters
    ----------
    project_root:
        The root directory of the project.  Defaults to the current
        working directory.
    """

    def __init__(self, project_root: str | Path = ".") -> None:
        self._root = Path(project_root).resolve()
        self._gitignore = _load_gitignore(self._root)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_map(self) -> str:
        """Build the full project map as a Markdown-formatted string.

        The result is suitable for injection into the system prompt.
        """
        sections: list[str] = []

        # 1. Directory tree.
        tree = self._build_tree()
        sections.append("## Project Structure\n\n```\n" + tree + "\n```")

        # 2. Config files.
        configs = self._find_config_files()
        if configs:
            lines = ["## Configuration Files"]
            for cf in sorted(configs):
                lines.append(f"- `{cf}`")
            sections.append("\n".join(lines))

        # 3. Import graph.
        imports_section = self._build_import_graph()
        if imports_section:
            sections.append(imports_section)

        return "\n\n".join(sections)

    def build_compact_map(self) -> str:
        """Like :meth:`build_map` but omits the full directory tree.

        Useful for injecting into every turn without consuming too many
        tokens — includes only config files and the import graph.
        """
        sections: list[str] = []

        configs = self._find_config_files()
        if configs:
            lines = ["## Project Configuration"]
            for cf in sorted(configs):
                lines.append(f"- `{cf}`")
            sections.append("\n".join(lines))

        imports_section = self._build_import_graph()
        if imports_section:
            sections.append(imports_section)

        return "\n\n".join(sections) if sections else ""

    # ------------------------------------------------------------------
    # Directory tree
    # ------------------------------------------------------------------

    def _build_tree(self) -> str:
        """Walk the project root and render an indented tree."""
        parts: list[str] = [self._root.name + "/"]

        try:
            entries = sorted(
                self._root.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except PermissionError:
            return self._root.name + "/  [permission denied]"

        for entry in entries:
            if self._should_exclude(entry):
                continue
            subtree = self._render_entry(entry, depth=1)
            if subtree:
                parts.append(subtree)

        return "\n".join(parts)

    def _render_entry(
        self, path: Path, *, depth: int
    ) -> str | None:
        """Render a single filesystem entry and its children.

        Returns ``None`` when *path* is fully excluded.
        """
        if depth > _MAX_TREE_DEPTH:
            return f"{'    ' * depth}…"

        prefix = "    " * depth
        if path.is_symlink():
            return f"{prefix}{path.name} → {path.readlink()}"

        if path.is_dir():
            return self._render_directory(path, depth=depth, prefix=prefix)

        # Regular file.
        return f"{prefix}{path.name}"

    def _render_directory(
        self, path: Path, *, depth: int, prefix: str
    ) -> str | None:
        """Render a directory entry with its children."""
        try:
            entries = sorted(
                path.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except PermissionError:
            return f"{prefix}{path.name}/  [permission denied]"

        visible: list[str] = []
        for entry in entries:
            if self._should_exclude(entry):
                continue
            rendered = self._render_entry(entry, depth=depth + 1)
            if rendered:
                visible.append(rendered)

        if not visible:
            return f"{prefix}{path.name}/" if depth <= 2 else ""

        header = f"{prefix}{path.name}/"
        if len(visible) > _MAX_FILES_PER_DIR:
            visible = visible[:_MAX_FILES_PER_DIR]
            visible.append(f"{'    ' * (depth + 1)}… ({len(entries)} entries)")

        return header + "\n" + "\n".join(visible)

    # ------------------------------------------------------------------
    # Config file detection
    # ------------------------------------------------------------------

    def _find_config_files(self) -> list[str]:
        """Return relative paths to recognised config files at the root."""
        found: list[str] = []
        for name in sorted(_KNOWN_CONFIG_FILES):
            candidate = self._root / name
            if candidate.is_file():
                found.append(name)
        # Also look one level deep for nested configs (e.g., .github/).
        for child in sorted(self._root.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                for name in sorted(_KNOWN_CONFIG_FILES):
                    candidate = child / name
                    if candidate.is_file():
                        found.append(str(candidate.relative_to(self._root)))
        return found

    # ------------------------------------------------------------------
    # Import graph
    # ------------------------------------------------------------------

    def _build_import_graph(self) -> str:
        """Build a textual summary of Python module imports."""
        # Collect all Python files (filtered by gitignore).
        py_files = self._collect_python_files()

        # Build internal module index.
        internal_modules: set[str] = set()
        for pf in py_files:
            internal_modules.add(self._module_name(pf))

        # Extract imports and build adjacency.
        internal_refs: dict[str, set[str]] = defaultdict(set)
        external_refs: set[str] = set()
        module_counts: dict[str, int] = defaultdict(int)

        for pf in py_files:
            mod = self._module_name(pf)
            direct, from_imports = _extract_imports(pf)

            # Count import statements as a rough "complexity" proxy.
            module_counts[mod] = len(direct) + len(from_imports)

            # Direct imports: "import foo.bar"
            for imp in direct:
                top = imp.split(".")[0]
                if top in internal_modules or any(
                    imp.startswith(m) for m in internal_modules
                ):
                    internal_refs[mod].add(imp)
                else:
                    external_refs.add(top)

            # From-imports: "from foo.bar import Baz"
            for mod_src, _names in from_imports:
                if not mod_src:
                    continue
                top = mod_src.split(".")[0]
                if top in internal_modules or any(
                    mod_src.startswith(m) for m in internal_modules
                ):
                    internal_refs[mod].add(mod_src)
                else:
                    external_refs.add(top)

        # Render.
        lines: list[str] = []

        if internal_refs:
            lines.append("## Module Import Graph")
            lines.append("")
            lines.append("**Internal dependencies:**")
            lines.append("")
            # Sort by most-referenced first.
            entries = sorted(
                internal_refs.items(),
                key=lambda kv: (-len(kv[1]), kv[0]),
            )
            for idx, (mod, refs) in enumerate(entries[:50], start=1):
                deps = ", ".join(
                    sorted(refs, key=lambda r: (r.count("."), r))[:8]
                )
                lines.append(f"- `{mod}` → {deps}")
                if idx >= _MAX_IMPORT_ENTRIES:
                    remaining = len(entries) - idx
                    if remaining > 0:
                        lines.append(
                            f"  … and {remaining} more modules"
                        )
                    break

        if external_refs:
            lines.append("")
            lines.append("**Key external dependencies:**")
            lines.append("")
            lines.append(
                ", ".join(f"`{r}`" for r in sorted(external_refs)[:30])
            )

        if not lines:
            return ""

        # Add module count overview.
        if py_files:
            lines.insert(
                2,
                f"_{len(py_files)} Python modules, "
                f"{len(internal_refs)} with internal dependencies_\n",
            )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _collect_python_files(self) -> list[Path]:
        """Walk the project collecting ``.py`` files, respecting ignores."""
        py_files: list[Path] = []
        for dirpath, _dirnames, filenames in self._walk():
            for fname in filenames:
                if fname.endswith(".py"):
                    py_files.append(dirpath / fname)
        return py_files

    def _walk(self) -> list[tuple[Path, list[str], list[str]]]:
        """Walk the project root, skipping ignored/excluded entries."""
        result: list[tuple[Path, list[str], list[str]]] = []

        def _recurse(dirpath: Path) -> None:
            try:
                entries = sorted(dirpath.iterdir())
            except PermissionError:
                return

            dirnames: list[str] = []
            filenames: list[str] = []
            subdirs: list[Path] = []

            for entry in entries:
                if self._should_exclude(entry):
                    continue
                if entry.is_dir():
                    dirnames.append(entry.name)
                    subdirs.append(entry)
                else:
                    filenames.append(entry.name)

            result.append((dirpath, dirnames, filenames))

            for sub in subdirs:
                _recurse(sub)

        _recurse(self._root)
        return result

    def _should_exclude(self, path: Path) -> bool:
        """Check whether *path* should be excluded from the tree."""
        name = path.name

        # Always-exclude patterns.
        for pat in _ALWAYS_EXCLUDE:
            if fnmatch.fnmatch(name, pat):
                return True

        # Check gitignore rules.
        return self._gitignore.is_ignored(path, is_dir=path.is_dir())

    def _module_name(self, file_path: Path) -> str:
        """Convert a ``.py`` file path into a dotted module name."""
        try:
            rel = file_path.relative_to(self._root)
        except ValueError:
            return file_path.stem

        parts = list(rel.parts)
        if parts[-1] == "__init__.py":
            parts = parts[:-1]
        else:
            parts[-1] = parts[-1].replace(".py", "")

        return ".".join(parts)
