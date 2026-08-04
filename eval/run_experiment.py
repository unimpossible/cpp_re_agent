"""
Experiment runner CLI.

    uv run python -m eval.run_experiment --algo control --tier 0
    uv run python -m eval.run_experiment --algo control --ids tier1_stats --no-judge

`control` runs the current DEFAULT_SYSTEM_PROMPT through the full round-trip and
scores it — the fixed baseline every optimized prompt is measured against.
`gepa` is added in P2 and plugs in here as another --algo without changing the
round-trip or metrics.
"""
import argparse
import os
import random
import statistics
from typing import List

# Disable Langfuse tracing for batch experiments: its exporter can time out
# against an unreachable server and stall the run. Must precede llm_factory use.
os.environ.setdefault("DISABLE_LANGFUSE", "1")

from cpp_re_agent import ai_improver
from cpp_re_agent.llm_factory import get_llm

from . import header as header_mod
from . import metrics
from .corpus import load_corpus
from .dataset import build_function_dataset, split
from .judge import make_judge
from .optimizers.reflective import ReflectiveOptimizer
from .roundtrip import run_roundtrip
from .tracking import ExperimentRun


# Evidence-based variants, written from the default prompt's actual per-function
# failures (run 20260618_153126_bakeoff): integer-type widening
# (unsigned int -> uint32_t), over-elaboration vs the minimal original (added
# namespaces/comments/std-swaps/defensive checks), and invented wrong struct
# layouts. These target those specific judge penalties without over-restricting.
SEED_VARIANTS = [
    # V5: SURGICAL edit of the default — keep its structure, patch only the two
    # biggest leaks (integer widening, over-elaboration). Lowest-variance bet to
    # strictly dominate the default rather than trade wins for losses.
    "You are an expert C++ reverse engineer. Improve the readability of the "
    "decompiled function so it matches the original as closely as possible.\n"
    "1. Rename Ghidra artifacts (iVar1, param_1, uVar2, undefined4, local_18) to "
    "meaningful names; if unsure what a value is, keep a neutral name.\n"
    "2. Fix a data type only when the decompiled one is clearly wrong. KEEP the "
    "original integer types and signature — never widen int / unsigned int to "
    "uint32_t, int64_t, size_t, or similar.\n"
    "3. Preserve behavior EXACTLY: do not change control flow, arithmetic, or "
    "constants.\n"
    "4. Match the minimal style of hand-written code: no added namespaces, no "
    "defensive checks, no standard-library substitutions (keep sqrt, not hypot; "
    "keep a simple ternary). Add a comment only for genuinely non-obvious logic.\n"
    "5. Do not introduce struct/class definitions unless clearly required, and "
    "never guess field names or layouts; assume types are declared elsewhere.\n"
    "6. Return ONLY the C++ code, with no markdown fences.",
    # V3: faithfulness-first, keep original types/style, don't invent structs.
    "You are an expert C++ reverse engineer. Rewrite the single decompiled "
    "function into clean, readable C++ that matches the ORIGINAL source as "
    "closely as possible — favor faithfulness over embellishment.\n"
    "1. Rename Ghidra artifacts (param_1, iVar2, uVar3, undefined4, local_18) to "
    "short meaningful names; if a value's purpose is unclear, keep a neutral name.\n"
    "2. Preserve behavior EXACTLY: keep the same control flow, arithmetic, and "
    "operations; never combine, reorder, or drop steps.\n"
    "3. Keep the ORIGINAL integer types and signature. Do NOT widen or swap types "
    "(keep int / unsigned int; never change them to uint32_t, int64_t, size_t, or "
    "similar). Fix a type only when the decompiled one is clearly wrong.\n"
    "4. Stay minimal and idiomatic, like hand-written code: do NOT add namespaces, "
    "defensive checks, or standard-library substitutions (keep sqrt, not hypot; "
    "keep a simple ternary). Add at most one brief comment, only for genuinely "
    "non-obvious logic.\n"
    "5. Do not invent struct/class definitions or #includes. If the code uses a "
    "struct, reference its fields faithfully without guessing extra fields or "
    "changing field types; assume the type is declared elsewhere.\n"
    "Return ONLY the C++ code for the function, with no markdown fences or prose.",
    # V4: terse version of the same lessons.
    "Rewrite this one decompiled C++ function to read like the concise original.\n"
    "- Identical behavior, control flow, and arithmetic — change nothing semantic.\n"
    "- Keep the original types and signature; never widen int/unsigned to "
    "uint32_t/int64_t/size_t.\n"
    "- Give variables and parameters short meaningful names; leave unclear values "
    "neutral.\n"
    "- Match the original's minimal style: no added namespaces, at most one "
    "comment, no helper functions, no std-function swaps, no invented struct or "
    "#include definitions (assume types live elsewhere).\n"
    "Return only the function's C++ code, no fences.",
]


def _build_improve_fn(provider: str, model_name: str, base_prompt: str):
    def improve_fn(code: str) -> str:
        return ai_improver.improve_function(
            code, provider=provider, model_name=model_name,
            binary_path=None, base_prompt=base_prompt,
        )
    return improve_fn


