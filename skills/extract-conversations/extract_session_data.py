#!/usr/bin/env python3
"""
Extract structured session metadata from Claude Code conversation logs.

Produces JSON output for process improvement analysis: conversations → sessions
→ turns, with tool usage, skill tracking, error signals, token counts, and
inlined subagent metadata. No assistant response text is included, but each
turn carries a uuid pointer so the full exchange can be found in the source JSONL.

Usage:
    python extract_session_data.py [project_dir]
    python extract_session_data.py --all
    python extract_session_data.py --from 2026-03-01 --to 2026-03-12
    python extract_session_data.py /path/to/project --session abc123
"""

import argparse, json, os, glob, re, sys
from datetime import datetime, timezone, date
from collections import Counter

# ── Helpers ──────────────────────────────────────────────────────────────────

def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def ts_in_range(ts, ts_from, ts_to):
    if ts is None:
        return True  # include entries without timestamps
    if ts_from and ts < ts_from:
        return False
    if ts_to and ts > ts_to:
        return False
    return True


# Built-in CLI commands that are NOT skills
_CLI_BUILTINS = frozenset({
    "clear", "model", "compact", "login", "mcp", "plugin", "hooks",
    "context", "sandbox", "terminal-setup", "help", "config",
    "permissions", "cost", "doctor", "bug", "init", "review",
    "memory", "status", "fast", "voice", "logout", "listen",
})


def is_real_user_turn(content):
    """True if this is a user-typed message, not a tool result or CLI injection."""
    if not isinstance(content, str):
        return False
    for prefix in ("<local-command", "<command-name>", "<command-message>",
                   "<task-notification>", "<local-command-stdout>",
                   "<local-command-caveat>"):
        if content.startswith(prefix):
            return False
    return True


# ── Extraction helpers ───────────────────────────────────────────────────────

def extract_cli_skills(entries, start, end):
    """Extract skill names from <command-name> CLI injections in a range."""
    skills = []
    for i in range(start, end):
        e = entries[i]
        if e.get("type") != "user":
            continue
        content = e.get("message", {}).get("content", "")
        if not isinstance(content, str):
            continue
        for m in re.findall(r"<command-name>/?([^<]+)</command-name>", content):
            name = m.strip()
            if name.lower() not in _CLI_BUILTINS:
                skills.append({"name": name, "source": "cli"})
    return skills


def extract_model_skills(content_blocks):
    """Extract skills from Skill tool_use blocks."""
    skills = []
    if not isinstance(content_blocks, list):
        return skills
    for b in content_blocks:
        if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
            skills.append({
                "name": b.get("input", {}).get("skill", "unknown"),
                "source": "model",
            })
    return skills


def extract_tool_counts(content_blocks):
    """Count tool usage from assistant content blocks (excludes Skill)."""
    counts = Counter()
    if not isinstance(content_blocks, list):
        return counts
    for b in content_blocks:
        if isinstance(b, dict) and b.get("type") == "tool_use":
            name = b.get("name", "unknown")
            if name != "Skill":
                counts[name] += 1
    return counts


def extract_errors(entries, start, end):
    """Extract error signals from tool results and bash failures."""
    errors = []
    for i in range(start, end):
        e = entries[i]
        if e.get("type") != "user":
            continue

        content = e.get("message", {}).get("content")

        # tool_use_error in tool_result blocks
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                rc = b.get("content", "")
                text = rc if isinstance(rc, str) else ""
                if isinstance(rc, list):
                    text = "\n".join(
                        item.get("text", "") for item in rc if isinstance(item, dict)
                    )
                for m in re.finditer(
                    r"<tool_use_error>(.*?)</tool_use_error>", text, re.DOTALL
                ):
                    errors.append(m.group(1).strip()[:300])

        # Bash exit code failures
        tur = e.get("toolUseResult", {})
        if isinstance(tur, dict):
            ec = tur.get("exitCode")
            if ec is not None and ec != 0:
                errors.append(f"Bash exit {ec}")

    return errors


