#!/usr/bin/env python3

import argparse
import logging
import os
import re
import sqlite3
import time
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlencode

import requests
from lxml import html
from requests.exceptions import ConnectionError, HTTPError, Timeout


BASE_URL = "https://www.marchespublics.gov.ma"
LISTING_URL = (
    BASE_URL
    + "/index.php?page=entreprise.EntrepriseAdvancedSearch"
    + "&AllCons&searchAnnCons"
)

TIMEOUT = 60
MAX_ATTEMPTS = 3
RETRY_DELAYS = (2, 5)
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}

DEFAULT_DB = os.getenv(
    "SCRAPER_DB",
    "/app/data/tenders.db",
)
DEFAULT_LOG = os.getenv(
    "SCRAPER_LOG",
    "/app/logs/scraper.log",
)

PAGE_SIZE_FIELD = (
    "ctl0$CONTENU_PAGE$resultSearch$listePageSizeTop"
)
NEXT_PAGE_TARGET = (
    "ctl0$CONTENU_PAGE$resultSearch$PagerTop$ctl2"
)

FIELDS = (
    "consultation_id",
    "org_acronyme",
    "reference",
    "objet",
    "procedure",
    "categorie",
    "acheteur_public",
    "lieu_execution",
    "date_publication",
    "date_limite",
    "source_url",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    consultation_id TEXT NOT NULL,
    org_acronyme TEXT NOT NULL,
    reference TEXT,
    objet TEXT,
    procedure TEXT,
    categorie TEXT,
    acheteur_public TEXT,
    lieu_execution TEXT,
    date_publication TEXT,
    date_limite TEXT,
    source_url TEXT,
    last_seen_run_id TEXT,
    documents_checked_at TEXT,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (consultation_id, org_acronyme)
);

CREATE TABLE IF NOT EXISTS scrape_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    last_completed_page INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER,
    error TEXT
);

CREATE TABLE IF NOT EXISTS offer_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    consultation_id TEXT NOT NULL,
    org_acronyme TEXT NOT NULL,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    file_type TEXT NOT NULL,
    download_attempted_at TEXT,
    download_error TEXT,
    UNIQUE (consultation_id, org_acronyme, url),
    FOREIGN KEY (consultation_id, org_acronyme)
        REFERENCES offers(consultation_id, org_acronyme) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS documents_by_offer
    ON offer_documents (consultation_id, org_acronyme);

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
    FOREIGN KEY (offer_document_id)
        REFERENCES offer_documents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS files_by_offer
    ON offer_files (consultation_id, org_acronyme);
