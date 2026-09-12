import hashlib
import itertools
import logging
import mimetypes
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import threading
import time
import unicodedata
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import SpooledTemporaryFile
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from lxml import html
from qdrant_client import QdrantClient, models
from starlette.background import BackgroundTask


AUTH_URL = os.getenv("AUTH_URL", "http://auth:8000").rstrip("/")
TENDERS_DB = os.getenv("TENDERS_DB", "/app/data/tenders.db")
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
SYNC_INTERVAL_SECONDS = int(os.getenv("MATCHING_SYNC_INTERVAL_SECONDS", "3600"))
DOCUMENT_REFRESH_SECONDS = int(os.getenv("DOCUMENT_REFRESH_SECONDS", "21600"))
DOCUMENT_DOWNLOAD_RETRY_SECONDS = int(
    os.getenv("DOCUMENT_DOWNLOAD_RETRY_SECONDS", "900")
)
MAX_DOCUMENT_BYTES = int(os.getenv("MAX_DOCUMENT_MB", "25")) * 1024 * 1024
MAX_ARCHIVE_BYTES = int(os.getenv("MAX_ARCHIVE_MB", "250")) * 1024 * 1024
MAX_ARCHIVE_FILES = int(os.getenv("MAX_ARCHIVE_FILES", "500"))
OFFER_FILES_DIR = Path(os.getenv("OFFER_FILES_DIR", "/app/data/offer_files"))

COLLECTION = "offers"
DENSE_VECTOR = "dense"
SPARSE_VECTOR = "sparse"
DENSE_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
SPARSE_MODEL = "Qdrant/bm25"
DENSE_SIZE = 384
INDEX_VERSION = "3"
MOROCCO_TIMEZONE = ZoneInfo("Africa/Casablanca")

logger = logging.getLogger("matching")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

qdrant = QdrantClient(url=QDRANT_URL, timeout=60)
auth_client = httpx.Client(base_url=AUTH_URL, timeout=10)
portal_client = httpx.Client(
    timeout=60,
    follow_redirects=False,
    headers={
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0 Safari/537.36 Safaqat/0.1"
        )
    },
)
bearer = HTTPBearer(auto_error=False)
sync_lock = threading.Lock()
document_sync_lock = threading.Lock()
stop_event = threading.Event()
index_ready = threading.Event()


def read_only_database(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    normalized = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )
    return " ".join(without_accents.casefold().split())


def normalize_locations(value: str | None) -> list[str]:
    if not value:
        return []
    locations: list[str] = []
    for part in [value, *re.split(r"[,;/|\-–—]", value)]:
        normalized = normalize_text(part)
        normalized = re.sub(
            r"^(?:region|prefecture|province)\s+(?:de|d')\s+",
            "",
            normalized,
        )
        if normalized and normalized != "maroc" and normalized not in locations:
            locations.append(normalized)
    return locations


def safe_portal_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.hostname in {
        "marchespublics.gov.ma",
        "www.marchespublics.gov.ma",
    }


def portal_request(
    url: str,
    *,
    method: str = "GET",
    data: dict | None = None,
    stream: bool = False,
    max_redirects: int = 5,
    client: httpx.Client | None = None,
):
    http_client = client or portal_client
    current_url = url
    current_method = method
    current_data = data
    for _ in range(max_redirects + 1):
        if not safe_portal_url(current_url):
            raise ValueError("Public portal redirected to an untrusted destination")

        request = http_client.build_request(
            current_method,
            current_url,
            data=current_data if current_method == "POST" else None,
        )
        response = http_client.send(request, stream=stream)
        if not response.is_redirect:
            return response

        location = response.headers.get("location")
        response.close()
        if not location:
            raise ValueError("Public portal returned a redirect without a destination")
        current_url = urljoin(current_url, location)
        if response.status_code in {301, 302, 303}:
            current_method = "GET"
            current_data = None

    raise ValueError("Public portal returned too many redirects")


DOCUMENT_FILES_SCHEMA = """
CREATE TABLE IF NOT EXISTS offer_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_document_id INTEGER NOT NULL,
    consultation_id TEXT NOT NULL,
    org_acronyme TEXT NOT NULL,
    name TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    media_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (offer_document_id, relative_path),
    FOREIGN KEY (offer_document_id) REFERENCES offer_documents(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS files_by_offer
ON offer_files (consultation_id, org_acronyme);
"""


def initialize_document_storage() -> None:
    OFFER_FILES_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(TENDERS_DB, timeout=30) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(DOCUMENT_FILES_SCHEMA)
        document_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(offer_documents)")
        }
        if "download_attempted_at" not in document_columns:
            connection.execute(
                "ALTER TABLE offer_documents ADD COLUMN download_attempted_at TEXT"
            )
        if "download_error" not in document_columns:
            connection.execute(
                "ALTER TABLE offer_documents ADD COLUMN download_error TEXT"
            )
        valid_document_ids = {
            str(row[0]) for row in connection.execute("SELECT id FROM offer_documents")
        }
    for document_directory in OFFER_FILES_DIR.glob("*/*"):
        if document_directory.is_dir() and document_directory.name not in valid_document_ids:
            shutil.rmtree(document_directory)


