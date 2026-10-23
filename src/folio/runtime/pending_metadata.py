"""Reject invalid metadata classifications on new model output, not old archives."""

import re

_GENERIC_LABELS = {
    "备注",
    "说明",
    "其他",
    "其它",
    "附注",
    "注释",
    "补充说明",
    "备注信息",
    "备注说明",
    "remarks",
    "remark",
    "notes",
    "note",
    "comments",
    "comment",
    "description",
    "other",
}
_EXPLICIT_LABELS = {
    "document_date": re.compile(r"日期|时间|年月|签发|生效|\bdate\b|\bdated\b|\btime\b", re.I),
    "document_version": re.compile(r"版本|版次|\bversion\b|\brevision\b", re.I),
    "document_number": re.compile(
        r"编号|编码|文号|号码|\bnumber\b|\bno\.?\b|\bcode\b|\bid\b", re.I
    ),
}


def validate_pending_metadata(value):
    """A blank remarks cell is not a missing date or an obligatory business gap.

    Coordinate/header matching remains SectionContent's responsibility. This check
    never infers a business requirement, invents values or edits the generated table.
    """
    for item in value.get("record_only_pending", []):
        label = item.get("field_name", "").strip()
        normalized = re.sub(r"[\s：:]*$", "", label).casefold()
        if normalized in _GENERIC_LABELS:
            raise ValueError(
                "Generic remarks/description column is not missing person or document metadata: "
                + label
                + "; leave the optional cell blank and remove this record_only_pending entry. "
                "Do not turn an optional remarks cell into pending_items or invent a date."
            )
        pattern = _EXPLICIT_LABELS.get(item.get("kind"))
        if pattern and not pattern.search(label):
            raise ValueError(
                "Pending metadata kind does not match the explicit table field: "
                + str(item.get("kind"))
                + "/"
                + label
                + "; preserve the table and classify only an actual document date/version/number."
            )
