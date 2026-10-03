"""Repo-root resolution, dependency-free.

Lives in its own module so the matplotlib-free surface (espn/vbd/csg/board — what a
deployed server runs for /api/refresh) never imports cli.py, whose run_league import
chain pulls in matplotlib.
"""
from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