def document_type(anchor, name: str, url: str) -> str:
    searchable = normalize_text(f'{anchor.get("id", "")} {name} {url}')
    if "reglement" in searchable:
        return "regulation"
    if "complement" in searchable:
        return "complement"
    if "avis" in searchable:
        return "notice"
    if "dce" in searchable or "dossier" in searchable:
        return "dossier"
    extension = urlparse(url).path.rsplit(".", 1)
    return extension[-1].lower() if len(extension) == 2 else "document"


def extract_offer_documents(detail_html: str, source_url: str) -> list[dict]:
    page = html.fromstring(detail_html)
    documents: list[dict] = []
    seen_urls: set[str] = set()
    for anchor in page.iter("a"):
        href = (anchor.get("href") or "").strip()
        searchable = normalize_text(f'{anchor.get("id", "")} {href}')
        if not href or not any(
            marker in searchable
            for marker in (
                "download",
                "telechargement",
                "dce",
                "reglement",
                "complement",
            )
        ):
            continue
        if any(
            "display:none" in (ancestor.get("style") or "").replace(" ", "").lower()
            for ancestor in anchor.iterancestors()
        ):
            continue

        url = urljoin(source_url, href)
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "marchespublics.gov.ma",
            "www.marchespublics.gov.ma",
        }:
            continue
        if url in seen_urls:
            continue

        name = " ".join(anchor.text_content().split())
        name = name or (anchor.get("title") or "").strip(" -")
        if not name:
            parent = anchor.getparent()
            image = next(parent.iter("img"), None) if parent is not None else None
            name = (image.get("alt") or "").strip() if image is not None else ""
        name = name or "Consultation document"
        documents.append(
            {"name": name, "url": url, "file_type": document_type(anchor, name, url)}
        )
        seen_urls.add(url)
    return documents


def parse_deadline(value: str | None) -> float | None:
    if not value:
        return None
    for date_format in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(value.strip(), date_format)
            return parsed.replace(tzinfo=MOROCCO_TIMEZONE).timestamp()
        except ValueError:
            continue
    return None


def point_id(consultation_id: str, org_acronyme: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"marchespublics.ma:{consultation_id}:{org_acronyme}",
        )
    )


def offer_hash(row: sqlite3.Row, deadline_timestamp: float) -> str:
    content = "\x1f".join(
        [
            INDEX_VERSION,
            row["objet"] or "",
            row["categorie"] or "",
            row["lieu_execution"] or "",
            str(deadline_timestamp),
        ]
    )
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def ensure_collection() -> None:
    for attempt in range(30):
        try:
            if not qdrant.collection_exists(COLLECTION):
                qdrant.create_collection(
                    collection_name=COLLECTION,
                    vectors_config={
                        DENSE_VECTOR: models.VectorParams(
                            size=DENSE_SIZE,
                            distance=models.Distance.COSINE,
                            on_disk=True,
                        )
                    },
                    sparse_vectors_config={
                        SPARSE_VECTOR: models.SparseVectorParams(
                            index=models.SparseIndexParams(on_disk=True),
                            modifier=models.Modifier.IDF,
                        )
                    },
                    on_disk_payload=True,
                )
                qdrant.create_payload_index(
                    collection_name=COLLECTION,
                    field_name="category",
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
                qdrant.create_payload_index(
                    collection_name=COLLECTION,
                    field_name="locations",
                    field_schema=models.PayloadSchemaType.KEYWORD,
                )
                qdrant.create_payload_index(
                    collection_name=COLLECTION,
                    field_name="deadline_timestamp",
                    field_schema=models.PayloadSchemaType.FLOAT,
                )
            return
        except Exception:
            if attempt == 29:
                raise
            time.sleep(2)


def indexed_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    offset = None
    while True:
        points, offset = qdrant.scroll(
            collection_name=COLLECTION,
            limit=1000,
            offset=offset,
            with_payload=["content_hash"],
            with_vectors=False,
        )
        for point in points:
            hashes[str(point.id)] = str(point.payload.get("content_hash", ""))
        if offset is None:
            return hashes


def offer_point(row: sqlite3.Row, deadline_timestamp: float) -> models.PointStruct:
    identifier = point_id(row["consultation_id"], row["org_acronyme"])
    text = " ".join((row["objet"] or "").split())
    return models.PointStruct(
        id=identifier,
        vector={
            DENSE_VECTOR: models.Document(text=text, model=DENSE_MODEL),
            SPARSE_VECTOR: models.Document(text=text, model=SPARSE_MODEL),
        },
        payload={
            "consultation_id": row["consultation_id"],
            "org_acronyme": row["org_acronyme"],
            "reference": row["reference"],
            "objet": row["objet"],
            "category": normalize_text(row["categorie"]),
            "category_display": row["categorie"],
            "locations": normalize_locations(row["lieu_execution"]),
            "location_display": row["lieu_execution"],
            "buyer": row["acheteur_public"],
            "procedure": row["procedure"],
            "publication_date": row["date_publication"],
            "deadline": row["date_limite"],
            "deadline_timestamp": deadline_timestamp,
            "source_url": row["source_url"],
            "content_hash": offer_hash(row, deadline_timestamp),
        },
    )


def synchronize_offers() -> int:
    if not sync_lock.acquire(blocking=False):
        logger.info("Offer synchronization already running")
        return 0

    try:
        ensure_collection()
        known_hashes = indexed_hashes()
        now = datetime.now(UTC).timestamp()
        pending: list[models.PointStruct] = []
        active_ids: set[str] = set()
        indexed = 0
        removed = 0
        skipped = 0

        with read_only_database(TENDERS_DB) as connection:
            rows = connection.execute(
                """
                SELECT consultation_id, org_acronyme, reference, objet,
                       procedure, categorie, acheteur_public, lieu_execution,
                       date_publication, date_limite, source_url
                FROM offers
                WHERE objet IS NOT NULL AND trim(objet) != ''
                """
            )

            for row in rows:
                deadline_timestamp = parse_deadline(row["date_limite"])
                if deadline_timestamp is None or deadline_timestamp <= now:
                    skipped += 1
                    continue

                identifier = point_id(row["consultation_id"], row["org_acronyme"])
                active_ids.add(identifier)
                current_hash = offer_hash(row, deadline_timestamp)
                if known_hashes.get(identifier) == current_hash:
                    continue

                pending.append(offer_point(row, deadline_timestamp))
                if len(pending) >= 64:
                    qdrant.upsert(
                        collection_name=COLLECTION,
                        points=pending,
                        wait=True,
                    )
                    indexed += len(pending)
                    logger.info("Indexed %s changed offers", indexed)
                    pending.clear()

        if pending:
            qdrant.upsert(
                collection_name=COLLECTION,
                points=pending,
                wait=True,
            )
            indexed += len(pending)

        stale_ids = list(set(known_hashes) - active_ids)
        for start in range(0, len(stale_ids), 512):
            batch = stale_ids[start : start + 512]
            qdrant.delete(
                collection_name=COLLECTION,
                points_selector=models.PointIdsList(points=batch),
                wait=True,
            )
            removed += len(batch)

        index_ready.set()
        logger.info(
            "Offer synchronization completed indexed=%s removed=%s expired_or_invalid=%s",
            indexed,
            removed,
            skipped,
        )
        return indexed
    except Exception:
        logger.exception("Offer synchronization failed")
        raise
    finally:
        sync_lock.release()


def synchronization_loop() -> None:
    while not stop_event.is_set():
        try:
            synchronize_offers()
        except Exception:
            pass
        stop_event.wait(SYNC_INTERVAL_SECONDS)


def load_enterprise(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> dict:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    try:
        response = auth_client.get(
            "/auth/profile",
            headers={"Authorization": f"Bearer {credentials.credentials}"},
        )
    except httpx.RequestError as error:
        logger.exception("Could not reach auth service")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Enterprise profile is temporarily unavailable",
        ) from error

    if response.status_code in (
        status.HTTP_401_UNAUTHORIZED,
        status.HTTP_403_FORBIDDEN,
        status.HTTP_404_NOT_FOUND,
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
        )
    if response.status_code != status.HTTP_200_OK:
        logger.error("Auth profile request failed status=%s", response.status_code)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Enterprise profile is temporarily unavailable",
        )
    try:
        enterprise = response.json()["enterprise"]
        return {
            "id": str(uuid.UUID(enterprise["id"])),
            "keywords": list(enterprise["keywords"]),
            "categories": list(enterprise["categories"]),
            "locations": list(enterprise["locations"]),
            "portal_download_consent": bool(
                enterprise.get("portal_download_consent", False)
            ),
        }
    except (KeyError, TypeError, ValueError) as error:
        logger.exception("Auth service returned an invalid profile")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Enterprise profile response is invalid",
        ) from error