def run_control(args) -> None:
    random.seed(args.seed)
    programs = load_corpus(tier=args.tier, ids=args.ids)
    if args.limit:
        programs = programs[: args.limit]
    if not programs:
        print("No corpus programs matched the filter.")
        return

    corpus_label = (f"ids:{','.join(args.ids)}" if args.ids
                    else (f"tier{args.tier}" if args.tier is not None else "all"))

    run = ExperimentRun("control", {
        "provider": args.provider,
        "model_name": args.model,
        "judge_model": (args.judge_model if args.judge else None),
        "corpus": corpus_label,
        "programs": [p.id for p in programs],
        "seed": args.seed,
        "weights": metrics.DEFAULT_WEIGHTS,
        "strip": False,
    })
    print(f"Experiment {run.id}: control over {len(programs)} program(s) -> {run.dir}")

    base_prompt = ai_improver.DEFAULT_SYSTEM_PROMPT
    run.save_candidate("control", base_prompt)
    improve_fn = _build_improve_fn(args.provider, args.model, base_prompt)
    judge_fn = make_judge(args.provider, args.judge_model) if args.judge else None

    scores: List[float] = []
    per_program = {}
    for prog in programs:
        print(f"  -> {prog.id} (tier {prog.tier})")
        result = run_roundtrip(prog, improve_fn, status=lambda m: print("     " + m))
        if args.judge:
            print("     scoring (judge LLM)...")
        mr = metrics.composite(result, judge_fn=judge_fn)
        run.log_result("control", prog.id, mr.score, mr.components, mr.feedback_text,
                       extra={"timings": result.timings, "error": result.error,
                              "n_targets": result.n_targets, "n_unique": result.n_unique})
        scores.append(mr.score)
        per_program[prog.id] = round(mr.score, 4)
        print(f"     score={mr.score:.3f}  {mr.components}")

    best_score = round(statistics.mean(scores), 4) if scores else 0.0
    run.finalize({
        "best_score": best_score,
        "n_programs": len(programs),
        "per_program": per_program,
        "candidates": {"control": best_score},
        "takeaway": f"control baseline mean={best_score:.3f} over {len(programs)} programs",
    })
    print(f"\nControl baseline mean score: {best_score}")
    print(f"Saved: {run.dir}")


def run_gepa(args) -> None:
    random.seed(args.seed)
    programs = load_corpus(tier=args.tier, ids=args.ids)
    examples = build_function_dataset(programs)
    if len(examples) < 6:
        print(f"Only {len(examples)} function examples — expand the corpus first.")
        return
    # 3-way split: optimize on train+val (val = selection), report on a TEST set
    # never used for selection, so the seed-vs-best claim is unbiased.
    trainval, testset = split(examples, val_frac=args.test_frac, seed=args.seed)
    trainset, valset = split(trainval, val_frac=args.val_frac, seed=args.seed)
    corpus_label = (f"ids:{','.join(args.ids)}" if args.ids
                    else (f"tier{args.tier}" if args.tier is not None else "all"))

    run = ExperimentRun("gepa", {
        "provider": args.provider, "model_name": args.model,
        "judge_model": args.judge_model, "reflect_model": args.reflect_model,
        "corpus": corpus_label, "n_examples": len(examples),
        "n_train": len(trainset), "n_val": len(valset), "n_test": len(testset),
        "iterations": args.iterations, "candidates_per_iter": args.candidates,
        "seed": args.seed, "weights": metrics.DEFAULT_WEIGHTS, "strip": False,
    })
    print(f"Experiment {run.id}: GEPA over {len(examples)} fns "
          f"(train {len(trainset)} / val {len(valset)} / test {len(testset)}) -> {run.dir}")

    judge_fn = make_judge(args.provider, args.judge_model)
    reflect_llm = get_llm(args.provider, args.reflect_model)
    cache_tag = args.cache_tag or args.model.replace('/', '_')
    cache_path = run.dir.parent / ".cache" / f"eval_{cache_tag}.json"
    opt = ReflectiveOptimizer(
        provider=args.provider, model_name=args.model, judge_fn=judge_fn,
        reflect_llm=reflect_llm, run=run,
        status=lambda m: print("  " + m), cache_path=cache_path,
    )

    best, front = opt.optimize(
        ai_improver.DEFAULT_SYSTEM_PROMPT, trainset, valset,
        iterations=args.iterations, candidates_per_iter=args.candidates,
        minibatch=args.minibatch,
        seed_variants=(SEED_VARIANTS if args.seed_variants else None),
    )
    seed = next(c for c in front if c.id == "seed")
    (run.dir / "best_prompt.txt").write_text(best.prompt, encoding="utf-8")

    # Unbiased headline: seed vs best on the held-out TEST set.
    print(f"\nScoring seed vs best on held-out test set ({len(testset)} fn)...")
    seed_test, _, _ = opt.evaluate(seed.prompt, testset, label="seed/test")
    best_test, _, _ = opt.evaluate(best.prompt, testset, label="best/test")
    delta_val = best.val_score - seed.val_score
    delta_test = best_test - seed_test

    run.finalize({
        "best_score": round(best_test, 4),                 # leaderboard = test
        "seed_test": round(seed_test, 4),
        "best_test": round(best_test, 4),
        "delta_test": round(delta_test, 4),
        "seed_val": round(seed.val_score, 4),
        "best_val": round(best.val_score, 4),
        "delta_val": round(delta_val, 4),
        "best_candidate": best.id,
        "n_candidates": len(front),
        "n_programs": len({e.program_id for e in examples}),
        "takeaway": f"best vs seed on TEST: {best_test:.3f} vs {seed_test:.3f} "
                    f"(delta {delta_test:+.3f}); val delta {delta_val:+.3f}",
    })
    print(f"\nVAL  seed={seed.val_score:.3f}  best={best.val_score:.3f} ({delta_val:+.3f})")
    print(f"TEST seed={seed_test:.3f}  best={best_test:.3f} ({delta_test:+.3f}, {best.id})")
    print(f"Best prompt saved: {run.dir / 'best_prompt.txt'}")


