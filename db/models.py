"""
SQLAlchemy models for the emissions database.
"""

from datetime import datetime, date

from sqlalchemy import (
    Column, Integer, String, Float, Text, Date, DateTime,
    Boolean, ForeignKey, UniqueConstraint, Index, create_engine
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

Base = declarative_base()


class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True)
    name = Column(String(500), nullable=False)
    ticker = Column(String(20))
    lei = Column(String(20), unique=True)
    lei_legal_name = Column(String(500))     # legal name returned by GLEIF
    lei_country = Column(String(10))          # country code from GLEIF
    lei_confidence = Column(String(20))       # high / medium / low
    lei_flag_reason = Column(Text)            # why it was flagged (if applicable)
    lei_review_status = Column(String(20), default="pending")  # pending / approved / rejected
    index_membership = Column(String(50))     # e.g. "FTSE100"

    # Industry classification
    yfinance_sector = Column(String(200))     # sector from yfinance
    yfinance_industry = Column(String(200))   # industry from yfinance
    sic_code = Column(String(20))             # SIC code (from registry or mapped)
    sic_description = Column(String(500))
    naics_code = Column(String(20))           # NAICS code (mapped by Claude)
    naics_description = Column(String(500))
    nace_code = Column(String(20))            # NACE code (mapped by Claude)
    nace_description = Column(String(500))
    industry_review_status = Column(String(20), default="pending")

    created_at = Column(DateTime, default=datetime.utcnow)

    emissions = relationship("EmissionsRecord", back_populates="company")
    sources = relationship("Source", back_populates="company")

    def __repr__(self):
        return f"<Company {self.name} ({self.ticker})>"


class EmissionsRecord(Base):
    __tablename__ = "emissions_records"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    reporting_year = Column(Integer, nullable=False)

    # Emissions values (tonnes CO2e)
    scope_1 = Column(Float)
    scope_2_location = Column(Float)  # location-based
    scope_2_market = Column(Float)    # market-based
    scope_3 = Column(Float)
    scope_3_categories = Column(Text)  # which of the 15 categories are included

    # Units and methodology
    unit = Column(String(50), default="tonnes CO2e")
    boundary = Column(String(100))  # operational control / equity share / financial control
    methodology_notes = Column(Text)
    is_restated = Column(Boolean, default=False)  # company revised a prior year figure

    # Extraction metadata
    source_id = Column(Integer, ForeignKey("sources.id"))
    extraction_date = Column(DateTime, default=datetime.utcnow)
    confidence_score = Column(Integer)  # 0-100

    # Review status
    review_status = Column(String(20), default="pending")  # pending / approved / rejected / flagged
    flag_reason = Column(Text)
    reviewed_by = Column(String(100))
    reviewed_at = Column(DateTime)

    company = relationship("Company", back_populates="emissions")
    source = relationship("Source", back_populates="emissions_records")

    __table_args__ = (
        UniqueConstraint("company_id", "reporting_year", "extraction_date",
                         name="uq_company_year_extraction"),
        Index("ix_company_year", "company_id", "reporting_year"),
    )

    def __repr__(self):
        return f"<EmissionsRecord {self.company_id} year={self.reporting_year}>"


class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    url = Column(Text)
    title = Column(String(500))
    document_type = Column(String(50))  # pdf / excel / html
    s3_pdf_key = Column(Text)       # S3 path to stored PDF
    s3_screenshot_key = Column(Text)  # S3 path to screenshot of relevant page
    screenshot_path = Column(Text)    # local path to PDF page screenshot
    html_snippet = Column(Text)       # raw HTML of the source table (for HTML/Excel)
    page_number = Column(Integer)     # page where data was found
    fetched_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="sources")
    emissions_records = relationship("EmissionsRecord", back_populates="source")

    def __repr__(self):
        return f"<Source {self.url}>"


class FinancialRecord(Base):
    __tablename__ = "financial_records"

    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    reporting_year = Column(Integer, nullable=False)
    fiscal_year_end = Column(Date)  # actual date the fiscal year ended

    # From company reports (in reporting currency)
    revenue = Column(Float)
    outstanding_debt = Column(Float)
    cash_and_equivalents = Column(Float)
    currency = Column(String(10))  # e.g. "GBP", "USD", "EUR"

    # From market data (yfinance)
    equity_value = Column(Float)          # market cap at fiscal year-end
    shares_outstanding = Column(Float)
    share_price_at_fy_end = Column(Float)
    equity_currency = Column(String(10))  # currency of equity value

    # Calculated
    enterprise_value = Column(Float)  # equity_value + debt - cash

    # Extraction metadata
    source_id = Column(Integer, ForeignKey("sources.id"))
    extraction_date = Column(DateTime, default=datetime.utcnow)
    confidence_score = Column(Integer)  # 0-100

    # Review status
    review_status = Column(String(20), default="pending")
    flag_reason = Column(Text)
    reviewed_by = Column(String(100))
    reviewed_at = Column(DateTime)

    company = relationship("Company", backref="financials")
    source = relationship("Source")

    __table_args__ = (
        Index("ix_financial_company_year", "company_id", "reporting_year"),
    )

    def __repr__(self):
        return f"<FinancialRecord {self.company_id} year={self.reporting_year}>"


class PipelineRun(Base):
    """Tracks each execution of the pipeline."""
    __tablename__ = "pipeline_runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)
    total_companies = Column(Integer)
    successful = Column(Integer, default=0)
    failed = Column(Integer, default=0)
    skipped = Column(Integer, default=0)  # no new data detected
    status = Column(String(20), default="running")  # running / completed / failed
    error_log = Column(Text)


def get_engine(database_url: str):
    return create_engine(database_url)


def get_session(database_url: str):
    engine = get_engine(database_url)
    Session = sessionmaker(bind=engine)
    return Session()


def create_tables(database_url: str):
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