def match_reasons(profile: dict, payload: dict) -> list[str]:
    normalized_object = normalize_text(payload.get("objet"))
    reasons = [
        f"Matched keyword: {keyword}"
        for keyword in profile["keywords"]
        if normalize_text(keyword) in normalized_object
    ][:3]

    normalized_categories = {normalize_text(value) for value in profile["categories"]}
    if payload.get("category") in normalized_categories:
        reasons.append(f"Matched category: {payload['category']}")

    requested_locations = {
        location
        for value in profile["locations"]
        for location in normalize_locations(value)
    }
    matched_locations = requested_locations.intersection(payload.get("locations", []))
    if matched_locations:
        reasons.append(f"Matched location: {sorted(matched_locations)[0]}")

    if not reasons:
        reasons.append("Semantic similarity with enterprise keywords")
    return reasons


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_document_storage()
    ensure_collection()
    worker = threading.Thread(target=synchronization_loop, daemon=True)
    worker.start()
    logger.info("Matching service started")
    yield
    stop_event.set()
    worker.join(timeout=5)
    auth_client.close()
    portal_client.close()
    qdrant.close()
    logger.info("Matching service stopped")


app = FastAPI(
    title="Public Market Matching",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    try:
        collection = qdrant.get_collection(COLLECTION)
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qdrant is unavailable",
        ) from error
    return {
        "status": "ok",
        "index_ready": index_ready.is_set(),
        "indexed_offers": collection.points_count,
    }


def active_offer_point(offer_id: str):
    try:
        identifier = str(uuid.UUID(offer_id))
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offer not found",
        ) from error

    try:
        points = qdrant.retrieve(
            collection_name=COLLECTION,
            ids=[identifier],
            with_payload=True,
            with_vectors=False,
        )
    except Exception as error:
        logger.exception("Could not load offer offer_id=%s", identifier)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offer catalogue is temporarily unavailable",
        ) from error

    if not points:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offer not found",
        )

    point = points[0]
    payload = point.payload or {}
    deadline_timestamp = payload.get("deadline_timestamp")
    if not isinstance(deadline_timestamp, (int, float)) or (
        deadline_timestamp <= datetime.now(UTC).timestamp()
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offer not found or no longer active",
        )
    return point


