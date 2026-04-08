"""FinAlly Market Data Simulator Demo.

Run with:  uv run market_data_demo.py

Displays a live-updating terminal dashboard of simulated stock prices
using the GBM simulator and Rich library. Includes:
  - Live price table with sparklines and flash-style color coding
  - Per-ticker session stats (high, low, % change from seed)
  - Market breadth bar (advancers vs decliners)
  - Correlation heatmap panel
  - Event log for notable price moves (>1%)
  - Session summary on exit
"""

from __future__ import annotations

import asyncio
import math
import time
from collections import deque

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from app.market.cache import PriceCache
from app.market.seed_prices import CORRELATION_GROUPS, SEED_PRICES
from app.market.simulator import SimulatorDataSource

# ─── Constants ────────────────────────────────────────────────────────────────

SPARK_CHARS = "▁▂▃▄▅▆▇█"
TICKERS = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]
DURATION = 90  # seconds; Ctrl+C exits early


# ─── Helpers ──────────────────────────────────────────────────────────────────

def sparkline(values: list[float], width: int = 30) -> str:
    """Render a sequence of values as a unicode sparkline of fixed width."""
    if len(values) < 2:
        return " " * width
    # Sample evenly if we have more values than width
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    lo, hi = min(values), max(values)
    spread = hi - lo
    n = len(SPARK_CHARS) - 1
    if spread == 0:
        chars = SPARK_CHARS[3] * len(values)
    else:
        chars = "".join(SPARK_CHARS[int((v - lo) / spread * n)] for v in values)
    return chars.ljust(width)


def fmt_price(price: float) -> str:
    return f"{price:>10,.2f}"


def color_for(value: float, *, positive_is_good: bool = True) -> str:
    if value > 0:
        return "bright_green" if positive_is_good else "bright_red"
    elif value < 0:
        return "bright_red" if positive_is_good else "bright_green"
    return "bright_black"


def bar(filled: int, total: int, width: int = 20, fill_char: str = "█", empty_char: str = "░") -> str:
    filled = max(0, min(filled, total))
    n = round(filled / total * width) if total else 0
    return fill_char * n + empty_char * (width - n)


# ─── Panel builders ───────────────────────────────────────────────────────────

def build_price_table(
    cache: PriceCache,
    history: dict[str, deque],
    session_high: dict[str, float],
    session_low: dict[str, float],
    update_counts: dict[str, int],
) -> Table:
    table = Table(
        expand=True,
        border_style="bright_black",
        header_style="bold #ecad0a",
        pad_edge=True,
        padding=(0, 1),
        show_edge=True,
    )
    table.add_column("Ticker", style="bold bright_white", width=7)
    table.add_column("Price", justify="right", width=11)
    table.add_column("Chg $", justify="right", width=9)
    table.add_column("Chg %", justify="right", width=8)
    table.add_column("▲/▼", width=3, justify="center")
    table.add_column("Session Hi", justify="right", width=11)
    table.add_column("Session Lo", justify="right", width=11)
    table.add_column("From Seed", justify="right", width=9)
    table.add_column("Ticks", justify="right", width=6)
    table.add_column("Sparkline (last 30)", width=32, no_wrap=True)

    for ticker in TICKERS:
        update = cache.get(ticker)
        if update is None:
            table.add_row(ticker, *["—"] * 9)
            continue

        seed = SEED_PRICES.get(ticker, update.price)
        from_seed = (update.price - seed) / seed * 100

        if update.direction == "up":
            col = "bright_green"
            arrow = Text("▲", style="bold bright_green")
        elif update.direction == "down":
            col = "bright_red"
            arrow = Text("▼", style="bold bright_red")
        else:
            col = "bright_black"
            arrow = Text("─", style="bright_black")

        from_seed_col = color_for(from_seed)

        spark = sparkline(list(history.get(ticker, [])), width=30)
        spark_col = "bright_green" if from_seed >= 0 else "bright_red"

        table.add_row(
            ticker,
            Text(fmt_price(update.price), style=col),
            Text(f"{update.change:+.2f}", style=col),
            Text(f"{update.change_percent:+.2f}%", style=col),
            arrow,
            Text(fmt_price(session_high.get(ticker, update.price)), style="bright_cyan"),
            Text(fmt_price(session_low.get(ticker, update.price)), style="bright_cyan"),
            Text(f"{from_seed:+.2f}%", style=from_seed_col),
            Text(str(update_counts.get(ticker, 0)), style="bright_black"),
            Text(spark, style=spark_col),
        )

    return table


