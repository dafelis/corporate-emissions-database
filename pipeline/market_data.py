"""Fetch market data (equity value, shares outstanding) from yfinance."""

import logging
from datetime import date, timedelta

import yfinance as yf

log = logging.getLogger(__name__)


def get_share_price_at_date(
    ticker: str,
    target_date: date,
) -> dict | None:
    """Get the closing share price at (or near) a specific date.

    Looks up the unadjusted closing price on the target date. If the market
    was closed (weekend/holiday), uses the nearest prior trading day within
    7 days.

    Returns:
        {share_price, price_date, currency} or None
    """
    try:
        stock = yf.Ticker(ticker)

        start = target_date - timedelta(days=7)
        end = target_date + timedelta(days=1)
        hist = stock.history(start=start.isoformat(), end=end.isoformat(),
                             auto_adjust=False)

        if hist.empty:
            log.warning(f"  No price data for {ticker} around {target_date}")
            return None

        hist.index = hist.index.date
        valid = hist[hist.index <= target_date]
        if valid.empty:
            valid = hist

        price = float(valid.iloc[-1]["Close"])
        price_date = valid.index[-1]

        info = stock.info
        currency = info.get("currency", "")

        # Convert minor currency units (GBp, ILA, ZAc) to major
        minor_to_major = {
            "GBp": ("GBP", 100),
            "ILA": ("ILS", 100),
            "ZAc": ("ZAR", 100),
        }
        if currency in minor_to_major:
            major_currency, divisor = minor_to_major[currency]
            price = price / divisor
            currency = major_currency

        return {
            "share_price": price,
            "price_date": price_date,
            "currency": currency,
        }

    except Exception as e:
        log.warning(f"  yfinance price error for {ticker}: {e}")
        return None


def get_fallback_shares(ticker: str, target_date: date) -> int | None:
    """Get shares outstanding for a date when filing data is unavailable.

    Tries get_shares_full() for historical data first, then falls back
    to stock.info (current shares).
    """
    try:
        stock = yf.Ticker(ticker)

        # Try historical shares series
        try:
            shares_series = stock.get_shares_full(start="2010-01-01")
            if shares_series is not None and not shares_series.empty:
                shares_series = shares_series[~shares_series.index.duplicated(keep="last")]
                shares_series.index = shares_series.index.tz_localize(None)
                # Forward-fill and pick the value at or before target_date
                import pandas as pd
                target_ts = pd.Timestamp(target_date)
                valid = shares_series[shares_series.index <= target_ts]
                if not valid.empty:
                    shares = int(valid.iloc[-1])
                    log.info(f"    Using historical shares from get_shares_full(): {shares:,}")
                    return shares
        except Exception as e:
            log.debug(f"    get_shares_full() unavailable for {ticker}: {e}")

        # Fall back to current shares from stock.info
        info = stock.info
        shares = info.get("sharesOutstanding")
        if shares:
            log.info(f"    Using current shares from stock.info: {shares:,} (may differ from historical)")
            return int(shares)

        return None

    except Exception as e:
        log.warning(f"  yfinance shares error for {ticker}: {e}")
        return None


def get_industry_info(ticker: str) -> dict | None:
    """Get sector and industry classification from yfinance.

    Returns:
        {sector, industry} or None
    """
    try:
        stock = yf.Ticker(ticker)
        info = stock.info

        sector = info.get("sector")
        industry = info.get("industry")

        if not sector and not industry:
            return None

        return {
            "sector": sector or "",
            "industry": industry or "",
        }

    except Exception as e:
        log.warning(f"  yfinance industry lookup error for {ticker}: {e}")
        return None
