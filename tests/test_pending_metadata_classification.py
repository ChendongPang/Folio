from copy import deepcopy

import pytest

from folio.models import SectionContent
from folio.runtime.pending_metadata import validate_pending_metadata
from folio.runtime.writing_transport import validate_writing_provenance


def metadata(field_name="备注", kind="document_date"):
    return {
        "field_name": field_name,
        "kind": kind,
        "block_id": "b1",
        "row_index": 1,
        "column_index": 1,
        "description": "缺少真实值。",
    }


def test_real_blank_remarks_cannot_be_reported_as_fifteen_missing_dates():
    content = {
        "blocks": [
            {
                "block_id": "b1",
                "kind": "table",
                "rows": [["协同对象", "备注"], *[[f"对象{i}", ""] for i in range(15)]],
            }
        ],
        "record_only_pending": [metadata() | {"row_index": i + 1} for i in range(15)],
    }
    before = deepcopy(content)
    # Existing serialized jobs remain readable; new output is checked at writer boundary.
    assert len(SectionContent.model_validate(content).record_only_pending) == 15
    for task in ("generate", "repair"):
        with pytest.raises(ValueError, match="Generic remarks"):
            validate_writing_provenance(task, {}, content)
    assert content == before
    content["record_only_pending"] = []
    validate_pending_metadata(content)


@pytest.mark.parametrize(
    "label,kind",
    [
        ("提交日期", "document_date"),
        ("Document date", "document_date"),
        ("文档版本", "document_version"),
        ("Revision", "document_version"),
        ("文档编号", "document_number"),
        ("接口人", "person_name"),
        ("联系邮箱", "contact_information"),
    ],
)
def test_actual_person_contact_and_document_fields_remain_valid(label, kind):
    validate_pending_metadata({"record_only_pending": [metadata(label, kind)]})


@pytest.mark.parametrize(
    "label,kind",
    [
        ("备注", "person_name"),
        ("备注", "contact_information"),
        ("说明", "document_date"),
        ("Remarks", "document_date"),
        ("接口人", "document_date"),
        ("提交日期", "document_version"),
        ("文档版本", "document_number"),
    ],
)
def test_misclassification_is_rejected_not_silently_relabelled(label, kind):
    with pytest.raises(ValueError):
        validate_pending_metadata({"record_only_pending": [metadata(label, kind)]})