def run_bakeoff(args) -> None:
    """
    Paired head-to-head: score the seed prompt and each candidate on the SAME
    full set of functions, compare per-function. Far more sensitive than a
    train/val/test split on a tiny corpus (removes the which-functions-landed-
    where confound). Reuses the disk cache.
    """
    from .optimizers.reflective import ReflectiveOptimizer
    random.seed(args.seed)
    examples = build_function_dataset(load_corpus(tier=args.tier, ids=args.ids))
    corpus_label = (f"ids:{','.join(args.ids)}" if args.ids
                    else (f"tier{args.tier}" if args.tier is not None else "all"))

    prompts = {"seed": ai_improver.DEFAULT_SYSTEM_PROMPT}
    for i, v in enumerate(SEED_VARIANTS, 1):
        prompts[f"seedvar{i}"] = v

    run = ExperimentRun("bakeoff", {
        "provider": args.provider, "model_name": args.model,
        "judge_model": args.judge_model, "corpus": corpus_label,
        "n_examples": len(examples), "prompts": list(prompts), "seed": args.seed,
        "weights": metrics.DEFAULT_WEIGHTS,
    })
    print(f"Experiment {run.id}: bakeoff of {len(prompts)} prompts on "
          f"{len(examples)} fns -> {run.dir}")

    judge_fn = make_judge(args.provider, args.judge_model)
    cache_tag = args.cache_tag or args.model.replace('/', '_')
    cache_path = run.dir.parent / ".cache" / f"eval_{cache_tag}.json"
    opt = ReflectiveOptimizer(args.provider, args.model, judge_fn,
                              get_llm(args.provider, args.model), run=run,
                              status=lambda m: print("  " + m), cache_path=cache_path)

    per_prompt = {}
    for idx, (name, prompt) in enumerate(prompts.items(), 1):
        print(f"\n[prompt {idx}/{len(prompts)}] '{name}' over {len(examples)} fn(s)")
        run.save_candidate(name, prompt)
        mean, scores, _ = opt.evaluate(prompt, examples, label=name)
        per_prompt[name] = scores
        run.log_result(name, "full_mean", mean, {"mean": round(mean, 4)}, "",
                       extra={"per_example": {k: round(v, 4) for k, v in scores.items()}})
        print(f"  {name:10} mean={mean:.3f}")

    seed_scores = per_prompt["seed"]
    seed_mean = sum(seed_scores.values()) / len(seed_scores)
    rows = []
    for name, scores in per_prompt.items():
        mean = sum(scores.values()) / len(scores)
        wins = sum(1 for k in scores if scores[k] > seed_scores[k] + 1e-9)
        losses = sum(1 for k in scores if scores[k] < seed_scores[k] - 1e-9)
        rows.append((name, round(mean, 4), round(mean - seed_mean, 4), wins, losses))
        print(f"  {name:10} mean={mean:.3f}  delta_vs_seed={mean-seed_mean:+.3f}  "
              f"wins/losses={wins}/{losses}")

    winner = max(rows, key=lambda r: r[1])
    (run.dir / "best_prompt.txt").write_text(prompts[winner[0]], encoding="utf-8")
    run.finalize({
        "best_score": winner[1], "winner": winner[0], "seed_mean": round(seed_mean, 4),
        "delta_vs_seed": winner[2], "n_examples": len(examples),
        "table": [{"prompt": r[0], "mean": r[1], "delta": r[2],
                   "wins": r[3], "losses": r[4]} for r in rows],
        "takeaway": f"winner={winner[0]} mean={winner[1]:.3f} "
                    f"(seed={seed_mean:.3f}, delta {winner[2]:+.3f})",
    })
    print(f"\nWinner: {winner[0]} (mean {winner[1]:.3f} vs seed {seed_mean:.3f}, "
          f"delta {winner[2]:+.3f})")