def dedup_tokens(entries, start, end):
    """Dedup streaming chunks by msg id, return aggregated token counts.

    Same dedup logic as api_usage.py: final entry beats streaming chunk;
    among streaming-only, keep the one with highest output_tokens.
    """
    best = {}
    no_id = 0

    for i in range(start, end):
        e = entries[i]
        if e.get("type") != "assistant":
            continue
        msg = e.get("message", {})
        if msg.get("role") != "assistant":
            continue
        usage = msg.get("usage")
        if not usage:
            continue
        model = msg.get("model", "unknown")
        if model == "<synthetic>":
            continue

        stop = msg.get("stop_reason")
        msg_id = msg.get("id")
        if not msg_id:
            if stop is None:
                continue
            no_id += 1
            msg_id = f"__noid_{start}_{no_id}"

        cc = usage.get("cache_creation", {})
        cw = 0
        if cc:
            cw = cc.get("ephemeral_1h_input_tokens", 0) + cc.get("ephemeral_5m_input_tokens", 0)
        else:
            cw = usage.get("cache_creation_input_tokens", 0)

        candidate = {
            "model": model,
            "stop": stop,
            "input": usage.get("input_tokens", 0),
            "cache_write": cw,
            "cache_read": usage.get("cache_read_input_tokens", 0),
            "output": usage.get("output_tokens", 0),
        }

        prev = best.get(msg_id)
        if prev is None:
            best[msg_id] = candidate
        elif stop is not None and prev["stop"] is None:
            best[msg_id] = candidate
        elif stop is None and prev["stop"] is None and candidate["output"] > prev["output"]:
            best[msg_id] = candidate

    tokens = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    models = Counter()
    for v in best.values():
        tokens["input"] += v["input"]
        tokens["cache_read"] += v["cache_read"]
        tokens["cache_write"] += v["cache_write"]
        tokens["output"] += v["output"]
        models[v["model"]] += 1

    return {
        "tokens": tokens,
        "api_calls": len(best),
        "model": models.most_common(1)[0][0] if models else None,
    }


def dedup_skills(skills):
    seen = set()
    out = []
    for s in skills:
        key = (s["name"], s["source"])
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


# ── Subagent parser ──────────────────────────────────────────────────────────

def parse_subagent_file(filepath):
    """Parse a subagent JSONL and return inlined metadata."""
    entries = []
    try:
        with open(filepath) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return None

    if not entries:
        return None

    # Timestamps
    ts_first = ts_last = None
    for e in entries:
        ts = parse_ts(e.get("timestamp"))
        if ts:
            if ts_first is None or ts < ts_first:
                ts_first = ts
            if ts_last is None or ts > ts_last:
                ts_last = ts

    duration = round((ts_last - ts_first).total_seconds()) if ts_first and ts_last else None

    # Tokens
    td = dedup_tokens(entries, 0, len(entries))

    # Tools, skills, errors
    tools = Counter()
    skills = []
    for e in entries:
        if e.get("type") == "assistant":
            c = e.get("message", {}).get("content", [])
            tools += extract_tool_counts(c)
            skills.extend(extract_model_skills(c))

    skills.extend(extract_cli_skills(entries, 0, len(entries)))
    errs = extract_errors(entries, 0, len(entries))

    return {
        "file": filepath,
        "model": td["model"],
        "api_calls": td["api_calls"],
        "duration_seconds": duration,
        "tokens": td["tokens"],
        "tools": dict(tools) if tools else {},
        "skills": dedup_skills(skills),
        "errors": errs if errs else [],
    }


# ── Session parser ───────────────────────────────────────────────────────────

