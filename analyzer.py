#!/usr/bin/env python3
"""Standalone usage analyzer for Pi JSONL session archives."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

INDEX_NAME = "token-usage-analyzer.sqlite3"


def default_agent_dir() -> Path:
    return Path(os.environ.get("PI_CODING_AGENT_DIR", Path.home() / ".pi" / "agent")).expanduser()


def session_id(path: Path) -> str:
    name = path.name
    return name[:-6] if name.endswith(".jsonl") else name


def decode_project_dir(name: str) -> str:
    # Pi encodes a cwd by replacing path separators with dashes. Actual session
    # metadata is preferred; this is only a fallback for old/incomplete files.
    if name.startswith("--"):
        return "/" + name[2:].replace("-", "/")
    if len(name) > 1 and name[1] == "-":
        return name[0] + ":/" + name[2:].replace("-", "/")
    return name.replace("-", "/") if name else "unknown"


def usage_from_entry(entry: dict[str, Any], project: str, sid: str) -> dict[str, Any] | None:
    typ = entry.get("type")
    raw_message = entry.get("message")
    message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
    if typ == "message":
        if message.get("role") not in ("assistant", "toolResult"):
            return None
        usage = message.get("usage")
        provider = message.get("provider") or entry.get("provider") or "unattributed"
        model = message.get("model") or entry.get("model") or "unattributed"
        timestamp = message.get("timestamp") if isinstance(message.get("timestamp"), (float, int)) else entry.get("timestamp")
    else:
        usage = entry.get("usage")
        provider = entry.get("provider") or "unattributed"
        model = entry.get("model") or "unattributed"
        timestamp = entry.get("timestamp")
    if not isinstance(usage, dict):
        return None
    try:
        raw_input, raw_output = usage.get("input"), usage.get("output")
        if raw_input is None or raw_output is None or isinstance(raw_input, bool) or isinstance(raw_output, bool):
            return None
        input_tokens, output_tokens = int(raw_input), int(raw_output)
        if input_tokens < 0 or output_tokens < 0:
            return None
        if isinstance(timestamp, (float, int)):
            stamp = float(timestamp) / (1000 if timestamp > 10_000_000_000 else 1)
        elif isinstance(timestamp, str):
            stamp = dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
        else:
            return None
        raw_cache_read, raw_cache_write = usage.get("cacheRead"), usage.get("cacheWrite")
        cache_read = max(0, int(raw_cache_read)) if raw_cache_read is not None else 0
        cache_write = max(0, int(raw_cache_write)) if raw_cache_write is not None else 0
        raw_total = usage.get("totalTokens")
        total_tokens = int(raw_total) if raw_total is not None else input_tokens + output_tokens + cache_read + cache_write
        raw_cost = usage.get("cost")
        cost_obj: dict[str, Any] = raw_cost if isinstance(raw_cost, dict) else {}
        cost_total = float(cost_obj.get("total") or 0)
    except (ValueError, TypeError, OverflowError):
        return None
    return {"timestamp": stamp, "provider": str(provider), "model": str(model), "project": project,
            "session": sid, "input": input_tokens, "output": output_tokens, "cache_read": cache_read,
            "cache_write": cache_write, "total_tokens": total_tokens, "cost": cost_total}


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS usage (
        source TEXT NOT NULL, entry_id TEXT, line_no INTEGER NOT NULL, timestamp REAL NOT NULL,
        provider TEXT NOT NULL, model TEXT NOT NULL, project TEXT NOT NULL, session TEXT NOT NULL,
        input INTEGER NOT NULL, output INTEGER NOT NULL, cache_read INTEGER NOT NULL,
        cache_write INTEGER NOT NULL, total_tokens INTEGER NOT NULL, cost REAL NOT NULL,
        PRIMARY KEY(source, line_no))""")
    db.execute("CREATE INDEX IF NOT EXISTS usage_timestamp ON usage(timestamp)")
    return db


