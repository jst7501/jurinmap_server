import os
from dotenv import load_dotenv
from sqlmodel import SQLModel, create_engine, Session

load_dotenv()

_PG_HOST = os.getenv("PGHOST", "127.0.0.1")
_PG_PORT = os.getenv("PGPORT", "5432")
_PG_DB = os.getenv("PGDATABASE", "postgres")
_PG_USER = os.getenv("PGUSER", "postgres")
_PG_PASS = os.getenv("PGPASSWORD", "")

DATABASE_URL = f"postgresql://{_PG_USER}:{_PG_PASS}@{_PG_HOST}:{_PG_PORT}/{_PG_DB}"
engine = create_engine(DATABASE_URL, echo=False)


def init_db() -> None:
    from server.services.trade_journal import models  # noqa: F401
    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
