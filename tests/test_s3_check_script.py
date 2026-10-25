"""Regression tests for scripts/check_video_s3.py: read-only checks, detected mismatches, no writes."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
os.environ.setdefault("RUNPOD_API_KEY", "test-only-not-a-secret")

import check_video_s3 as check  # noqa: E402
from src.s3_video_store import settings_from_env  # noqa: E402


class FakeS3:
    def __init__(self, location="ap-south-1", algo="AES256", block=None, fail_head=False, writes=None):
        self.location = location
        self.algo = algo
        self.block = block if block is not None else {
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        }
        self.fail_head = fail_head
        self.calls = []

    def head_bucket(self, **kw):
        self.calls.append("head_bucket")
        if self.fail_head:
            raise PermissionError("AccessDenied")

    def get_bucket_location(self, **kw):
        self.calls.append("get_bucket_location")
        return {"LocationConstraint": self.location}

    def get_bucket_encryption(self, **kw):
        self.calls.append("get_bucket_encryption")
        return {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": self.algo}}]}}

    def get_public_access_block(self, **kw):
        self.calls.append("get_public_access_block")
        return {"PublicAccessBlockConfiguration": self.block}

    def list_objects_v2(self, **kw):
        self.calls.append("list_objects_v2")
        return {"KeyCount": 0}

    def generate_presigned_url(self, op, Params, ExpiresIn):
        self.calls.append("generate_presigned_url")
        return f"https://bucket.s3.ap-south-1.amazonaws.com/x?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=abc"


class FakeSession:
    def __init__(self, s3, identity_ok=True):
        self.s3 = s3
        self.identity_ok = identity_ok

    def client(self, name, region_name=None):
        if name == "sts":
            outer = self

            class Sts:
                def get_caller_identity(self):
                    if not outer.identity_ok:
                        raise PermissionError("no credentials")
                    return {"Account": "757374023708", "Arn": "arn:aws:iam::757374023708:user/video-summarizer"}

            return Sts()
        return self.s3


class S3CheckScriptTests(unittest.TestCase):
    def setUp(self):
        self.settings = settings_from_env({})

    def _by_name(self, results):
        return {name: (ok, detail) for name, ok, detail in results}

    def test_all_checks_pass_for_correct_bucket(self):
        results = check.run_checks(FakeSession(FakeS3()), self.settings)
        self.assertTrue(all(ok for _n, ok, _d in results), results)

    def test_wrong_region_is_reported(self):
        results = self._by_name(check.run_checks(FakeSession(FakeS3(location="us-east-1")), self.settings))
        self.assertFalse(results["bucket region"][0])

    def test_missing_encryption_is_reported(self):
        results = self._by_name(check.run_checks(FakeSession(FakeS3(algo="aws:kms")), self.settings))
        self.assertFalse(results["default encryption"][0])

    def test_public_access_not_fully_blocked_is_reported(self):
        block = {"BlockPublicAcls": True, "IgnorePublicAcls": False, "BlockPublicPolicy": True, "RestrictPublicBuckets": True}
        results = self._by_name(check.run_checks(FakeSession(FakeS3(block=block)), self.settings))
        self.assertFalse(results["public access blocked"][0])

    def test_no_credentials_stops_before_any_bucket_call(self):
        s3 = FakeS3()
        results = check.run_checks(FakeSession(s3, identity_ok=False), self.settings)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0][1])
        self.assertEqual(s3.calls, [], "no bucket call may happen without a verified identity")

    def test_only_read_only_calls_are_made(self):
        s3 = FakeS3()
        check.run_checks(FakeSession(s3), self.settings)
        forbidden = {"put_object", "upload_file", "delete_object", "delete_objects", "copy_object"}
        self.assertFalse(forbidden & set(s3.calls))

    def test_presigned_url_is_checked_but_not_returned(self):
        results = check.run_checks(FakeSession(FakeS3()), self.settings)
        detail = self._by_name(results)["presigned URL signs"][1]
        self.assertIn("URL not shown", detail)
        self.assertNotIn("X-Amz-Signature", detail)


if __name__ == "__main__":
    unittest.main()
