"""Verify byte-preserving uploads and API proxy failure behaviour."""
import asyncio
import importlib
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException
from starlette.datastructures import UploadFile
from starlette.requests import Request
import requests

with patch.dict(os.environ, {"ORACLE_PASSWORD": "test", "PG_PASSWORD": "test"}):
    api = importlib.import_module("legacy_api")
import web_server


class UploadContractTests(unittest.TestCase):
    def test_original_bytes_survive_decoding(self):
        raw = b"-- caf\xe9\r\nCREATE TABLE t (id NUMBER(9));\r\n"
        with tempfile.TemporaryDirectory() as folder, patch.object(api, "UPLOAD_ROOT", folder), \
                patch.object(api, "jobs", {}):
            tasks = BackgroundTasks()
            upload = UploadFile(filename="oracle.sql", file=io.BytesIO(raw))
            result = asyncio.run(api.start_upload_file(tasks, upload, False))
            response = api.download_original(result["job_id"])
            self.assertEqual(Path(response.path).read_bytes(), raw)
            self.assertEqual(response.filename, "oracle.sql")
            self.assertIn("Windows-1252", api.jobs[result["job_id"]]["warnings"][0])
            self.assertEqual(len(tasks.tasks), 1)  # Queued, never executed in the test.

    def test_oversize_upload_does_not_queue_a_job(self):
        with patch.object(api, "MAX_UPLOAD_BYTES", 10), patch.object(api, "jobs", {}):
            tasks = BackgroundTasks()
            upload = UploadFile(filename="big.sql", file=io.BytesIO(b"x" * 11))
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(api.start_upload_file(tasks, upload, False))
            self.assertEqual(caught.exception.status_code, 413)
            self.assertEqual(tasks.tasks, [])
            self.assertEqual(api.jobs, {})

    def test_original_for_unknown_job_is_404(self):
        with patch.object(api, "jobs", {}):
            with self.assertRaises(HTTPException) as caught:
                api.download_original("../private")
            self.assertEqual(caught.exception.status_code, 404)


class FrontendProxyTests(unittest.TestCase):
    def request(self):
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        return Request({"type": "http", "method": "GET", "path": "/api/health/ready",
                        "headers": [], "query_string": b""}, receive)

    def test_proxy_rejects_unrelated_paths(self):
        with self.assertRaises(HTTPException) as caught:
            asyncio.run(web_server.proxy("private/config", self.request()))
        self.assertEqual(caught.exception.status_code, 404)

    def test_offline_api_returns_actionable_502(self):
        with patch("web_server.requests.request", side_effect=requests.ConnectionError("private hostname")):
            with self.assertRaises(HTTPException) as caught:
                asyncio.run(web_server.proxy("health/ready", self.request()))
        self.assertEqual(caught.exception.status_code, 502)
        self.assertNotIn("private hostname", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()

