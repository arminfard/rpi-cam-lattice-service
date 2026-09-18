"""Guard: the service is an agent and must never create tasks itself.

Operators create tasks from the Lattice UI. The only place that creates tasks
is the separate validation driver ``scripts/send_task.py``. This test fails if
task creation ever leaks into the installed service package.
"""

from pathlib import Path

import lattice_cam

PACKAGE_DIR = Path(lattice_cam.__file__).parent
FORBIDDEN = ("create_task(", "CreateTaskRequest")


def test_service_package_never_creates_tasks():
    offenders = []
    for path in PACKAGE_DIR.rglob("*.py"):
        text = path.read_text()
        for token in FORBIDDEN:
            # Ignore comments explaining the rule; flag code.
            for line in text.splitlines():
                if token in line and not line.strip().startswith("#"):
                    offenders.append(f"{path.relative_to(PACKAGE_DIR)}: {line.strip()}")
    assert offenders == [], f"task creation found in the service package: {offenders}"
