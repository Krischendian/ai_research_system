"""公司列表（监控标的）管理：SQLite `companies` 表。"""
from __future__ import annotations

from dataclasses import dataclass

from research_automation.core.database import get_connection, init_db


@dataclass(frozen=True)
class CompanyRecord:
    """数据库中的一行公司信息。"""

    ticker: str
    company_name: str
    sector: str
    country: str
    is_active: int
    bloomberg_ticker: str | None = None


def add_company(
    ticker: str,
    company_name: str,
    sector: str,
    country: str,
    *,
    is_active: int = 1,
    bloomberg_ticker: str | None = None,
) -> None:
    """
    添加或更新公司（按 ticker upsert）。
    ``is_active`` 非 0 视为监控中。
    ``company_name`` 可为空串（占位，后续可由 API 补全）。
    """
    sym = (ticker or "").strip().upper()
    if not sym:
        raise ValueError("ticker 不能为空")
    name = (company_name or "").strip()
    sec = (sector or "").strip()
    if not sec:
        raise ValueError("sector 不能为空")
    ctry = (country or "").strip()
    if not ctry:
        raise ValueError("country 不能为空")
    active = 1 if int(is_active) else 0
    bb = (bloomberg_ticker or "").strip() or None

    conn = get_connection()
    try:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO companies (
                ticker, company_name, sector, country, is_active, bloomberg_ticker
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                company_name = excluded.company_name,
                sector = excluded.sector,
                country = excluded.country,
                is_active = excluded.is_active,
                bloomberg_ticker = COALESCE(excluded.bloomberg_ticker, companies.bloomberg_ticker)
            """,
            (sym, name, sec, ctry, active, bb),
        )
        conn.commit()
    finally:
        conn.close()


def list_companies(
    *,
    sector: str | None = None,
    active_only: bool = False,
) -> list[CompanyRecord]:
    """
    查询公司列表；``sector`` 非空时按行业过滤；
    ``active_only=True`` 时仅 ``is_active=1``。
    """
    conn = get_connection()
    try:
        init_db(conn)
        where: list[str] = ["1=1"]
        params: list[object] = []
        if sector is not None and (s := sector.strip()):
            where.append("sector = ?")
            params.append(s)
        if active_only:
            where.append("is_active = 1")
        sql = f"""
            SELECT ticker, company_name, sector, country, is_active, bloomberg_ticker
            FROM companies
            WHERE {' AND '.join(where)}
            ORDER BY ticker
        """
        cur = conn.execute(sql, params)
        return [
            CompanyRecord(
                ticker=str(r["ticker"]),
                company_name=str(r["company_name"]),
                sector=str(r["sector"]),
                country=str(r["country"]),
                is_active=int(r["is_active"]),
                bloomberg_ticker=(
                    None
                    if r["bloomberg_ticker"] is None
                    else str(r["bloomberg_ticker"])
                ),
            )
            for r in cur.fetchall()
        ]
    finally:
        conn.close()


def get_active_tickers() -> list[str]:
    """返回所有 ``is_active=1`` 的 ticker，按字母序。"""
    conn = get_connection()
    try:
        init_db(conn)
        cur = conn.execute(
            """
            SELECT ticker FROM companies
            WHERE is_active = 1
            ORDER BY ticker
            """
        )
        return [str(r["ticker"]) for r in cur.fetchall()]
    finally:
        conn.close()


def seed_default_tech_companies() -> None:
    """预置 5 家科技龙头（Technology / US），已存在则更新名称等信息。"""
    defaults: list[tuple[str, str, str, str]] = [
        ("AAPL", "Apple Inc.", "Technology", "US"),
        ("MSFT", "Microsoft Corporation", "Technology", "US"),
        ("GOOGL", "Alphabet Inc.", "Technology", "US"),
        ("AMZN", "Amazon.com Inc.", "Technology", "US"),
        ("META", "Meta Platforms Inc.", "Technology", "US"),
    ]
    for t, n, s, c in defaults:
        add_company(t, n, s, c, is_active=1)
