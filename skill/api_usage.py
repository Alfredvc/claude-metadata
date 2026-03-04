#!/usr/bin/env python3
"""
Claude API usage breakdown: token counts and costs by model and token type.
Pricing sourced from https://docs.anthropic.com/en/about-claude/pricing (March 2026).

Cache write TTL rates:
  5-minute TTL: 1.25x base input rate
  1-hour TTL:   2.00x base input rate  (Claude Code always uses 1h TTL)

Cache read rate: 0.10x base input rate (all models).

Long context pricing (>200K effective input tokens per call):
  Applies to: Opus 4.6, Opus 4.5, Sonnet (all versions)
  Input and cache tokens: 2x normal rate
  Output tokens: 1.5x normal rate
"""

import argparse, json, os, glob, re, sys, multiprocessing
from collections import defaultdict
from datetime import datetime, timezone

# ── Pricing ($ per million tokens) ──────────────────────────────────────────
# Source: https://platform.claude.com/docs/en/about-claude/pricing
# Keyed by the substring pattern that uniquely identifies the model version.
# More-specific patterns are checked first to avoid misclassification.
#
#   cache_write_5m = 5-minute TTL write (1.25x input)
#   cache_write_1h = 1-hour TTL write   (2.00x input)
#   cache_read     = cache hit/refresh  (0.10x input)
#   lc             = supports long-context pricing (>200K effective input)
#
PRICING_TABLE = [
    # ── Opus 4.5 / 4.6 (repriced vs Opus 4 / 4.1) ──────────────────────────
    ("opus-4-6",  {"input":  5.00, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50, "output": 25.00, "lc": True}),
    ("opus-4-5",  {"input":  5.00, "cache_write_5m":  6.25, "cache_write_1h": 10.00, "cache_read": 0.50, "output": 25.00, "lc": True}),
    # ── Opus 4.1 / 4 (legacy pricing, no LC pricing) ─────────────────────────
    ("opus-4-1",  {"input": 15.00, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50, "output": 75.00}),
    ("opus-4",    {"input": 15.00, "cache_write_5m": 18.75, "cache_write_1h": 30.00, "cache_read": 1.50, "output": 75.00}),
    # ── Sonnet (all versions same price) ────────────────────────────────────
    ("sonnet",    {"input":  3.00, "cache_write_5m":  3.75, "cache_write_1h":  6.00, "cache_read": 0.30, "output": 15.00, "lc": True}),
    # ── Haiku 4.5 / 4 ───────────────────────────────────────────────────────
    ("haiku-4-5", {"input":  1.00, "cache_write_5m":  1.25, "cache_write_1h":  2.00, "cache_read": 0.10, "output":  5.00}),
    ("haiku-4",   {"input":  1.00, "cache_write_5m":  1.25, "cache_write_1h":  2.00, "cache_read": 0.10, "output":  5.00}),
    # ── Haiku 3.5 ───────────────────────────────────────────────────────────
    ("haiku-3-5", {"input":  0.80, "cache_write_5m":  1.00, "cache_write_1h":  1.60, "cache_read": 0.08, "output":  4.00}),
    # ── Haiku 3 — model ID is claude-3-haiku-20240307 (note: "3-haiku" order)
    ("3-haiku",   {"input":  0.25, "cache_write_5m":  0.31, "cache_write_1h":  0.50, "cache_read": 0.03, "output":  1.25}),
    ("haiku",     {"input":  0.25, "cache_write_5m":  0.31, "cache_write_1h":  0.50, "cache_read": 0.03, "output":  1.25}),
]

LC_THRESHOLD = 200_000  # effective input tokens per call

_warned_unknown = set()

def get_pricing(model: str) -> dict:
    m = model.lower()
    for pattern, prices in PRICING_TABLE:
        if pattern in m:
            return prices
    # Unknown model — warn once and fall back to Sonnet rates
    if model not in _warned_unknown:
        print(f"WARNING: unknown model '{model}', falling back to Sonnet pricing", file=sys.stderr)
        _warned_unknown.add(model)
    return {"input": 3.00, "cache_write_5m": 3.75, "cache_write_1h": 6.00, "cache_read": 0.30, "output": 15.00, "lc": True}

def cost(tokens: int, rate: float) -> float:
    return tokens / 1_000_000 * rate

# ── Per-file worker (module level so multiprocessing can pickle it) ──────────
_ts_re = re.compile(r'"timestamp"\s*:\s*"([^"]+)"')

