import sqlite3
import tempfile
import unittest
from pathlib import Path

import scrapper


def offer(identifier: str) -> dict[str, str]:
    return {
        "consultation_id": identifier,
        "org_acronyme": "ORG",
        "reference": identifier.upper(),
        "objet": f"Offer {identifier}",
        "procedure": "open",
        "categorie": "Services",
        "acheteur_public": "Buyer",
        "lieu_execution": "Rabat",
        "date_publication": "01/09/2026",
        "date_limite": "01/01/2027 12:00",
        "source_url": f"https://example.test/{identifier}",
    }


class ScrapeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(Path(self.temp_dir.name) / "tenders.db")
        self.connection.execute("PRAGMA foreign_keys = ON")
        scrapper.initialize_schema(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def test_interrupted_run_keeps_live_snapshot_and_resumes(self) -> None:
        old = offer("old")
        columns = ", ".join(scrapper.FIELDS)
        placeholders = ", ".join("?" for _ in scrapper.FIELDS)
        self.connection.execute(
            f"INSERT INTO offers ({columns}) VALUES ({placeholders})",
            tuple(old[field] for field in scrapper.FIELDS),
        )
        self.connection.commit()

        run_id, page = scrapper.resume_or_create_run(self.connection)
        self.assertEqual(page, 0)
        scrapper.save_page(self.connection, run_id, [offer("new")], 1, 2)

        live_ids = {
            row[0]
            for row in self.connection.execute("SELECT consultation_id FROM offers")
        }
        self.assertEqual(live_ids, {"old", "new"})

        scrapper.mark_run(self.connection, run_id, "failed", "network")
        resumed_id, resumed_page = scrapper.resume_or_create_run(self.connection)
        self.assertEqual((resumed_id, resumed_page), (run_id, 1))

    def test_complete_run_removes_only_offers_not_seen_in_run(self) -> None:
        old = offer("old")
        columns = ", ".join(scrapper.FIELDS)
        placeholders = ", ".join("?" for _ in scrapper.FIELDS)
        self.connection.execute(
            f"INSERT INTO offers ({columns}) VALUES ({placeholders})",
            tuple(old[field] for field in scrapper.FIELDS),
        )
        self.connection.execute(
            """
            INSERT INTO offer_documents (
                consultation_id, org_acronyme, name, url, file_type
            ) VALUES ('old', 'ORG', 'Old document', 'https://example.test/old.pdf', 'pdf')
            """
        )
        self.connection.commit()

        run_id, _ = scrapper.resume_or_create_run(self.connection)
        scrapper.save_page(self.connection, run_id, [offer("new")], 1, 1)
        self.assertEqual(scrapper.complete_run(self.connection, run_id), 1)
        self.assertEqual(
            self.connection.execute("SELECT consultation_id FROM offers").fetchone()[0],
            "new",
        )
        self.assertEqual(
            self.connection.execute(
                "SELECT status FROM scrape_state WHERE id = 1"
            ).fetchone()[0],
            "completed",
        )
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM offer_documents").fetchone()[0],
            0,
        )

    def test_schema_contains_document_cache_tables(self) -> None:
        tables = {
            row[0]
            for row in self.connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        }
        self.assertEqual(
            tables,
            {"offers", "offer_documents", "offer_files", "scrape_state"},
        )


if __name__ == "__main__":
    unittest.main()
