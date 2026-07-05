import os
from pathlib import Path
from setuptools import setup, find_packages

ROOT = Path(__file__).parent

def read(filename):
    return (ROOT / filename).read_text(encoding="utf-8")

def parse_requirements(filename):
    if not (ROOT / filename).exists():
        return []
    lines = (ROOT / filename).read_text(encoding="utf-8").splitlines()
    return [
        line.strip()
        for line in lines
        if line.strip() and not line.startswith("#") and not line.startswith("-")
    ]

# Load version from _version.py
VERSION = {}
version_file = ROOT / "coreai" / "_version.py"
if version_file.exists():
    exec(version_file.read_text(encoding="utf-8"), VERSION)
else:
    VERSION["__version__"] = "1.0.0"

setup(
    name="coreai-protocol-suite",
    version=VERSION["__version__"],
    description="Intelligent LLM routing and agent orchestration framework for production AI systems",
    long_description=read("README.md") if (ROOT / "README.md").exists() else "",
    long_description_content_type="text/markdown",
    author="Lakshit Singh Bisht",
    author_email="lakshit@coreai.dev",
    url="https://github.com/LakshitSinghBishtTM/CoreAI-Protocol-Suite",
    project_urls={
        "Bug Tracker":   "https://github.com/LakshitSinghBishtTM/CoreAIProtocolSuite/issues",
        "Changelog":     "https://github.com/LakshitSinghBishtTM/CoreAIProtocolSuite/blob/main/CHANGELOG.md",
        "Documentation": "https://docs.coreai.dev",
    },
    license="GPL-3.0",
    packages=find_packages(exclude=["tests*", "scripts*", "docs*"]),
    package_data={
        "coreai": ["py.typed"],
        "coreai.configs": ["*.yml", "*.yaml"],
    },
    include_package_data=True,
    python_requires=">=3.10",
    install_requires=parse_requirements("requirements.txt"),
    extras_require={
        "dev": parse_requirements("requirements-dev.txt"),
        "docs": [
            "mkdocs>=1.5",
            "mkdocs-material>=9.0",
            "mkdocstrings[python]>=0.24",
        ],
        "observability": [
            "sentry-sdk[fastapi]>=1.40",
            "ddtrace>=2.5",
            "opentelemetry-sdk>=1.22",
            "opentelemetry-instrumentation-fastapi>=0.43b0",
        ],
    },
    entry_points={
        "console_scripts": [
            "coreai=coreai.cli:main",
            "coreai-server=coreai.api.server:run",
            "coreai-worker=coreai.workers.agent:run",
            "coreai-migrate=coreai.db.migrations:run",
        ],
    },
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: GNU General Public License v3 (GPLv3)",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
        "Topic :: Software Development :: Libraries :: Application Frameworks",
        "Topic :: Internet :: WWW/HTTP :: HTTP Servers",
        "Typing :: Typed",
    ],
    keywords=[
        "llm", "ai", "routing", "orchestration", "openai", "anthropic",
        "gemini", "agents", "inference", "production", "mlops",
    ],
    zip_safe=False,
)