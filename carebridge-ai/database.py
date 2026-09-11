"""
WeCareSG - SQLAlchemy Persistence Layer
================================================
Relational schema (WAL-mode SQLite) for triage cases:

    Citizen  1---* Case  1---1 ExtractedProfile
                     Case  1---* MatchedScheme
                     Case  1---* UnmetGap
                     Case  1---* CaseAuditLog

`save_full_case()` writes all six tables in a single transaction. This
module also owns the in-process publish/subscribe registry used to push
newly saved cases to Server-Sent Events (SSE) listeners in real time, so
both the Flask web tier and the Telegram bot tier (running on a separate
thread/event loop) can share one source of truth without any circular
imports between `app.py` and `bot_service.py`.
"""

import json
import queue
import random
import string
import threading
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    inspect,
    text,
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from werkzeug.security import check_password_hash, generate_password_hash

DB_PATH = Path(__file__).parent / "wecaresg.db"
DATABASE_URL = __import__("os").environ.get("DATABASE_URL", "").strip()

# SQLite is convenient for local demos, but Railway containers have an
# ephemeral filesystem.  In production, set DATABASE_URL from a Railway
# PostgreSQL service so accounts, Telegram links, cases, and follow-ups
# survive restarts and deployments.
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL.removeprefix("postgres://")
    if DATABASE_URL.startswith("postgresql://"):
        # Railway supplies a generic PostgreSQL URL.  Select the modern
        # psycopg v3 driver installed in requirements instead of SQLAlchemy's
        # legacy psycopg2 default.
        DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL.removeprefix("postgresql://")
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
else:
    engine = create_engine(
        f"sqlite:///{DB_PATH}",
        connect_args={"check_same_thread": False},
    )


@event.listens_for(engine, "connect")
def _enable_wal_mode(dbapi_connection, connection_record):
    """SQLite runs in WAL mode so concurrent readers (Flask request threads,
    the SSE stream, and the Telegram bot's asyncio thread) never block on a
    single writer."""
    # PostgreSQL does not support SQLite PRAGMAs.  Foreign keys and safe
    # concurrent reads are already enforced by PostgreSQL itself.
    if not DATABASE_URL:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
Base = declarative_base()