def run_contextual_control(args) -> None:
    """
    Measure the default contextual ('function merging') prompt: does the second
    pass (refine with project types + callee signatures) move functions closer to
    the original than the first-pass improvement alone?
    """
    from cpp_re_agent import contextual_improver
    from . import contextual as ctx_mod
    random.seed(args.seed)
    programs = load_corpus(tier=args.tier, ids=args.ids)
    by_program = {p.id: p for p in programs}
    examples = build_function_dataset(programs)
    if args.limit:
        examples = examples[: args.limit]
    corpus_label = (f"ids:{','.join(args.ids)}" if args.ids
                    else (f"tier{args.tier}" if args.tier is not None else "all"))

    run = ExperimentRun("contextual_control", {
        "provider": args.provider, "model_name": args.model,
        "judge_model": args.judge_model, "corpus": corpus_label,
        "n_examples": len(examples), "seed": args.seed,
        "weights": metrics.DEFAULT_WEIGHTS,
    })
    run.save_candidate("contextual_control", contextual_improver.DEFAULT_CONTEXTUAL_PROMPT)
    print(f"Experiment {run.id}: contextual control over {len(examples)} fns -> {run.dir}")

    judge_fn = make_judge(args.provider, args.judge_model)

    def improve_fn(code):
        return ai_improver.improve_function(
            code, provider=args.provider, model_name=args.model, binary_path=None)

    def refine_fn(target, header, callees):
        return contextual_improver.refine_function(
            target, header, callees, provider=args.provider, model_name=args.model)

    ctx_cache = {pid: ctx_mod.program_context(p) for pid, p in by_program.items()}
    base_scores, refined_scores = [], []
    for ex in examples:
        header, sigs = ctx_cache[ex.program_id]
        callees = ctx_mod.callee_context(ex.original, sigs, roundtrip._normalize_name(ex.name))
        print(f"  -> {ex.program_id}:{ex.name}")
        res = ctx_mod.run_contextual_roundtrip(
            ex, header, callees, improve_fn, refine_fn,
            status=lambda m: print("     " + m))
        if res.error:
            print(f"     error: {res.error}")
            continue
        print("     scoring base (judge)...")
        base = metrics.score_pair(ex.original, res.base_improved, ex.decompiled, judge_fn)
        print("     scoring refined (judge)...")
        refined = metrics.score_pair(ex.original, res.refined, ex.decompiled, judge_fn)
        delta = refined.score - base.score
        run.log_result("contextual_control", ex.name, refined.score, refined.components,
                       refined.feedback_text,
                       extra={"base_score": round(base.score, 4),
                              "refined_score": round(refined.score, 4),
                              "delta": round(delta, 4)})
        base_scores.append(base.score)
        refined_scores.append(refined.score)
        print(f"     base={base.score:.3f} refined={refined.score:.3f} delta={delta:+.3f}")

    n = len(refined_scores)
    base_mean = sum(base_scores) / n if n else 0.0
    refined_mean = sum(refined_scores) / n if n else 0.0
    run.finalize({
        "best_score": round(refined_mean, 4), "base_mean": round(base_mean, 4),
        "refined_mean": round(refined_mean, 4),
        "delta_vs_base": round(refined_mean - base_mean, 4), "n_examples": n,
        "takeaway": f"contextual pass: refined={refined_mean:.3f} vs "
                    f"base={base_mean:.3f} (delta {refined_mean - base_mean:+.3f}) over {n} fns",
    })
    print(f"\nContextual pass: base={base_mean:.3f} -> refined={refined_mean:.3f} "
          f"(delta {refined_mean - base_mean:+.3f})")


def run_header_control(args) -> None:
    """Measure the default header-synthesis prompt (knowledge_graph) as control."""
    from cpp_re_agent import knowledge_graph
    random.seed(args.seed)
    programs = load_corpus(tier=args.tier, ids=args.ids)
    if args.limit:
        programs = programs[: args.limit]
    corpus_label = (f"ids:{','.join(args.ids)}" if args.ids
                    else (f"tier{args.tier}" if args.tier is not None else "all"))

    run = ExperimentRun("header_control", {
        "provider": args.provider, "model_name": args.model,
        "judge_model": args.judge_model, "corpus": corpus_label,
        "programs": [p.id for p in programs], "seed": args.seed,
        "weights": header_mod.HEADER_WEIGHTS,
    })
    print(f"Experiment {run.id}: header control over {len(programs)} program(s) -> {run.dir}")

    base_consolidate = knowledge_graph.DEFAULT_CONSOLIDATE_PROMPT
    run.save_candidate("header_control", base_consolidate)

    def improve_fn(code):
        return ai_improver.improve_function(
            code, provider=args.provider, model_name=args.model, binary_path=None)

    def consolidate_fn(defs):
        return knowledge_graph.consolidate_definitions(
            defs, provider=args.provider, model_name=args.model,
            base_prompt=base_consolidate)

    judge_fn = make_judge(args.provider, args.judge_model) if args.judge else None

    scores, per_program = [], {}
    for prog in programs:
        print(f"  -> {prog.id} (tier {prog.tier})")
        result = header_mod.run_header_roundtrip(
            prog, improve_fn, consolidate_fn, status=lambda m: print("     " + m))
        if args.judge:
            print("     scoring header (judge LLM)...")
        mr = header_mod.score_header(result, judge_fn=judge_fn)
        # Programs with no structs are skipped from the aggregate (neutral).
        if result.error is None and not header_mod.extract_structs(prog.source):
            print("     (no structs — skipped)")
            continue
        run.log_result("header_control", prog.id, mr.score, mr.components,
                       mr.feedback_text, extra={"error": result.error,
                                                "n_fragments": len(result.fragments)})
        scores.append(mr.score)
        per_program[prog.id] = round(mr.score, 4)
        print(f"     score={mr.score:.3f}  {mr.components}")

    best = round(statistics.mean(scores), 4) if scores else 0.0
    run.finalize({
        "best_score": best, "n_programs": len(scores), "per_program": per_program,
        "takeaway": f"header control mean={best:.3f} over {len(scores)} programs with structs",
    })
    print(f"\nHeader control mean score: {best}\nSaved: {run.dir}")


