"""
IG CFD Trading Bot — Dashboard Exporter

Reads the audit trail (equity_history.jsonl, order_log.jsonl) and writes
small JSON files that index.html fetches directly.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _read_jsonl(path: Path) -> list:
    if not path.exists():
        return []
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


def export_for_dashboard(data_dir: Path, out_dir: Path):
    logger.info("Exporting CFD bot data to dashboard format...")

    equity_history = _read_jsonl(data_dir / "equity_history.jsonl")
    order_log = _read_jsonl(data_dir / "order_log.jsonl")

    out_dir.mkdir(parents=True, exist_ok=True)

    if not equity_history:
        logger.warning("No equity history found yet. Nothing to export.")
        return

    equity_series = [
        {"timestamp": snap["timestamp"], "balance": snap["balance"], "available": snap["available"]}
        for snap in equity_history
    ]
    with open(out_dir / "equity.json", "w") as f:
        json.dump(equity_series, f, indent=2)

    latest = equity_history[-1]
    with open(out_dir / "positions.json", "w") as f:
        json.dump({
            "timestamp": latest["timestamp"],
            "balance": latest["balance"],
            "available": latest["available"],
            "deposit": latest["deposit"],
            "profit_loss": latest["profit_loss"],
            "currency": latest.get("currency"),
            "positions": latest["positions"],
        }, f, indent=2)

    trades = [e for e in order_log if e.get("action")]
    trades.sort(key=lambda e: e.get("logged_at", ""), reverse=True)
    with open(out_dir / "trades.json", "w") as f:
        json.dump(trades[:200], f, indent=2)

    with open(out_dir / "last_updated.json", "w") as f:
        json.dump({"timestamp": datetime.now(timezone.utc).isoformat()}, f, indent=2)

    logger.info(
        f"Dashboard export complete. {len(equity_series)} equity points, "
        f"{len(latest['positions'])} open positions, {len(trades)} logged order events."
    )
