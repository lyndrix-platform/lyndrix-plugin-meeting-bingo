"""SQLAlchemy models for Meeting Bingo (shared by every core instance)."""
from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase


class _Base(DeclarativeBase):
    pass


class BingoSession(_Base):
    __tablename__ = "bingo_sessions"

    id = Column(String(16), primary_key=True)
    name = Column(String(255), nullable=False)
    size = Column(Integer, nullable=False)
    terms = Column(JSON, nullable=False)              # term pool boards are dealt from
    status = Column(String(16), nullable=False, default="open", index=True)  # open | closed
    created_by = Column(String(255), nullable=True)
    created_at = Column(DateTime, nullable=False)
    last_update = Column(DateTime, nullable=False)
    # First winner, claimed with a conditional UPDATE (WHERE winner IS NULL) so
    # two instances can never both award it. The full ordered winner list is
    # derived from the players' won_at.
    winner = Column(String(64), nullable=True)


class BingoPlayer(_Base):
    __tablename__ = "bingo_players"

    # (session_id, name) is the primary key and therefore unique.
    session_id = Column(String(16), primary_key=True)
    name = Column(String(64), primary_key=True)
    board = Column(JSON, nullable=False)              # list[str], row-major
    marks = Column(JSON, nullable=False)              # list[bool], same order
    won = Column(Boolean, nullable=False, default=False)
    won_at = Column(DateTime, nullable=True)
    joined_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=False)
    # Optimistic concurrency: a mark only applies to the version it read.
    version = Column(Integer, nullable=False, default=0)


class BingoScore(_Base):
    __tablename__ = "bingo_scores"

    player = Column(String(64), primary_key=True)
    wins = Column(Integer, nullable=False, default=0)


class BingoConfig(_Base):
    """Small key/value store: default terms and one-time import markers."""

    __tablename__ = "bingo_config"

    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=True)