def process_file(f: str) -> tuple[dict, datetime | None, datetime | None]:
    """Parse one JSONL file; return (per-msg_id entry dict, ts_first, ts_last).

    Each API call may produce multiple log entries sharing the same message id:
      • Streaming chunks (stop_reason=None) logged at each content-block boundary.
      • One final entry (stop_reason != None) with complete cumulative token counts.
      • Interrupted calls produce only streaming chunks (no final ever written).

    We return one "best" entry per message id so the caller can deduplicate across
    files before aggregating: a final always beats a streaming chunk; among streaming-
    only entries we keep the one with the highest output_tokens (closest to what the
    API billed before the stream was interrupted).

    Entries without a message id are rare legacy entries; if they have a stop_reason
    they are counted as-is under a unique synthetic key so they are never merged.
    """
    entries: dict[str, dict] = {}   # msg_id (or synthetic key) -> best entry
    ts_first: datetime | None = None
    ts_last:  datetime | None = None
    _no_id_counter = 0

    try:
        with open(f) as fh:
            for line in fh:
                if '"assistant"' not in line or '"usage"' not in line:
                    m = _ts_re.search(line)
                    if m:
                        try:
                            ts = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
                            if ts_first is None or ts < ts_first:
                                ts_first = ts
                            if ts_last is None or ts > ts_last:
                                ts_last = ts
                        except ValueError:
                            pass
                    continue

                try:
                    obj = json.loads(line)
                except Exception:
                    continue

                raw_ts = obj.get("timestamp")
                if raw_ts:
                    try:
                        ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
                        if ts_first is None or ts < ts_first:
                            ts_first = ts
                        if ts_last is None or ts > ts_last:
                            ts_last = ts
                    except ValueError:
                        pass

                msg = obj.get("message", {})
                if not msg or msg.get("role") != "assistant":
                    continue
                model = msg.get("model", "unknown")
                if model == "<synthetic>":
                    continue
                usage = msg.get("usage", {})
                if not usage:
                    continue

                stop = msg.get("stop_reason")
                msg_id = msg.get("id")

                # Entries without a message id: only count finals (can't deduplicate
                # streaming chunks without an id, and they're likely rare edge cases).
                if not msg_id:
                    if stop is None:
                        continue
                    _no_id_counter += 1
                    msg_id = f"__no_id_{f}_{_no_id_counter}"

                cc = usage.get("cache_creation", {})
                candidate = {
                    "model":          model,
                    "stop":           stop,
                    "input":          usage.get("input_tokens", 0),
                    "cache_write_5m": cc.get("ephemeral_5m_input_tokens", 0) if cc else 0,
                    "cache_write_1h": cc.get("ephemeral_1h_input_tokens", 0) if cc
                                      else usage.get("cache_creation_input_tokens", 0),
                    "cache_read":     usage.get("cache_read_input_tokens", 0),
                    "output":         usage.get("output_tokens", 0),
                }

                existing = entries.get(msg_id)
                if existing is None:
                    entries[msg_id] = candidate
                elif stop is not None and existing["stop"] is None:
                    # Final entry always beats a streaming chunk.
                    entries[msg_id] = candidate
                elif stop is None and existing["stop"] is None:
                    # Both streaming: keep whichever has more output (further into stream).
                    if candidate["output"] > existing["output"]:
                        entries[msg_id] = candidate
                # If existing is already a final (stop != None), don't overwrite.

    except Exception:
        pass

    return entries, ts_first, ts_last

# ── Formatting helpers ───────────────────────────────────────────────────────
def fmt_t(n):   return f"{n:>16,}"
def fmt_c(n):   return f"${n:>10,.2f}"
def sep(w=100): return "─" * w

TOKEN_TYPES = [
    ("input",          "Input (uncached)"),
    ("cache_write_5m", "Cache write (5m)"),
    ("cache_write_1h", "Cache write (1h)"),
    ("cache_read",     "Cache read"),
    ("output",         "Output"),
]