"""

logger = logging.getLogger("scraper")


def configure_logging(log_file):
    Path(log_file).parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(console_handler)
    logger.addHandler(file_handler)


def clean_text(element):
    if element is None:
        return ""

    return " ".join(
        element.text_content().split()
    )


def find_by_id_part(parent, id_part):
    for element in parent.iter():
        element_id = element.get("id", "")

        if id_part in element_id:
            return element

    return None


def child_div_text(element):
    if element is None:
        return ""

    for child in element:
        if child.tag == "div":
            return clean_text(child)

    return clean_text(element)


def hidden_value(row, name_ending):
    for element in row.iter("input"):
        name = element.get("name", "")

        if name.endswith(name_ending):
            return element.get(
                "value",
                "",
            ).strip()

    return ""


def first_match(pattern, text):
    match = re.search(pattern, text)

    if match:
        return match.group(0)

    return ""


def extract_offer(row):
    cells = row.findall("td")

    if len(cells) < 6:
        raise ValueError(
            "Offer row has too few columns"
        )

    procedure_cell = cells[1]
    details_cell = cells[2]
    location_cell = cells[3]
    closing_cell = cells[4]

    consultation_id = hidden_value(
        row,
        "$refCons",
    )
    org_acronyme = hidden_value(
        row,
        "$orgCons",
    )

    if not consultation_id or not org_acronyme:
        raise ValueError(
            "Offer identifier is missing"
        )

    reference_element = find_by_id_part(
        details_cell,
        "_reference",
    )
    reference = clean_text(
        reference_element
    )

    object_element = find_by_id_part(
        details_cell,
        "infosBullesObjet",
    )
    object_text = child_div_text(
        object_element
    )

    buyer_element = find_by_id_part(
        details_cell,
        "panelBlocDenomination",
    )
    public_buyer = clean_text(
        buyer_element
    )
    public_buyer = re.sub(
        r"^Acheteur public\s*:\s*",
        "",
        public_buyer,
    )

    procedure_element = find_by_id_part(
        procedure_cell,
        "panelBlocTypesProc",
    )
    procedure = clean_text(
        procedure_element
    )

    category_element = find_by_id_part(
        procedure_cell,
        "panelBlocCategorie",
    )
    category = clean_text(
        category_element
    )

    procedure_text = clean_text(
        procedure_cell
    )
    publication_date = first_match(
        r"\d{2}/\d{2}/\d{4}",
        procedure_text,
    )

    location_element = find_by_id_part(
        location_cell,
        "infosLieuExecution",
    )
    execution_location = child_div_text(
        location_element
    )

    closing_elements = closing_cell.find_class(
        "cloture-line"
    )

    closing_text = ""

    if closing_elements:
        closing_text = clean_text(
            closing_elements[0]
        )

    closing_date = first_match(
        r"\d{2}/\d{2}/\d{4}",
        closing_text,
    )
    closing_time = first_match(
        r"\d{2}:\d{2}",
        closing_text,
    )

    date_limite = " ".join(
        value
        for value in (
            closing_date,
            closing_time,
        )
        if value
    )

    query = urlencode(
        {
            "page": (
                "entreprise."
                "EntrepriseDetailConsultation"
            ),
            "refConsultation": consultation_id,
            "orgAcronyme": org_acronyme,
        }
    )

    return {
        "consultation_id": consultation_id,
        "org_acronyme": org_acronyme,
        "reference": reference,
        "objet": object_text,
        "procedure": procedure,
        "categorie": category,
        "acheteur_public": public_buyer,
        "lieu_execution": execution_location,
        "date_publication": publication_date,
        "date_limite": date_limite,
        "source_url": (
            f"{BASE_URL}/index.php?{query}"
        ),
    }


def parse_page(page_html):
    document = html.fromstring(page_html)

    tables = document.find_class(
        "table-results"
    )

    if not tables:
        raise RuntimeError(
            "Results table not found"
        )

    rows = []

    for row in tables[0].iter("tr"):
        if hidden_value(row, "$refCons"):
            rows.append(row)

    if not rows:
        raise RuntimeError(
            "No offers found"
        )

    page_input = document.get_element_by_id(
        "ctl0_CONTENU_PAGE_resultSearch_numPageTop",
        None,
    )
    page_count = document.get_element_by_id(
        "ctl0_CONTENU_PAGE_resultSearch_nombrePageTop",
        None,
    )

    if page_input is None or page_count is None:
        raise RuntimeError(
            "Pagination information not found"
        )

    offers = [
        extract_offer(row)
        for row in rows
    ]

    current_page = int(
        page_input.get("value")
    )
    total_pages = int(
        clean_text(page_count)
    )

    return offers, current_page, total_pages


def form_values(page_html):
    document = html.fromstring(page_html)

    if not document.forms:
        raise RuntimeError(
            "Page form not found"
        )

    values = {}

    for element in document.forms[0].iter():
        name = element.get("name")

        if not name:
            continue

        if element.get("disabled") is not None:
            continue

        if element.tag == "input":
            input_type = element.get(
                "type",
                "text",
            ).lower()

            if input_type in {"hidden", "text"}:
                values[name] = element.get(
                    "value",
                    "",
                )

        elif element.tag == "select":
            options = list(
                element.iter("option")
            )

            selected = next(
                (
                    option
                    for option in options
                    if option.get("selected")
                    is not None
                ),
                options[0] if options else None,
            )

            if selected is not None:
                values[name] = selected.get(
                    "value",
                    clean_text(selected),
                )

    return values


def request_url(
    session,
    method,
    url,
    data=None,
):
    last_error = None

    for attempt in range(
        1,
        MAX_ATTEMPTS + 1,
    ):
        try:
            response = session.request(
                method,
                url,
                data=data,
                timeout=TIMEOUT,
            )

            response.raise_for_status()

            return response.text

        except (
            Timeout,
            ConnectionError,
        ) as error:
            last_error = error

        except HTTPError as error:
            status_code = (
                error.response.status_code
                if error.response is not None
                else None
            )

            if (
                status_code
                not in RETRYABLE_STATUS_CODES
            ):
                logger.error(
                    "Request failed with HTTP %s",
                    status_code,
                )
                raise

            last_error = error

        if attempt == MAX_ATTEMPTS:
            logger.error(
                "%s request failed after "
                "%s attempts: %s",
                method,
                MAX_ATTEMPTS,
                last_error,
            )
            raise last_error

        delay = RETRY_DELAYS[
            attempt - 1
        ]

        logger.warning(
            "%s request failed on attempt "
            "%s/%s: %s. Retrying in %ss",
            method,
            attempt,
            MAX_ATTEMPTS,
            last_error,
            delay,
        )

        time.sleep(delay)

    raise RuntimeError(
        "Request failed unexpectedly"
    )


def get_first_page(session):
    return request_url(
        session,
        "GET",
        LISTING_URL,
    )


def postback(
    session,
    page_html,
    target,
    changes=None,
):
    values = form_values(page_html)

    values["PRADO_POSTBACK_TARGET"] = target
    values["PRADO_POSTBACK_PARAMETER"] = ""

    if changes:
        values.update(changes)

    return request_url(
        session,
        "POST",
        LISTING_URL,
        data=values,
    )


def initialize_schema(connection):
    connection.executescript(SCHEMA)
    offer_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(offers)")
    }
    if "last_seen_run_id" not in offer_columns:
        connection.execute("ALTER TABLE offers ADD COLUMN last_seen_run_id TEXT")
    if "documents_checked_at" not in offer_columns:
        connection.execute("ALTER TABLE offers ADD COLUMN documents_checked_at TEXT")
    document_columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(offer_documents)")
    }
    if "download_attempted_at" not in document_columns:
        connection.execute(
            "ALTER TABLE offer_documents ADD COLUMN download_attempted_at TEXT"
        )
    if "download_error" not in document_columns:
        connection.execute("ALTER TABLE offer_documents ADD COLUMN download_error TEXT")

    # These tables belonged to the previous full-snapshot implementation.
    connection.execute("DROP TABLE IF EXISTS offer_documents_staging")
    connection.execute("DROP TABLE IF EXISTS offers_staging")
    connection.execute("DROP TABLE IF EXISTS scrape_runs")
    connection.commit()


def resume_or_create_run(connection):
    row = connection.execute(
        """
        SELECT run_id, status, last_completed_page
        FROM scrape_state
        WHERE id = 1
        """
    ).fetchone()

    if row is not None and row[1] in {"running", "failed", "paused"}:
        run_id, _, last_completed_page = row
        with connection:
            connection.execute(
                """
                UPDATE scrape_state
                SET status = 'running', error = NULL
                WHERE id = 1
                """
            )
        logger.info("Resuming scrape run %s after page %s", run_id, last_completed_page)
        return run_id, last_completed_page

    run_id = uuid.uuid4().hex
    with connection:
        connection.execute(
            """
            INSERT INTO scrape_state (
                id, run_id, status, started_at, completed_at,
                last_completed_page, total_pages, error
            ) VALUES (1, ?, 'running', CURRENT_TIMESTAMP, NULL, 0, NULL, NULL)
            ON CONFLICT(id) DO UPDATE SET
                run_id = excluded.run_id,
                status = excluded.status,
                started_at = excluded.started_at,
                completed_at = NULL,
                last_completed_page = 0,
                total_pages = NULL,
                error = NULL
            """,
            (run_id,),
        )
    logger.info("Created scrape run %s", run_id)
    return run_id, 0


def save_page(connection, run_id, offers, page, total_pages):
    columns = ", ".join(FIELDS)
    placeholders = ", ".join("?" for _ in FIELDS)
    updates = ", ".join(
        f"{field} = excluded.{field}"
        for field in FIELDS[2:]
    )
    query = f"""
        INSERT INTO offers ({columns}, last_seen_run_id)
        VALUES ({placeholders}, ?)
        ON CONFLICT (consultation_id, org_acronyme) DO UPDATE SET
            {updates},
            last_seen_run_id = excluded.last_seen_run_id,
            last_seen_at = CURRENT_TIMESTAMP
    """
    values = [
        (*(offer[field] for field in FIELDS), run_id)
        for offer in offers
    ]

    with connection:
        connection.executemany(query, values)
        connection.execute(
            """
            UPDATE scrape_state
            SET last_completed_page = ?, total_pages = ?
            WHERE id = 1 AND run_id = ?
            """,
            (page, total_pages, run_id),
        )


def mark_run(connection, run_id, status, error=None):
    with connection:
        connection.execute(
            """
            UPDATE scrape_state
            SET status = ?, error = ?
            WHERE id = 1 AND run_id = ?
            """,
            (status, error, run_id),
        )


def complete_run(connection, run_id):
    with connection:
        connection.execute(
            """
            DELETE FROM offers
            WHERE last_seen_run_id IS NULL OR last_seen_run_id != ?
            """,
            (run_id,),
        )
        connection.execute(
            """
            UPDATE scrape_state
            SET status = 'completed', completed_at = CURRENT_TIMESTAMP, error = NULL
            WHERE id = 1 AND run_id = ?
            """,
            (run_id,),
        )
    return connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0]


def run(
    db_path,
    page_size,
    page_limit,
):
    Path(db_path).parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    connection = sqlite3.connect(db_path, timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA journal_mode = WAL")
    initialize_schema(connection)
    run_id, last_completed_page = resume_or_create_run(connection)

    try:
        with requests.Session() as session:
            session.headers["User-Agent"] = (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36 Safaqat/0.1"
            )

            page_html = get_first_page(
                session
            )

            if page_size != 10:
                page_html = postback(
                    session,
                    page_html,
                    PAGE_SIZE_FIELD,
                    {
                        PAGE_SIZE_FIELD: str(
                            page_size
                        )
                    },
                )

            expected_page = 1

            while True:
                (
                    offers,
                    current_page,
                    total_pages,
                ) = parse_page(page_html)

                if current_page != expected_page:
                    raise RuntimeError(
                        "Expected page "
                        f"{expected_page}, "
                        "received page "
                        f"{current_page}"
                    )

                if current_page > last_completed_page:
                    save_page(
                        connection,
                        run_id,
                        offers,
                        current_page,
                        total_pages,
                    )
                    logger.info(
                        "Run %s page %s/%s: %s offers saved",
                        run_id,
                        current_page,
                        total_pages,
                        len(offers),
                    )
                    last_completed_page = current_page
                else:
                    logger.info(
                        "Run %s page %s/%s already saved; navigating to resume point",
                        run_id,
                        current_page,
                        total_pages,
                    )

                reached_limit = (
                    page_limit is not None
                    and current_page
                    >= page_limit
                )

                if current_page >= total_pages:
                    total_offers = complete_run(connection, run_id)
                    logger.info(
                        "Run %s completed with %s active offers",
                        run_id,
                        total_offers,
                    )
                    return total_offers

                if reached_limit:
                    mark_run(connection, run_id, "paused")
                    logger.info(
                        "Run %s paused after page %s",
                        run_id,
                        current_page,
                    )
                    return connection.execute("SELECT COUNT(*) FROM offers").fetchone()[0]

                time.sleep(1)

                page_html = postback(
                    session,
                    page_html,
                    NEXT_PAGE_TARGET,
                )

                expected_page += 1

    except Exception as error:
        mark_run(connection, run_id, "failed", f"{type(error).__name__}: {error}")
        logger.error("Run %s failed after page %s", run_id, last_completed_page)
        raise
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Scrape public tender listings"
        )
    )

    parser.add_argument(
        "--pages",
        type=int,
        default=None,
        help=(
            "Maximum pages to scrape "
            "(default: all pages)"
        ),
    )

    parser.add_argument(
        "--page-size",
        type=int,
        choices=(10, 20, 50, 100, 500),
        default=500,
    )

    parser.add_argument(
        "--db",
        default=DEFAULT_DB,
    )

    parser.add_argument(
        "--log-file",
        default=DEFAULT_LOG,
    )

    args = parser.parse_args()

    if (
        args.pages is not None
        and args.pages < 1
    ):
        parser.error(
            "--pages must be at least 1"
        )

    configure_logging(
        args.log_file
    )

    logger.info(
        "Starting scrape: "
        "page size=%s, page limit=%s",
        args.page_size,
        args.pages or "all",
    )

    total_offers = run(
        db_path=args.db,
        page_size=args.page_size,
        page_limit=args.pages,
    )

    logger.info(
        "Scrape run finished: "
        "%s offers saved in %s",
        total_offers,
        args.db,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception(
            "Scraper failed"
        )
        raise SystemExit(1)