def parse_session(filepath, project_dir):
    """Parse a main session JSONL and return the session dict."""
    entries = []
    try:
        with open(filepath) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return None

    if not entries:
        return None

    session_id = os.path.basename(filepath).replace(".jsonl", "")

    # ── Session-level metadata ───────────────────────────────────────────
    version = git_branch = slug = None
    is_continuation = False

    ts_first = ts_last = None
    for e in entries:
        if e.get("slug"):
            slug = e["slug"]
        if not version and e.get("version"):
            version = e["version"]
        if not git_branch and e.get("gitBranch"):
            git_branch = e["gitBranch"]
        ts = parse_ts(e.get("timestamp"))
        if ts:
            if ts_first is None or ts < ts_first:
                ts_first = ts
            if ts_last is None or ts > ts_last:
                ts_last = ts

    # Detect continuation
    for e in entries:
        if e.get("type") == "user":
            c = e.get("message", {}).get("content", "")
            if isinstance(c, str) and "continued from a previous conversation" in c:
                is_continuation = True
            break  # only check first user message

    # ── Turn boundaries ──────────────────────────────────────────────────
    turn_starts = []
    for i, e in enumerate(entries):
        if e.get("type") == "user":
            c = e.get("message", {}).get("content", "")
            if is_real_user_turn(c):
                turn_starts.append(i)

    # ── Find all agent launches + completions (session-wide) ─────────────
    agent_info = []  # {tool_use_id, description, agent_id, launch_idx, completion_idx}

    for i, e in enumerate(entries):
        if e.get("type") != "assistant":
            continue
        content = e.get("message", {}).get("content", [])
        if not isinstance(content, list):
            continue
        for b in content:
            if not (isinstance(b, dict) and b.get("type") == "tool_use"
                    and b.get("name") == "Agent"):
                continue

            tool_use_id = b.get("id")
            description = b.get("input", {}).get("description", "")

            # Find agentId from the immediate tool_result
            agent_id = None
            for j in range(i + 1, min(i + 15, len(entries))):
                ej = entries[j]
                if ej.get("type") != "user":
                    continue
                c = ej.get("message", {}).get("content")
                # String content (async launch message)
                if isinstance(c, str) and "agentId:" in c:
                    m = re.search(r"agentId:\s*(\S+)", c)
                    if m:
                        agent_id = m.group(1).rstrip(".")
                    break
                # List content (tool_result blocks)
                if isinstance(c, list):
                    for rb in c:
                        if not isinstance(rb, dict):
                            continue
                        if rb.get("type") == "tool_result" and rb.get("tool_use_id") == tool_use_id:
                            rc = rb.get("content", "")
                            text = rc if isinstance(rc, str) else ""
                            if isinstance(rc, list):
                                text = " ".join(
                                    item.get("text", "")
                                    for item in rc if isinstance(item, dict)
                                )
                            m = re.search(r"agentId:\s*(\S+)", text)
                            if m:
                                agent_id = m.group(1).rstrip(".")
                            break
                    break

            # Find completion via task-notification
            completion_idx = None
            if agent_id:
                for j in range(i + 1, len(entries)):
                    ej = entries[j]
                    if ej.get("type") != "user":
                        continue
                    c = ej.get("message", {}).get("content", "")
                    if isinstance(c, str) and f"<task-id>{agent_id}</task-id>" in c:
                        completion_idx = j
                        break

            agent_info.append({
                "tool_use_id": tool_use_id,
                "description": description,
                "agent_id": agent_id,
                "launch_idx": i,
                "completion_idx": completion_idx,
            })

    def idx_to_turn(idx):
        """Map an entry index to its turn index."""
        if idx is None:
            return None
        t = None
        for t_idx, t_start in enumerate(turn_starts):
            if t_start > idx:
                break
            t = t_idx
        return t

    # ── Build turns ──────────────────────────────────────────────────────
    turns = []
    for t_idx, t_start in enumerate(turn_starts):
        t_end = turn_starts[t_idx + 1] if t_idx + 1 < len(turn_starts) else len(entries)

        user_entry = entries[t_start]
        user_text = user_entry.get("message", {}).get("content", "")
        user_ts = parse_ts(user_entry.get("timestamp"))
        user_uuid = user_entry.get("uuid")

        # Duration
        last_ts = user_ts
        for i in range(t_start, t_end):
            ts = parse_ts(entries[i].get("timestamp"))
            if ts and (last_ts is None or ts > last_ts):
                last_ts = ts
        duration = round((last_ts - user_ts).total_seconds()) if user_ts and last_ts else None

        # Interrupted?
        interrupted = False
        for i in range(t_start, t_end):
            e = entries[i]
            if e.get("type") != "user":
                continue
            c = e.get("message", {}).get("content")
            if isinstance(c, str) and "[Request interrupted" in c:
                interrupted = True
                break
            if isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict) and "interrupt" in blk.get("text", "").lower():
                        interrupted = True
                        break
                if interrupted:
                    break

        post_interrupt_text = None
        if interrupted and t_idx + 1 < len(turn_starts):
            nxt = entries[turn_starts[t_idx + 1]].get("message", {}).get("content", "")
            if isinstance(nxt, str):
                post_interrupt_text = nxt

        # Tokens
        td = dedup_tokens(entries, t_start, t_end)

        # Tools + model-invoked skills
        tools = Counter()
        skills = []
        for i in range(t_start, t_end):
            e = entries[i]
            if e.get("type") == "assistant":
                c = e.get("message", {}).get("content", [])
                tools += extract_tool_counts(c)
                skills.extend(extract_model_skills(c))

        # CLI skills: scan from previous turn end (or session start) to this turn end
        cli_start = turn_starts[t_idx - 1] if t_idx > 0 else 0
        skills.extend(extract_cli_skills(entries, cli_start, t_end))

        # Errors
        errs = extract_errors(entries, t_start, t_end)

        # Subagents launched in this turn
        subagents = []
        for ai in agent_info:
            if idx_to_turn(ai["launch_idx"]) != t_idx:
                continue

            sa_entry = {
                "agent_id": ai["agent_id"],
                "description": ai["description"],
            }

            # Completion turn
            comp_turn = idx_to_turn(ai["completion_idx"])
            sa_entry["completed_in_turn"] = comp_turn if comp_turn != t_idx else None

            # Parse subagent file for metadata
            if ai["agent_id"]:
                sa_file = os.path.join(
                    project_dir, session_id, "subagents",
                    f"agent-{ai['agent_id']}.jsonl",
                )
                sa_data = parse_subagent_file(sa_file) if os.path.exists(sa_file) else None
                if sa_data:
                    sa_entry.update(sa_data)
                else:
                    sa_entry["file"] = sa_file if os.path.exists(sa_file) else None

            subagents.append(sa_entry)

        turn = {
            "index": t_idx,
            "uuid": user_uuid,
            "timestamp": user_ts.isoformat() if user_ts else None,
            "duration_seconds": duration,
            "user_text": user_text,
            "interrupted": interrupted,
            "model": td["model"],
            "api_calls": td["api_calls"],
            "tokens": td["tokens"],
            "tools": dict(tools) if tools else {},
            "skills": dedup_skills(skills),
            "errors": errs if errs else [],
        }
        if post_interrupt_text is not None:
            turn["post_interrupt_text"] = post_interrupt_text
        if subagents:
            turn["subagents"] = subagents

        turns.append(turn)

    # ── Estimate context for cancelled-before-response turns ─────────
    for i, turn in enumerate(turns):
        if not (turn["interrupted"] and turn["api_calls"] == 0):
            continue
        # Find nearest turn with actual token data
        ctx = None
        for offset in (1, -1, 2, -2, 3, -3):
            j = i + offset
            if 0 <= j < len(turns) and turns[j]["api_calls"] > 0:
                t = turns[j]["tokens"]
                ctx = t["input"] + t["cache_read"] + t["cache_write"]
                break
        if ctx is not None:
            turn["estimated_input_tokens"] = ctx

    return {
        "session_id": session_id,
        "file": filepath,
        "is_continuation": is_continuation,
        "slug": slug,
        "version": version,
        "git_branch": git_branch,
        "start": ts_first.isoformat() if ts_first else None,
        "end": ts_last.isoformat() if ts_last else None,
        "turns": turns,
    }