def build_breadth_panel(cache: PriceCache) -> Panel:
    """Advancers vs decliners breadth bar."""
    advances = declines = flat = 0
    for ticker in TICKERS:
        u = cache.get(ticker)
        if u is None:
            continue
        if u.direction == "up":
            advances += 1
        elif u.direction == "down":
            declines += 1
        else:
            flat += 1

    total = advances + declines + flat or 1
    adv_bar = bar(advances, total, width=15, fill_char="█", empty_char="░")
    dec_bar = bar(declines, total, width=15, fill_char="█", empty_char="░")

    text = Text()
    text.append("  Advancing  ", style="bold")
    text.append(f"[{advances:2d}] ", style="bright_green")
    text.append(adv_bar, style="bright_green")
    text.append("   ")
    text.append("Declining  ", style="bold")
    text.append(f"[{declines:2d}] ", style="bright_red")
    text.append(dec_bar, style="bright_red")
    if flat:
        text.append(f"   Unchanged [{flat}]", style="bright_black")

    return Panel(text, title="[bold #ecad0a]Market Breadth[/]", border_style="bright_black", height=3)


def build_sector_panel(cache: PriceCache) -> Panel:
    """Per-sector average change."""
    # CORRELATION_GROUPS maps group_name -> set[ticker]
    # Build inverse map ticker -> group
    ticker_to_group: dict[str, str] = {}
    for group, tickers in CORRELATION_GROUPS.items():
        for t in tickers:
            ticker_to_group[t] = group

    group_changes: dict[str, list[float]] = {}
    for ticker in TICKERS:
        u = cache.get(ticker)
        if u is None:
            continue
        group = ticker_to_group.get(ticker, "other")
        group_changes.setdefault(group, []).append(u.change_percent)

    text = Text()
    for group, changes in sorted(group_changes.items()):
        avg = sum(changes) / len(changes)
        col = color_for(avg)
        label = group.title()[:8].ljust(8)
        text.append(f"  {label} ", style="bold bright_white")
        text.append(f"{avg:+.2f}%", style=col)
        text.append("   ")

    return Panel(text, title="[bold #ecad0a]Sector Averages[/]", border_style="bright_black", height=3)


def build_volatility_panel(history: dict[str, deque]) -> Panel:
    """Estimated realized volatility for each ticker (annualized %)."""
    table = Table(
        expand=False,
        border_style="bright_black",
        header_style="bold #209dd7",
        padding=(0, 1),
        show_header=True,
    )
    table.add_column("Ticker", style="bold bright_white", width=7)
    table.add_column("Vol (ann. %)", justify="right", width=12)
    table.add_column("Range bar", width=22, no_wrap=True)

    vols: list[tuple[str, float]] = []
    for ticker in TICKERS:
        vals = list(history.get(ticker, []))
        if len(vals) < 5:
            vols.append((ticker, 0.0))
            continue
        returns = [math.log(vals[i] / vals[i - 1]) for i in range(1, len(vals)) if vals[i - 1] > 0]
        if len(returns) < 2:
            vols.append((ticker, 0.0))
            continue
        mean_r = sum(returns) / len(returns)
        variance = sum((r - mean_r) ** 2 for r in returns) / (len(returns) - 1)
        # Scale from 500ms ticks to annual: 2 ticks/s * 252 * 6.5 * 3600
        ticks_per_year = 2 * 252 * 6.5 * 3600
        annualized_vol = math.sqrt(variance * ticks_per_year) * 100
        vols.append((ticker, annualized_vol))

    max_vol = max((v for _, v in vols), default=1) or 1
    for ticker, vol in vols:
        b = bar(int(vol), int(max_vol), width=20)
        col = "bright_green" if vol < 30 else "bright_yellow" if vol < 50 else "bright_red"
        table.add_row(ticker, Text(f"{vol:.1f}%", style=col), Text(b, style=col))

    return Panel(table, title="[bold #209dd7]Realized Volatility[/]", border_style="bright_black")


