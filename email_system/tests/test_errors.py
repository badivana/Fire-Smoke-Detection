from app.core.errors import ERROR_POLICIES, ErrorCode, FailureState, PipelineError


def test_every_error_code_has_a_policy():
    assert set(ERROR_POLICIES) == set(ErrorCode)


def test_processing_errors_go_to_error_and_send_errors_to_failed():
    for code in (
        ErrorCode.LLM_UNAVAILABLE,
        ErrorCode.LLM_SCHEMA_INVALID,
        ErrorCode.GMAIL_AUTH_FAILED,
        ErrorCode.OCR_UNAVAILABLE,
    ):
        assert ERROR_POLICIES[code].state == FailureState.ERROR
    for code in (ErrorCode.SEND_FAILED, ErrorCode.SEND_DELIVERY_UNKNOWN):
        assert ERROR_POLICIES[code].state == FailureState.FAILED


def test_ambiguous_send_is_not_retryable():
    # A blind retry after a timeout could deliver the same reply twice.
    assert ERROR_POLICIES[ErrorCode.SEND_DELIVERY_UNKNOWN].retryable is False


def test_rejected_requests_do_not_change_state():
    for code in (
        ErrorCode.SEND_NOT_APPROVED,
        ErrorCode.SEND_DISABLED,
        ErrorCode.INVALID_TRANSITION,
        ErrorCode.DUPLICATE_EMAIL,
    ):
        assert ERROR_POLICIES[code].state == FailureState.NONE


def test_pipeline_error_detail_is_bounded():
    e = PipelineError(ErrorCode.LLM_SCHEMA_INVALID, "x" * 5000)
    assert len(e.detail) == 500
    assert e.policy.retryable is True
