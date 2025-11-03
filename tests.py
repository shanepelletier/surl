import json
import unittest
from unittest.mock import Mock, patch
import tempfile
import os

import surl


class TestSurl(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(1, 1)


class TestMacaroonRefresh(unittest.TestCase):
    """Tests for automatic macaroon refresh functionality."""

    def setUp(self):
        """Set up test fixtures."""
        self.test_config = surl.ClientConfig(
            root="test_root_macaroon",
            discharge="test_discharge_macaroon",
            store_env="staging",
            store_type="snapcraft",
            path="/tmp/test.surl",
        )

    def test_needs_macaroon_refresh_with_error_list(self):
        """Test detection of macaroon-needs-refresh in error_list format."""
        response_text = json.dumps(
            {
                "error_list": [
                    {
                        "message": "Expired macaroon (age: 103902 seconds)",
                        "code": "macaroon-needs-refresh",
                        "extra": {"age": 103902},
                    }
                ]
            }
        )
        self.assertTrue(surl._needs_macaroon_refresh(response_text))

    def test_needs_macaroon_refresh_with_error_dash_list(self):
        """Test detection in error-list format (Charmhub style)."""
        response_text = json.dumps(
            {
                "error-list": [
                    {
                        "message": "Expired macaroon (age: 103902 seconds)",
                        "code": "macaroon-needs-refresh",
                        "extra": {"age": 103902},
                    }
                ]
            }
        )
        self.assertTrue(surl._needs_macaroon_refresh(response_text))

    def test_needs_macaroon_refresh_no_error(self):
        """Test that normal responses don't trigger refresh."""
        response_text = json.dumps({"account": {"email": "test@test.com"}})
        self.assertFalse(surl._needs_macaroon_refresh(response_text))

    def test_needs_macaroon_refresh_different_error(self):
        """Test that other error codes don't trigger refresh."""
        response_text = json.dumps(
            {"error_list": [{"message": "Invalid token", "code": "invalid-token"}]}
        )
        self.assertFalse(surl._needs_macaroon_refresh(response_text))

    def test_needs_macaroon_refresh_invalid_json(self):
        """Test that invalid JSON doesn't crash."""
        response_text = "Not a JSON response"
        self.assertFalse(surl._needs_macaroon_refresh(response_text))

    @patch("surl.requests.post")
    def test_get_refreshed_discharge_success(self, mock_post):
        """Test successful macaroon refresh."""
        mock_response = Mock()
        mock_response.ok = True
        mock_response.json.return_value = {
            "discharge_macaroon": "new_discharge_macaroon"
        }
        mock_post.return_value = mock_response

        new_discharge = surl.get_refreshed_discharge(self.test_config)

        self.assertEqual(new_discharge, "new_discharge_macaroon")
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        self.assertIn(
            "/api/v2/tokens/refresh", call_args[0][0]
        )  # URL contains refresh endpoint
        self.assertEqual(
            call_args[1]["json"]["discharge_macaroon"], "test_discharge_macaroon"
        )

    @patch("surl.requests.post")
    def test_get_refreshed_discharge_failure(self, mock_post):
        """Test failed macaroon refresh."""
        mock_response = Mock()
        mock_response.ok = False
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_post.return_value = mock_response

        with self.assertRaises(surl.CliError):
            surl.get_refreshed_discharge(self.test_config)

    def test_get_refreshed_discharge_no_discharge(self):
        """Test refresh fails when no discharge macaroon exists."""
        config_no_discharge = surl.ClientConfig(
            root="test_root_macaroon",
            discharge=None,
            store_env="staging",
            store_type="charmhub",
            path="/tmp/test.surl",
        )

        with self.assertRaises(surl.CliError) as context:
            surl.get_refreshed_discharge(config_no_discharge)
        self.assertIn("no discharge macaroon", str(context.exception))

    @patch("surl.requests.request")
    @patch("surl.get_refreshed_discharge")
    @patch("surl.save_config")
    @patch("surl.get_authorization_header")
    def test_store_request_with_refresh(
        self, mock_get_auth_header, mock_save_config, mock_get_refreshed, mock_request
    ):
        """Test store_request automatically refreshes on error."""
        # First request fails with macaroon-needs-refresh
        first_response = Mock()
        first_response.ok = False
        first_response.text = json.dumps(
            {"error_list": [{"code": "macaroon-needs-refresh", "message": "Expired"}]}
        )

        # Second request succeeds
        second_response = Mock()
        second_response.ok = True
        second_response.text = json.dumps({"success": True})

        mock_request.side_effect = [first_response, second_response]
        mock_get_refreshed.return_value = "new_discharge_macaroon"
        mock_get_auth_header.return_value = {"Authorization": "test_auth"}

        # Call store_request
        headers = surl.DEFAULT_HEADERS.copy()
        headers.update({"Authorization": "test_auth"})

        result = surl.store_request(
            self.test_config, method="GET", url="http://test.url", headers=headers
        )

        # Verify refresh was called
        mock_get_refreshed.assert_called_once_with(self.test_config)
        # Verify config was saved
        mock_save_config.assert_called_once()
        # Verify we got the second (successful) response
        self.assertEqual(result, second_response)
        # Verify request was called twice
        self.assertEqual(mock_request.call_count, 2)

    @patch("surl.requests.request")
    def test_store_request_no_refresh_on_success(self, mock_request):
        """Test store_request doesn't refresh on successful request."""
        success_response = Mock()
        success_response.ok = True
        success_response.text = json.dumps({"success": True})
        mock_request.return_value = success_response

        headers = surl.DEFAULT_HEADERS.copy()
        result = surl.store_request(
            self.test_config, method="GET", url="http://test.url", headers=headers
        )

        # Verify request was only called once
        mock_request.assert_called_once()
        self.assertEqual(result, success_response)

    @patch("surl.requests.request")
    @patch("surl.get_refreshed_discharge")
    def test_store_request_refresh_failure_returns_original_error(
        self, mock_get_refreshed, mock_request
    ):
        """Test that if refresh fails, original error is returned."""
        error_response = Mock()
        error_response.ok = False
        error_response.text = json.dumps(
            {"error_list": [{"code": "macaroon-needs-refresh", "message": "Expired"}]}
        )
        mock_request.return_value = error_response

        # Make refresh fail
        mock_get_refreshed.side_effect = surl.CliError("Refresh failed")

        headers = surl.DEFAULT_HEADERS.copy()
        result = surl.store_request(
            self.test_config, method="GET", url="http://test.url", headers=headers
        )

        # Should return the original error response
        self.assertEqual(result, error_response)
        # Request should only be called once (no retry)
        mock_request.assert_called_once()


class TestConfigPersistence(unittest.TestCase):
    """Test that refreshed macaroons are saved correctly."""

    def test_save_and_load_config(self):
        """Test that config can be saved and loaded with refreshed macaroons."""
        with tempfile.NamedTemporaryFile(mode="w", delete=False, suffix=".surl") as f:
            temp_path = f.name

        try:
            # Create and save config
            config = surl.ClientConfig(
                root="test_root",
                discharge="test_discharge",
                store_env="production",
                store_type="snapcraft",
                path=temp_path,
            )
            surl.save_config(config)

            # Load config
            loaded_config = surl.load_config(temp_path)

            # Verify all fields match
            self.assertEqual(loaded_config.root, config.root)
            self.assertEqual(loaded_config.discharge, config.discharge)
            self.assertEqual(loaded_config.store_env, config.store_env)
            self.assertEqual(loaded_config.store_type, config.store_type)

        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