def document_rows(connection, consultation_id: str, org_acronyme: str):
    return connection.execute(
        """
        SELECT id, name, url, file_type
        FROM offer_documents
        WHERE consultation_id = ? AND org_acronyme = ?
        ORDER BY id
        """,
        (consultation_id, org_acronyme),
    ).fetchall()


def public_document_rows(rows) -> list[dict]:
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "file_type": row["file_type"],
            "portal_url": row["url"],
            "requires_portal": "EntrepriseDemandeTelechargement" in row["url"],
        }
        for row in rows
    ]


def offer_documents(
    consultation_id: str,
    org_acronyme: str,
    source_url: str,
) -> list[dict]:
    try:
        with read_only_database(TENDERS_DB) as connection:
            offer = connection.execute(
                """
                SELECT documents_checked_at
                FROM offers
                WHERE consultation_id = ? AND org_acronyme = ?
                """,
                (consultation_id, org_acronyme),
            ).fetchone()
            rows = document_rows(connection, consultation_id, org_acronyme)
    except sqlite3.Error as error:
        logger.exception(
            "Could not load offer documents consultation_id=%s org=%s",
            consultation_id,
            org_acronyme,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offer documents are temporarily unavailable",
        ) from error

    if offer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Offer not found")

    checked_at = offer["documents_checked_at"]
    if checked_at:
        try:
            checked = datetime.fromisoformat(checked_at).replace(tzinfo=UTC)
            if (datetime.now(UTC) - checked).total_seconds() < DOCUMENT_REFRESH_SECONDS:
                return public_document_rows(rows)
        except ValueError:
            pass

    parsed_source = urlparse(source_url)
    if parsed_source.scheme not in {"http", "https"} or parsed_source.hostname not in {
        "marchespublics.gov.ma",
        "www.marchespublics.gov.ma",
    }:
        logger.error("Rejected invalid offer source URL consultation_id=%s", consultation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offer document source is invalid",
        )

    try:
        response = portal_request(source_url)
        response.raise_for_status()
        discovered = extract_offer_documents(response.text, source_url)
    except (httpx.HTTPError, ValueError) as error:
        logger.exception("Document discovery failed consultation_id=%s", consultation_id)
        if rows:
            return public_document_rows(rows)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offer documents are temporarily unavailable",
        ) from error

    write_connection = None
    removed_document_ids: set[int] = set()
    try:
        write_connection = sqlite3.connect(TENDERS_DB, timeout=30)
        write_connection.row_factory = sqlite3.Row
        write_connection.execute("PRAGMA foreign_keys = ON")
        write_connection.execute("PRAGMA busy_timeout = 30000")
        with write_connection:
            existing_by_url = {
                str(row["url"]): int(row["id"])
                for row in document_rows(
                    write_connection,
                    consultation_id,
                    org_acronyme,
                )
            }
            retained_ids: set[int] = set()
            for document in discovered:
                existing_id = existing_by_url.get(str(document["url"]))
                if existing_id is None:
                    cursor = write_connection.execute(
                        """
                        INSERT INTO offer_documents (
                            consultation_id, org_acronyme, name, url, file_type
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            consultation_id,
                            org_acronyme,
                            document["name"],
                            document["url"],
                            document["file_type"],
                        ),
                    )
                    retained_ids.add(int(cursor.lastrowid))
                else:
                    write_connection.execute(
                        """
                        UPDATE offer_documents
                        SET name = ?, file_type = ?
                        WHERE id = ?
                        """,
                        (document["name"], document["file_type"], existing_id),
                    )
                    retained_ids.add(existing_id)

            removed_document_ids = set(existing_by_url.values()) - retained_ids
            write_connection.executemany(
                "DELETE FROM offer_documents WHERE id = ?",
                [(document_id,) for document_id in removed_document_ids],
            )
            write_connection.execute(
                """
                UPDATE offers
                SET documents_checked_at = CURRENT_TIMESTAMP
                WHERE consultation_id = ? AND org_acronyme = ?
                """,
                (consultation_id, org_acronyme),
            )
            rows = document_rows(write_connection, consultation_id, org_acronyme)
    except sqlite3.Error as error:
        logger.exception("Could not cache documents consultation_id=%s", consultation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offer documents are temporarily unavailable",
        ) from error
    finally:
        if write_connection is not None:
            write_connection.close()

    for document_id in removed_document_ids:
        shutil.rmtree(
            document_storage_root(
                {
                    "id": document_id,
                    "consultation_id": consultation_id,
                    "org_acronyme": org_acronyme,
                }
            ),
            ignore_errors=True,
        )

    return public_document_rows(rows)


def stored_document(
    consultation_id: str,
    org_acronyme: str,
    document_id: int,
) -> sqlite3.Row:
    try:
        with read_only_database(TENDERS_DB) as connection:
            row = connection.execute(
                """
                SELECT id, consultation_id, org_acronyme, name, url, file_type,
                       download_attempted_at, download_error
                FROM offer_documents
                WHERE id = ? AND consultation_id = ? AND org_acronyme = ?
                """,
                (document_id, consultation_id, org_acronyme),
            ).fetchone()
    except sqlite3.Error as error:
        logger.exception("Could not load document document_id=%s", document_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offer document is temporarily unavailable",
        ) from error

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offer document not found",
        )
    return row


def download_name(document_id: int, name: str, media_type: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", normalize_text(name)).strip("-.")
    cleaned = cleaned or f"document-{document_id}"
    if media_type == "application/pdf" and not cleaned.endswith(".pdf"):
        cleaned += ".pdf"
    return cleaned[:150]


def buffer_document(first_chunk: bytes, iterator, max_bytes: int | None = None):
    byte_limit = max_bytes or MAX_DOCUMENT_BYTES
    storage = SpooledTemporaryFile(max_size=2 * 1024 * 1024, mode="w+b")
    total = 0
    try:
        for chunk in itertools.chain((first_chunk,), iterator):
            total += len(chunk)
            if total > byte_limit:
                logger.warning("Rejected document above byte limit")
                raise HTTPException(
                    status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                    detail="The offer document is too large to open in Safaqat",
                )
            if chunk:
                storage.write(chunk)
        storage.seek(0)
        return storage, total
    except Exception:
        storage.close()
        raise


def anonymous_download_form(page_html: str, page_url: str) -> tuple[str, dict]:
    page = html.fromstring(page_html)
    forms = page.xpath('//form[.//*[@id="ctl0_CONTENU_PAGE_validateButton"]]')
    if not forms:
        raise ValueError("The public portal download form was not found")
    form = forms[0]
    action = urljoin(page_url, form.get("action") or page_url)
    if not safe_portal_url(action):
        raise ValueError("The public portal form target is invalid")

    data: dict[str, str] = {}
    for field in form.xpath(".//input | .//select | .//textarea"):
        name = field.get("name")
        if not name or field.get("disabled") is not None:
            continue
        if field.tag == "input":
            input_type = (field.get("type") or "text").lower()
            if input_type in {"submit", "button", "image", "file", "reset"}:
                continue
            if input_type in {"checkbox", "radio"} and field.get("checked") is None:
                continue
            data[name] = field.get("value") or (
                "on" if input_type == "checkbox" else ""
            )
        elif field.tag == "select":
            options = field.xpath("./option")
            selected = next(
                (option for option in options if option.get("selected") is not None),
                options[0] if options else None,
            )
            if selected is not None:
                data[name] = selected.get("value") or selected.text_content().strip()
        else:
            data[name] = field.text or ""
    anonymous = form.xpath(
        './/input[@id="ctl0_CONTENU_PAGE_EntrepriseFormulaireDemande_choixAnonyme"]'
    )
    consent = form.xpath(
        './/input[@id="ctl0_CONTENU_PAGE_EntrepriseFormulaireDemande_accepterConditions"]'
    )
    submit = form.xpath('.//input[@id="ctl0_CONTENU_PAGE_validateButton"]')
    if not anonymous or not consent or not submit:
        raise ValueError("The public portal download form has changed")

    radio_name = anonymous[0].get("name")
    if not radio_name:
        raise ValueError("The anonymous download option is invalid")
    data[radio_name] = anonymous[0].get("value") or ""
    data[consent[0].get("name")] = "on"
    submit_name = submit[0].get("name")
    if not submit_name:
        raise ValueError("The public portal submit control is invalid")
    data["PRADO_POSTBACK_TARGET"] = submit_name
    data["PRADO_POSTBACK_PARAMETER"] = "undefined"
    return action, data


def download_anonymous_archive(url: str):
    with httpx.Client(
        timeout=90,
        follow_redirects=False,
        headers=dict(portal_client.headers),
    ) as client:
        form_response = portal_request(url, client=client)
        form_response.raise_for_status()
        action, data = anonymous_download_form(form_response.text, str(form_response.url))
        client.headers["Referer"] = str(form_response.url)
        response = portal_request(
            action,
            method="POST",
            data=data,
            stream=True,
            client=client,
        )
        try:
            response.raise_for_status()
            iterator = response.iter_bytes()
            first_chunk = next(iterator, b"")
            media_type = response.headers.get("content-type", "").split(";", 1)[0]
            if not first_chunk.startswith(b"PK") and "zip" not in media_type:
                raise ValueError("The public portal did not return a ZIP archive")
            return buffer_document(first_chunk, iterator, MAX_ARCHIVE_BYTES)
        finally:
            response.close()


def safe_archive_path(filename: str) -> PurePosixPath:
    path = PurePosixPath(filename.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("The ZIP archive contains an unsafe path")
    return path


def document_storage_root(document: sqlite3.Row | dict) -> Path:
    offer_key = hashlib.sha256(
        f'{document["consultation_id"]}\0{document["org_acronyme"]}'.encode()
    ).hexdigest()[:24]
    return OFFER_FILES_DIR / offer_key / str(document["id"])


def extract_archive(
    archive,
    archive_size: int,
    document: sqlite3.Row,
) -> list[dict]:
    archive.seek(0)
    archive_hash = hashlib.sha256(archive.read()).hexdigest()
    archive.seek(0)
    document_root = document_storage_root(document)
    document_root.mkdir(parents=True, exist_ok=True)
    destination_root = document_root / archive_hash[:16]
    temporary_root = Path(tempfile.mkdtemp(prefix=".extract-", dir=document_root))
    extracted: list[dict] = []
    total_size = 0
    seen_paths: set[str] = set()

    try:
        with zipfile.ZipFile(archive) as package:
            members = [member for member in package.infolist() if not member.is_dir()]
            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError("The ZIP archive contains too many files")
            for member in members:
                mode = member.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise ValueError("The ZIP archive contains a symbolic link")
                relative = safe_archive_path(member.filename)
                normalized_path = str(relative).casefold()
                if normalized_path in seen_paths:
                    raise ValueError("The ZIP archive contains a duplicate path")
                seen_paths.add(normalized_path)
                total_size += member.file_size
                if total_size > MAX_ARCHIVE_BYTES:
                    raise ValueError("The extracted ZIP archive is too large")

                destination = temporary_root.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                written = 0
                with package.open(member) as source, destination.open("wb") as output:
                    while chunk := source.read(1024 * 1024):
                        written += len(chunk)
                        total_actual = sum(item["size"] for item in extracted) + written
                        if total_actual > MAX_ARCHIVE_BYTES:
                            raise ValueError("The extracted ZIP archive is too large")
                        digest.update(chunk)
                        output.write(chunk)

                media_type = mimetypes.guess_type(relative.name)[0] or "application/octet-stream"
                extracted.append(
                    {
                        "name": str(relative),
                        "relative_path": str(
                            destination_root.joinpath(*relative.parts).relative_to(
                                OFFER_FILES_DIR
                            )
                        ),
                        "media_type": media_type,
                        "size": written,
                        "sha256": digest.hexdigest(),
                    }
                )

        if not extracted:
            raise ValueError("The ZIP archive is empty")
        if destination_root.exists():
            shutil.rmtree(temporary_root)
        else:
            os.replace(temporary_root, destination_root)
    except Exception:
        shutil.rmtree(temporary_root, ignore_errors=True)
        raise
    logger.info(
        "Extracted offer archive document_id=%s archive_bytes=%s files=%s",
        document["id"],
        archive_size,
        len(extracted),
    )
    return extracted


def cached_offer_files(
    consultation_id: str,
    org_acronyme: str,
    offer_document_id: int | None = None,
) -> list[dict]:
    document_filter = " AND offer_document_id = ?" if offer_document_id is not None else ""
    parameters: tuple = (consultation_id, org_acronyme)
    if offer_document_id is not None:
        parameters += (offer_document_id,)
    with read_only_database(TENDERS_DB) as connection:
        rows = connection.execute(
            f"""
            SELECT id, name, media_type, size
            FROM offer_files
            WHERE consultation_id = ? AND org_acronyme = ?
            {document_filter}
            ORDER BY name COLLATE NOCASE
            """,
            parameters,
        ).fetchall()
    return [dict(row) for row in rows]


def cache_archive_files(document: sqlite3.Row) -> list[dict]:
    archive, archive_size = download_anonymous_archive(str(document["url"]))
    try:
        extracted = extract_archive(archive, archive_size, document)
    finally:
        archive.close()

    with sqlite3.connect(TENDERS_DB, timeout=30) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute(
                "DELETE FROM offer_files WHERE offer_document_id = ?",
                (document["id"],),
            )
            connection.executemany(
                """
                INSERT INTO offer_files (
                    offer_document_id, consultation_id, org_acronyme, name,
                    relative_path, media_type, size, sha256
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        document["id"],
                        document["consultation_id"],
                        document["org_acronyme"],
                        item["name"],
                        item["relative_path"],
                        item["media_type"],
                        item["size"],
                        item["sha256"],
                    )
                    for item in extracted
                ],
            )
            connection.execute(
                """
                UPDATE offer_documents
                SET download_attempted_at = CURRENT_TIMESTAMP, download_error = NULL
                WHERE id = ?
                """,
                (document["id"],),
            )
    document_root = document_storage_root(document)
    relative_parts = Path(extracted[0]["relative_path"]).parts
    active_directory = relative_parts[
        len(document_root.relative_to(OFFER_FILES_DIR).parts)
    ]
    for directory in document_root.iterdir():
        if directory.is_dir() and directory.name != active_directory:
            shutil.rmtree(directory)
    return cached_offer_files(document["consultation_id"], document["org_acronyme"])


