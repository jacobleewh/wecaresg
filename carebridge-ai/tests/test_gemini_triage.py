"""Offline contract tests only; never call Google or persist cases."""

import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import gemini_triage as engine


def assessment():
    return {
        "urgency": "Low", "vulnerability_score": 20,
        "profile": {"household_income": None, "dependents": 0, "housing_type": "unknown", "elderly_in_household": False, "needs_tags": [], "conditions": [], "unrecognized_condition": False},
        "matched_schemes": [{"name": "Source-provided test option", "short_name": "Test", "agency": "Test agency", "category": "financial", "aid_type": "Support", "summary": "Test description", "coverage_amount": "Confirm with agency", "how_to_apply": "Contact agency", "documents_required": [], "reasons": ["From report"], "match_percent": 60, "source_indices": [0]}],
        "gaps": [], "support_advice": "Test advice from Gemini", "patient_summary_markdown": "Test summary", "referral_summary": "Test handover",
    }


METADATA = {
    "groundingChunks": [{"web": {"uri": "https://www.msf.gov.sg/", "title": "MSF"}}],
    "groundingSupports": [{"segment": {"text": "Test report"}, "groundingChunkIndices": [0]}],
}


class GeminiTriageTests(unittest.TestCase):
    def setUp(self):
        source_patch = patch.object(engine, "fetch_official_sources", return_value=[{"title": "MSF", "url": "https://www.msf.gov.sg/", "content": "Test source text"}])
        source_patch.start()
        self.addCleanup(source_patch.stop)

    def test_recommendations_come_from_provider_and_sources_are_preserved(self):
        with patch.object(engine, "_generate", return_value=(json.dumps(assessment()), {})) as generate:
            result = engine.analyze_hardship("Test case")
        self.assertEqual(generate.call_count, 1)
        self.assertIn("Test source text", generate.call_args.args[0])
        self.assertEqual(result["matched_schemes"][0]["name"], "Source-provided test option")
        self.assertEqual(result["recommendation_method"], "gemini-official-pages")
        self.assertIn("https://www.msf.gov.sg/", result["patient_summary_markdown"])
        self.assertIn("https://www.msf.gov.sg/", result["matched_schemes"][0]["how_to_apply"])

    def test_missing_grounding_stops_instead_of_making_up_options(self):
        with patch.object(engine, "fetch_official_sources", return_value=[]), patch.object(engine, "_generate") as generate:
            with self.assertRaises(engine.TriageError):
                engine.analyze_hardship("Test case")
            self.assertEqual(generate.call_count, 0)

    def test_invalid_source_reference_is_rejected(self):
        result = assessment()
        result["matched_schemes"][0]["source_indices"] = [99]
        with self.assertRaises(engine.TriageError):
            engine._validate(result, [{"url": "https://www.msf.gov.sg/"}])

    def test_quoted_source_ids_are_normalized_but_still_bounds_checked(self):
        result = assessment()
        result["matched_schemes"][0]["source_indices"] = ["0"]
        checked = engine._validate(result, [{"url": "https://www.msf.gov.sg/"}])
        self.assertEqual(checked["matched_schemes"][0]["source_indices"], [0])
        result["matched_schemes"][0]["source_indices"] = ["99"]
        with self.assertRaises(engine.TriageError):
            engine._validate(result, [{"url": "https://www.msf.gov.sg/"}])

    def test_request_uses_plain_generation_without_paid_tools(self):
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "{}"}]}}]}).encode()
        with patch.dict(engine.os.environ, {"GEMINI_API_KEY": "test-key"}), patch.object(engine, "urlopen", return_value=response) as request:
            engine._generate("Test")
        payload = json.loads(request.call_args.args[0].data)
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["generationConfig"]["responseMimeType"], "application/json")

    def test_quota_failure_is_reported_and_not_retried_with_offline_data(self):
        error = HTTPError("https://generativelanguage.googleapis.com/", 429, "quota", {}, None)
        with patch.dict(engine.os.environ, {"GEMINI_API_KEY": "test-key"}), patch.object(engine, "urlopen", side_effect=error) as request:
            with self.assertRaisesRegex(engine.TriageError, "usage limit"):
                engine.analyze_hardship("Test case")
            self.assertEqual(request.call_count, 1)

    def test_api_returns_retryable_error_and_never_saves_on_failure(self):
        with patch("database.init_db"), patch("bot_service.start_polling_in_background"), patch("bot_service.start_webhook_mode"):
            import app
        with app.app.test_client() as client, patch("triage_engine.analyze_hardship", side_effect=engine.TriageError("Quota reached")), patch("database.save_full_case") as save:
            with client.session_transaction() as session:
                session["role"] = "citizen"
                session["citizen_id"] = "test"
            response = client.post("/api/triage/preview", json={"message": "Test case"})
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.get_json()["error"], "Quota reached")
            save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
