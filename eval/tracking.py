"""
Experiment persistence — "save off experiments as they run".

One directory per run under experiments/<timestamp>_<algo>/, written
append-only so a crash still leaves a readable partial record:

    config.yaml          run config + reproducibility snapshot (versions, git, seed)
    candidates/cand_N.txt every prompt variant evaluated
    results.jsonl        one line per (candidate, sample), flushed as it lands
    summary.json         final per-candidate aggregates + best prompt

Plus two cross-run registries: experiments/INDEX.md (human-readable) and
experiments/leaderboard.csv. Langfuse tracing is already wired in get_llm and
captures every LLM call independently.
"""
import csv
import json
import platform
import subprocess
import sys
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Optional

import yaml

from .paths import EXPERIMENTS_DIR, ensure_dir

_TRACKED_PACKAGES = ("langchain", "langchain-openai", "dspy-ai", "gepa", "langfuse")


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "unknown"


def _package_versions() -> dict:
    out = {}
    for pkg in _TRACKED_PACKAGES:
        try:
            out[pkg] = version(pkg)
        except PackageNotFoundError:
            out[pkg] = None
    return out


def _environment_snapshot() -> dict:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "git_commit": _git_commit(),
        "packages": _package_versions(),
    }


class ExperimentRun:
    """Owns one experiments/<id>/ directory and writes results as they arrive."""

    def __init__(self, algo: str, config: dict):
        self.id = f"{datetime.now():%Y%m%d_%H%M%S}_{algo}"
        self.algo = algo
        self.dir = ensure_dir(EXPERIMENTS_DIR / self.id)
        self.candidates_dir = ensure_dir(self.dir / "candidates")
        self.results_path = self.dir / "results.jsonl"

        self.config = {
            "id": self.id,
            "algo": algo,
            "created": datetime.now().isoformat(timespec="seconds"),
            **config,
            "environment": _environment_snapshot(),
        }
        (self.dir / "config.yaml").write_text(
            yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8"
        )

    def save_candidate(self, candidate_id: str, prompt_text: str) -> None:
        (self.candidates_dir / f"{candidate_id}.txt").write_text(
            prompt_text, encoding="utf-8"
        )

    def log_result(self, candidate_id: str, program_id: str, score: float,
                   components: dict, feedback: str, extra: Optional[dict] = None) -> None:
        row = {
            "candidate": candidate_id,
            "program": program_id,
            "score": round(score, 4),
            "components": {k: round(v, 4) if isinstance(v, float) else v
                           for k, v in components.items()},
            "feedback": feedback,
            **(extra or {}),
        }
        with self.results_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")

    def finalize(self, summary: dict) -> None:
        """Write summary.json and append the cross-run INDEX.md + leaderboard.csv."""
        summary = {"id": self.id, "algo": self.algo, **summary}
        (self.dir / "summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        self._append_index(summary)
        self._append_leaderboard(summary)

    def _append_index(self, summary: dict) -> None:
        index = EXPERIMENTS_DIR / "INDEX.md"
        if not index.exists():
            index.write_text(
                "# Experiment registry\n\n"
                "| id | algo | model | corpus | best score | takeaway |\n"
                "|---|---|---|---|---|---|\n",
                encoding="utf-8",
            )
        cfg = self.config
        row = (f"| {self.id} | {self.algo} | {cfg.get('model_name','?')} "
               f"| {cfg.get('corpus','?')} | {summary.get('best_score','?')} "
               f"| {summary.get('takeaway','')} |\n")
        with index.open("a", encoding="utf-8") as f:
            f.write(row)

    def _append_leaderboard(self, summary: dict) -> None:
        board = EXPERIMENTS_DIR / "leaderboard.csv"
        new = not board.exists()
        with board.open("a", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["id", "algo", "model", "corpus", "best_score", "n_programs"])
            w.writerow([
                self.id, self.algo, self.config.get("model_name", ""),
                self.config.get("corpus", ""), summary.get("best_score", ""),
                summary.get("n_programs", ""),
            ])
