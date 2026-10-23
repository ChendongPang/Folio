from folio.source_documents import explicit_request_title, request_title


def user_message(text, **extra):
    return {"role": "user", "content": text, "action": "generate", **extra}


def test_prompt_prefix_is_not_treated_as_a_document_title():
    messages = [user_message(
        "Using the uploaded sources, create a complete bid proposal covering the technical "
        "solution, implementation plan and acceptance terms."
    )]
    assert explicit_request_title(messages) is None
    assert request_title(messages) == "Document"


def test_explicit_titles_support_chinese_and_english_wording():
    assert explicit_request_title([
        user_message("生成一份标书。文档标题为“DECC 网站改版投标方案”。")
    ]) == "DECC 网站改版投标方案"
    assert explicit_request_title([
        user_message("Create a proposal. Title: DECC Website Redesign Proposal.")
    ]) == "DECC Website Redesign Proposal"
    assert explicit_request_title([
        user_message("Create the report titled “Annual Research Report”.")
    ]) == "Annual Research Report"


def test_markdown_heading_is_an_explicit_title_but_a_plain_first_line_is_not():
    assert explicit_request_title([
        user_message("# 外汇交易需求说明书\n说明撤销规则。")
    ]) == "外汇交易需求说明书"
    assert explicit_request_title([
        user_message("请根据资料写一份需求说明书，重点写业务流程。")
    ]) is None


def test_writing_guide_is_only_a_fallback_title():
    messages = [user_message("请根据资料生成完整需求说明书。")]
    assert explicit_request_title(messages, {"name": "软件需求规格说明书"}) == "软件需求规格说明书"
    assert explicit_request_title(
        [user_message("标题为“交易审批需求”。")],
        {"name": "软件需求规格说明书"},
    ) == "交易审批需求"


def test_builtin_guide_fallback_follows_request_but_explicit_title_is_not_translated():
    guide = {"name": "软件需求规格说明书"}
    fallback = explicit_request_title([user_message("Write the requirements document.")], guide)
    assert fallback and "软件" not in fallback
    assert explicit_request_title([user_message('Write a report. Title: "软件需求规格说明书"')], guide) == guide["name"]
