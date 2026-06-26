"""
GEPA-style reflective prompt optimizer.

Loop: evaluate a candidate prompt per-function (score + natural-language
feedback) -> reflect on the worst cases with an LLM to mutate the instruction ->
score the mutation on a held-out val set -> keep a Pareto front and sample the
next parent from it. This embodies GEPA's core ideas (reflective mutation from
textual feedback + Pareto selection) directly over our round-trip metric, with
explicit budget control. The `gepa`/DSPy libraries are a future swap-in.
"""
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from cpp_re_agent import ai_improver

from .. import metrics
from ..dataset import FunctionExample

_REFLECTION_SYSTEM = (
    "You are optimizing the SYSTEM INSTRUCTION given to another LLM that rewrites "
    "decompiled C++ (full of Ghidra artifacts like param_1, iVar2, undefined4) "
    "back into clean, readable C++ that matches the ORIGINAL source as closely as "
    "possible. You are shown the current instruction and concrete cases where it "
    "underperformed, each with the decompiled input, the model's output, the "
    "reference original, and a critique. Write an IMPROVED instruction that would "
    "fix these failure modes.\n\n"
    "Rules for the instruction you write:\n"
    "- It must keep the model returning ONLY C++ code (no markdown, no prose).\n"
    "- It must forbid changing behavior, and discourage re-declaring types/structs "
    "or #includes that belong elsewhere.\n"
    "- Be specific and general at once: encode the lessons from the failures "
    "without hard-coding these exact functions.\n"
    "- Output ONLY the new instruction text, nothing else."
)


@dataclass
class Candidate:
    id: str
    prompt: str
    parent: Optional[str] = None
    val_score: float = 0.0
    per_example: Dict[str, float] = field(default_factory=dict)


