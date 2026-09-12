import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator


DATABASE_PATH = "/app/data/auth.db"
LOG_PATH = "/app/logs/auth.log"
JWT_SECRET = os.getenv("AUTH_JWT_SECRET", None)

if not JWT_SECRET:
    raise ValueError("Set the JWT_SECRET")

JWT_ALGORITHM = "HS256"
TOKEN_MINUTES = int(os.getenv("AUTH_ACCESS_TOKEN_MINUTES", "60"))
ALLOWED_CATEGORIES = {"travaux", "services", "fournitures"}
password_hasher = PasswordHasher()
logger = logging.getLogger("auth")
bearer = HTTPBearer(auto_error=False)


def configure_logging() -> None:
    Path(LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        LOG_PATH,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)
    logger.propagate = False


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def initialize_database() -> None:
    Path(DATABASE_PATH).parent.mkdir(parents=True, exist_ok=True)
    with connect() as connection:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS enterprises (
                id TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                enterprise_name TEXT NOT NULL,
                phone TEXT,
                legal_identifier TEXT,
                description TEXT NOT NULL,
                keywords TEXT NOT NULL DEFAULT '[]',
                categories TEXT NOT NULL DEFAULT '[]',
                locations TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(enterprises)").fetchall()
        }
        if "portal_download_consent_at" not in columns:
            connection.execute(
                "ALTER TABLE enterprises ADD COLUMN portal_download_consent_at TEXT"
            )


def normalize_list(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(value.split())
        key = cleaned.casefold()
        if cleaned and key not in seen:
            result.append(cleaned)
            seen.add(key)
    return result


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    enterprise_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    legal_identifier: str | None = Field(default=None, max_length=100)
    description: str = Field(min_length=1, max_length=5000)
    keywords: list[str] = Field(min_length=1, max_length=50)
    categories: list[str] = Field(min_length=1, max_length=3)
    locations: list[str] = Field(min_length=1, max_length=30)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()

    @field_validator("enterprise_name", "description")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("phone", "legal_identifier")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()) or None

    @field_validator("keywords", "locations")
    @classmethod
    def clean_lists(cls, values: list[str]) -> list[str]:
        return normalize_list(values)

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, values: list[str]) -> list[str]:
        cleaned = [value.casefold() for value in normalize_list(values)]
        if set(cleaned) - ALLOWED_CATEGORIES:
            raise ValueError(
                "categories must contain only travaux, services, or fournitures"
            )
        return cleaned


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return str(value).strip().lower()


class PortalConsentRequest(BaseModel):
    accepted: bool


class ProfileUpdateRequest(BaseModel):
    enterprise_name: str = Field(min_length=1, max_length=200)
    phone: str | None = Field(default=None, max_length=30)
    legal_identifier: str | None = Field(default=None, max_length=100)
    description: str = Field(min_length=1, max_length=5000)
    keywords: list[str] = Field(min_length=1, max_length=50)
    categories: list[str] = Field(min_length=1, max_length=3)
    locations: list[str] = Field(min_length=1, max_length=30)

    @field_validator("enterprise_name", "description")
    @classmethod
    def clean_required_text(cls, value: str) -> str:
        cleaned = " ".join(value.split())
        if not cleaned:
            raise ValueError("must not be empty")
        return cleaned

    @field_validator("phone", "legal_identifier")
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()) or None

    @field_validator("keywords", "locations")
    @classmethod
    def clean_lists(cls, values: list[str]) -> list[str]:
        return normalize_list(values)

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, values: list[str]) -> list[str]:
        cleaned = [value.casefold() for value in normalize_list(values)]
        if set(cleaned) - ALLOWED_CATEGORIES:
            raise ValueError(
                "categories must contain only travaux, services, or fournitures"
            )
        return cleaned


def public_enterprise(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "enterprise_name": row["enterprise_name"],
        "phone": row["phone"],
        "legal_identifier": row["legal_identifier"],
        "description": row["description"],
        "keywords": json.loads(row["keywords"]),
        "categories": json.loads(row["categories"]),
        "locations": json.loads(row["locations"]),
        "portal_download_consent": bool(row["portal_download_consent_at"]),
    }


def create_access_token(enterprise_id: str) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": enterprise_id,
            "enterprise_id": enterprise_id,
            "iat": now,
            "exp": now + timedelta(minutes=TOKEN_MINUTES),
        },
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


def authenticated_enterprise_id(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> str:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    try:
        payload = jwt.decode(
            credentials.credentials,
            JWT_SECRET,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "enterprise_id", "iat", "exp"]},
        )
        enterprise_id = str(payload["enterprise_id"])
        if payload["sub"] != enterprise_id:
            raise ValueError("Token subject mismatch")
        uuid.UUID(enterprise_id)
        return enterprise_id
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        ) from None


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    initialize_database()
    if JWT_SECRET == "local-development-only-change-me":
        logger.warning("Using development JWT secret")
    logger.info("Auth service started database=%s", DATABASE_PATH)
    yield
    logger.info("Auth service stopped")