def document_download_due(document: sqlite3.Row) -> bool:
    attempted_at = document["download_attempted_at"]
    if not attempted_at or not document["download_error"]:
        return True
    try:
        attempted = datetime.fromisoformat(str(attempted_at)).replace(tzinfo=UTC)
    except ValueError:
        return True
    return (
        datetime.now(UTC) - attempted
    ).total_seconds() >= DOCUMENT_DOWNLOAD_RETRY_SECONDS


def record_document_download_error(document_id: int, error: Exception) -> None:
    message = f"{type(error).__name__}: {error}"[:500]
    try:
        with sqlite3.connect(TENDERS_DB, timeout=30) as connection:
            connection.execute(
                """
                UPDATE offer_documents
                SET download_attempted_at = CURRENT_TIMESTAMP, download_error = ?
                WHERE id = ?
                """,
                (message, document_id),
            )
    except sqlite3.Error:
        logger.exception(
            "Could not record document download failure document_id=%s",
            document_id,
        )


def stored_offer_file(
    consultation_id: str,
    org_acronyme: str,
    file_id: int,
) -> sqlite3.Row:
    with read_only_database(TENDERS_DB) as connection:
        row = connection.execute(
            """
            SELECT id, name, relative_path, media_type, size
            FROM offer_files
            WHERE id = ? AND consultation_id = ? AND org_acronyme = ?
            """,
            (file_id, consultation_id, org_acronyme),
        ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offer file not found",
        )
    return row