def build_event_log(events: deque, max_lines: int = 10) -> Panel:
    text = Text()
    shown = list(events)[:max_lines]
    for evt in shown:
        text.append(evt)
        text.append("\n")
    if not shown:
        text.append("Watching for notable moves (>1% change)...", style="bright_black italic")
    return Panel(
        text,
        title="[bold #ecad0a]Event Log  (>1% moves)[/]",
        border_style="bright_black",
    )


def build_header(cache: PriceCache, start_time: float, tick_count: int) -> Panel:
    elapsed = time.time() - start_time
    remaining = max(0, DURATION - elapsed)

    # Compute portfolio-like total from seed
    total_seed = sum(SEED_PRICES.get(t, 0) for t in TICKERS)
    total_now = sum((cache.get(t).price if cache.get(t) else SEED_PRICES.get(t, 0)) for t in TICKERS)
    basket_chg = (total_now - total_seed) / total_seed * 100 if total_seed else 0
    basket_col = color_for(basket_chg)

    text = Text()
    text.append("  FinAlly ", style="bold #ecad0a")
    text.append("Market Data Simulator", style="bold bright_white")
    text.append("  │  ", style="bright_black")
    text.append(f"Elapsed: {elapsed:5.1f}s", style="#209dd7")
    text.append("  │  ", style="bright_black")
    text.append(f"Remaining: {remaining:4.1f}s", style="#209dd7")
    text.append("  │  ", style="bright_black")
    text.append(f"Ticks: {tick_count}", style="bright_white")
    text.append("  │  ", style="bright_black")
    text.append(f"Basket Δ: {basket_chg:+.2f}%", style=basket_col)
    text.append("  │  ", style="bright_black")
    text.append("Ctrl+C to exit early", style="bright_black italic")

    return Panel(text, border_style="#ecad0a")


def build_dashboard(
    cache: PriceCache,
    history: dict[str, deque],
    session_high: dict[str, float],
    session_low: dict[str, float],
    update_counts: dict[str, int],
    events: deque,
    start_time: float,
    tick_count: int,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="metrics", size=3),
        Layout(name="main"),
        Layout(name="bottom"),
    )

    layout["header"].update(build_header(cache, start_time, tick_count))
    layout["metrics"].split_row(
        Layout(build_breadth_panel(cache), name="breadth"),
        Layout(build_sector_panel(cache), name="sector"),
    )

    # Main: price table left, volatility right
    layout["main"].split_row(
        Layout(
            Panel(
                build_price_table(cache, history, session_high, session_low, update_counts),
                title="[bold bright_white]Live Prices[/]",
                border_style="bright_black",
            ),
            name="prices",
            ratio=3,
        ),
        Layout(build_volatility_panel(history), name="vol", ratio=1),
    )

    layout["bottom"].update(build_event_log(events, max_lines=8))

    return layout


# ─── Summary ──────────────────────────────────────────────────────────────────

