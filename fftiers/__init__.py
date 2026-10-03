"""Configurable port of borisachen/fftiers (borischen.co tier charts)."""
from .config import LeagueConfig, load_league
from .depth import plan_positions

__all__ = ["LeagueConfig", "load_league", "plan_positions", "run_league"]


def __getattr__(name):
    # Lazy: run_league drags in matplotlib via plot.py, which the deployed server
    # (manage refresh: espn/vbd/csg/board only) doesn't install.
    if name == "run_league":
        from .run import run_league
        return run_league
    raise AttributeError(name)