# ── Conversation grouping ───────────────────────────────────────────────────

def group_into_conversations(sessions):
    """Group sessions by slug into conversations. Sessions without a slug
    each become their own conversation."""
    by_slug = {}
    no_slug = []

    for s in sessions:
        slug = s.pop("slug")  # move slug to conversation level
        if slug:
            by_slug.setdefault(slug, []).append(s)
        else:
            no_slug.append(s)

    conversations = []

    for slug, sess_list in sorted(by_slug.items()):
        sess_list.sort(key=lambda s: s["start"] or "")
        conversations.append({"slug": slug, "sessions": sess_list})

    for s in no_slug:
        conversations.append({"slug": None, "sessions": [s]})

    conversations.sort(key=lambda c: c["sessions"][0]["start"] or "")
    return conversations


# ── File discovery (shared with api_usage.py) ────────────────────────────────

def find_project_dirs(args):
    """Return list of matched project directories under ~/.claude/projects/."""
    claude_dir = os.path.expanduser("~/.claude")
    if not os.path.isdir(claude_dir):
        print(f"Error: {claude_dir} not found", file=sys.stderr)
        sys.exit(1)

    projects_root = os.path.join(claude_dir, "projects")
    if not os.path.isdir(projects_root):
        print(f"Error: {projects_root} not found", file=sys.stderr)
        sys.exit(1)

    if args.all:
        return [
            os.path.join(projects_root, d)
            for d in sorted(os.listdir(projects_root))
            if os.path.isdir(os.path.join(projects_root, d))
        ], None

    project_dir = os.path.realpath(args.project_dir)
    project_key = re.sub(r"[^a-zA-Z0-9]", "-", project_dir)

    matched = []
    for d in sorted(os.listdir(projects_root)):
        if d == project_key:
            matched.append(os.path.join(projects_root, d))
        elif not args.no_subdirs and d.startswith(project_key + "-"):
            matched.append(os.path.join(projects_root, d))

    if not matched:
        print(f"No Claude project data for: {project_dir}", file=sys.stderr)
        print(f"Expected key: {project_key}", file=sys.stderr)
        sys.exit(1)

    return matched, project_dir


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Extract structured session metadata from Claude Code logs.",
    )
    parser.add_argument(
        "project_dir", nargs="?", default=os.getcwd(),
        help="Path to the project directory (default: cwd)",
    )
    parser.add_argument("--all", action="store_true",
                        help="Scan ALL project directories")
    parser.add_argument("--no-subdirs", action="store_true",
                        help="Exclude sub-directories (worktrees, etc.)")
    parser.add_argument("--session", type=str, default=None,
                        help="Filter to a specific session ID (or prefix)")
    parser.add_argument("--from", dest="date_from", type=str, default=None,
                        help="Include sessions starting from this date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="date_to", type=str, default=None,
                        help="Include sessions up to this date (YYYY-MM-DD)")

    args = parser.parse_args()

    # Parse date filters
    ts_from = ts_to = None
    if args.date_from:
        ts_from = datetime.strptime(args.date_from, "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    if args.date_to:
        ts_to = datetime.strptime(args.date_to, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=timezone.utc
        )

    matched_dirs, project_dir_display = find_project_dirs(args)

    # Find session JSONL files (top-level only, not subagents)
    files = sorted(
        f for d in matched_dirs
        for f in glob.glob(os.path.join(d, "*.jsonl"))
    )

    if args.session:
        files = [f for f in files if args.session in os.path.basename(f)]

    # Parse sessions
    sessions = []
    for filepath in files:
        # Determine which project dir this file belongs to
        proj_dir = os.path.dirname(filepath)
        session = parse_session(filepath, proj_dir)
        if session is None:
            continue

        # Date filter
        start_ts = parse_ts(session["start"])
        if ts_from and start_ts and start_ts < ts_from:
            continue
        if ts_to and start_ts and start_ts > ts_to:
            continue

        # Skip sessions with no turns
        if not session["turns"]:
            continue

        sessions.append(session)

    conversations = group_into_conversations(sessions)

    output = {
        "project": project_dir_display if not args.all else "ALL",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "conversation_count": len(conversations),
        "session_count": sum(len(c["sessions"]) for c in conversations),
        "conversations": conversations,
    }

    json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
    print()  # trailing newline
