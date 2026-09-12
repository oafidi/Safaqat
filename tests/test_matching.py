import io
import sqlite3
import tempfile
import unittest
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

import main


class MatchingTests(unittest.TestCase):
    @classmethod
    def tearDownClass(cls) -> None:
        main.auth_client.close()
        main.qdrant.close()

    def test_location_variants_share_canonical_terms(self) -> None:
        self.assertIn("rabat", main.normalize_locations("Préfecture de Rabat"))
        regional = main.normalize_locations("Rabat-Salé-Kénitra")
        self.assertIn("rabat", regional)
        self.assertIn("sale", regional)
        self.assertIn("kenitra", regional)

    def test_portal_redirects_are_followed_only_on_the_official_host(self) -> None:
        redirect = Mock()
        redirect.is_redirect = True
        redirect.headers = {"location": "/safe-document"}
        final = Mock()
        final.is_redirect = False
        final.headers = {}

        with (
            patch.object(
                main.portal_client,
                "build_request",
                side_effect=lambda _, url, **__: url,
            ),
            patch.object(main.portal_client, "send", side_effect=[redirect, final]) as send,
        ):
            result = main.portal_request(
                "https://www.marchespublics.gov.ma/first",
                stream=True,
            )

        self.assertIs(result, final)
        self.assertEqual(send.call_count, 2)
        redirect.close.assert_called_once()

    def test_portal_redirect_to_an_external_host_is_rejected(self) -> None:
        redirect = Mock()
        redirect.is_redirect = True
        redirect.headers = {"location": "http://127.0.0.1/private"}

        with (
            patch.object(main.portal_client, "build_request", return_value="request"),
            patch.object(main.portal_client, "send", return_value=redirect),
        ):
            with self.assertRaises(ValueError):
                main.portal_request("https://www.marchespublics.gov.ma/first")

        redirect.close.assert_called_once()

    def test_portal_rejects_unencrypted_official_url(self) -> None:
        self.assertFalse(
            main.safe_portal_url("http://www.marchespublics.gov.ma/document")
        )

    def test_document_buffer_rejects_content_above_the_limit(self) -> None:
        with patch.object(main, "MAX_DOCUMENT_BYTES", 3):
            with self.assertRaises(HTTPException) as raised:
                main.buffer_document(b"12", iter([b"34"]))

        self.assertEqual(raised.exception.status_code, 413)

    def test_anonymous_download_form_includes_consent(self) -> None:
        page = """
        <form action="/download">
          <input type="hidden" name="state" value="abc">
          <input id="ctl0_CONTENU_PAGE_EntrepriseFormulaireDemande_choixAnonyme"
                 type="radio" name="mode" value="anonymous">
          <input id="ctl0_CONTENU_PAGE_EntrepriseFormulaireDemande_accepterConditions"
                 type="checkbox" name="terms">
          <input id="ctl0_CONTENU_PAGE_validateButton"
                 type="submit" name="validate" value="Valider">
        </form>
        """
        action, data = main.anonymous_download_form(
            page,
            "https://www.marchespublics.gov.ma/form",
        )
        self.assertEqual(action, "https://www.marchespublics.gov.ma/download")
        self.assertEqual(data["mode"], "anonymous")
        self.assertEqual(data["terms"], "on")
        self.assertEqual(data["state"], "abc")
        self.assertEqual(data["PRADO_POSTBACK_TARGET"], "validate")
        self.assertEqual(data["PRADO_POSTBACK_PARAMETER"], "undefined")
        self.assertNotIn("validate", data)

    def test_complete_download_form_targets_the_archive_control(self) -> None:
        page = """
        <form name="main_form" action="/index.php?page=dce">
          <input type="hidden" name="PRADO_PAGESTATE" value="state">
          <input type="hidden" name="PRADO_POSTBACK_TARGET" value="">
          <a id="ctl0_CONTENU_PAGE_EntrepriseDownloadDce_completeDownload"
             href="javascript:;">Telecharger</a>
        </form>
        """
        action, data = main.complete_download_form(
            page,
            "https://www.marchespublics.gov.ma/form",
        )
        self.assertEqual(
            action,
            "https://www.marchespublics.gov.ma/index.php?page=dce",
        )
        self.assertEqual(data["PRADO_PAGESTATE"], "state")
        self.assertEqual(
            data["PRADO_POSTBACK_TARGET"],
            "ctl0$CONTENU_PAGE$EntrepriseDownloadDce$completeDownload",
        )
        self.assertEqual(data["PRADO_POSTBACK_PARAMETER"], "")

    def test_complete_download_form_requires_the_archive_control(self) -> None:
        with self.assertRaises(ValueError):
            main.complete_download_form(
                '<form name="main_form" action="/index.php"></form>',
                "https://www.marchespublics.gov.ma/form",
            )

    def test_anonymous_archive_follows_the_confirmation_postback(self) -> None:
        portal = "https://www.marchespublics.gov.ma"
        request_form_page = f"""
        <form action="{portal}/download">
          <input id="ctl0_CONTENU_PAGE_EntrepriseFormulaireDemande_choixAnonyme"
                 type="radio" name="mode" value="anonymous">
          <input id="ctl0_CONTENU_PAGE_EntrepriseFormulaireDemande_accepterConditions"
                 type="checkbox" name="terms">
          <input id="ctl0_CONTENU_PAGE_validateButton"
                 type="submit" name="validate" value="Valider">
        </form>
        """
        confirmation_page = f"""
        <form name="main_form" action="{portal}/download">
          <input type="hidden" name="PRADO_PAGESTATE" value="state">
          <a id="ctl0_CONTENU_PAGE_EntrepriseDownloadDce_completeDownload"
             href="javascript:;">Telecharger</a>
        </form>
        """
        archive_bytes = b"PK\x03\x04 archive"

        def response(url: str, content: bytes, media_type: str) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"content-type": media_type},
                content=content,
                request=httpx.Request("POST", url),
            )

        responses = [
            response(
                f"{portal}/form",
                request_form_page.encode(),
                "text/html;charset=utf-8",
            ),
            response(
                f"{portal}/download",
                confirmation_page.encode(),
                "text/html;charset=utf-8",
            ),
            response(f"{portal}/download", archive_bytes, "application/zip"),
        ]
        calls: list[dict] = []

        def fake_request(url, method="GET", **options):
            calls.append({"url": url, "method": method, "data": options.get("data")})
            return responses.pop(0)

        with patch.object(main, "portal_request", side_effect=fake_request):
            archive, size = main.download_anonymous_archive(f"{portal}/form")

        try:
            self.assertEqual(archive.read(), archive_bytes)
            self.assertEqual(size, len(archive_bytes))
        finally:
            archive.close()
        self.assertEqual([call["method"] for call in calls], ["GET", "POST", "POST"])
        self.assertEqual(calls[1]["data"]["PRADO_POSTBACK_TARGET"], "validate")
        self.assertEqual(
            calls[2]["data"]["PRADO_POSTBACK_TARGET"],
            "ctl0$CONTENU_PAGE$EntrepriseDownloadDce$completeDownload",
        )

    def test_anonymous_archive_rejects_a_non_archive_confirmation(self) -> None:
        portal = "https://www.marchespublics.gov.ma"
        request_form_page = f"""
        <form action="{portal}/download">
          <input id="ctl0_CONTENU_PAGE_EntrepriseFormulaireDemande_choixAnonyme"
                 type="radio" name="mode" value="anonymous">
          <input id="ctl0_CONTENU_PAGE_EntrepriseFormulaireDemande_accepterConditions"
                 type="checkbox" name="terms">
          <input id="ctl0_CONTENU_PAGE_validateButton"
                 type="submit" name="validate" value="Valider">
        </form>
        """
        responses = [
            httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=request_form_page.encode(),
                request=httpx.Request("GET", f"{portal}/form"),
            ),
            httpx.Response(
                200,
                headers={"content-type": "text/html"},
                content=b"<html><body>Identification requise</body></html>",
                request=httpx.Request("POST", f"{portal}/download"),
            ),
        ]

        with patch.object(main, "portal_request", side_effect=lambda *a, **k: responses.pop(0)):
            with self.assertRaises(ValueError):
                main.download_anonymous_archive(f"{portal}/form")

    def test_zip_archive_is_extracted_for_future_parsing(self) -> None:
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr("CPS/cahier.pdf", b"%PDF-1.4\ncontent")
            package.writestr("plans/readme.txt", b"instructions")
        archive.seek(0)
        document = {
            "id": 7,
            "consultation_id": "123",
            "org_acronyme": "abc",
        }
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(main, "OFFER_FILES_DIR", Path(directory)):
                files = main.extract_archive(archive, len(archive.getvalue()), document)

        self.assertEqual([item["name"] for item in files], [
            "CPS/cahier.pdf",
            "plans/readme.txt",
        ])
        self.assertEqual(files[0]["media_type"], "application/pdf")

    def test_archive_paths_are_strictly_validated(self) -> None:
        for filename in ("../secret", "..\\secret", "/absolute/file"):
            with self.subTest(filename=filename), self.assertRaises(ValueError):
                main.safe_archive_path(filename)

    def test_unsafe_archives_leave_no_partial_extraction(self) -> None:
        document = {
            "id": 7,
            "consultation_id": "123",
            "org_acronyme": "abc",
        }
        archives: list[io.BytesIO] = []

        duplicate = io.BytesIO()
        with zipfile.ZipFile(duplicate, "w") as package:
            package.writestr("same.txt", b"one")
            package.writestr("SAME.txt", b"two")
        archives.append(duplicate)

        symlink = io.BytesIO()
        with zipfile.ZipFile(symlink, "w") as package:
            member = zipfile.ZipInfo("link")
            member.create_system = 3
            member.external_attr = (main.stat.S_IFLNK | 0o777) << 16
            package.writestr(member, "target")
        archives.append(symlink)

        traversal = io.BytesIO()
        with zipfile.ZipFile(traversal, "w") as package:
            package.writestr("../secret.txt", b"secret")
        archives.append(traversal)

        with tempfile.TemporaryDirectory() as directory:
            with patch.object(main, "OFFER_FILES_DIR", Path(directory)):
                for archive in archives:
                    archive.seek(0)
                    with self.assertRaises(ValueError):
                        main.extract_archive(
                            archive,
                            len(archive.getvalue()),
                            document,
                        )
                self.assertFalse(
                    any(path.name.startswith(".extract-") for path in Path(directory).rglob("*"))
                )

    def test_archive_limits_file_count_and_expanded_size(self) -> None:
        document = {
            "id": 7,
            "consultation_id": "123",
            "org_acronyme": "abc",
        }
        too_many = io.BytesIO()
        with zipfile.ZipFile(too_many, "w") as package:
            package.writestr("one.txt", b"1")
            package.writestr("two.txt", b"2")
        too_large = io.BytesIO()
        with zipfile.ZipFile(too_large, "w") as package:
            package.writestr("large.txt", b"1234")

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch.object(main, "OFFER_FILES_DIR", Path(directory)),
                patch.object(main, "MAX_ARCHIVE_FILES", 1),
            ):
                with self.assertRaises(ValueError):
                    main.extract_archive(too_many, len(too_many.getvalue()), document)
            with (
                patch.object(main, "OFFER_FILES_DIR", Path(directory)),
                patch.object(main, "MAX_ARCHIVE_BYTES", 3),
            ):
                with self.assertRaises(ValueError):
                    main.extract_archive(too_large, len(too_large.getvalue()), document)

    def test_cached_file_content_requires_enterprise_consent(self) -> None:
        offer_id = str(uuid.uuid4())
        point = SimpleNamespace(
            id=offer_id,
            payload={
                "consultation_id": "123",
                "org_acronyme": "abc",
                "deadline_timestamp": (
                    datetime.now(UTC) + timedelta(days=10)
                ).timestamp(),
            },
        )
        with patch.object(main.qdrant, "retrieve", return_value=[point]):
            with self.assertRaises(HTTPException) as raised:
                main.offer_file_content(
                    offer_id,
                    1,
                    {
                        "id": str(uuid.uuid4()),
                        "portal_download_consent": False,
                    },
                )

        self.assertEqual(raised.exception.status_code, 403)

    def test_profile_is_loaded_from_auth_service(self) -> None:
        response = httpx.Response(
            200,
            json={
                "enterprise": {
                    "id": "9b45e9b4-1bc9-47a2-88f1-7778d9f22000",
                    "keywords": ["network"],
                    "categories": ["services"],
                    "locations": ["Rabat"],
                }
            },
        )
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")
        with patch.object(main.auth_client, "get", return_value=response) as get:
            profile = main.load_enterprise(credentials)

        self.assertEqual(profile["keywords"], ["network"])
        get.assert_called_once_with(
            "/auth/profile",
            headers={"Authorization": "Bearer token"},
        )

    def test_stale_auth_connection_is_retried_once(self) -> None:
        enterprise = {
            "id": str(uuid.uuid4()),
            "keywords": ["route"],
            "categories": ["travaux"],
            "locations": ["casablanca"],
            "portal_download_consent": True,
        }
        profile_response = httpx.Response(
            200,
            json={"enterprise": enterprise},
            request=httpx.Request("GET", "http://auth:8000/auth/profile"),
        )
        client = Mock()
        client.get.side_effect = [
            httpx.RemoteProtocolError("Server disconnected without sending a response."),
            profile_response,
        ]

        with patch.object(main, "auth_client", client):
            profile = main.load_enterprise(
                HTTPAuthorizationCredentials(
                    scheme="Bearer",
                    credentials="token",
                )
            )

        self.assertEqual(profile["id"], enterprise["id"])
        self.assertEqual(client.get.call_count, 2)

    def test_auth_service_outage_still_reports_unavailable(self) -> None:
        client = Mock()
        client.get.side_effect = httpx.ConnectError("connection refused")

        with patch.object(main, "auth_client", client):
            with self.assertRaises(HTTPException) as raised:
                main.load_enterprise(
                    HTTPAuthorizationCredentials(scheme="Bearer", credentials="token")
                )

        self.assertEqual(raised.exception.status_code, 503)

    def test_location_filter_falls_back_to_category(self) -> None:
        profile = {
            "id": "9b45e9b4-1bc9-47a2-88f1-7778d9f22000",
            "keywords": ["network"],
            "categories": ["services"],
            "locations": ["Rabat"],
        }
        payload = {
            "objet": "Network service",
            "category": "services",
            "locations": ["rabat"],
        }
        first = SimpleNamespace(id="1", score=1.0, payload=payload)
        second = SimpleNamespace(id="2", score=0.8, payload={**payload, "locations": ["fes"]})

        main.index_ready.set()
        with patch.object(main, "query_offers", side_effect=[[first], [first, second]]) as query:
            result = main.top_matches(limit=2, profile=profile)

        self.assertEqual(query.call_count, 2)
        self.assertEqual([item["offer_id"] for item in result["matches"]], ["1", "2"])

    def test_offer_details_returns_active_offer_and_document_metadata(self) -> None:
        offer_id = str(uuid.uuid4())
        payload = {
            "consultation_id": "123",
            "org_acronyme": "abc",
            "reference": "REF-123",
            "objet": "Network equipment",
            "category": "services",
            "category_display": "Services",
            "buyer": "Public buyer",
            "procedure": "Open",
            "location_display": "Rabat",
            "publication_date": "01/09/2026",
            "deadline": "01/01/2027 12:00",
            "deadline_timestamp": (datetime.now(UTC) + timedelta(days=10)).timestamp(),
            "source_url": "https://www.marchespublics.gov.ma/detail/123",
        }
        point = SimpleNamespace(id=offer_id, payload=payload)
        profile = {"id": str(uuid.uuid4())}

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tenders.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE offers (
                        consultation_id TEXT,
                        org_acronyme TEXT,
                        documents_checked_at TEXT,
                        PRIMARY KEY (consultation_id, org_acronyme)
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE offer_documents (
                        id INTEGER PRIMARY KEY,
                        consultation_id TEXT,
                        org_acronyme TEXT,
                        name TEXT,
                        url TEXT,
                        file_type TEXT,
                        download_attempted_at TEXT,
                        download_error TEXT
                    )
                    """
                )
                connection.executescript(main.DOCUMENT_FILES_SCHEMA)
                connection.execute(
                    """
                    INSERT INTO offers
                        (consultation_id, org_acronyme, documents_checked_at)
                    VALUES ('123', 'abc', CURRENT_TIMESTAMP)
                    """
                )
                connection.execute(
                    """
                    INSERT INTO offer_documents
                        (consultation_id, org_acronyme, name, url, file_type)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        "123",
                        "abc",
                        "Dossier de consultation",
                        "https://www.marchespublics.gov.ma/download/123",
                        "dossier",
                    ),
                )

            with (
                patch.object(main.qdrant, "retrieve", return_value=[point]),
                patch.object(main, "TENDERS_DB", str(database)),
            ):
                result = main.offer_details(offer_id, profile)
                document_result = main.offer_document_list(offer_id, profile)

        self.assertEqual(result["id"], offer_id)
        self.assertEqual(result["category"], "Services")
        self.assertEqual(
            document_result["documents"],
            [
                {
                    "id": 1,
                    "name": "Dossier de consultation",
                    "file_type": "dossier",
                    "portal_url": "https://www.marchespublics.gov.ma/download/123",
                    "requires_portal": False,
                }
            ],
        )
        self.assertNotIn("documents", result)
        self.assertNotIn("url", document_result["documents"][0])

    def test_documents_are_discovered_lazily_and_cached(self) -> None:
        source_url = (
            "https://www.marchespublics.gov.ma/index.php?"
            "page=entreprise.EntrepriseDetailConsultation&refConsultation=123"
        )
        detail_html = """
        <div>
          <a id="linkDownloadDce"
             href="index.php?page=entreprise.EntrepriseDemandeTelechargementDce&amp;ref=123">
             Dossier de consultation
          </a>
          <a id="externalDownload" href="https://evil.test/file.pdf">Bad</a>
        </div>
        """
        response = SimpleNamespace(text=detail_html, raise_for_status=lambda: None)

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tenders.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE offers (
                        consultation_id TEXT,
                        org_acronyme TEXT,
                        documents_checked_at TEXT,
                        PRIMARY KEY (consultation_id, org_acronyme)
                    );
                    CREATE TABLE offer_documents (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        consultation_id TEXT,
                        org_acronyme TEXT,
                        name TEXT,
                        url TEXT,
                        file_type TEXT,
                        download_attempted_at TEXT,
                        download_error TEXT
                    );
                    INSERT INTO offers (consultation_id, org_acronyme)
                    VALUES ('123', 'abc');
                    """
                )

            with (
                patch.object(main, "TENDERS_DB", str(database)),
                patch.object(main, "portal_request", return_value=response) as get,
                patch.object(main, "DOCUMENT_REFRESH_SECONDS", -1),
            ):
                documents = main.offer_documents("123", "abc", source_url)
                refreshed_documents = main.offer_documents("123", "abc", source_url)

            with sqlite3.connect(database) as connection:
                checked_at = connection.execute(
                    "SELECT documents_checked_at FROM offers"
                ).fetchone()[0]
                cached_url = connection.execute(
                    "SELECT url FROM offer_documents"
                ).fetchone()[0]

        self.assertEqual(
            documents,
            [
                {
                    "id": 1,
                    "name": "Dossier de consultation",
                    "file_type": "dossier",
                    "portal_url": (
                        "https://www.marchespublics.gov.ma/index.php?"
                        "page=entreprise.EntrepriseDemandeTelechargementDce&ref=123"
                    ),
                    "requires_portal": True,
                }
            ],
        )
        self.assertIsNotNone(checked_at)
        self.assertIn("EntrepriseDemandeTelechargementDce", cached_url)
        self.assertEqual(refreshed_documents[0]["id"], documents[0]["id"])
        self.assertEqual(get.call_count, 2)

    def test_protected_document_redirects_user_to_official_portal(self) -> None:
        offer_id = str(uuid.uuid4())
        portal_url = "https://www.marchespublics.gov.ma/download/123"
        point = SimpleNamespace(
            id=offer_id,
            payload={
                "consultation_id": "123",
                "org_acronyme": "abc",
                "deadline_timestamp": (
                    datetime.now(UTC) + timedelta(days=10)
                ).timestamp(),
            },
        )
        remote = Mock()
        remote.url = portal_url
        remote.headers = {"content-type": "text/html; charset=utf-8"}
        remote.is_redirect = False

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tenders.db"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE offer_documents (
                        id INTEGER PRIMARY KEY,
                        consultation_id TEXT,
                        org_acronyme TEXT,
                        name TEXT,
                        url TEXT,
                        file_type TEXT,
                        download_attempted_at TEXT,
                        download_error TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO offer_documents
                        (id, consultation_id, org_acronyme, name, url, file_type)
                    VALUES (1, '123', 'abc', 'Dossier', ?, 'dossier')
                    """,
                    (portal_url,),
                )

            with (
                patch.object(main.qdrant, "retrieve", return_value=[point]),
                patch.object(main, "TENDERS_DB", str(database)),
                patch.object(main.portal_client, "send", return_value=remote),
            ):
                with self.assertRaises(HTTPException) as raised:
                    main.document_content(offer_id, 1, {"id": str(uuid.uuid4())})

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(raised.exception.detail["code"], "portal_action_required")
        self.assertEqual(raised.exception.detail["portal_url"], portal_url)
        remote.close.assert_called_once()

    def test_offer_details_hides_expired_offer(self) -> None:
        offer_id = str(uuid.uuid4())
        point = SimpleNamespace(
            id=offer_id,
            payload={
                "deadline_timestamp": (
                    datetime.now(UTC) - timedelta(minutes=1)
                ).timestamp()
            },
        )
        with patch.object(main.qdrant, "retrieve", return_value=[point]):
            with self.assertRaises(HTTPException) as raised:
                main.offer_details(offer_id, {"id": str(uuid.uuid4())})

        self.assertEqual(raised.exception.status_code, 404)

    def test_archive_failure_preserves_portal_document_fallback(self) -> None:
        offer_id = str(uuid.uuid4())
        point = SimpleNamespace(
            id=offer_id,
            payload={
                "consultation_id": "123",
                "org_acronyme": "abc",
                "deadline_timestamp": (
                    datetime.now(UTC) + timedelta(days=10)
                ).timestamp(),
                "source_url": "https://www.marchespublics.gov.ma/detail/123",
            },
        )
        portal_url = (
            "https://www.marchespublics.gov.ma/index.php?"
            "page=entreprise.EntrepriseDemandeTelechargementDce&ref=123"
        )

        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "tenders.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE offers (
                        consultation_id TEXT,
                        org_acronyme TEXT,
                        documents_checked_at TEXT,
                        PRIMARY KEY (consultation_id, org_acronyme)
                    );
                    CREATE TABLE offer_documents (
                        id INTEGER PRIMARY KEY,
                        consultation_id TEXT,
                        org_acronyme TEXT,
                        name TEXT,
                        url TEXT,
                        file_type TEXT,
                        download_attempted_at TEXT,
                        download_error TEXT
                    );
                    INSERT INTO offers VALUES ('123', 'abc', CURRENT_TIMESTAMP);
                    """
                )
                connection.executescript(main.DOCUMENT_FILES_SCHEMA)
                connection.execute(
                    """
                    INSERT INTO offer_documents
                        (id, consultation_id, org_acronyme, name, url, file_type)
                    VALUES (1, '123', 'abc', 'Dossier', ?, 'dossier')
                    """,
                    (portal_url,),
                )

            with (
                patch.object(main.qdrant, "retrieve", return_value=[point]),
                patch.object(main, "TENDERS_DB", str(database)),
                patch.object(main, "OFFER_FILES_DIR", Path(directory) / "files"),
                patch.object(
                    main,
                    "cache_archive_files",
                    side_effect=ValueError("portal validation failed"),
                ),
            ):
                result = main.offer_document_list(
                    offer_id,
                    {"id": str(uuid.uuid4()), "portal_download_consent": True},
                )

        self.assertEqual(result["files"], [])
        self.assertEqual(result["documents"][0]["portal_url"], portal_url)
        self.assertIsNotNone(result["preparation_error"])


if __name__ == "__main__":
    unittest.main()
