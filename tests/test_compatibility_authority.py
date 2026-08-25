import json

import pytest

import rag_pdf_gradio_app as app


pytestmark = pytest.mark.offline_deterministic


def test_compact_mutation_authority_is_qualified_and_redacted():
    report = {
        "status": "pass",
        "reason": "exact_native_mutation_contract_matched",
        "required_capabilities": ["can_upload_native_metadata"],
        "characterization": {
            "desktop_version_normalized": "1.16.0",
            "desktop_release_status": "recognized_mutation_profile",
            "matched_profile": "profile-1",
            "native_mutation_contract": "contract-1",
            "storage_schema_status": "matched",
            "storage_dir": r"C:\Users\Private\storage",
            "desktop_package": {
                "app_asar_sha256": "a" * 64,
                "app_asar": r"C:\Users\Private\app.asar",
            },
            "capabilities": {
                "can_upload_native_metadata": {"status": "supported"},
            },
            "api_key": "must-not-persist",  # pragma: allowlist secret -- redaction fixture
        },
    }

    authority = app.compact_native_mutation_authority(report)
    serialized = json.dumps(authority)

    assert authority["status"] == "qualified"
    assert authority["native_mutation_contract"] == "contract-1"
    assert authority["capability_statuses"] == {
        "can_upload_native_metadata": "supported",
    }
    assert "Private" not in serialized
    assert "must-not-persist" not in serialized
