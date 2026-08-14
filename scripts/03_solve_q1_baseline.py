from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import DEFAULT_CONFIG_PATH
from src.io_utils import sha256, write_csv, write_json
from src.solver import (
    SolverCache,
    export_q1_solution,
    improve_q1_batch_relocation,
    improve_q1_route_ejection,
    improve_q1_savings,
    load_problem_data,
    load_q1_solution,
    solve_q1_baseline,
)
from src.solver.models import SolverConfig
from src.validation import validate_solution


def _git_commit() -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def _git_dirty() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode != 0 or bool(result.stdout.strip())


def _comparison_key(metrics: dict[str, object]) -> tuple[float, ...]:
    return (
        float(metrics["total_aircraft_time_minutes"]),
        float(metrics["total_passenger_travel_time_minutes"]),
        float(metrics["total_flights"]),
        float(metrics["total_fuel_consumption_kg"]),
        -float(metrics["seat_utilization"]),
    )


def _promote_if_best(run_dir: Path, output_root: Path, metrics: dict[str, object]) -> bool:
    best_dir = output_root / "best"
    best_metrics_path = best_dir / "metrics.json"
    if best_metrics_path.exists():
        previous = json.loads(best_metrics_path.read_text(encoding="utf-8"))["validator_metrics"]
        if _comparison_key(previous) <= _comparison_key(metrics):
            return False
    best_dir.mkdir(parents=True, exist_ok=True)
    for name in ("q1-routes.csv", "q1-assignments.csv", "metrics.json", "validator.json", "run_config.json"):
        shutil.copy2(run_dir / name, best_dir / name)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="构造、导出并验证 Q1 B0 安全基线")
    parser.add_argument("--run-id", help="固定实验编号；默认使用时间戳")
    parser.add_argument("--output-root", type=Path, default=ROOT / "outputs" / "q1")
    parser.add_argument("--promote", action="store_true", help="若优于当前 best，则提升为最佳合法方案")
    parser.add_argument("--start-best", action="store_true", help="从 outputs/q1/best 恢复方案，跳过 B0 重建")
    parser.add_argument("--savings", action="store_true", help="在 B0 后运行确定性 Generalized Savings")
    parser.add_argument("--relocate", action="store_true", help="在 Savings 后运行批量重分配局部搜索")
    parser.add_argument("--ejection", action="store_true", help="运行双目标路线吸收的 LAND ejection 链")
    parser.add_argument("--max-neighbors", type=int, default=8, help="每条路线最多精确评价的相邻路线数")
    parser.add_argument("--max-relocation-targets", type=int, default=4)
    parser.add_argument("--max-relocation-iterations", type=int, default=30)
    parser.add_argument("--max-ejection-targets", type=int, default=6)
    parser.add_argument("--max-ejection-iterations", type=int, default=15)
    args = parser.parse_args()

    run_id = args.run_id or datetime.now().strftime("%Y%m%d-%H%M%S") + "-b0"
    run_dir = args.output_root / "runs" / run_id
    if run_dir.exists():
        raise FileExistsError(f"运行目录已存在：{run_dir}")
    run_dir.mkdir(parents=True)

    started = time.perf_counter()
    data = load_problem_data()
    solver_config = SolverConfig(seed=0)
    # One shared run-scoped cache so static route physics computed in any
    # stage (baseline / Savings / relocation / ejection) is reused everywhere.
    cache = SolverCache(data)
    if args.start_best:
        solution = load_q1_solution(
            args.output_root / "best" / "q1-routes.csv",
            args.output_root / "best" / "q1-assignments.csv",
            data,
            method="q1_resumed_best",
        )
    else:
        solution = solve_q1_baseline(data, solver_config, cache=cache)
    ran_savings = args.savings or (not args.start_best and (args.relocate or args.ejection))
    if ran_savings:
        solution = improve_q1_savings(
            solution,
            data,
            solver_config,
            max_neighbors=args.max_neighbors,
            cache=cache,
        )
    if args.relocate:
        solution = improve_q1_batch_relocation(
            solution,
            data,
            solver_config,
            max_targets_per_batch=args.max_relocation_targets,
            max_iterations=args.max_relocation_iterations,
            cache=cache,
        )
    if args.ejection:
        solution = improve_q1_route_ejection(
            solution,
            data,
            solver_config,
            max_targets=args.max_ejection_targets,
            max_iterations=args.max_ejection_iterations,
            cache=cache,
        )
    solve_seconds = time.perf_counter() - started
    export_q1_solution(
        solution,
        run_dir / "q1-routes.csv",
        run_dir / "q1-assignments.csv",
    )
    validation = validate_solution(
        "q1",
        run_dir / "q1-routes.csv",
        run_dir / "q1-assignments.csv",
        data_dir=ROOT / "data" / "raw",
        config=data.config,
    )
    elapsed_seconds = time.perf_counter() - started
    validator_metrics = validation.metrics.to_dict() if validation.metrics else None
    internal_metrics = solution.metrics.to_dict()
    metrics_match = bool(
        validator_metrics
        and all(
            abs(float(validator_metrics[key]) - float(internal_metrics[key])) <= 1e-6
            for key in internal_metrics
        )
    )
    gate_pass = bool(
        validation.valid
        and validator_metrics
        and validator_metrics["served_passengers"] == data.q1_passenger_count
        and metrics_match
    )

    run_config = {
        "run_id": run_id,
        "method": solution.method,
        "git_commit": _git_commit(),
        "git_dirty": _git_dirty(),
        "problem_config_sha256": sha256(DEFAULT_CONFIG_PATH),
        "seed": solver_config.seed,
        "secondary_order": list(solver_config.secondary_order),
        "savings": ran_savings,
        "start_best": args.start_best,
        "relocate": args.relocate,
        "ejection": args.ejection,
        "max_neighbors": args.max_neighbors,
        "max_relocation_targets": args.max_relocation_targets,
        "max_relocation_iterations": args.max_relocation_iterations,
        "max_ejection_targets": args.max_ejection_targets,
        "max_ejection_iterations": args.max_ejection_iterations,
        "deterministic": True,
        "passenger_count": data.q1_passenger_count,
        "solve_seconds": round(solve_seconds, 6),
        "elapsed_seconds": round(elapsed_seconds, 6),
        "performance": cache.stats(),
        "diagnostics": solution.diagnostics,
    }
    write_json(run_dir / "run_config.json", run_config)
    write_json(run_dir / "validator.json", validation.to_dict())
    write_json(
        run_dir / "metrics.json",
        {
            "gate_pass": gate_pass,
            "metrics_match": metrics_match,
            "internal_metrics": internal_metrics,
            "validator_metrics": validator_metrics,
            "comparison_key": list(_comparison_key(validator_metrics)) if validator_metrics else None,
        },
    )
    savings_stats = solution.diagnostics.get("generalized_savings", {})
    relocation_stats = solution.diagnostics.get("batch_relocation", {})
    ejection_stats = solution.diagnostics.get("route_ejection", {})
    operator_rows = []
    if ran_savings:
        operator_rows.append(
            {
                "operator": "generalized_savings_merge",
                "calls": savings_stats.get("evaluated_pairs", 0),
                "accepted": savings_stats.get("accepted_merges", 0),
                "improved": savings_stats.get("accepted_merges", 0),
                "total_improvement_minutes": savings_stats.get("primary_improvement_minutes", 0),
            }
        )
    if args.relocate:
        operator_rows.append(
            {
                "operator": "batch_relocation_rebuild",
                "calls": relocation_stats.get("candidate_moves", 0),
                "accepted": relocation_stats.get("accepted_moves", 0),
                "improved": relocation_stats.get("accepted_moves", 0),
                "total_improvement_minutes": relocation_stats.get("primary_improvement_minutes", 0),
            }
        )
    if args.ejection:
        operator_rows.append(
            {
                "operator": "land_route_ejection_chain",
                "calls": ejection_stats.get("candidate_chains", 0),
                "accepted": ejection_stats.get("accepted_chains", 0),
                "improved": ejection_stats.get("accepted_chains", 0),
                "total_improvement_minutes": ejection_stats.get("primary_improvement_minutes", 0),
            }
        )
    write_csv(
        run_dir / "operator_stats.csv",
        ["operator", "calls", "accepted", "improved", "total_improvement_minutes"],
        operator_rows,
    )
    (run_dir / "run.log").write_text(
        "\n".join(
            [
                f"运行编号: {run_id}",
                f"方法: {solution.method}",
                f"路线数: {len(solution.routes)}",
                f"服务人数: {solution.metrics.served_passengers}",
                f"Validator: {'PASS' if validation.valid else 'FAIL'}",
                f"指标一致: {metrics_match}",
                f"阶段门: {'PASS' if gate_pass else 'FAIL'}",
                f"总耗时秒: {elapsed_seconds:.3f}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    promoted = args.promote and gate_pass and _promote_if_best(run_dir, args.output_root, validator_metrics)
    print(f"运行目录：{run_dir}")
    print(f"Q1 阶段门：{'PASS' if gate_pass else 'FAIL'}")
    print(f"Validator：{'PASS' if validation.valid else 'FAIL'}")
    print(f"服务人数：{solution.metrics.served_passengers}/{data.q1_passenger_count}")
    print(f"五项指标：{validator_metrics}")
    print(f"是否提升 best：{promoted}")
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