def _parse_levels(spec: str) -> List[int]:
    """'0,20,80' -> [0, 20, 80]; tolerant of spaces/empties."""
    out = []
    for tok in str(spec).split(","):
        tok = tok.strip()
        if tok:
            out.append(int(tok))
    return out or [0]


def run_rag_context(args) -> None:
    """
    Context-retrieval decision gate (TODO #5). Hold the improver prompt fixed;
    vary ONLY the context strategy. Compares full-dump project.h (c1, padded with
    K distractor types) against the static-deps oracle (c2) and a no-context floor
    (c0), per function on the SAME set (paired). Sweeps the distractor count so we
    can see whether full-dump degrades with context size while the oracle holds.
    No embeddings: c2 is the upper bound a real retriever would chase.
    """
    import hashlib
    import json as _json

    from . import context_retrieval as cr

    random.seed(args.seed)
    programs = load_corpus(tier=args.tier, ids=args.ids)
    examples = build_function_dataset(programs)
    if args.limit:
        examples = examples[: args.limit]
    if not examples:
        print("No function examples matched the filter.")
        return
    pt_by_program = {p.id: cr.program_types(p) for p in programs}
    levels = _parse_levels(args.distractors)
    corpus_label = (f"ids:{','.join(args.ids)}" if args.ids
                    else (f"tier{args.tier}" if args.tier is not None else "all"))

    run = ExperimentRun("rag_context", {
        "provider": args.provider, "model_name": args.model,
        "judge_model": (args.judge_model if args.judge else None),
        "corpus": corpus_label, "n_examples": len(examples),
        "distractor_levels": levels, "seed": args.seed,
        "weights": metrics.DEFAULT_WEIGHTS, "fixed_prompt": "DEFAULT_SYSTEM_PROMPT",
    })
    fixed_prompt = ai_improver.DEFAULT_SYSTEM_PROMPT
    run.save_candidate("fixed_prompt", fixed_prompt)
    print(f"Experiment {run.id}: rag-context over {len(examples)} fns, "
          f"distractor levels {levels} -> {run.dir}")

    judge_fn = make_judge(args.provider, args.judge_model) if args.judge else None

    # Cache improved output by (model, prompt, context, code) so re-runs and the
    # K-independent strategies (c0/c2) are not re-sent to the slow endpoint.
    cache_tag = args.cache_tag or args.model.replace('/', '_')
    cache_path = run.dir.parent / ".cache" / f"rag_{cache_tag}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = _json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}

    def improve(code: str, ctx: str) -> str:
        key = hashlib.sha1(
            (args.model + "\x00" + fixed_prompt + "\x00" + ctx + "\x00" + code)
            .encode("utf-8")).hexdigest()
        if key in cache:
            return cache[key]
        out = ai_improver.improve_function(
            code, provider=args.provider, model_name=args.model,
            binary_path=None, base_prompt=fixed_prompt, extra_context=ctx)
        cache[key] = out
        cache_path.write_text(_json.dumps(cache), encoding="utf-8")
        return out

    # Build the strategy list. c0/c2 are distractor-independent (evaluated once);
    # c1 is rebuilt per K with a fresh deterministic distractor set.
    strategies = [
        ("c0_none", lambda ex, pt: cr.ctx_none(pt, ex)),
        ("c2_static", lambda ex, pt: cr.ctx_static_deps(pt, ex)),
    ]
    for k in levels:
        dts = cr.synth_distractor_types(k, seed=args.seed)
        dss = cr.synth_distractor_sigs(max(0, k // 4), seed=args.seed)
        strategies.append(
            (f"c1_full@k{k}",
             lambda ex, pt, dts=dts, dss=dss: cr.ctx_full_dump(pt, ex, dts, dss)))

    per_strategy: Dict[str, Dict[str, float]] = {}
    chars: Dict[str, List[int]] = {}
    for label, ctx_fn in strategies:
        run.save_candidate(label, label)
        scores, clens = {}, []
        print(f"\n[{label}] over {len(examples)} fn(s)")
        for ex in examples:
            pt = pt_by_program[ex.program_id]
            ctx = ctx_fn(ex, pt)
            clens.append(len(ctx))
            improved = improve(ex.decompiled, ctx)
            mr = metrics.score_pair(ex.original, improved, ex.decompiled, judge_fn)
            key = f"{ex.program_id}:{ex.name}"
            scores[key] = mr.score
            run.log_result(label, key, mr.score, mr.components, mr.feedback_text,
                           extra={"context_chars": len(ctx),
                                  "k": int(label.split("@k")[1]) if "@k" in label else None})
            print(f"  {key:34} score={mr.score:.3f}  ctx={len(ctx):>6}c")
        per_strategy[label] = scores
        chars[label] = clens
        print(f"  {label}: mean={sum(scores.values())/len(scores):.3f}  "
              f"mean_ctx={sum(clens)/len(clens):.0f}c")

    # Report: c2 oracle is the reference. For each c1@K, paired delta vs c2.
    def mean(d):
        return sum(d.values()) / len(d) if d else 0.0

    c2 = per_strategy["c2_static"]
    c2_mean = mean(c2)
    rows = []
    for label, scores in per_strategy.items():
        wins = sum(1 for k in scores if scores[k] > c2.get(k, 0) + 1e-9)
        losses = sum(1 for k in scores if scores[k] < c2.get(k, 0) - 1e-9)
        rows.append({
            "strategy": label, "mean": round(mean(scores), 4),
            "mean_ctx_chars": round(sum(chars[label]) / len(chars[label])),
            "delta_vs_c2": round(mean(scores) - c2_mean, 4),
            "wins_vs_c2": wins, "losses_vs_c2": losses,
        })

    print("\n=== context-retrieval bakeoff (paired vs c2 oracle) ===")
    print(f"{'strategy':16} {'mean':>6} {'ctx':>7} {'Δ vs c2':>8} {'w/l':>7}")
    for r in rows:
        print(f"{r['strategy']:16} {r['mean']:.3f} {r['mean_ctx_chars']:>6}c "
              f"{r['delta_vs_c2']:+.3f}  {r['wins_vs_c2']}/{r['losses_vs_c2']}")

    c1_rows = [r for r in rows if r["strategy"].startswith("c1_full")]
    degraded = (len(c1_rows) >= 2
                and c1_rows[-1]["mean"] < c1_rows[0]["mean"] - 0.02)
    run.finalize({
        "best_score": round(c2_mean, 4),
        "c2_oracle_mean": round(c2_mean, 4),
        "n_examples": len(examples), "n_programs": len({e.program_id for e in examples}),
        "distractor_levels": levels, "table": rows,
        "c1_degrades_with_distractors": bool(degraded),
        "takeaway": (f"c2 oracle={c2_mean:.3f}; "
                     + "; ".join(f"{r['strategy']}={r['mean']:.3f}" for r in c1_rows)
                     + (" (c1 degrades with K -> retrieval has headroom)" if degraded
                        else " (c1 flat across K -> model ignores distractors)")),
    })
    print(f"\nSaved: {run.dir}")


_PROVIDERS = {"local", "gemini"}


def _parse_model_spec(spec: str, default_provider: str):
    """'local:openai/gpt-oss-20b' -> ('local','openai/gpt-oss-20b'); bare model
    id -> (default_provider, spec). Only splits on a leading known provider so
    model ids containing ':' aren't mangled."""
    spec = spec.strip()
    if ":" in spec:
        head, rest = spec.split(":", 1)
        if head in _PROVIDERS:
            return head, rest
    return default_provider, spec


def run_model_sweep(args) -> None:
    """
    Whole-chain model sweep: drive the full chain (improve -> synth header ->
    contextual refine) with each task model, score the FINAL refined output (and
    stage 1, for the delta) against the original with a FIXED judge. Answers
    "which model is best end-to-end". Expensive — ~(2*fns + 1) task calls + judge
    per program per model per repeat; use --ids/--limit and the cache.
    """
    import hashlib
    import json as _json
    import time

    from cpp_re_agent import ai_improver, contextual_improver, knowledge_graph
    from . import chain as chain_mod
    from . import header as header_mod
    from . import roundtrip
    from .dataset import _original_bodies

    random.seed(args.seed)
    programs = load_corpus(tier=args.tier, ids=args.ids)
    if args.limit:
        programs = programs[: args.limit]
    if not programs:
        print("No corpus programs matched the filter.")
        return

    models = ([_parse_model_spec(s, args.provider) for s in args.models.split(",") if s.strip()]
              if args.models else [(args.provider, args.model)])
    judge_provider, judge_model = _parse_model_spec(args.judge_model, args.provider)
    corpus_label = (f"ids:{','.join(args.ids)}" if args.ids
                    else (f"tier{args.tier}" if args.tier is not None else "all"))

    run = ExperimentRun("model_sweep", {
        "models": [f"{p}:{m}" for p, m in models],
        "judge": f"{judge_provider}:{judge_model}" if args.judge else None,
        "corpus": corpus_label, "programs": [p.id for p in programs],
        "repeats": args.repeats, "seed": args.seed, "weights": metrics.DEFAULT_WEIGHTS,
    })
    print(f"Experiment {run.id}: model sweep of {len(models)} model(s) over "
          f"{len(programs)} program(s), repeats={args.repeats} -> {run.dir}")
    if args.judge and (judge_provider, judge_model) in models:
        print(f"  NOTE: judge {judge_provider}:{judge_model} is also a candidate — "
              f"watch for self-preference bias in its own row.")

    judge_fn = make_judge(judge_provider, judge_model) if args.judge else None

    cache_tag = args.cache_tag or "sweep"
    cache_path = run.dir.parent / ".cache" / f"chain_{cache_tag}.json"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = _json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    cost = {"calls": 0, "secs": 0.0}  # only counts uncached (real) endpoint calls

    def make_fns(provider, model, rep):
        def cached(role, *parts):
            key = hashlib.sha1(("|".join([provider, model, role, str(rep)]) + "|"
                                + "\x00".join(parts)).encode("utf-8")).hexdigest()
            if key in cache:
                return cache[key]
            t0 = time.time()
            if role == "s1":
                out = ai_improver.improve_function(
                    parts[0], provider=provider, model_name=model, binary_path=None,
                    base_prompt=ai_improver.DEFAULT_SYSTEM_PROMPT)
            elif role == "synth":
                out = knowledge_graph.consolidate_definitions(
                    parts[0], provider=provider, model_name=model)
            else:  # s2 contextual refine
                out = contextual_improver.refine_function(
                    parts[0], parts[1], parts[2], provider=provider, model_name=model)
            cost["calls"] += 1
            cost["secs"] += time.time() - t0
            cache[key] = out
            cache_path.write_text(_json.dumps(cache), encoding="utf-8")
            return out
        return (lambda c: cached("s1", c),
                lambda d: cached("synth", d),
                lambda c, h, k: cached("s2", c, h, k))

    def ms(xs):
        if not xs:
            return 0.0, 0.0
        return statistics.mean(xs), (statistics.stdev(xs) if len(xs) > 1 else 0.0)

    rows = []
    for provider, model in models:
        label = f"{provider}:{model}"
        print(f"\n==== model {label} ====")
        s1_means, chain_means, struct_means = [], [], []
        calls0, secs0 = cost["calls"], cost["secs"]
        for rep in range(args.repeats):
            improve_fn, consolidate_fn, refine_fn = make_fns(provider, model, rep)
            s1_scores, chain_scores, structs = [], [], []
            for prog in programs:
                originals = _original_bodies(prog.source)
                res = chain_mod.run_chain(prog, improve_fn, consolidate_fn, refine_fn,
                                          status=lambda m: print("   " + m))
                if res.error:
                    print(f"   {prog.id}: ERROR {res.error}")
                    continue
                for name, refined_code in res.refined.items():
                    # Identity, not bare name: two same-named methods on
                    # different classes must not be scored against each other.
                    raw_body = res.raw_functions.get(name, "")
                    orig = originals.get(roundtrip.match_key(name, raw_body))
                    if not orig:
                        continue
                    raw = res.raw_functions.get(name, "")
                    s1 = metrics.score_pair(orig, res.improved[name], raw, judge_fn)
                    ch = metrics.score_pair(orig, refined_code, raw, judge_fn)
                    s1_scores.append(s1.score)
                    chain_scores.append(ch.score)
                    run.log_result(label, f"r{rep}:{prog.id}:{name}", ch.score,
                                   ch.components, ch.feedback_text,
                                   extra={"stage1": round(s1.score, 4),
                                          "chain": round(ch.score, 4),
                                          "delta": round(ch.score - s1.score, 4),
                                          "repeat": rep})
                if header_mod.extract_structs(prog.source):
                    structs.append(header_mod.struct_recovery(prog.source, res.project_h).score)
            if s1_scores:
                s1_means.append(statistics.mean(s1_scores))
                chain_means.append(statistics.mean(chain_scores))
                print(f"   repeat {rep}: stage1={statistics.mean(s1_scores):.3f} "
                      f"chain={statistics.mean(chain_scores):.3f}")
            if structs:
                struct_means.append(statistics.mean(structs))
        s1_m, s1_sd = ms(s1_means)
        ch_m, ch_sd = ms(chain_means)
        st_m, _ = ms(struct_means)
        row = {"model": label, "stage1_mean": round(s1_m, 4), "stage1_std": round(s1_sd, 4),
               "chain_mean": round(ch_m, 4), "chain_std": round(ch_sd, 4),
               "delta_chain_minus_stage1": round(ch_m - s1_m, 4),
               "header_struct_mean": round(st_m, 4),
               "llm_calls": cost["calls"] - calls0, "llm_secs": round(cost["secs"] - secs0, 1)}
        rows.append(row)
        print(f"   {label}: chain={ch_m:.3f}±{ch_sd:.3f} "
              f"(stage1={s1_m:.3f}±{s1_sd:.3f}, Δ{ch_m - s1_m:+.3f}) "
              f"struct={st_m:.3f} calls={row['llm_calls']} {row['llm_secs']}s")

    rows.sort(key=lambda r: r["chain_mean"], reverse=True)
    print("\n=== whole-chain model sweep (sorted by chain mean) ===")
    print(f"{'model':28} {'chain':>13} {'stage1':>13} {'Δs2':>6} {'struct':>6} "
          f"{'calls':>6} {'secs':>8}")
    for r in rows:
        print(f"{r['model']:28} {r['chain_mean']:.3f}±{r['chain_std']:.3f} "
              f"{r['stage1_mean']:.3f}±{r['stage1_std']:.3f} "
              f"{r['delta_chain_minus_stage1']:+.3f} {r['header_struct_mean']:.3f} "
              f"{r['llm_calls']:>6} {r['llm_secs']:>8}")

    winner = rows[0] if rows else {"model": "(none)", "chain_mean": 0.0}
    run.finalize({
        "best_score": winner["chain_mean"], "winner": winner["model"],
        "n_programs": len(programs), "repeats": args.repeats, "table": rows,
        "judge": f"{judge_provider}:{judge_model}" if args.judge else None,
        "takeaway": f"best whole-chain model: {winner['model']} "
                    f"(chain={winner['chain_mean']:.3f}) over {len(programs)} "
                    f"programs, judge={judge_provider}:{judge_model}",
    })
    print(f"\nSaved: {run.dir}")


def main():
    p = argparse.ArgumentParser(description="Decompiled-C++ recovery experiments.")
    p.add_argument("--algo",
                   choices=["control", "gepa", "bakeoff", "header-control",
                            "contextual-control", "rag-context", "model-sweep"],
                   default="control")
    p.add_argument("--distractors", default="0",
                   help="rag-context only: comma list of distractor-type counts "
                        "to pad full-dump project.h with, e.g. '0,20,80'.")
    p.add_argument("--models", default=None,
                   help="model-sweep only: comma list of task models as "
                        "provider:model (e.g. 'local:openai/gpt-oss-20b,"
                        "gemini:gemini-2.5-flash'); defaults to --provider/--model.")
    p.add_argument("--repeats", type=int, default=1,
                   help="repeat each condition N times for mean +/- std "
                        "(endpoint sampling is noisy; used by model-sweep).")
    p.add_argument("--tier", type=int, default=None, help="Filter corpus to one tier.")
    p.add_argument("--ids", nargs="*", default=None, help="Explicit program ids.")
    p.add_argument("--limit", type=int, default=None, help="Cap number of programs.")
    p.add_argument("--provider", default="local")
    p.add_argument("--model", default="openai/gpt-oss-20b")
    p.add_argument("--judge", dest="judge", action="store_true", default=True)
    p.add_argument("--no-judge", dest="judge", action="store_false")
    p.add_argument("--judge-model", default="openai/gpt-oss-20b")
    p.add_argument("--reflect-model", default="openai/gpt-oss-20b",
                   help="Model that proposes prompt mutations (GEPA reflection).")
    p.add_argument("--iterations", type=int, default=5)
    p.add_argument("--candidates", type=int, default=2, help="Mutations per iteration.")
    p.add_argument("--minibatch", type=int, default=0,
                   help="Train examples used for reflection feedback (0=all). "
                        "Set small on slow endpoints.")
    p.add_argument("--no-seed-variants", dest="seed_variants", action="store_false",
                   default=True, help="Disable the hand-written GEPA seed variants.")
    p.add_argument("--val-frac", type=float, default=0.4, help="Val fraction of train+val.")
    p.add_argument("--test-frac", type=float, default=0.3, help="Held-out test fraction.")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--cache-tag", default=None,
                   help="Name the eval cache file eval_<tag>.json instead of by "
                        "model id — use to isolate a run from a concurrent one.")
    args = p.parse_args()

    # Echo the resolved config up front so a run is self-documenting in the log
    # (which endpoint, which models, which corpus filter, key reliability knobs).
    corpus_label = (f"ids={','.join(args.ids)}" if args.ids
                    else (f"tier{args.tier}" if args.tier is not None else "all"))
    print("=" * 70)
    print(f"algo={args.algo}  provider={args.provider}  model={args.model}")
    print(f"judge={'on:' + args.judge_model if args.judge else 'off'}  "
          f"corpus={corpus_label}  limit={args.limit}  seed={args.seed}")
    if args.provider == "local":
        print(f"endpoint={os.getenv('LOCAL_LLM_URL', 'http://localhost:1234/v1')}")
    print(f"LLM_TIMEOUT={os.getenv('LLM_TIMEOUT', '300')}s  "
          f"DISABLE_LANGFUSE={os.getenv('DISABLE_LANGFUSE')}")
    print("=" * 70)

    if args.algo == "control":
        run_control(args)
    elif args.algo == "bakeoff":
        run_bakeoff(args)
    elif args.algo == "header-control":
        run_header_control(args)
    elif args.algo == "contextual-control":
        run_contextual_control(args)
    elif args.algo == "rag-context":
        run_rag_context(args)
    elif args.algo == "model-sweep":
        run_model_sweep(args)
    else:
        run_gepa(args)


if __name__ == "__main__":
    main()