@app.get("/offers/{offer_id}/files/{file_id}/content")
def offer_file_content(
    offer_id: str,
    file_id: int,
    profile: dict = Depends(load_enterprise),
):
    point = active_offer_point(offer_id)
    if not profile.get("portal_download_consent"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Portal download consent is required",
        )
    payload = point.payload or {}
    consultation_id = str(payload.get("consultation_id", ""))
    org_acronyme = str(payload.get("org_acronyme", ""))
    row = stored_offer_file(consultation_id, org_acronyme, file_id)
    path = (OFFER_FILES_DIR / row["relative_path"]).resolve()
    storage_root = OFFER_FILES_DIR.resolve()
    if storage_root not in path.parents or not path.is_file():
        logger.error("Rejected missing or unsafe cached file file_id=%s", file_id)
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Offer file not found",
        )

    media_type = str(row["media_type"])
    inline = media_type == "application/pdf" or media_type.startswith(("image/", "text/"))
    logger.info(
        "Returned cached offer file offer_id=%s file_id=%s enterprise_id=%s",
        offer_id,
        file_id,
        profile["id"],
    )
    return FileResponse(
        path,
        media_type=media_type,
        filename=str(row["name"]).rsplit("/", 1)[-1],
        content_disposition_type="inline" if inline else "attachment",
        headers={"Cache-Control": "private, no-store"},
    )


