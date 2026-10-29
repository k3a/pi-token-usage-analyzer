# Pi token usage analyzer (standalone)

A small standalone Python tool that reads Pi's retained session JSONL files and reports token usage and recorded cost. It does **not** load Pi, require Node, install packages, or modify session files. Python 3.10+; only the standard library is used.

It is a standalone version of github.com/cartwmic/pi-token-usage extension ported by AI.

## Usage

```sh
python3 analyzer.py                 # refresh index, print totals
python3 analyzer.py summary
python3 analyzer.py models
python3 analyzer.py projects
python3 analyzer.py sessions --limit 20
python3 analyzer.py days --days 30
python3 analyzer.py months
```

By default it reads `~/.pi/agent/sessions/`. Specify a different agent directory with `--agent-dir`, or override the local disposable index with `--db`:

```sh
python3 analyzer.py --agent-dir ~/.pi/agent summary
python3 analyzer.py --agent-dir /path/to/agent sessions -n 10
python3 analyzer.py --no-refresh models
```

The index is written to `token-usage-analyzer.sqlite3` in the current working directory. Every normal run rescans session files and replaces index rows transactionally; the index is derived data and can be deleted safely. Use `--no-refresh` to query the last indexed snapshot without scanning. Override its location with `--db PATH`.

## Example output

```
Scanned 1740 session files; indexed 33738 usage entries.
Period                Input       Output      Cache R      Cache W         Cost
Lifetime             171.3M        13.5M      1196.0M       148.0k      $150.61
This month            56.1M         8.6M      1020.4M       148.0k       $85.90
Last 30 days          56.7M         8.8M      1033.5M       148.0k       $86.20
Last 7 days           11.7M       698.8k       156.5M        38.3k       $16.50
Today                  2.1M       133.0k        50.7M            0        $1.90
```

## Data and limitations

- Reads `*.jsonl` recursively from the chosen agent directory's `sessions/` tree.
- Uses usage fields already recorded in session entries (`input`, `output`, `cacheRead`, `cacheWrite`, `totalTokens`, and `cost.total`). Missing cost is treated as zero.
- Reports totals and breakdowns by model, project, session, day, and month.
- Uses session `cwd` metadata for project attribution when available.
- Duplicate entries copied into nested child histories may be counted more than once; the original extension deduplicates these. This standalone first version deliberately prioritizes straightforward, transparent session-file aggregation.
- It only analyzes files currently retained under that agent directory. It cannot recover usage absent from the JSONL files.
- It writes no transcript content to the index; source path, session/usage metadata and line position are indexed locally.
