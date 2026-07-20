"""Build the installable local Meta-Research product and its system assets."""
from pathlib import Path

from setuptools import find_packages, setup


ROOT = Path(__file__).parent
ASSET_ROOTS = (
    "db", "engines", "input", "policies", "prompts", "quest_templates", "schemas", "views",
)


def system_data_files():
    grouped = {}
    for name in ASSET_ROOTS:
        for path in sorted((ROOT / name).rglob("*")):
            if path.is_file() and not path.is_symlink():
                target = Path("share") / "meta-research" / path.parent.relative_to(ROOT)
                grouped.setdefault(str(target), []).append(str(path.relative_to(ROOT)))
    return sorted(grouped.items())


setup(
    name="meta-research-local",
    version="0.1.0",
    description="Local Web-first autonomous meta-research orchestrator",
    long_description=(ROOT / "README.md").read_text(encoding="utf-8"),
    long_description_content_type="text/markdown",
    python_requires=">=3.9",
    packages=find_packages(include=("orchestrator", "orchestrator.*")),
    data_files=system_data_files(),
    install_requires=[
        "jsonschema>=4.18,<5",
        "referencing>=0.30,<1",
        "PyYAML>=6,<7",
        "numpy>=1.24,<3",
        "scipy>=1.10,<2",
    ],
    entry_points={"console_scripts": ["meta-research=orchestrator.web_app:main"]},
)
