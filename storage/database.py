"""
Database Engine & Session Factory

SQLite with WAL mode for production-ready local persistence.
Designed for seamless PostgreSQL migration via SQLAlchemy.
"""
import os
import threading
from contextlib import contextmanager
from typing import Generator, Optional
from sqlalchemy import create_engine, event, Engine, text
from sqlalchemy.orm import sessionmaker, Session, DeclarativeBase
from sqlalchemy.pool import StaticPool
import logging

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base class for all declarative models."""
    pass


# Global engine and session factory (initialized lazily)
_engine: Optional[Engine] = None
_session_factory: Optional[sessionmaker] = None
_init_lock = threading.Lock()


def get_database_url() -> str:
    """
    Get database URL from environment or use default SQLite path.

    Supports:
    - SQLite: sqlite:///path/to/db.sqlite (with WAL mode)
    - PostgreSQL: postgresql://user:pass@host:port/db
    """
    url = os.getenv("DATABASE_URL", "")
    if url:
        return url

    # Default to SQLite in data directory
    data_dir = os.getenv("DATA_DIR", os.path.join(os.getcwd(), "data"))
    os.makedirs(data_dir, exist_ok=True)
    db_path = os.path.join(data_dir, "delta_terminal.sqlite")
    return f"sqlite:///{db_path}"


def create_database_engine(url: Optional[str] = None) -> Engine:
    """
    Create SQLAlchemy engine with appropriate configuration.

    For SQLite:
    - WAL mode for concurrent reads/writes
    - Foreign keys enabled
    - Connection pooling with StaticPool

    For PostgreSQL:
    - Standard connection pooling
    """
    global _engine
    url = url or get_database_url()

    if _engine is not None:
        return _engine

    with _init_lock:
        if _engine is not None:
            return _engine

        if url.startswith("sqlite"):
            # SQLite-specific configuration
            engine = create_engine(
                url,
                # Use StaticPool for SQLite to maintain single connection
                poolclass=StaticPool,
                # Allow multiple threads to use the connection
                connect_args={
                    "check_same_thread": False,
                    "timeout": 30,
                },
                echo=os.getenv("SQL_ECHO", "false").lower() == "true",
            )

            # Enable WAL mode and foreign keys on connect
            @event.listens_for(engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                # WAL mode: better concurrency, crash recovery
                cursor.execute("PRAGMA journal_mode=WAL")
                # Synchronous NORMAL: good balance of safety/performance
                cursor.execute("PRAGMA synchronous=NORMAL")
                # Foreign keys enforcement
                cursor.execute("PRAGMA foreign_keys=ON")
                # Cache size: -32768 = 32MB cache
                cursor.execute("PRAGMA cache_size=-32768")
                # Temp store in memory
                cursor.execute("PRAGMA temp_store=MEMORY")
                # Page size 4096
                cursor.execute("PRAGMA page_size=4096")
                cursor.close()

        else:
            # PostgreSQL or other databases
            engine = create_engine(
                url,
                pool_size=5,
                max_overflow=10,
                pool_pre_ping=True,
                echo=os.getenv("SQL_ECHO", "false").lower() == "true",
            )

        _engine = engine
        logger.info(f"Database engine created: {url}")
        return engine


def get_engine() -> Engine:
    """Get the global engine instance, creating if necessary."""
    if _engine is None:
        return create_database_engine()
    return _engine


def create_session_factory(engine: Optional[Engine] = None) -> sessionmaker:
    """Create session factory bound to engine."""
    global _session_factory
    if _session_factory is None:
        engine = engine or get_engine()
        _session_factory = sessionmaker(
            bind=engine,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
            class_=Session,
        )
    return _session_factory


def get_session_factory() -> sessionmaker:
    """Get the global session factory, creating if necessary."""
    if _session_factory is None:
        return create_session_factory()
    return _session_factory


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    """
    Provide a transactional scope around a series of operations.

    Usage:
        with session_scope() as session:
            session.add(obj)
            session.commit()  # Auto-commits on success
        # Auto-rollbacks on exception
    """
    session_factory = get_session_factory()
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Session:
    """
    Get a new session for manual management.

    Caller is responsible for commit/rollback/close.
    Prefer session_scope() for automatic management.
    """
    return get_session_factory()()


def init_database(drop_existing: bool = False) -> None:
    """
    Initialize database tables.

    Args:
        drop_existing: If True, drop all tables first (USE WITH CAUTION)
    """
    from storage.models import Base  # Import here to avoid circular deps

    engine = get_engine()
    if drop_existing:
        logger.warning("Dropping all existing tables!")
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    logger.info("Database tables initialized")


def check_database_connection() -> bool:
    """Verify database connectivity."""
    try:
        engine = get_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"Database connection check failed: {e}")
        return False


def get_database_info() -> dict:
    """Get database information for health checks."""
    engine = get_engine()
    info = {
        "url": str(engine.url).replace(engine.url.password or "", "***") if engine.url.password else str(engine.url),
        "driver": engine.dialect.name,
        "connected": False,
        "tables": [],
    }
    try:
        with engine.connect() as conn:
            info["connected"] = True
            # Get table names
            if engine.dialect.name == "sqlite":
                result = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
            else:
                result = conn.execute(text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))
            info["tables"] = [row[0] for row in result]
    except Exception as e:
        info["error"] = str(e)
    return info


def close_database() -> None:
    """Close database connections (for shutdown)."""
    global _engine, _session_factory
    if _engine:
        _engine.dispose()
        _engine = None
    _session_factory = None
    logger.info("Database connections closed")


# For backwards compatibility
SessionLocal = get_session_factory