# ── Main ─────────────────────────────────────────────────────────────────────
# Guard required on macOS: multiprocessing uses 'spawn', which re-imports this
# module in each worker. Without the guard the top-level code would re-run.
if __name__ == "__main__":
    # ── Resolve project directory ────────────────────────────────────────────
    parser = argparse.ArgumentParser(
        description="Claude API usage breakdown by model and token type.",
    )
    parser.add_argument(
        "project_dir",
        nargs="?",
        default=os.getcwd(),
        help="Path to the project directory (default: current working directory)",
    )
    parser.add_argument(
        "--no-subdirs",
        action="store_true",
        help="Exclude sub-directories (worktrees, mobile/, etc.) from the scan",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Scan ALL project directories (ignores project_dir and --no-subdirs)",
    )
    args = parser.parse_args()

    claude_dir = os.path.expanduser("~/.claude")
    if not os.path.isdir(claude_dir):
        print(f"Error: Claude directory not found at {claude_dir}", file=sys.stderr)
        print("Please install Claude Code first: https://claude.ai/download", file=sys.stderr)
        sys.exit(1)

    projects_root = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_root):
        print(f"Error: No Claude project data found at {projects_root}", file=sys.stderr)
        print("Claude Code must be run at least once before usage data is available.", file=sys.stderr)
        sys.exit(1)

    if args.all:
        candidate_dirs = sorted(os.listdir(projects_root))
        matched_dirs = [
            os.path.join(projects_root, d)
            for d in candidate_dirs
            if os.path.isdir(os.path.join(projects_root, d))
        ]
        project_dir = None
        project_key = None
    else:
        project_dir = os.path.realpath(args.project_dir)
        project_key = re.sub(r"[^a-zA-Z0-9]", "-", project_dir)

        candidate_dirs = sorted(os.listdir(projects_root))
        matched_dirs = []
        for d in candidate_dirs:
            if d == project_key:
                matched_dirs.append(os.path.join(projects_root, d))
            elif not args.no_subdirs and d.startswith(project_key + "-"):
                matched_dirs.append(os.path.join(projects_root, d))

    if not matched_dirs:
        if args.all:
            print(f"No Claude project data found in: {projects_root}", file=sys.stderr)
        else:
            print(f"No Claude project data found for: {project_dir}", file=sys.stderr)
            print(f"Expected key: {project_key}", file=sys.stderr)
        sys.exit(1)

    files = sorted(f for d in matched_dirs for f in glob.glob(os.path.join(d, "*.jsonl")))

    # ── Accumulate data in parallel ──────────────────────────────────────────
    # Phase 1: collect per-msg_id best entries across all files.
    # Deduplication is global: if the same msg_id appears in multiple files
    # (e.g. worktree sub-agents), we still count it exactly once.
    all_entries: dict[str, dict] = {}
    ts_first: datetime | None = None
    ts_last:  datetime | None = None

    with multiprocessing.Pool() as pool:
        for file_entries, f, l in pool.imap_unordered(process_file, files, chunksize=8):
            for msg_id, candidate in file_entries.items():
                existing = all_entries.get(msg_id)
                if existing is None:
                    all_entries[msg_id] = candidate
                elif candidate["stop"] is not None and existing["stop"] is None:
                    all_entries[msg_id] = candidate   # final beats streaming
                elif candidate["stop"] is None and existing["stop"] is None:
                    if candidate["output"] > existing["output"]:
                        all_entries[msg_id] = candidate  # keep furthest-along stream
            if f is not None:
                if ts_first is None or f < ts_first:
                    ts_first = f
            if l is not None:
                if ts_last is None or l > ts_last:
                    ts_last = l

    # Phase 2: aggregate deduplicated entries by model, computing costs per-call
    # so that long-context pricing (>200K effective input) is applied accurately.
    data: dict[str, dict] = defaultdict(lambda: {
        "calls": 0, "lc_calls": 0,
        "input": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0, "output": 0,
        "cost_input": 0.0, "cost_cache_write_5m": 0.0, "cost_cache_write_1h": 0.0,
        "cost_cache_read": 0.0, "cost_output": 0.0,
        "no_cache_cost": 0.0,
    })
    for entry in all_entries.values():
        model = entry["model"]
        d = data[model]
        p = get_pricing(model)

        d["calls"] += 1
        for k in ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output"):
            d[k] += entry[k]

        # Long context: input + all cache variants > 200K triggers 2x/1.5x pricing.
        total_input = entry["input"] + entry["cache_write_5m"] + entry["cache_write_1h"] + entry["cache_read"]
        if p.get("lc") and total_input > LC_THRESHOLD:
            d["lc_calls"] += 1
            mul_in, mul_out = 2.0, 1.5
        else:
            mul_in, mul_out = 1.0, 1.0

        d["cost_input"]          += cost(entry["input"],          p["input"]          * mul_in)
        d["cost_cache_write_5m"] += cost(entry["cache_write_5m"], p["cache_write_5m"] * mul_in)
        d["cost_cache_write_1h"] += cost(entry["cache_write_1h"], p["cache_write_1h"] * mul_in)
        d["cost_cache_read"]     += cost(entry["cache_read"],     p["cache_read"]     * mul_in)
        d["cost_output"]         += cost(entry["output"],         p["output"]         * mul_out)

        # Hypothetical no-cache cost: all input billed at input rate (LC-aware).
        d["no_cache_cost"] += cost(total_input, p["input"] * mul_in) + cost(entry["output"], p["output"] * mul_out)

    # ── Print ────────────────────────────────────────────────────────────────
    print()
    print("═" * 104)
    print("  CLAUDE API USAGE BREAKDOWN")
    if args.all:
        print(f"  Scope: ALL projects ({len(matched_dirs)} directories)")
    else:
        print(f"  Project: {project_dir}")
        subdirs = [d for d in matched_dirs if os.path.basename(d) != project_key]
        if subdirs:
            labels = [os.path.basename(d)[len(project_key):].lstrip("-") for d in subdirs]
            print(f"  Including sub-dirs: {', '.join(labels)}")
    print("═" * 104)

    models = sorted(data.keys())

    grand = {k: 0 for k in ["calls", "lc_calls", "input", "cache_write_5m", "cache_write_1h", "cache_read", "output"]}
    grand_cost = 0.0

    for model in models:
        d     = data[model]
        calls = d["calls"]
        lc_calls = d["lc_calls"]

        effective_input = d["input"] + d["cache_write_5m"] + d["cache_write_1h"] + d["cache_read"]

        costs = {
            "input":          d["cost_input"],
            "cache_write_5m": d["cost_cache_write_5m"],
            "cache_write_1h": d["cost_cache_write_1h"],
            "cache_read":     d["cost_cache_read"],
            "output":         d["cost_output"],
        }
        model_total_cost = sum(costs.values())

        for k in ["input", "cache_write_5m", "cache_write_1h", "cache_read", "output"]:
            grand[k] += d[k]
        grand["calls"]    += calls
        grand["lc_calls"] += lc_calls
        grand_cost += model_total_cost

        p = get_pricing(model)

        print()
        print(f"  MODEL: {model}")
        lc_note = f"  ({lc_calls:,} long-context calls >200K tokens)" if lc_calls else ""
        print(f"  API calls: {calls:,}{lc_note}")
        print()
        print(f"  {'Token type':<24} {'Tokens':>16}  {'Rate ($/M)':>12}  {'Cost':>12}")
        print(f"  {sep(68)}")

        for key, label in TOKEN_TYPES:
            tokens = d[key]
            c = costs[key]
            # Effective blended rate (accounts for mix of LC and standard calls)
            eff_rate = c / tokens * 1_000_000 if tokens else p[key]
            print(f"  {label:<24} {fmt_t(tokens)}  {eff_rate:>12.2f}  {fmt_c(c)}")

        print(f"  {sep(68)}")
        print(f"  {'Effective input total':<24} {fmt_t(effective_input)}  {'':>12}  {'':>12}")
        print(f"  {'Model total cost':<24} {'':>16}  {'':>12}  {fmt_c(model_total_cost)}")

    # ── Grand totals ─────────────────────────────────────────────────────────
    print()
    print("═" * 104)
    print("  GRAND TOTALS (all models)")
    print("═" * 104)
    print()
    lc_grand_note = f"  ({grand['lc_calls']:,} long-context calls >200K tokens)" if grand["lc_calls"] else ""
    print(f"  Total API calls: {grand['calls']:,}{lc_grand_note}")
    print()
    print(f"  {'Token type':<24} {'Tokens':>16}  {'Cost':>12}")
    print(f"  {sep(56)}")

    grand_by_type_cost = {}
    for key, label in TOKEN_TYPES:
        type_cost = sum(data[m][f"cost_{key}"] for m in models)
        grand_by_type_cost[key] = type_cost
        print(f"  {label:<24} {fmt_t(grand[key])}  {fmt_c(type_cost)}")

    print(f"  {sep(56)}")
    effective_input_grand = grand["input"] + grand["cache_write_5m"] + grand["cache_write_1h"] + grand["cache_read"]
    all_tokens = effective_input_grand + grand["output"]
    print(f"  {'Effective input total':<24} {fmt_t(effective_input_grand)}  {'':>12}")
    print(f"  {'All tokens':<24} {fmt_t(all_tokens)}  {'':>12}")
    print(f"  {'TOTAL COST':<24} {'':>16}  {fmt_c(grand_cost)}")

    # ── Cost matrix ───────────────────────────────────────────────────────────
    print()
    print("═" * 104)
    print("  COST MATRIX ($ by model and token type)")
    print("═" * 104)
    print()
    col = 16
    header = f"  {'Model':<30}"
    for _, label in TOKEN_TYPES:
        header += f"  {label[:col]:>{col}}"
    header += f"  {'TOTAL':>{col}}"
    print(header)
    print(f"  {sep(30 + (col + 2) * 6 + 2)}")

    for model in models:
        d = data[model]
        costs = {
            "input":          d["cost_input"],
            "cache_write_5m": d["cost_cache_write_5m"],
            "cache_write_1h": d["cost_cache_write_1h"],
            "cache_read":     d["cost_cache_read"],
            "output":         d["cost_output"],
        }
        total = sum(costs.values())
        row = f"  {model:<30}"
        for key, _ in TOKEN_TYPES:
            row += f"  ${costs[key]:>{col-1},.2f}"
        row += f"  ${total:>{col-1},.2f}"
        print(row)

    print(f"  {sep(30 + (col + 2) * 6 + 2)}")
    totals_row = f"  {'TOTAL':<30}"
    for key, _ in TOKEN_TYPES:
        totals_row += f"  ${grand_by_type_cost[key]:>{col-1},.2f}"
    totals_row += f"  ${grand_cost:>{col-1},.2f}"
    print(totals_row)

    # ── Cache efficiency ──────────────────────────────────────────────────────
    print()
    print("═" * 104)
    print("  CACHE EFFICIENCY")
    print("═" * 104)
    print()
    total_input_all = grand["input"] + grand["cache_write_5m"] + grand["cache_write_1h"] + grand["cache_read"]
    hit_rate = grand["cache_read"] / total_input_all * 100 if total_input_all else 0
    no_cache_cost = sum(data[m]["no_cache_cost"] for m in models)
    savings = no_cache_cost - grand_cost
    print(f"  Cache hit rate:         {hit_rate:.1f}% of all input tokens served from cache")
    print(f"  Cost without caching:  {fmt_c(no_cache_cost)}")
    print(f"  Actual cost:           {fmt_c(grand_cost)}")
    print(f"  Saved by caching:      {fmt_c(savings)}")
    if grand_cost:
        print(f"  Cache multiplier:       {no_cache_cost / grand_cost:.1f}x cheaper with cache")

    # ── Time summary ──────────────────────────────────────────────────────────
    print()
    print("═" * 104)
    print("  TIME SUMMARY")
    print("═" * 104)
    print()
    if ts_first and ts_last:
        duration = ts_last - ts_first
        days = duration.total_seconds() / 86_400
        months = days / 30.44  # average days per month

        print(f"  First event:  {ts_first.strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  Last event:   {ts_last.strftime('%Y-%m-%d %H:%M UTC')}")

        if days < 1:
            duration_str = f"{duration.total_seconds() / 3600:.1f} hours"
        elif days < 14:
            duration_str = f"{days:.1f} days"
        else:
            duration_str = f"{months:.1f} months ({int(days)} days)"
        print(f"  Duration:     {duration_str}")
        print()

        if months >= 0.1:
            cost_per_month = grand_cost / months
            calls_per_month = grand["calls"] / months
            print(f"  Avg cost / month:   {fmt_c(cost_per_month).strip()}")
            print(f"  Avg calls / month:  {calls_per_month:,.0f}")
            if months > 1:
                # Show projected monthly costs per model
                print()
                print(f"  {'Model':<40} {'$/month':>12}")
                print(f"  {sep(54)}")
                for model in models:
                    d = data[model]
                    model_cost = (
                        d["cost_input"] + d["cost_cache_write_5m"] + d["cost_cache_write_1h"]
                        + d["cost_cache_read"] + d["cost_output"]
                    )
                    print(f"  {model:<40} ${model_cost / months:>11,.2f}")
    else:
        print("  No timestamp data found.")
    print()
