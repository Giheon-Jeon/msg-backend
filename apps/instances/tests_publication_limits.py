import base64
import io
import json
import os
import zipfile
from unittest.mock import patch

import yaml
from django.test import SimpleTestCase

from apps.instances import published_challenges as publications
from apps.instances.releases import ReleaseValidationError


def bundle(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return output.getvalue()


class PublicationCompatibilityTests(SimpleTestCase):
    def metadata(self, category):
        raw = yaml.safe_dump({"name": "Evidence", "category": category,
                              "description": "Author description", "flag": "test-grading-value"}).encode()
        response = {"encoding": "base64", "size": len(raw),
                    "content": base64.b64encode(raw).decode()}
        with patch.object(publications, "_json", return_value=response):
            return publications._metadata("forensic-evidence", "a" * 40)

    def test_published_forensics_category_maps_to_backend_category(self):
        self.assertEqual(self.metadata("forensics")["category"], "FORENSIC")

    def test_canonical_category_is_preserved(self):
        self.assertEqual(self.metadata("forensic")["category"], "FORENSIC")

    def test_unknown_category_is_still_rejected(self):
        with self.assertRaises(ReleaseValidationError):
            self.metadata("unknown-category")

    def download(self, raw):
        # Exercise the real bounded HTTP reader; only the network response is replaced.
        with patch("apps.instances.poller.urlopen", return_value=io.BytesIO(raw)):
            return publications._release_artifact({"id": 1})

    def test_publication_with_large_sbom_reads_only_release_manifest(self):
        payload = {"schema_version": "2.0", "challenge_slug": "pwn-price-tag"}
        raw = bundle({"artifact-v2.json": json.dumps(payload),
                      "sbom.json": os.urandom(3 * 1024 * 1024)})
        self.assertGreater(len(raw), 2 * 1024 * 1024)
        self.assertEqual(self.download(raw), payload)

    def test_publication_download_remains_bounded(self):
        with self.assertRaisesMessage(ReleaseValidationError, "크기가 허용 범위를 초과"):
            self.download(b"x" * (16 * 1024 * 1024 + 1))

    def test_compressed_oversized_manifest_is_rejected(self):
        raw = bundle({"artifact-v2.json": json.dumps({"padding": "x" * (1024 * 1024)})})
        with self.assertRaisesMessage(ReleaseValidationError, "실행 발행 파일"):
            self.download(raw)

    def test_ambiguous_release_manifests_are_rejected(self):
        raw = bundle({"artifact-v2.json": "{}", "nested/artifact-v2.json": "{}"})
        with self.assertRaisesMessage(ReleaseValidationError, "실행 발행 파일"):
            self.download(raw)
