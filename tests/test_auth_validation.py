import unittest
import uuid

from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

import main


class SignupValidationTests(unittest.TestCase):
    def test_matching_preferences_are_required(self) -> None:
        with self.assertRaises(ValidationError):
            main.SignupRequest(
                email="client@example.ma",
                password="password123",
                enterprise_name="Example",
                description="Network services",
                keywords=[],
                categories=[],
                locations=[],
            )

    def test_authenticated_profile_endpoint(self) -> None:
        main.initialize_database()
        signup = main.signup(
            main.SignupRequest(
                email=f"profile-{uuid.uuid4().hex}@example.ma",
                password="password123",
                enterprise_name="Example",
                description="Network services",
                keywords=["network"],
                categories=["services"],
                locations=["Rabat"],
            )
        )
        credentials = HTTPAuthorizationCredentials(
            scheme="Bearer",
            credentials=signup["access_token"],
        )
        enterprise_id = main.authenticated_enterprise_id(credentials)
        profile = main.get_profile(enterprise_id)
        self.assertFalse(profile["enterprise"]["portal_download_consent"])
        consent = main.update_portal_consent(
            main.PortalConsentRequest(accepted=True),
            enterprise_id,
        )
        self.assertTrue(consent["enterprise"]["portal_download_consent"])


if __name__ == "__main__":
    unittest.main()