def scan(agent: Path, db: sqlite3.Connection, quiet: bool = False) -> tuple[int, int]:
    root = agent / "sessions"
    if not root.exists():
        return (0, 0)
    candidates = sorted(root.rglob("*.jsonl"))
    present = {str(p.resolve()) for p in candidates}
    db.execute("BEGIN")
    try:
        for (source,) in db.execute("SELECT DISTINCT source FROM usage").fetchall():
            if source not in present:
                db.execute("DELETE FROM usage WHERE source=?", (source,))
        scanned = charges = 0
        for path in candidates:
            source = str(path.resolve())
            db.execute("DELETE FROM usage WHERE source=?", (source,))
            project = decode_project_dir(path.parent.name) if path.parent != root else "unknown"
            sid = session_id(path)
            with path.open("r", encoding="utf-8") as f:
                for line_no, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"Malformed JSON in {path}:{line_no}: {exc}") from exc
                    if not isinstance(entry, dict):
                        continue
                    if entry.get("type") == "session" and isinstance(entry.get("cwd"), str):
                        project = entry["cwd"]
                    row = usage_from_entry(entry, project, sid)
                    if row is None:
                        continue
                    db.execute("""INSERT INTO usage(source,entry_id,line_no,timestamp,provider,model,project,session,
                        input,output,cache_read,cache_write,total_tokens,cost) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (source, entry.get("id"), line_no, row["timestamp"], row["provider"], row["model"], row["project"],
                         row["session"], row["input"], row["output"], row["cache_read"], row["cache_write"], row["total_tokens"], row["cost"]))
                    charges += 1
            scanned += 1
        db.commit()
        if not quiet:
            print(f"Scanned {scanned} session files; indexed {charges} usage entries.")
        return scanned, charges
    except Exception:
        db.rollback()
        raise


def records(db: sqlite3.Connection) -> list[dict[str, Any]]:
    db.row_factory = sqlite3.Row
    return [dict(row) for row in db.execute("SELECT * FROM usage ORDER BY timestamp")]


def summarize(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {key: sum(r[key] for r in rows) for key in ("input", "output", "cache_read", "cache_write", "total_tokens", "cost")}


def money(v: float) -> str:
    return f"${v:,.4f}" if v < 0.01 and v else f"${v:,.2f}"


def count(v: float) -> str:
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if v >= 1_000:
        return f"{v / 1_000:.1f}k"
    return f"{int(v):,}"


def print_summary(rows: list[dict[str, Any]]) -> None:
    now = dt.datetime.now().astimezone()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    week = (now - dt.timedelta(days=7)).timestamp()
    month_ago = (now - dt.timedelta(days=30)).timestamp()
    periods = [("Lifetime", rows), ("This month", [r for r in rows if r["timestamp"] >= month]),
               ("Last 30 days", [r for r in rows if r["timestamp"] >= month_ago]),
               ("Last 7 days", [r for r in rows if r["timestamp"] >= week]), ("Today", [r for r in rows if r["timestamp"] >= today])]
    print(f"{'Period':<14} {'Input':>12} {'Output':>12} {'Cache R':>12} {'Cache W':>12} {'Cost':>12}")
    for name, subset in periods:
        s = summarize(subset)
        print(f"{name:<14} {count(s['input']):>12} {count(s['output']):>12} {count(s['cache_read']):>12} {count(s['cache_write']):>12} {money(s['cost']):>12}")


def grouped(rows: list[dict[str, Any]], key: str, limit: int = 30) -> list[tuple[str, dict[str, float]]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get(key) or "unknown")].append(row)
    result = [(name, summarize(subset)) for name, subset in groups.items()]
    return sorted(result, key=lambda pair: pair[1]["cost"], reverse=True)[:limit]


def print_breakdown(rows: list[dict[str, Any]], key: str, title: str, limit: int) -> None:
    print(title)
    for name, s in grouped(rows, key, limit):
        print(f"{money(s['cost']):>12}  {count(s['total_tokens']):>14} tokens  {name}")


def print_days(rows: list[dict[str, Any]], days: int) -> None:
    now = dt.datetime.now().astimezone()
    start = (now - dt.timedelta(days=max(0, days - 1))).date()
    daily: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        day = dt.datetime.fromtimestamp(r["timestamp"]).astimezone().date()
        if day >= start:
            daily[day.isoformat()].append(r)
    for day in sorted(daily):
        s = summarize(daily[day])
        print(f"{day}  {count(s['total_tokens']):>12} tokens  {money(s['cost']):>10}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze token usage from Pi session JSONL files (standalone, no Pi runtime required).")
    parser.add_argument("--agent-dir", type=Path, default=default_agent_dir(), help="Pi agent directory (default: ~/.pi/agent)")
    parser.add_argument("--db", type=Path, help="SQLite index path (default: <agent-dir>/token-usage-analyzer.sqlite3)")
    parser.add_argument("--no-refresh", action="store_true", help="Use existing index without rescanning sessions")
    parser.add_argument("--command", dest="command_override", choices=["summary", "models", "projects", "sessions", "days", "months"], help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("summary", help="Show lifetime and recent totals")
    sub.add_parser("models", help="Break usage down by provider/model")
    sub.add_parser("projects", help="Break usage down by project directory")
    p = sub.add_parser("sessions", help="Show most expensive sessions")
    p.add_argument("-n", "--limit", type=int, default=20)
    p = sub.add_parser("days", help="Show daily usage for the last N days")
    p.add_argument("-n", "--days", type=int, default=7)
    sub.add_parser("months", help="Show monthly usage")
    args = parser.parse_args()
    agent = args.agent_dir.expanduser().resolve()
    db_path = (args.db or Path.cwd() / INDEX_NAME).expanduser().resolve()
    try:
        with open_db(db_path) as db:
            if not args.no_refresh:
                scan(agent, db)
            rows = records(db)
        command = args.command_override or args.command or "summary"
        if command == "summary":
            print_summary(rows)
        elif command == "models":
            print_breakdown(rows, "model", "By model (provider/model):", 100)
        elif command == "projects":
            print_breakdown(rows, "project", "By project:", 100)
        elif command == "sessions":
            print_breakdown(rows, "session", "Top sessions by cost:", args.limit)
        elif command == "days":
            print_days(rows, args.days)
        elif command == "months":
            monthly: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for r in rows:
                month = dt.datetime.fromtimestamp(r["timestamp"]).astimezone().strftime("%Y-%m")
                monthly[month].append(r)
            for month in sorted(monthly):
                s = summarize(monthly[month])
                print(f"{month}  {count(s['total_tokens']):>12} tokens  {money(s['cost']):>10}")
        return 0
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError) as exc:
        print(f"analyzer: {exc}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