# ---------------------------------------------------------------------------
# ORM Models
# ---------------------------------------------------------------------------
class Citizen(Base):
    __tablename__ = "citizens"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    username = Column(String, nullable=False, unique=True)
    email = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    telegram_chat_id = Column(String, nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    cases = relationship("Case", back_populates="citizen", cascade="all, delete-orphan")


class Case(Base):
    __tablename__ = "cases"

    id = Column(String, primary_key=True)
    citizen_id = Column(String, ForeignKey("citizens.id"), nullable=False)
    channel = Column(String, nullable=False)
    raw_text = Column(Text, nullable=False)
    status = Column(String, nullable=False, default="New")
    vulnerability_score = Column(Integer, nullable=False, default=0)
    urgency = Column(String, nullable=False, default="Low")
    referral_summary = Column(Text, nullable=False, default="")
    patient_summary = Column(Text, nullable=False, default="")
    assigned_worker_id = Column(String, ForeignKey("case_workers.id"), nullable=True)
    next_action_at = Column(DateTime, nullable=True)
    escalation_reason = Column(Text, nullable=False, default="")
    escalated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    citizen = relationship("Citizen", back_populates="cases")
    profile = relationship(
        "ExtractedProfile", uselist=False, back_populates="case", cascade="all, delete-orphan"
    )
    matched_schemes = relationship("MatchedScheme", back_populates="case", cascade="all, delete-orphan")
    gaps = relationship("UnmetGap", back_populates="case", cascade="all, delete-orphan")
    audit_logs = relationship("CaseAuditLog", back_populates="case", cascade="all, delete-orphan")
    follow_ups = relationship("CaseFollowUp", back_populates="case", cascade="all, delete-orphan")
    worker_notes = relationship("WorkerCaseNote", back_populates="case", cascade="all, delete-orphan")
    document_items = relationship("CaseDocumentItem", back_populates="case", cascade="all, delete-orphan")
    assigned_worker = relationship("CaseWorker", foreign_keys=[assigned_worker_id])


class ExtractedProfile(Base):
    __tablename__ = "extracted_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False, unique=True)
    household_income = Column(Integer, nullable=True)
    dependents = Column(Integer, nullable=False, default=0)
    housing_type = Column(String, nullable=False, default="unknown")
    elderly_in_household = Column(Boolean, nullable=False, default=False)
    needs_tags = Column(Text, nullable=False, default="[]")  # JSON-encoded list[str]
    conditions = Column(Text, nullable=False, default="[]")  # JSON-encoded list[str]
    unrecognized_condition = Column(Boolean, nullable=False, default=False)
    extraction_method = Column(String, nullable=False, default="heuristic")

    case = relationship("Case", back_populates="profile")


class MatchedScheme(Base):
    __tablename__ = "matched_schemes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False)
    scheme_id = Column(String, nullable=False)
    name = Column(String, nullable=False)
    short_name = Column(String, nullable=False)
    agency = Column(String, nullable=False)
    category = Column(String, nullable=False)
    aid_type = Column(String, nullable=False)
    summary = Column(Text, nullable=False)
    coverage_amount = Column(Text, nullable=False, default="")
    how_to_apply = Column(Text, nullable=False, default="")
    documents_required = Column(Text, nullable=False, default="[]")  # JSON-encoded list[str]
    match_percent = Column(Integer, nullable=False)
    reasons = Column(Text, nullable=False, default="[]")  # JSON-encoded list[str]

    case = relationship("Case", back_populates="matched_schemes")


class UnmetGap(Base):
    __tablename__ = "unmet_gaps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False)
    description = Column(Text, nullable=False)

    case = relationship("Case", back_populates="gaps")


class CaseAuditLog(Base):
    __tablename__ = "case_audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False)
    actor = Column(String, nullable=False)
    action = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("Case", back_populates="audit_logs")


class CaseFollowUp(Base):
    """A worker update a citizen can review, optionally with a document and job leads."""

    __tablename__ = "case_follow_ups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False)
    worker_id = Column(String, ForeignKey("case_workers.id"), nullable=False)
    note = Column(Text, nullable=False, default="")
    document_name = Column(String, nullable=True)
    document_path = Column(String, nullable=True)
    document_mime = Column(String, nullable=True)
    jobs_json = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("Case", back_populates="follow_ups")
    worker = relationship("CaseWorker", foreign_keys=[worker_id])


class WorkerCaseNote(Base):
    """Private operational notes. These are never returned to citizen APIs."""

    __tablename__ = "worker_case_notes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False)
    worker_id = Column(String, ForeignKey("case_workers.id"), nullable=False)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    case = relationship("Case", back_populates="worker_notes")
    worker = relationship("CaseWorker", foreign_keys=[worker_id])


class CaseDocumentItem(Base):
    """Worker-managed checklist for documents requested for a case."""

    __tablename__ = "case_document_items"
    __table_args__ = (UniqueConstraint("case_id", "label", name="uq_case_document_label"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    case_id = Column(String, ForeignKey("cases.id"), nullable=False)
    label = Column(String, nullable=False)
    status = Column(String, nullable=False, default="Pending")
    updated_by = Column(String, ForeignKey("case_workers.id"), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    case = relationship("Case", back_populates="document_items")


class CaseWorker(Base):
    __tablename__ = "case_workers"

    id = Column(String, primary_key=True, default=lambda: uuid.uuid4().hex)
    username = Column(String, nullable=False, unique=True)
    email = Column(String, nullable=False, unique=True)
    password_hash = Column(String, nullable=False)
    display_name = Column(String, nullable=False)
    worker_code = Column(String, nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class WorkerColleague(Base):
    """A worker's added-colleague directory, used to pick a referral target
    when handing off a case to another case worker."""

    __tablename__ = "worker_colleagues"
    __table_args__ = (UniqueConstraint("worker_id", "colleague_id", name="uq_worker_colleague_pair"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    worker_id = Column(String, ForeignKey("case_workers.id"), nullable=False)
    colleague_id = Column(String, ForeignKey("case_workers.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ColleagueRequest(Base):
    """A pending invite sent by one case worker to another (by worker_code).
    The recipient must accept before both workers appear in each other's
    colleague directory and can refer cases to one another."""

    __tablename__ = "colleague_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    requester_id = Column(String, ForeignKey("case_workers.id"), nullable=False)
    recipient_id = Column(String, ForeignKey("case_workers.id"), nullable=False)
    status = Column(String, nullable=False, default="pending")  # pending | accepted | denied
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    responded_at = Column(DateTime, nullable=True)


def init_db():
    Base.metadata.create_all(engine)
    # Lightweight migrations keep existing demo databases compatible.
    columns = {column["name"] for column in inspect(engine).get_columns("cases")}
    with engine.begin() as connection:
        if "next_action_at" not in columns:
            connection.execute(text("ALTER TABLE cases ADD COLUMN next_action_at DATETIME"))
        if "escalation_reason" not in columns:
            connection.execute(text("ALTER TABLE cases ADD COLUMN escalation_reason TEXT NOT NULL DEFAULT ''"))
        if "escalated_at" not in columns:
            connection.execute(text("ALTER TABLE cases ADD COLUMN escalated_at DATETIME"))
    _seed_default_worker()


def _generate_worker_code(session) -> str:
    """A short, human-shareable ID (e.g. 'WK-7F3K2A') case workers give to
    colleagues so they can be added and referred cases to."""
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "WK-" + "".join(random.choices(alphabet, k=6))
        if not session.query(CaseWorker).filter_by(worker_code=code).first():
            return code


def _seed_default_worker():
    """Ensures at least one demo case-worker account exists so the worker
    login page is usable out of the box (username: worker / password: worker123)."""
    session = SessionLocal()
    try:
        if session.query(CaseWorker).count() == 0:
            session.add(
                CaseWorker(
                    username="worker",
                    email="worker@wecaresg.demo",
                    password_hash=generate_password_hash("worker123"),
                    display_name="Demo Case Worker",
                    worker_code=_generate_worker_code(session),
                )
            )
            session.commit()
    finally:
        session.close()


def verify_worker_credentials(username: str, password: str):
    """Returns {"id", "username", "email", "display_name", "worker_code"} on success, else None."""
    session = SessionLocal()
    try:
        worker = session.query(CaseWorker).filter_by(username=username).first()
        if not worker or not check_password_hash(worker.password_hash, password):
            return None
        return {
            "id": worker.id,
            "username": worker.username,
            "email": worker.email,
            "display_name": worker.display_name,
            "worker_code": worker.worker_code,
        }
    finally:
        session.close()


class UsernameTakenError(Exception):
    """Raised when a signup username is already registered."""


class EmailTakenError(Exception):
    """Raised when a signup email is already registered."""


def create_worker_account(username: str, email: str, password: str, display_name: str):
    """Registers a new case worker account. Returns {"id", "username",
    "email", "display_name", "worker_code"} on success, or raises
    UsernameTakenError / EmailTakenError if already registered."""
    session = SessionLocal()
    try:
        if session.query(CaseWorker).filter_by(username=username).first():
            raise UsernameTakenError(f"Username '{username}' is already taken.")
        if session.query(CaseWorker).filter_by(email=email).first():
            raise EmailTakenError(f"Email '{email}' is already registered.")
        worker = CaseWorker(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            display_name=display_name,
            worker_code=_generate_worker_code(session),
        )
        session.add(worker)
        session.commit()
        return {
            "id": worker.id,
            "username": worker.username,
            "email": worker.email,
            "display_name": worker.display_name,
            "worker_code": worker.worker_code,
        }
    finally:
        session.close()


def create_citizen_account(username: str, email: str, password: str, display_name: str):
    """Registers a new citizen account. Returns {"id", "username", "email",
    "display_name"} on success, or raises UsernameTakenError / EmailTakenError
    if already registered."""
    session = SessionLocal()
    try:
        if session.query(Citizen).filter_by(username=username).first():
            raise UsernameTakenError(f"Username '{username}' is already taken.")
        if session.query(Citizen).filter_by(email=email).first():
            raise EmailTakenError(f"Email '{email}' is already registered.")
        citizen = Citizen(
            username=username,
            email=email,
            password_hash=generate_password_hash(password),
            display_name=display_name,
        )
        session.add(citizen)
        session.commit()
        return {"id": citizen.id, "username": citizen.username, "email": citizen.email, "display_name": citizen.display_name}
    finally:
        session.close()


def verify_citizen_credentials(username: str, password: str):
    """Returns {"id", "username", "email", "display_name"} on success, else None."""
    session = SessionLocal()
    try:
        citizen = session.query(Citizen).filter_by(username=username).first()
        if not citizen or not check_password_hash(citizen.password_hash, password):
            return None
        return {"id": citizen.id, "username": citizen.username, "email": citizen.email, "display_name": citizen.display_name}
    finally:
        session.close()


def link_telegram_chat(citizen_id: str, chat_id):
    """Links a Telegram chat_id to an existing citizen account so the bot
    recognizes this chat as logged-in on subsequent messages."""
    session = SessionLocal()
    try:
        # A chat can only be linked to one account at a time.
        session.query(Citizen).filter_by(telegram_chat_id=str(chat_id)).update({"telegram_chat_id": None})
        citizen = session.query(Citizen).filter_by(id=citizen_id).first()
        if citizen:
            citizen.telegram_chat_id = str(chat_id)
        session.commit()
    finally:
        session.close()


def get_citizen_by_id(citizen_id: str):
    """Returns a minimal citizen record when the account still exists."""
    if not citizen_id:
        return None
    session = SessionLocal()
    try:
        citizen = session.query(Citizen).filter_by(id=citizen_id).first()
        if not citizen:
            return None
        return {"id": citizen.id, "username": citizen.username, "display_name": citizen.display_name}
    finally:
        session.close()


def get_citizen_by_telegram_chat(chat_id):
    """Returns {"id", "username", "display_name"} if this Telegram chat is
    linked to a logged-in citizen account, else None."""
    session = SessionLocal()
    try:
        citizen = session.query(Citizen).filter_by(telegram_chat_id=str(chat_id)).first()
        if not citizen:
            return None
        return {"id": citizen.id, "username": citizen.username, "display_name": citizen.display_name}
    finally:
        session.close()


def get_telegram_chat_for_citizen(citizen_id: str):
    """Returns the linked Telegram chat ID for a citizen, if any."""
    session = SessionLocal()
    try:
        citizen = session.query(Citizen).filter_by(id=citizen_id).first()
        return citizen.telegram_chat_id if citizen else None
    finally:
        session.close()


def unlink_telegram_chat(chat_id):
    """Logs a Telegram chat out by clearing its citizen link."""
    session = SessionLocal()
    try:
        session.query(Citizen).filter_by(telegram_chat_id=str(chat_id)).update({"telegram_chat_id": None})
        session.commit()
    finally:
        session.close()


def get_cases_for_citizen(citizen_id: str, limit: int = 20) -> list:
    session = SessionLocal()
    try:
        cases = (
            session.query(Case)
            .filter_by(citizen_id=citizen_id)
            .order_by(Case.created_at.desc())
            .limit(limit)
            .all()
        )
        return [_hydrate_case(c) for c in cases]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Hydration: ORM rows -> the JSON contract shared by app.py / bot_service.py / app.js
# ---------------------------------------------------------------------------
def _hydrate_case(case: Case, include_worker_tools: bool = False) -> dict:
    profile = case.profile
    record = {
        "case_id": case.id,
        "citizen_id": case.citizen_id,
        "timestamp": case.created_at.isoformat(),
        "channel": case.channel,
        "citizen_name": case.citizen.display_name if case.citizen else None,
        "raw_text": case.raw_text,
        "status": case.status,
        "vulnerability_score": case.vulnerability_score,
        "urgency": case.urgency,
        "profile": {
            "household_income": profile.household_income if profile else None,
            "dependents": profile.dependents if profile else 0,
            "housing_type": profile.housing_type if profile else "unknown",
            "elderly_in_household": profile.elderly_in_household if profile else False,
            "needs_tags": json.loads(profile.needs_tags) if profile else [],
            "conditions": json.loads(profile.conditions) if profile else [],
            "unrecognized_condition": profile.unrecognized_condition if profile else False,
            "extraction_method": profile.extraction_method if profile else "heuristic",
        },
        "matched_schemes": [
            {
                "scheme_id": m.scheme_id,
                "name": m.name,
                "short_name": m.short_name,
                "agency": m.agency,
                "category": m.category,
                "aid_type": m.aid_type,
                "summary": m.summary,
                "coverage_amount": m.coverage_amount,
                "how_to_apply": m.how_to_apply,
                "documents_required": json.loads(m.documents_required),
                "match_percent": m.match_percent,
                "reasons": json.loads(m.reasons),
            }
            for m in sorted(case.matched_schemes, key=lambda m: m.match_percent, reverse=True)
        ],
        "gaps": [g.description for g in case.gaps],
        "referral_summary": case.referral_summary,
        "patient_summary": case.patient_summary,
        "assigned_worker_id": case.assigned_worker_id,
        "assigned_worker_name": case.assigned_worker.display_name if case.assigned_worker else None,
        "follow_ups": [
            {
                "id": update.id,
                "note": update.note,
                "document_name": update.document_name,
                "has_document": bool(update.document_path),
                "jobs": json.loads(update.jobs_json or "[]"),
                "worker_name": update.worker.display_name if update.worker else "Case worker",
                "created_at": update.created_at.isoformat(),
            }
            for update in sorted(case.follow_ups, key=lambda item: item.created_at, reverse=True)
        ],
    }
    if include_worker_tools:
        record.update({
            "next_action_at": case.next_action_at.isoformat() if case.next_action_at else None,
            "escalation_reason": case.escalation_reason or "",
            "escalated_at": case.escalated_at.isoformat() if case.escalated_at else None,
            "private_notes": [
                {"id": note.id, "note": note.note, "worker_name": note.worker.display_name if note.worker else "Case worker", "created_at": note.created_at.isoformat()}
                for note in sorted(case.worker_notes, key=lambda item: item.created_at, reverse=True)
            ],
            "document_checklist": [
                {"id": item.id, "label": item.label, "status": item.status, "updated_at": item.updated_at.isoformat()}
                for item in sorted(case.document_items, key=lambda item: item.label.lower())
            ],
            "audit_trail": [
                {"id": log.id, "actor": log.actor, "action": log.action, "created_at": log.created_at.isoformat()}
                for log in sorted(case.audit_logs, key=lambda item: item.created_at, reverse=True)
            ],
        })
    return record


# ---------------------------------------------------------------------------
# Persistence API
# ---------------------------------------------------------------------------
def save_full_case(payload: dict, channel: str, citizen_id: str) -> str:
    """
    Persists a full triage_engine.analyze_hardship() payload across
    citizens / cases / extracted_profiles / matched_schemes / unmet_gaps /
    case_audit_logs in a single transaction. Returns the generated case_id.
    `citizen_id` must belong to an already-registered, logged-in citizen
    account (see create_citizen_account / verify_citizen_credentials).
    """
    session = SessionLocal()
    try:
        case_id = f"CB-{uuid.uuid4().hex[:8].upper()}"
        case = Case(
            id=case_id,
            citizen_id=citizen_id,
            channel=channel,
            raw_text=payload["raw_text"],
            status="New",
            vulnerability_score=payload["vulnerability_score"],
            urgency=payload["urgency"],
            referral_summary=payload["referral_summary"],
            patient_summary=payload.get("patient_summary_markdown", ""),
        )
        session.add(case)

        profile_data = payload.get("profile", {})
        session.add(
            ExtractedProfile(
                case_id=case_id,
                household_income=profile_data.get("household_income"),
                dependents=profile_data.get("dependents", 0),
                housing_type=profile_data.get("housing_type", "unknown"),
                elderly_in_household=bool(profile_data.get("elderly_in_household", False)),
                needs_tags=json.dumps(profile_data.get("needs_tags", [])),
                conditions=json.dumps(profile_data.get("conditions", [])),
                unrecognized_condition=bool(profile_data.get("unrecognized_condition", False)),
                extraction_method=profile_data.get("extraction_method", "heuristic"),
            )
        )

        for m in payload.get("matched_schemes", []):
            session.add(
                MatchedScheme(
                    case_id=case_id,
                    scheme_id=m["scheme_id"],
                    name=m["name"],
                    short_name=m["short_name"],
                    agency=m["agency"],
                    category=m["category"],
                    aid_type=m["aid_type"],
                    summary=m["summary"],
                    coverage_amount=m.get("coverage_amount", ""),
                    how_to_apply=m.get("how_to_apply", ""),
                    documents_required=json.dumps(m.get("documents_required", [])),
                    match_percent=m["match_percent"],
                    reasons=json.dumps(m.get("reasons", [])),
                )
            )

        for gap_description in payload.get("gaps", []):
            session.add(UnmetGap(case_id=case_id, description=gap_description))

        session.add(CaseAuditLog(case_id=case_id, actor=channel, action="case_created"))

        session.commit()
        return case_id
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_case(case_id: str, include_worker_tools: bool = False):
    session = SessionLocal()
    try:
        case = session.query(Case).filter_by(id=case_id).first()
        return _hydrate_case(case, include_worker_tools=include_worker_tools) if case else None
    finally:
        session.close()


def create_case_follow_up(case_id: str, worker_id: str, note: str, document: dict | None, jobs: list[dict]) -> dict | None:
    """Stores a follow-up only for the worker currently assigned to the case."""
    session = SessionLocal()
    try:
        case = session.query(Case).filter_by(id=case_id).first()
        if not case:
            return None
        if case.assigned_worker_id != worker_id:
            raise PermissionError("Accept this case before sending a follow-up.")

        update = CaseFollowUp(
            case_id=case_id,
            worker_id=worker_id,
            note=note,
            document_name=document.get("name") if document else None,
            document_path=document.get("path") if document else None,
            document_mime=document.get("mime") if document else None,
            jobs_json=json.dumps(jobs),
        )
        session.add(update)
        session.add(CaseAuditLog(case_id=case_id, actor=worker_id, action="follow_up_sent"))
        session.commit()
        return _hydrate_case(case, include_worker_tools=True)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def delete_case_for_citizen(case_id: str, citizen_id: str) -> list[str] | None:
    """Permanently removes one citizen-owned case and returns its attachment
    filenames so the web layer can remove the corresponding local files."""
    session = SessionLocal()
    try:
        case = session.query(Case).filter_by(id=case_id, citizen_id=citizen_id).first()
        if not case:
            return None
        document_paths = [item.document_path for item in case.follow_ups if item.document_path]
        session.delete(case)
        session.commit()
        return document_paths
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_follow_up_for_download(follow_up_id: int) -> dict | None:
    session = SessionLocal()
    try:
        update = session.query(CaseFollowUp).filter_by(id=follow_up_id).first()
        if not update or not update.document_path:
            return None
        return {
            "worker_id": update.worker_id,
            "citizen_id": update.case.citizen_id,
            "document_path": update.document_path,
            "document_name": update.document_name,
        }
    finally:
        session.close()


def get_recent_cases(limit: int = 20) -> list:
    session = SessionLocal()
    try:
        cases = session.query(Case).order_by(Case.created_at.desc()).limit(limit).all()
        return [_hydrate_case(c) for c in cases]
    finally:
        session.close()


def get_recent_cases_for_worker(worker_id: str, limit: int = 100) -> list:
    """Same as get_recent_cases(), but only returns cases that are either
    unclaimed or already accepted by this specific worker — once a colleague
    accepts a case, it's excluded here entirely. Reviewed cases are excluded
    too (see get_reviewed_cases_for_worker for those)."""
    session = SessionLocal()
    try:
        cases = (
            session.query(Case)
            .filter(
                (Case.assigned_worker_id.is_(None)) | (Case.assigned_worker_id == worker_id),
                Case.status != "Reviewed",
            )
            .order_by(Case.created_at.desc())
            .limit(limit)
            .all()
        )
        return [_hydrate_case(c, include_worker_tools=True) for c in cases]
    finally:
        session.close()


def get_reviewed_cases_for_worker(worker_id: str, limit: int = 50) -> list:
    """Cases this worker claimed and has already marked as Reviewed."""
    session = SessionLocal()
    try:
        cases = (
            session.query(Case)
            .filter_by(assigned_worker_id=worker_id, status="Reviewed")
            .order_by(Case.updated_at.desc())
            .limit(limit)
            .all()
        )
        return [_hydrate_case(c, include_worker_tools=True) for c in cases]
    finally:
        session.close()


def update_case_status(case_id: str, new_status: str, actor: str, worker_id: str | None = None):
    session = SessionLocal()
    try:
        case = session.query(Case).filter_by(id=case_id).first()
        if not case:
            return None
        if worker_id and case.assigned_worker_id != worker_id:
            raise PermissionError("Accept this case before changing its status.")
        case.status = new_status
        case.updated_at = datetime.utcnow()
        session.add(CaseAuditLog(case_id=case_id, actor=actor, action=f"status_changed_to_{new_status}"))
        session.commit()
        return _hydrate_case(case, include_worker_tools=True)
    finally:
        session.close()


def _owned_case(session, case_id: str, worker_id: str):
    case = session.query(Case).filter_by(id=case_id).first()
    if not case:
        return None
    if case.assigned_worker_id != worker_id:
        raise PermissionError("Accept this case before making worker-only updates.")
    return case


def add_private_note(case_id: str, worker_id: str, note: str) -> dict | None:
    session = SessionLocal()
    try:
        case = _owned_case(session, case_id, worker_id)
        if not case:
            return None
        session.add(WorkerCaseNote(case_id=case_id, worker_id=worker_id, note=note))
        session.add(CaseAuditLog(case_id=case_id, actor=worker_id, action="private_note_added"))
        session.commit()
        return _hydrate_case(case, include_worker_tools=True)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def set_next_action(case_id: str, worker_id: str, next_action_at: datetime | None) -> dict | None:
    session = SessionLocal()
    try:
        case = _owned_case(session, case_id, worker_id)
        if not case:
            return None
        case.next_action_at = next_action_at
        session.add(CaseAuditLog(case_id=case_id, actor=worker_id, action="next_action_set" if next_action_at else "next_action_cleared"))
        session.commit()
        return _hydrate_case(case, include_worker_tools=True)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def set_escalation(case_id: str, worker_id: str, reason: str) -> dict | None:
    session = SessionLocal()
    try:
        case = _owned_case(session, case_id, worker_id)
        if not case:
            return None
        case.escalation_reason = reason
        case.escalated_at = datetime.utcnow() if reason else None
        session.add(CaseAuditLog(case_id=case_id, actor=worker_id, action="case_escalated" if reason else "escalation_cleared"))
        session.commit()
        return _hydrate_case(case, include_worker_tools=True)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def replace_document_checklist(case_id: str, worker_id: str, items: list[dict]) -> dict | None:
    session = SessionLocal()
    try:
        case = _owned_case(session, case_id, worker_id)
        if not case:
            return None
        session.query(CaseDocumentItem).filter_by(case_id=case_id).delete()
        for item in items:
            label = str(item.get("label", "")).strip()[:160]
            status = str(item.get("status", "Pending")).strip()
            if label and status in {"Pending", "Received", "Verified"}:
                session.add(CaseDocumentItem(case_id=case_id, label=label, status=status, updated_by=worker_id))
        session.add(CaseAuditLog(case_id=case_id, actor=worker_id, action="document_checklist_updated"))
        session.commit()
        return _hydrate_case(case, include_worker_tools=True)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_worker_dashboard_summary(worker_id: str) -> dict:
    """Counts and upcoming actions for the signed-in worker's own workload."""
    session = SessionLocal()
    try:
        now = datetime.utcnow()
        mine = session.query(Case).filter_by(assigned_worker_id=worker_id).all()
        active = [case for case in mine if case.status != "Reviewed"]
        due = [case for case in active if case.next_action_at and case.next_action_at <= now]
        upcoming = sorted(
            [case for case in active if case.next_action_at], key=lambda case: case.next_action_at
        )[:5]
        return {
            "unclaimed": session.query(Case).filter(Case.assigned_worker_id.is_(None), Case.status != "Reviewed").count(),
            "my_active": len(active),
            "due": len(due),
            "escalated": sum(bool(case.escalation_reason) for case in active),
            "upcoming": [{"case_id": case.id, "citizen_name": case.citizen.display_name if case.citizen else "Citizen", "next_action_at": case.next_action_at.isoformat(), "escalated": bool(case.escalation_reason)} for case in upcoming],
        }
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Case-worker directory & referrals — a worker sends a colleague *request* by
# shareable worker_code; the recipient must accept before either side can
# refer cases to the other.
# ---------------------------------------------------------------------------
class WorkerNotFoundError(Exception):
    """Raised when a worker_code doesn't match any registered case worker."""


class ColleagueAlreadyAddedError(Exception):
    """Raised when trying to add a colleague who is already in the directory."""


class RequestAlreadyPendingError(Exception):
    """Raised when a colleague request already exists between two workers."""


def get_worker_by_id(worker_id: str):
    session = SessionLocal()
    try:
        worker = session.query(CaseWorker).filter_by(id=worker_id).first()
        if not worker:
            return None
        return {
            "id": worker.id,
            "username": worker.username,
            "email": worker.email,
            "display_name": worker.display_name,
            "worker_code": worker.worker_code,
        }
    finally:
        session.close()


def _colleague_dict(worker: CaseWorker) -> dict:
    return {"id": worker.id, "username": worker.username, "display_name": worker.display_name, "worker_code": worker.worker_code}


def send_colleague_request(worker_id: str, colleague_code: str):
    """Sends a colleague request to another case worker, looked up by their
    shareable worker_code. Returns {"id" (request id), "recipient": {...}}
    on success, or raises WorkerNotFoundError / ColleagueAlreadyAddedError /
    RequestAlreadyPendingError."""
    session = SessionLocal()
    try:
        recipient = session.query(CaseWorker).filter_by(worker_code=colleague_code.strip().upper()).first()
        if not recipient:
            raise WorkerNotFoundError(f"No case worker found with ID '{colleague_code}'.")
        if recipient.id == worker_id:
            raise WorkerNotFoundError("You can't add yourself as a colleague.")

        already_colleagues = (
            session.query(WorkerColleague).filter_by(worker_id=worker_id, colleague_id=recipient.id).first()
        )
        if already_colleagues:
            raise ColleagueAlreadyAddedError(f"{recipient.display_name} is already in your colleague list.")

        existing_request = (
            session.query(ColleagueRequest)
            .filter(
                ColleagueRequest.status == "pending",
                (
                    ((ColleagueRequest.requester_id == worker_id) & (ColleagueRequest.recipient_id == recipient.id))
                    | ((ColleagueRequest.requester_id == recipient.id) & (ColleagueRequest.recipient_id == worker_id))
                ),
            )
            .first()
        )
        if existing_request:
            raise RequestAlreadyPendingError(f"A colleague request with {recipient.display_name} is already pending.")

        req = ColleagueRequest(requester_id=worker_id, recipient_id=recipient.id, status="pending")
        session.add(req)
        session.commit()
        return {"id": req.id, "recipient": _colleague_dict(recipient)}
    finally:
        session.close()


def get_incoming_requests(worker_id: str) -> list:
    """Pending colleague requests sent TO this worker, awaiting accept/deny."""
    session = SessionLocal()
    try:
        requests = (
            session.query(ColleagueRequest)
            .filter_by(recipient_id=worker_id, status="pending")
            .order_by(ColleagueRequest.created_at.desc())
            .all()
        )
        out = []
        for req in requests:
            requester = session.query(CaseWorker).filter_by(id=req.requester_id).first()
            if requester:
                out.append({"id": req.id, "requester": _colleague_dict(requester), "created_at": req.created_at.isoformat()})
        return out
    finally:
        session.close()


def get_outgoing_requests(worker_id: str) -> list:
    """Pending colleague requests this worker has sent, awaiting a response."""
    session = SessionLocal()
    try:
        requests = (
            session.query(ColleagueRequest)
            .filter_by(requester_id=worker_id, status="pending")
            .order_by(ColleagueRequest.created_at.desc())
            .all()
        )
        out = []
        for req in requests:
            recipient = session.query(CaseWorker).filter_by(id=req.recipient_id).first()
            if recipient:
                out.append({"id": req.id, "recipient": _colleague_dict(recipient), "created_at": req.created_at.isoformat()})
        return out
    finally:
        session.close()


def respond_to_colleague_request(worker_id: str, request_id: int, accept: bool):
    """The recipient accepts or denies a pending colleague request. On
    accept, both workers are added to each other's colleague directory.
    Returns {"status", "colleague": {...} | None}, or None if the request
    doesn't exist / doesn't belong to this worker / isn't pending."""
    session = SessionLocal()
    try:
        req = session.query(ColleagueRequest).filter_by(id=request_id, recipient_id=worker_id, status="pending").first()
        if not req:
            return None

        req.status = "accepted" if accept else "denied"
        req.responded_at = datetime.utcnow()

        colleague_dict = None
        if accept:
            requester = session.query(CaseWorker).filter_by(id=req.requester_id).first()
            if requester:
                if not session.query(WorkerColleague).filter_by(worker_id=worker_id, colleague_id=requester.id).first():
                    session.add(WorkerColleague(worker_id=worker_id, colleague_id=requester.id))
                if not session.query(WorkerColleague).filter_by(worker_id=requester.id, colleague_id=worker_id).first():
                    session.add(WorkerColleague(worker_id=requester.id, colleague_id=worker_id))
                colleague_dict = _colleague_dict(requester)

        session.commit()
        return {"status": req.status, "colleague": colleague_dict}
    finally:
        session.close()


def remove_colleague(worker_id: str, colleague_id: str):
    session = SessionLocal()
    try:
        session.query(WorkerColleague).filter_by(worker_id=worker_id, colleague_id=colleague_id).delete()
        session.commit()
    finally:
        session.close()


def get_colleagues(worker_id: str) -> list:
    session = SessionLocal()
    try:
        links = session.query(WorkerColleague).filter_by(worker_id=worker_id).all()
        colleagues = []
        for link in links:
            c = session.query(CaseWorker).filter_by(id=link.colleague_id).first()
            if c:
                colleagues.append(_colleague_dict(c))
        return colleagues
    finally:
        session.close()


def refer_case(case_id: str, colleague_id: str, actor: str):
    """Assigns a case to another case worker (must already be an added
    colleague of `actor`'s account) and logs the handoff in the audit trail."""
    session = SessionLocal()
    try:
        case = session.query(Case).filter_by(id=case_id).first()
        if not case:
            return None
        colleague = session.query(CaseWorker).filter_by(id=colleague_id).first()
        if not colleague:
            raise WorkerNotFoundError("Selected colleague no longer exists.")
        case.assigned_worker_id = colleague.id
        case.updated_at = datetime.utcnow()
        session.add(
            CaseAuditLog(case_id=case_id, actor=actor, action=f"referred_to_{colleague.display_name}")
        )
        session.commit()
        return _hydrate_case(case, include_worker_tools=True)
    finally:
        session.close()


class CaseAlreadyClaimedError(Exception):
    """Raised when a worker tries to accept a case another worker already claimed."""


def accept_case(case_id: str, worker_id: str, actor: str):
    """Claims an unassigned case for this worker. Once claimed, the case is
    only visible to the claiming worker and disappears from every other
    worker's live feed. Raises CaseAlreadyClaimedError if someone else beat
    them to it (race between two workers clicking Accept at once)."""
    session = SessionLocal()
    try:
        case = session.query(Case).filter_by(id=case_id).first()
        if not case:
            return None
        if case.assigned_worker_id and case.assigned_worker_id != worker_id:
            claimant = session.query(CaseWorker).filter_by(id=case.assigned_worker_id).first()
            name = claimant.display_name if claimant else "another case worker"
            raise CaseAlreadyClaimedError(f"This case was already accepted by {name}.")

        case.assigned_worker_id = worker_id
        case.updated_at = datetime.utcnow()
        session.add(CaseAuditLog(case_id=case_id, actor=actor, action="accepted_case"))
        session.commit()
        return _hydrate_case(case, include_worker_tools=True)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Real-time pub/sub for Server-Sent Events
# ---------------------------------------------------------------------------
_subscribers = []
_subscribers_lock = threading.Lock()


def subscribe() -> "queue.Queue":
    """Register a new SSE listener. Returns a thread-safe queue.Queue that
    receives every case record published after this call."""
    q = queue.Queue()
    with _subscribers_lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: "queue.Queue"):
    with _subscribers_lock:
        if q in _subscribers:
            _subscribers.remove(q)


def publish_case(record: dict):
    """Broadcasts an already-persisted, hydrated case record to every
    connected dashboard. Called explicitly by app.py / bot_service.py after
    save_full_case() succeeds."""
    with _subscribers_lock:
        listeners = list(_subscribers)
    for q in listeners:
        q.put(record)