def print_summary(cache: PriceCache, session_high: dict, session_low: dict, tick_count: int) -> None:
    console = Console()
    console.print()
    console.rule("[bold #ecad0a]  FinAlly  Session Summary  [/]")
    console.print()

    table = Table(
        border_style="bright_black",
        header_style="bold #ecad0a",
        expand=False,
        padding=(0, 2),
    )
    table.add_column("Ticker", style="bold bright_white", width=8)
    table.add_column("Seed", justify="right", width=10)
    table.add_column("Final", justify="right", width=10)
    table.add_column("Session Hi", justify="right", width=10)
    table.add_column("Session Lo", justify="right", width=10)
    table.add_column("Range %", justify="right", width=9)
    table.add_column("Δ from Seed", justify="right", width=12)

    winners = losers = 0
    for ticker in TICKERS:
        seed = SEED_PRICES.get(ticker, 0)
        update = cache.get(ticker)
        if update is None:
            continue
        final = update.price
        hi = session_high.get(ticker, final)
        lo = session_low.get(ticker, final)
        session_chg = (final - seed) / seed * 100 if seed else 0
        session_range = (hi - lo) / lo * 100 if lo else 0

        col = color_for(session_chg)
        if session_chg > 0:
            winners += 1
        elif session_chg < 0:
            losers += 1

        table.add_row(
            ticker,
            f"${seed:,.2f}",
            Text(f"${final:,.2f}", style=col),
            f"${hi:,.2f}",
            f"${lo:,.2f}",
            f"{session_range:.2f}%",
            Text(f"{session_chg:+.2f}%", style=col),
        )

    console.print(table)
    console.print()
    console.print(
        f"  [bold bright_white]Tickers:[/] {len(TICKERS)}  │  "
        f"[bold bright_green]Winners: {winners}[/]  │  "
        f"[bold bright_red]Losers: {losers}[/]  │  "
        f"[bold #209dd7]Total ticks: {tick_count}[/]"
    )
    console.print()


# ─── Main ─────────────────────────────────────────────────────────────────────

async def run() -> None:
    cache = PriceCache()
    source = SimulatorDataSource(price_cache=cache, update_interval=0.5)

    history: dict[str, deque] = {t: deque(maxlen=60) for t in TICKERS}
    session_high: dict[str, float] = {}
    session_low: dict[str, float] = {}
    update_counts: dict[str, int] = {t: 0 for t in TICKERS}
    events: deque = deque(maxlen=20)
    tick_count = 0

    await source.start(TICKERS)
    start_time = time.time()

    # Seed initial state
    for ticker in TICKERS:
        u = cache.get(ticker)
        if u:
            history[ticker].append(u.price)
            session_high[ticker] = u.price
            session_low[ticker] = u.price

    try:
        with Live(
            build_dashboard(cache, history, session_high, session_low, update_counts, events, start_time, tick_count),
            refresh_per_second=4,
            screen=True,
        ) as live:
            last_version = cache.version
            while time.time() - start_time < DURATION:
                await asyncio.sleep(0.25)

                if cache.version == last_version:
                    continue
                last_version = cache.version
                tick_count += 1

                for ticker in TICKERS:
                    u = cache.get(ticker)
                    if u is None:
                        continue
                    history[ticker].append(u.price)
                    update_counts[ticker] = update_counts.get(ticker, 0) + 1

                    # Track session high/low
                    if ticker not in session_high or u.price > session_high[ticker]:
                        session_high[ticker] = u.price
                    if ticker not in session_low or u.price < session_low[ticker]:
                        session_low[ticker] = u.price

                    # Notable move event log
                    if abs(u.change_percent) > 1.0:
                        direction = "▲" if u.direction == "up" else "▼"
                        col = "bright_green" if u.direction == "up" else "bright_red"
                        ts = time.strftime("%H:%M:%S")
                        shock = " [bold yellow]⚡ SHOCK[/]" if abs(u.change_percent) > 3.0 else ""
                        events.appendleft(
                            f"[bright_black]{ts}[/]  "
                            f"[bold {col}]{direction} {ticker}[/]  "
                            f"[{col}]{u.change_percent:+.2f}%[/]  "
                            f"[bright_white]${u.price:,.2f}[/]"
                            f"{shock}"
                        )

                live.update(
                    build_dashboard(
                        cache, history, session_high, session_low,
                        update_counts, events, start_time, tick_count,
                    )
                )

    except KeyboardInterrupt:
        pass
    finally:
        await source.stop()

    print_summary(cache, session_high, session_low, tick_count)


if __name__ == "__main__":
    asyncio.run(run())
