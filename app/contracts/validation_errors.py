"""Allowlisted public validation reasons; never expose exception text verbatim."""

VALIDATION_REASONS = frozenset(
    {
        "audio_requires_visual_reference",
        "conflicting_media_aliases",
        "conflicting_media_inputs",
        "conflicting_size",
        "duplicate_form_field",
        "edit_requires_single_video",
        "explicit_resolution_required_for_minimax_size",
        "frame_aspect_ratio_is_derived_from_input",
        "input_required",
        "invalid_duration",
        "invalid_edit_request",
        "invalid_frame_images",
        "invalid_generate_audio",
        "invalid_input_references",
        "invalid_mask_count",
        "invalid_max_completion_tokens",
        "invalid_max_output_tokens",
        "invalid_max_tokens",
        "invalid_media_reference",
        "invalid_n",
        "invalid_prompt",
        "invalid_reference_images",
        "invalid_request_body",
        "invalid_size",
        "invalid_stream",
        "max_tokens_required",
        "messages_required",
        "model_protocol_mismatch",
        "model_required",
        "prompt_required",
        "prompt_too_long",
        "reference_limit_exceeded",
        "reference_resolution_not_supported",
        "unknown_model_contract",
        "unsupported_aspect_ratio",
        "unsupported_generation_control",
        "unsupported_resolution",
        "unsupported_response_format",
        "unsupported_size",
        "unsupported_size_ratio",
        "unsupported_task_type",
        "video_body_too_large",
    }
)


def validation_reason(error: Exception) -> str | None:
    """Only our intentional ValueError codes may cross the public/log boundary."""
    if not isinstance(error, ValueError):
        return None
    code = str(error)
    return code if code in VALIDATION_REASONS else None
