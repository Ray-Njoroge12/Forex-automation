"""
Micro-Capital Configuration ($10-$100 accounts)

This module provides risk parameters optimized for accounts starting with $10-$100.
Use this configuration by setting environment variables before running the engine.

Usage:
    Windows CMD:
        set MICRO_CAPITAL_MODE=1
        set FIXED_RISK_USD=0.50
        python main.py --mode demo

    Windows PowerShell:
        $env:MICRO_CAPITAL_MODE="1"
        $env:FIXED_RISK_USD="0.50"
        python main.py --mode demo
"""

# Micro-Capital Risk Profile (for $10-$100 accounts)
MICRO_CAPITAL_CONFIG = {
    # Fixed USD risk per trade (overrides percentage-based risk)
    # For $10 account: $0.50 = 5% risk
    # For $50 account: $0.50 = 1% risk (more conservative as balance grows)
    "FIXED_RISK_USD": 0.50,
    
    # Maximum simultaneous trades (reduced from 2 to 1 for micro accounts)
    "MAX_SIMULTANEOUS_TRADES": 1,
    
    # Daily stop loss (percentage of balance)
    # $10 account: 15% = $1.50 daily loss limit
    "DAILY_STOP_LOSS_PCT": 0.15,
    
    # Weekly stop loss (percentage of balance)
    # $10 account: 25% = $2.50 weekly loss limit
    "WEEKLY_STOP_LOSS_PCT": 0.25,
    
    # Hard equity drawdown stop (percentage)
    # $10 account: 30% = $3.00 total drawdown limit
    "HARD_DRAWDOWN_PCT": 0.30,
    
    # Consecutive loss halt (reduced from 3 to 2)
    "CONSECUTIVE_LOSS_HALT": 2,
    
    # Spread filter relaxation (for demo accounts with wider spreads)
    # Maximum acceptable spread in pips
    "MAX_SPREAD_PIPS": 3.5,  # Increased from default ~2.0
    
    # ML Ranker threshold (lower = more trades allowed)
    # Set to -1.0 to effectively disable ML filtering for initial testing
    "ML_PREDICT_THRESHOLD": -1.0,
    
    # Minimum R:R ratio (keep at 2.2 for quality trades)
    "MIN_RISK_REWARD": 2.2,
}

# Standard Capital Risk Profile ($1000+ accounts)
STANDARD_CAPITAL_CONFIG = {
    "FIXED_RISK_USD": None,  # Use percentage-based risk
    "MAX_SIMULTANEOUS_TRADES": 2,
    "DAILY_STOP_LOSS_PCT": 0.08,
    "WEEKLY_STOP_LOSS_PCT": 0.15,
    "HARD_DRAWDOWN_PCT": 0.20,
    "CONSECUTIVE_LOSS_HALT": 3,
    "MAX_SPREAD_PIPS": 2.0,
    "ML_PREDICT_THRESHOLD": 0.0,
    "MIN_RISK_REWARD": 2.2,
}

# Compounding Milestones
# As account grows, gradually transition from micro to standard config
COMPOUNDING_MILESTONES = {
    10: {"risk_usd": 0.50, "max_trades": 1},   # $10-$20
    20: {"risk_usd": 0.75, "max_trades": 1},   # $20-$50
    50: {"risk_usd": 1.50, "max_trades": 1},   # $50-$100
    100: {"risk_usd": 3.00, "max_trades": 2},  # $100-$200
    200: {"risk_usd": 6.00, "max_trades": 2},  # $200-$500
    500: {"risk_usd": None, "max_trades": 2},  # $500+ use percentage mode (3.2%)
}


def get_config_for_balance(balance: float) -> dict:
    """
    Returns appropriate risk configuration based on current account balance.
    
    Args:
        balance: Current account balance in USD
        
    Returns:
        Dictionary with risk parameters
    """
    if balance < 500:
        # Find the appropriate milestone
        for threshold in sorted(COMPOUNDING_MILESTONES.keys(), reverse=True):
            if balance >= threshold:
                milestone = COMPOUNDING_MILESTONES[threshold]
                config = MICRO_CAPITAL_CONFIG.copy()
                config["FIXED_RISK_USD"] = milestone["risk_usd"]
                config["MAX_SIMULTANEOUS_TRADES"] = milestone["max_trades"]
                return config
        
        # Default to smallest milestone if balance < $10
        return MICRO_CAPITAL_CONFIG
    else:
        # Use standard configuration for accounts $500+
        return STANDARD_CAPITAL_CONFIG


def print_config_summary(balance: float):
    """Print a summary of the active configuration for given balance."""
    config = get_config_for_balance(balance)
    
    print("=" * 60)
    print(f"RISK CONFIGURATION FOR ${balance:.2f} ACCOUNT")
    print("=" * 60)
    
    if config["FIXED_RISK_USD"]:
        risk_pct = (config["FIXED_RISK_USD"] / balance) * 100
        print(f"Risk per trade: ${config['FIXED_RISK_USD']:.2f} ({risk_pct:.1f}%)")
    else:
        print(f"Risk per trade: 3.2% (percentage mode)")
    
    print(f"Max simultaneous trades: {config['MAX_SIMULTANEOUS_TRADES']}")
    print(f"Daily stop loss: {config['DAILY_STOP_LOSS_PCT']:.1%} (${balance * config['DAILY_STOP_LOSS_PCT']:.2f})")
    print(f"Weekly stop loss: {config['WEEKLY_STOP_LOSS_PCT']:.1%} (${balance * config['WEEKLY_STOP_LOSS_PCT']:.2f})")
    print(f"Hard drawdown: {config['HARD_DRAWDOWN_PCT']:.1%} (${balance * config['HARD_DRAWDOWN_PCT']:.2f})")
    print(f"Consecutive loss halt: {config['CONSECUTIVE_LOSS_HALT']}")
    print(f"Max spread: {config['MAX_SPREAD_PIPS']} pips")
    print(f"ML threshold: {config['ML_PREDICT_THRESHOLD']}")
    print("=" * 60)


if __name__ == "__main__":
    # Test configurations at different balance levels
    test_balances = [10, 25, 50, 100, 250, 500, 1000]
    
    for balance in test_balances:
        print_config_summary(balance)
        print()
