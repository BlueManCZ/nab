#!/usr/bin/env python3
"""Install a per-user .desktop entry that points at this checkout.

Drops `io.github.bluemancz.nab.desktop` into ~/.local/share/applications/ with
an Exec line that runs `python3 -m nab` against the current project directory
(via PYTHONPATH, so it works no matter where the launch happens from).

Re-run after moving the repo to refresh the absolute path.
"""

import shutil
import subprocess
import sys
from pathlib import Path

APP_ID = "io.github.bluemancz.nab"


def project_root() -> Path:
    # scripts/ lives one level under the repo root.
    return Path(__file__).resolve().parent.parent


def pick_python(repo: Path) -> tuple[str, bool]:
    """Return (python_path, is_venv).

    Prefer the project's uv-managed venv if it exists — that's where deps like
    `subliminal` actually live. Fall back to system python3 with a PYTHONPATH
    pointing at the checkout (works as long as the user installs deps system
    or per-user themselves).
    """
    venv_python = repo / ".venv" / "bin" / "python"
    if venv_python.is_file():
        return str(venv_python), True
    return (shutil.which("python3") or "python3"), False


def render(template: str, repo: Path) -> str:
    python, is_venv = pick_python(repo)
    if is_venv:
        # The venv has nab installed as an editable package, so no PYTHONPATH
        # hack is needed — and crucially, the venv has the optional deps
        # (subliminal, etc.) that aren't necessarily in system site-packages.
        exec_line = f"{python} -m nab"
    else:
        exec_line = f"env PYTHONPATH={repo} {python} -m nab"
    return (template
            .replace("@EXEC@", exec_line)
            .replace("@TRYEXEC@", python))


def main() -> int:
    repo = project_root()
    # Reuse nab's canonical XDG resolver instead of re-deriving it here.
    # data_home() is app-scoped (<XDG_DATA_HOME>/nab); its parent is the base
    # data dir, and .desktop entries live in its sibling applications/ dir.
    sys.path.insert(0, str(repo))
    from nab.paths import data_home

    template_path = repo / "packaging" / f"{APP_ID}.desktop.in"
    if not template_path.is_file():
        print(f"error: missing template {template_path}", file=sys.stderr)
        return 1

    # Sanity-check that the package is actually here so we don't install a
    # broken entry that points nowhere.
    if not (repo / "nab" / "__main__.py").is_file():
        print(f"error: {repo}/nab/__main__.py not found — is this the repo root?",
              file=sys.stderr)
        return 1

    rendered = render(template_path.read_text(), repo)

    applications_dir = data_home().parent / "applications"
    applications_dir.mkdir(parents=True, exist_ok=True)
    target = applications_dir / f"{APP_ID}.desktop"
    target.write_text(rendered)
    target.chmod(0o644)
    print(f"installed: {target}")

    # Refresh the MIME / desktop cache so file managers pick it up immediately.
    # update-desktop-database is optional; skip silently if absent (some minimal
    # systems don't ship it, and the file is still usable without it).
    udd = shutil.which("update-desktop-database")
    if udd:
        try:
            subprocess.run([udd, str(applications_dir)], check=True)
            print(f"refreshed: {applications_dir}")
        except subprocess.CalledProcessError as exc:
            print(f"warning: update-desktop-database exited {exc.returncode}",
                  file=sys.stderr)
    else:
        print("note: update-desktop-database not found; the entry is installed "
              "but your file manager may need a re-login to see it.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