app = FastAPI(
    title="Public Market Auth",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def log_request(request: Request, call_next):
    request_id = str(uuid.uuid4())
    started_at = time.monotonic()

    try:
        response = await call_next(request)
    except Exception:
        duration_ms = (time.monotonic() - started_at) * 1000
        logger.exception(
            "request_id=%s method=%s path=%s status=500 duration_ms=%.1f",
            request_id,
            request.method,
            request.url.path,
            duration_ms,
        )
        raise

    duration_ms = (time.monotonic() - started_at) * 1000
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
        request_id,
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
    )
    return response


@app.get("/health")
def health() -> dict[str, str]:
    with connect() as connection:
        connection.execute("SELECT 1")
    return {"status": "ok"}


@app.post("/auth/signup", status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest) -> dict:
    enterprise_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()

    try:
        with connect() as connection:
            connection.execute(
                """
                INSERT INTO enterprises (
                    id, email, password_hash, enterprise_name, phone,
                    legal_identifier, description, keywords, categories,
                    locations, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    enterprise_id,
                    str(payload.email),
                    password_hasher.hash(payload.password),
                    payload.enterprise_name,
                    payload.phone,
                    payload.legal_identifier,
                    payload.description,
                    json.dumps(payload.keywords, ensure_ascii=False),
                    json.dumps(payload.categories, ensure_ascii=False),
                    json.dumps(payload.locations, ensure_ascii=False),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM enterprises WHERE id = ?",
                (enterprise_id,),
            ).fetchone()
    except sqlite3.IntegrityError:
        logger.warning("Signup rejected: email already registered")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An enterprise with this email already exists",
        ) from None

    logger.info("Signup succeeded enterprise_id=%s", enterprise_id)
    return {
        "access_token": create_access_token(enterprise_id),
        "token_type": "bearer",
        "enterprise": public_enterprise(row),
    }


@app.post("/auth/login")
def login(payload: LoginRequest) -> dict:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM enterprises WHERE email = ? COLLATE NOCASE",
            (str(payload.email),),
        ).fetchone()

    password_is_valid = False
    if row is not None:
        try:
            password_is_valid = password_hasher.verify(
                row["password_hash"],
                payload.password,
            )
        except (VerifyMismatchError, InvalidHashError):
            pass

    if row is None or not password_is_valid:
        logger.warning("Login failed: invalid credentials")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Email or password is incorrect",
        )

    logger.info("Login succeeded enterprise_id=%s", row["id"])
    return {
        "access_token": create_access_token(row["id"]),
        "token_type": "bearer",
        "enterprise": public_enterprise(row),
    }


@app.get("/auth/profile")
def get_profile(
    enterprise_id: str = Depends(authenticated_enterprise_id),
) -> dict:
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM enterprises WHERE id = ?",
            (enterprise_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Enterprise not found",
        )
    return {"enterprise": public_enterprise(row)}


@app.patch("/auth/profile")
def update_profile(
    payload: ProfileUpdateRequest,
    enterprise_id: str = Depends(authenticated_enterprise_id),
) -> dict:
    now = datetime.now(UTC).isoformat()
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE enterprises
            SET enterprise_name = ?, phone = ?, legal_identifier = ?,
                description = ?, keywords = ?, categories = ?, locations = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                payload.enterprise_name,
                payload.phone,
                payload.legal_identifier,
                payload.description,
                json.dumps(payload.keywords, ensure_ascii=False),
                json.dumps(payload.categories, ensure_ascii=False),
                json.dumps(payload.locations, ensure_ascii=False),
                now,
                enterprise_id,
            ),
        )
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Enterprise not found",
            )
        row = connection.execute(
            "SELECT * FROM enterprises WHERE id = ?",
            (enterprise_id,),
        ).fetchone()

    logger.info("Profile updated enterprise_id=%s", enterprise_id)
    return {"enterprise": public_enterprise(row)}


@app.post("/auth/portal-consent")
def update_portal_consent(
    payload: PortalConsentRequest,
    enterprise_id: str = Depends(authenticated_enterprise_id),
) -> dict:
    consent_at = datetime.now(UTC).isoformat() if payload.accepted else None
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE enterprises
            SET portal_download_consent_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (consent_at, datetime.now(UTC).isoformat(), enterprise_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Enterprise not found",
            )
        row = connection.execute(
            "SELECT * FROM enterprises WHERE id = ?",
            (enterprise_id,),
        ).fetchone()

    logger.info(
        "Portal download consent updated enterprise_id=%s accepted=%s",
        enterprise_id,
        payload.accepted,
    )
    return {"enterprise": public_enterprise(row)}