@app.get("/offers/{offer_id}/documents/{document_id}/content")
def document_content(
    offer_id: str,
    document_id: int,
    profile: dict = Depends(load_enterprise),
):
    point = active_offer_point(offer_id)
    payload = point.payload or {}
    consultation_id = str(payload.get("consultation_id", ""))
    org_acronyme = str(payload.get("org_acronyme", ""))
    document = stored_document(consultation_id, org_acronyme, document_id)
    url = str(document["url"])
    if "EntrepriseDemandeTelechargement" in url and not profile.get(
        "portal_download_consent"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Portal download consent is required",
        )
    if not safe_portal_url(url):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offer document source is invalid",
        )

    remote = None
    try:
        remote = portal_request(url, stream=True)
        remote.raise_for_status()
        if not safe_portal_url(str(remote.url)):
            raise ValueError("Document download redirected outside the public portal")

        media_type = remote.headers.get("content-type", "").split(";", 1)[0].lower()
        content_length = int(remote.headers.get("content-length", "0") or 0)
        if content_length > MAX_DOCUMENT_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="The offer document is too large to open in Safaqat",
            )
        if media_type in {"text/html", "application/xhtml+xml"}:
            remote.close()
            remote = None
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "code": "portal_action_required",
                    "message": (
                        "The official portal requires your information before download"
                    ),
                    "portal_url": url,
                },
            )

        allowed_types = {
            "application/pdf",
            "application/zip",
            "application/x-zip-compressed",
            "application/octet-stream",
            "application/msword",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        if media_type not in allowed_types:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail="This document type cannot be opened in Safaqat",
            )

        iterator = remote.iter_bytes()
        first_chunk = next(iterator, b"")
        if first_chunk.startswith(b"%PDF-"):
            media_type = "application/pdf"
        stored_file, stored_size = buffer_document(first_chunk, iterator)
        remote.close()
        remote = None
        filename = download_name(document_id, str(document["name"]), media_type)
        disposition = "inline" if media_type == "application/pdf" else "attachment"
        logger.info(
            "Streaming offer document offer_id=%s document_id=%s enterprise_id=%s",
            offer_id,
            document_id,
            profile["id"],
        )
        return StreamingResponse(
            iter(lambda: stored_file.read(64 * 1024), b""),
            media_type=media_type,
            headers={
                "Content-Disposition": f'{disposition}; filename="{filename}"',
                "Content-Length": str(stored_size),
                "Cache-Control": "private, no-store",
            },
            background=BackgroundTask(stored_file.close),
        )
    except HTTPException:
        if remote is not None:
            remote.close()
        raise
    except (httpx.HTTPError, OSError, ValueError) as error:
        if remote is not None:
            remote.close()
        logger.exception("Document download failed document_id=%s", document_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="The official portal document is temporarily unavailable",
        ) from error


def offer_payload(point) -> dict:
    payload = point.payload or {}
    return {
        "id": str(point.id),
        "reference": payload.get("reference"),
        "object": payload.get("objet"),
        "category": payload.get("category_display") or payload.get("category"),
        "buyer": payload.get("buyer"),
        "procedure": payload.get("procedure"),
        "location": payload.get("location_display"),
        "publication_date": payload.get("publication_date"),
        "deadline": payload.get("deadline"),
        "source_url": payload.get("source_url"),
    }


