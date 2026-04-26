"""Quick check that pipe-table conversion produces sensible Discord output.

Run with: python scripts/verify_table_conversion.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.discord.formatter import (  # noqa: E402
    apply_discord_safety, convert_pipe_tables,
)


def show(label: str, text: str) -> None:
    print(f"\n=== {label} ===")
    print(text)
    print(f"--- end ({len(text)} chars) ---")


SCREENSHOT_TABLE = """\
| Trigger | Backtest simulator? |
|---|---|
| price_cross (fixed level) | works |
| price_cross (indicator) | 999ms mismatch — wakes every candle |
| candle_close | works |
| exact_time | works |
| consecutive_condition | missing — wakes every candle |
| compound | missing — wakes every candle |
| order_fill | missing (legit — needs trade sim) |
| portfolio_state | missing (legit — needs portfolio sim) |
| social | missing (legit — external feed) |
| prediction_market | missing (legit — external feed) |
| tradingview | missing (legit — webhook) |
"""

NARROW_TABLE = """\
| Name | Status |
|------|--------|
| foo  | ok     |
| bar  | err    |
"""

THREE_COL_TABLE = """\
| File | Lines | Notes |
|------|-------|-------|
| a.py | 12 | new |
| b.py | 5 | refactor |
"""

CODE_BLOCK_WITH_FAKE_TABLE = """\
Here is a markdown table example:
```
| col | col |
|-----|-----|
| x | y |
```
That should NOT be rewritten.
"""

NO_TABLE = "Just regular text with a | pipe in it but no separator."


def main() -> None:
    show("Screenshot table (wide → bullets)", convert_pipe_tables(SCREENSHOT_TABLE))
    show("Narrow table (code block)", convert_pipe_tables(NARROW_TABLE))
    show("Three-col table", convert_pipe_tables(THREE_COL_TABLE))
    show("Table inside fence (untouched)", convert_pipe_tables(CODE_BLOCK_WITH_FAKE_TABLE))
    show("No table (passthrough)", convert_pipe_tables(NO_TABLE))

    # Truncation safety: build a long table that produces a code block, then
    # force a tight limit and confirm fences stay balanced.
    long_table = "| a | b |\n|---|---|\n" + "\n".join(
        f"| row{i} | val{i} |" for i in range(20)
    )
    safe = apply_discord_safety(long_table, limit=120)
    show("Truncated to 120 (fence balanced)", safe)
    assert safe.count("```") % 2 == 0, "Unbalanced fence after truncation!"

    # Empty input
    assert apply_discord_safety("") == ""
    print("\nAll assertions passed.")


if __name__ == "__main__":
    main()
