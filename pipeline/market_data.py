"""Fetch market data (equity value, shares outstanding) from yfinance."""

import logging
from datetime import date, timedelta

import yfinance as yf

log = logging.getLogger(__name__)


def get_equity_value_at_date(
    ticker: str,
    target_date: date,
) -> dict | None:
    """Get market cap and share price at (or near) a specific date.

    Looks up the closing price on the target date. If the market was closed
    (weekend/holiday), uses the nearest prior trading day within 7 days.

    Args:
        ticker: Yahoo Finance ticker (e.g. "III.L" for 3i Group)
        target_date: Date to look up (typically fiscal year-end)

    Returns:
        {share_price, shares_outstanding, market_cap, currency} or None
    """
    try:
        stock = yf.Ticker(ticker)

        # Get historical price around the target date
        start = target_date - timedelta(days=7)
        end = target_date + timedelta(days=1)
        hist = stock.history(start=start.isoformat(), end=end.isoformat())

        if hist.empty:
            log.warning(f"  No price data for {ticker} around {target_date}")
            return None

        # Use the last available price on or before the target date
        hist.index = hist.index.date
        valid = hist[hist.index <= target_date]
        if valid.empty:
            valid = hist  # fall back to whatever we have

        price = float(valid.iloc[-1]["Close"])
        price_date = valid.index[-1]

        # Get shares outstanding from the stock info
        info = stock.info
        shares = info.get("sharesOutstanding")
        currency = info.get("currency", "")

        if shares is None:
            log.warning(f"  No shares outstanding data for {ticker}")
            return None

        market_cap = price * shares

        return {
            "share_price": price,
            "price_date": price_date,
            "shares_outstanding": shares,
            "market_cap": market_cap,
            "currency": currency,
        }

    except Exception as e:
        log.warning(f"  yfinance error for {ticker}: {e}")
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
