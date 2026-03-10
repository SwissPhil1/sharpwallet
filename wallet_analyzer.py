"""
Wallet Analyzer — analyze a specific Polymarket wallet by username or address.

Usage:
    python wallet_analyzer.py kch123              # by Polymarket username
    python wallet_analyzer.py 0xabc123...         # by wallet address
    python wallet_analyzer.py kch123 --save       # analyze + save to Supabase
"""
import os
import sys
from pathlib import Path
from collections import defaultdict

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))
from scoring import (
    resolve_address, score_wallet,
    categorize_market, compute_clv, compute_calibration, assign_tier,
    fetch_user_trades, fetch_user_positions,
    SUPABASE_URL, SUPABASE_KEY,
)

# Lazy-init Supabase client (only if --save)
_supabase = None


def get_supabase():
    global _supabase
    if _supabase is None:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _supabase


# Re-export for backward compatibility with batch_score.py imports
def save_to_supabase(report):
    """Save the analysis results to Supabase using the Python client."""
    from scoring import save_to_supabase_client
    save_to_supabase_client(report, get_supabase())


# ── Pretty printer ──────────────────────────────────────────

def print_report(r):
    """Pretty-print the analysis report."""
    tier_colors = {"elite": "\033[92m", "sharp": "\033[96m", "moderate": "\033[93m", "noise": "\033[90m", "unknown": "\033[90m"}
    reset = "\033[0m"
    tc = tier_colors.get(r["tier"], "")

    print(f"\n{'=' * 60}")
    print(f"  SHARPNESS REPORT")
    print(f"{'=' * 60}")
    print(f"  Address:    {r['address']}")
    if r.get("username"):
        print(f"  Username:   {r['username']}")
    print(f"  Tier:       {tc}{r['tier'].upper()}{reset}")
    print(f"{'_' * 60}")
    print(f"  Total Bets:      {r['total_bets']}")
    print(f"  Total Volume:    ${r['total_volume']:,.2f}")
    print(f"  Resolved Bets:   {r['resolved_bets']}")
    print(f"  Wins:            {r['wins']}")
    print(f"{'_' * 60}")
    clv_sign = "+" if r["clv"] > 0 else ""
    roi_sign = "+" if r["roi"] > 0 else ""
    print(f"  Win Rate:        {r['win_rate'] * 100:.1f}%")
    print(f"  CLV:             {clv_sign}{r['clv'] * 100:.2f}%")
    print(f"  ROI:             {roi_sign}{r['roi'] * 100:.1f}%")
    print(f"  Calibration:     {r['calibration']:.4f}" if r['calibration'] is not None else "  Calibration:     N/A (< 20 resolved bets)")
    print(f"  Avg Edge:        {r['avg_edge'] * 100:.2f}%")
    print(f"  Consistency:     {r['sharpe_ratio']:.2f}" if r['sharpe_ratio'] is not None else "  Consistency:     N/A")
    print(f"  Kelly Fraction:  {r['kelly_fraction']:.4f}" if r['kelly_fraction'] is not None else "  Kelly Fraction:  N/A")
    print(f"  Open Positions:  {r['open_positions']}")

    if r.get("categories"):
        print(f"\n{'_' * 60}")
        print(f"  CATEGORY BREAKDOWN")
        print(f"  {'Category':<15} {'Bets':>5} {'Win%':>7} {'CLV':>8} {'ROI':>8}")
        for cat, cs in sorted(r["categories"].items(), key=lambda x: x[1]["clv"], reverse=True):
            clv_s = f"{'+' if cs['clv'] > 0 else ''}{cs['clv'] * 100:.1f}%"
            roi_s = f"{'+' if cs['roi'] > 0 else ''}{cs['roi'] * 100:.1f}%"
            print(f"  {cat:<15} {cs['total_bets']:>5} {cs['win_rate'] * 100:>6.1f}% {clv_s:>8} {roi_s:>8}")

    print(f"\n{'=' * 60}")


# ── CLI entry point ────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python wallet_analyzer.py <username_or_address> [--save]")
        print()
        print("Examples:")
        print("  python wallet_analyzer.py kch123")
        print("  python wallet_analyzer.py 0xabc123... --save")
        print()
        print("Flags:")
        print("  --save    Save results to Supabase")
        sys.exit(1)

    identifier = sys.argv[1]
    save = "--save" in sys.argv

    # Resolve address
    print(f"\nResolving '{identifier}'...")
    address, profile = resolve_address(identifier)
    print(f"  Address: {address}")
    if profile.get("username"):
        print(f"  Username: {profile['username']}")

    # Run analysis using shared scoring module
    existing_label = profile.get("username")
    report = score_wallet(address, existing_label=existing_label)

    if report:
        print_report(report)
        if save:
            save_to_supabase(report)
            print(f"\n  Saved to Supabase!")
        else:
            print(f"\n  Tip: run with --save to store results in Supabase")
    else:
        print("\n  No trades found for this wallet.")
        print("  This could mean:")
        print("    - The username/address is incorrect")
        print("    - The wallet has no CLOB trades (may use AMM)")
        print("    - API rate limiting")


if __name__ == "__main__":
    main()