@app.get("/offers/{offer_id}/documents")
def offer_document_list(
    offer_id: str,
    profile: dict = Depends(load_enterprise),
) -> dict:
    point = active_offer_point(offer_id)
    payload = point.payload or {}
    consultation_id = str(payload.get("consultation_id", ""))
    org_acronyme = str(payload.get("org_acronyme", ""))
    if not consultation_id or not org_acronyme:
        logger.error("Indexed offer is missing source identifiers offer_id=%s", offer_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offer data is incomplete",
        )

    source_url = str(payload.get("source_url", ""))
    documents = offer_documents(consultation_id, org_acronyme, source_url)
    files = cached_offer_files(consultation_id, org_acronyme)
    protected_documents = [
        item for item in documents if item.get("requires_portal")
    ]
    if protected_documents and not profile.get("portal_download_consent"):
        return {
            "documents": documents,
            "files": [],
            "consent_required": True,
        }

    preparation_error = None
    missing_documents = []
    for item in protected_documents:
        if cached_offer_files(consultation_id, org_acronyme, int(item["id"])):
            continue
        document = stored_document(
            consultation_id,
            org_acronyme,
            int(item["id"]),
        )
        if document_download_due(document):
            missing_documents.append(document)
        elif document["download_error"]:
            preparation_error = (
                "Le portail officiel n’a pas fourni l’archive. "
                "Le téléchargement officiel reste disponible."
            )

    if missing_documents:
        if document_sync_lock.acquire(blocking=False):
            try:
                for document in missing_documents:
                    try:
                        cache_archive_files(document)
                    except (
                        httpx.HTTPError,
                        OSError,
                        ValueError,
                        zipfile.BadZipFile,
                    ) as error:
                        record_document_download_error(int(document["id"]), error)
                        preparation_error = (
                            "Le portail officiel n’a pas fourni l’archive. "
                            "Le téléchargement officiel reste disponible."
                        )
                        logger.exception(
                            "Automatic archive download failed offer_id=%s document_id=%s",
                            offer_id,
                            document["id"],
                        )
                files = cached_offer_files(consultation_id, org_acronyme)
            finally:
                document_sync_lock.release()
        else:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "documents_preparing"},
                headers={"Retry-After": "3"},
            )

    logger.info(
        "Returned offer documents offer_id=%s enterprise_id=%s documents=%s files=%s",
        offer_id,
        profile["id"],
        len(documents),
        len(files),
    )
    return {
        "documents": documents,
        "files": files,
        "consent_required": False,
        "preparation_error": preparation_error,
    }


@app.get("/offers/{offer_id}")
def offer_details(
    offer_id: str,
    profile: dict = Depends(load_enterprise),
) -> dict:
    point = active_offer_point(offer_id)
    logger.info(
        "Returned offer details offer_id=%s enterprise_id=%s",
        offer_id,
        profile["id"],
    )
    return offer_payload(point)


def query_offers(
    query_text: str,
    conditions: list,
    limit: int,
) -> list:
    response = qdrant.query_points(
        collection_name=COLLECTION,
        prefetch=[
            models.Prefetch(
                query=models.Document(text=query_text, model=DENSE_MODEL),
                using=DENSE_VECTOR,
                limit=50,
            ),
            models.Prefetch(
                query=models.Document(text=query_text, model=SPARSE_MODEL),
                using=SPARSE_VECTOR,
                limit=50,
            ),
        ],
        query=models.FusionQuery(fusion=models.Fusion.RRF),
        query_filter=models.Filter(must=conditions),
        limit=limit,
        with_payload=True,
    )
    return list(response.points)


@app.get("/matches/top")
def top_matches(
    limit: int = 10,
    profile: dict = Depends(load_enterprise),
) -> dict:
    if limit < 1 or limit > 10:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 10",
        )
    if not index_ready.is_set():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Offer index is still being prepared",
        )

    enterprise_id = profile["id"]
    keywords = [str(value).strip() for value in profile["keywords"] if str(value).strip()]
    if not keywords:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add at least one enterprise keyword before requesting matches",
        )

    categories = [normalize_text(value) for value in profile["categories"]]
    locations = sorted(
        {
            location
            for value in profile["locations"]
            for location in normalize_locations(value)
        }
    )
    conditions = [
        models.FieldCondition(
            key="deadline_timestamp",
            range=models.Range(gt=datetime.now(UTC).timestamp()),
        )
    ]
    if categories:
        conditions.append(
            models.FieldCondition(
                key="category",
                match=models.MatchAny(any=categories),
            )
        )
    if locations:
        conditions.append(
            models.FieldCondition(
                key="locations",
                match=models.MatchAny(any=locations),
            )
        )

    query_text = " ".join(keywords)
    try:
        points = query_offers(query_text, conditions, limit)
        if locations and len(points) < limit:
            fallback_conditions = conditions[:-1]
            seen = {str(point.id) for point in points}
            for point in query_offers(query_text, fallback_conditions, limit):
                if str(point.id) not in seen:
                    points.append(point)
                    seen.add(str(point.id))
                if len(points) == limit:
                    break
    except Exception as error:
        logger.exception("Hybrid search failed enterprise_id=%s", enterprise_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Matching is temporarily unavailable",
        ) from error

    matches = []
    for point in points:
        payload = point.payload or {}
        matches.append(
            {
                "offer_id": str(point.id),
                "score": point.score,
                "reasons": match_reasons(profile, payload),
                "reference": payload.get("reference"),
                "objet": payload.get("objet"),
                "category": payload.get("category"),
                "buyer": payload.get("buyer"),
                "location": payload.get("location_display"),
                "publication_date": payload.get("publication_date"),
                "deadline": payload.get("deadline"),
                "source_url": payload.get("source_url"),
            }
        )

    logger.info("Returned %s matches enterprise_id=%s", len(matches), enterprise_id)
    return {"matches": matches, "total": len(matches)}