class ReflectiveOptimizer:
    def __init__(self, provider: str, model_name: str,
                 judge_fn: Callable[[str, str], metrics.MetricResult],
                 reflect_llm, weights: Optional[dict] = None,
                 run=None, status: Callable[[str], None] = print,
                 cache_path: Optional[Path] = None):
        self.provider = provider
        self.model_name = model_name
        self.judge_fn = judge_fn
        self.reflect_llm = reflect_llm
        self.weights = weights or metrics.DEFAULT_WEIGHTS
        self.run = run
        self.status = status
        # Disk-backed eval cache (model-scoped): a (prompt, example) score is
        # deterministic enough to reuse across runs, which matters a lot on the
        # slow endpoint — the seed prompt is re-scored every run otherwise.
        self.cache_path = cache_path
        self._cache: Dict[str, Tuple[float, str]] = {}
        if cache_path and Path(cache_path).exists():
            try:
                raw = json.loads(Path(cache_path).read_text(encoding="utf-8"))
                self._cache = {k: tuple(v) for k, v in raw.items()}
                self.status(f"loaded {len(self._cache)} cached evals")
            except Exception:
                pass

    def _save_cache(self) -> None:
        if not self.cache_path:
            return
        Path(self.cache_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self.cache_path).write_text(
            json.dumps({k: list(v) for k, v in self._cache.items()}), encoding="utf-8")

    @staticmethod
    def _key(prompt: str, ex: FunctionExample) -> str:
        ph = hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:12]
        dh = hashlib.sha1(ex.decompiled.encode("utf-8")).hexdigest()[:8]
        return f"{ph}|{ex.name}|{dh}"

    # --- core operations ---
    def _improve(self, code: str, prompt: str) -> str:
        return ai_improver.improve_function(
            code, provider=self.provider, model_name=self.model_name,
            binary_path=None, base_prompt=prompt,
        )

    def evaluate(self, prompt: str, examples: List[FunctionExample],
                 label: str = "") -> Tuple[float, Dict[str, float], List[dict]]:
        """Mean score + per-example scores + rich per-example records (cached).

        Emits a status line at every step (improve call, judge call, score) so a
        long run is debuggable in real time rather than silent until the mean.
        """
        scores: Dict[str, float] = {}
        records: List[dict] = []
        dirty = False
        n = len(examples)
        tag = f"{label} " if label else ""
        for i, ex in enumerate(examples, 1):
            key = self._key(prompt, ex)
            if key in self._cache:
                score, feedback = self._cache[key]
                improved = ""
                self.status(f"{tag}[{i}/{n}] {ex.name}: cached -> {score:.3f}")
            else:
                t0 = time.time()
                self.status(f"{tag}[{i}/{n}] {ex.name}: improving (LLM)...")
                improved = self._improve(ex.decompiled, prompt)
                if self.judge_fn:
                    self.status(f"{tag}[{i}/{n}] {ex.name}: judging (LLM)...")
                mr = metrics.score_pair(ex.original, improved, ex.decompiled,
                                        judge_fn=self.judge_fn, weights=self.weights)
                score, feedback = mr.score, mr.feedback_text
                self._cache[key] = (score, feedback)
                dirty = True
                self.status(f"{tag}[{i}/{n}] {ex.name}: score={score:.3f} "
                            f"{mr.components} ({time.time() - t0:.0f}s)")
            scores[ex.name] = score
            records.append({"name": ex.name, "score": score,
                            "feedback": feedback, "improved": improved,
                            "decompiled": ex.decompiled, "original": ex.original})
        if dirty:
            self._save_cache()
        mean = sum(scores.values()) / len(scores) if scores else 0.0
        self.status(f"{tag}done: mean={mean:.3f} over {n} fn(s)")
        return mean, scores, records

    # Per-candidate focus hints: the reflection model runs deterministically
    # (temp 0), so we diversify mutations by steering each toward a different
    # failure mode — which also targets the weaknesses seen in P1.
    _ANGLES = [
        "Focus on eliminating duplicated/again-declared types, structs, and "
        "#includes — assume types and callees are declared elsewhere.",
        "Focus on strictly preserving the original behavior and choosing correct "
        "data types (no widening, no hard-coded results, no logic changes).",
        "Focus on recovering clear, specific variable and function names and "
        "concise comments that match the original's intent.",
        "Rewrite the instruction to be shorter and sharper without losing any "
        "essential constraint.",
    ]

    def reflect(self, prompt: str, records: List[dict], k: int) -> List[str]:
        """Generate k mutated instructions by reflecting on the worst examples."""
        worst = sorted(records, key=lambda r: r["score"])[:k + 1]
        cases = []
        for r in worst:
            cases.append(
                f"### Decompiled input\n{r['decompiled'][:700]}\n"
                f"### Model output\n{(r['improved'] or '(cached)')[:700]}\n"
                f"### Reference original\n{r['original'][:700]}\n"
                f"### Critique (score {r['score']:.2f})\n{r['feedback'][:600]}\n"
            )
        cases_block = "\n".join(cases)
        mutations: List[str] = []
        for i in range(k):
            angle = self._ANGLES[i % len(self._ANGLES)]
            user = (f"## Current instruction\n{prompt}\n\n"
                    f"## Failure cases\n{cases_block}\n\n"
                    f"## This revision's emphasis\n{angle}\n\n"
                    "Write the improved instruction now.")
            try:
                out = self.reflect_llm.invoke(
                    [("system", _REFLECTION_SYSTEM), ("user", user)]
                ).content
                text = out.replace("```", "").strip()
                if text and len(text) > 40:
                    mutations.append(text)
            except Exception as e:
                self.status(f"   reflection {i} failed: {e}")
        return mutations

    # --- the loop ---
    def optimize(self, seed_prompt: str, trainset: List[FunctionExample],
                 valset: List[FunctionExample], iterations: int = 5,
                 candidates_per_iter: int = 2, minibatch: int = 0,
                 seed_variants: Optional[List[str]] = None
                 ) -> Tuple[Candidate, List[Candidate]]:
        seed = Candidate(id="seed", prompt=seed_prompt)
        seed.val_score, seed.per_example, _ = self.evaluate(
            seed_prompt, valset, label="seed/val")
        self.status(f"seed val={seed.val_score:.3f}")
        self._save(seed)

        front: List[Candidate] = [seed]
        best = seed
        counter = 0

        # Hand-crafted starting variants (principled fixes for known failure
        # modes) seed the search so reflection refines from a strong base.
        for variant in (seed_variants or []):
            counter += 1
            cand = Candidate(id=f"seedvar{counter}", prompt=variant, parent="seed")
            cand.val_score, cand.per_example, _ = self.evaluate(
                variant, valset, label=f"seedvar{counter}/val")
            self._save(cand)
            self.status(f"seedvar{counter} val={cand.val_score:.3f}"
                        + ("  <-- new best" if cand.val_score > best.val_score else ""))
            front.append(cand)
            if cand.val_score > best.val_score:
                best = cand

        for it in range(iterations):
            parent = self._sample_parent(front, valset)
            # Gather feedback for the parent on a train minibatch (keeps the
            # expensive eval small on slow endpoints), then mutate.
            batch = trainset
            if minibatch and minibatch < len(trainset):
                batch = random.sample(trainset, minibatch)
            self.status(f"iter {it}: gathering feedback on parent={parent.id} "
                        f"({len(batch)} train fn)...")
            _, _, train_records = self.evaluate(
                parent.prompt, batch, label=f"iter{it}/{parent.id}/train")
            self.status(f"iter {it}: reflecting -> {candidates_per_iter} mutation(s)...")
            mutations = self.reflect(parent.prompt, train_records, candidates_per_iter)
            self.status(f"iter {it}: parent={parent.id} -> {len(mutations)} mutation(s)")

            for prompt in mutations:
                counter += 1
                cand = Candidate(id=f"cand{counter}", prompt=prompt, parent=parent.id)
                cand.val_score, cand.per_example, _ = self.evaluate(
                    prompt, valset, label=f"cand{counter}/val")
                self._save(cand)
                self.status(f"   {cand.id} val={cand.val_score:.3f}"
                            + ("  <-- new best" if cand.val_score > best.val_score else ""))
                front.append(cand)
                if cand.val_score > best.val_score:
                    best = cand

        return best, front

    def _sample_parent(self, front: List[Candidate], valset: List[FunctionExample]
                       ) -> Candidate:
        """Pareto-style: weight parents by how many val examples they win."""
        wins = {c.id: 0 for c in front}
        for ex in valset:
            best_score = max(c.per_example.get(ex.name, 0.0) for c in front)
            for c in front:
                if c.per_example.get(ex.name, 0.0) >= best_score > 0:
                    wins[c.id] += 1
        weights = [wins[c.id] + 1 for c in front]  # +1 so everyone is reachable
        return random.choices(front, weights=weights, k=1)[0]

    def _save(self, cand: Candidate) -> None:
        if self.run is not None:
            self.run.save_candidate(cand.id, cand.prompt)
            self.run.log_result(
                cand.id, "val_mean", cand.val_score,
                {"val_score": round(cand.val_score, 4)},
                f"parent={cand.parent}",
                extra={"per_example": {k: round(v, 4) for k, v in cand.per_example.items()}},
            )
