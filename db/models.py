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
    index_membership = Column(String(50))  # e.g. "FTSE100"
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
    page_number = Column(Integer)     # page where data was found
    fetched_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="sources")
    emissions_records = relationship("EmissionsRecord", back_populates="source")

    def __repr__(self):
        return f"<Source {self.url}>"


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
