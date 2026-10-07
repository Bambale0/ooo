from app.inference.router import video_failure_error


def test_video_capacity_error_is_actionable_and_retryable():
    assert video_failure_error(
        "provider_generation_failed",
        "This model is at capacity right now. Submit the request again later.",
    ) == {
        "type": "provider_generation_failed",
        "code": "model_capacity",
        "message": "The video model is temporarily at capacity. Retry later.",
        "retryable": True,
    }


def test_generic_provider_generation_failure_is_actionable():
    assert video_failure_error(
        "provider_generation_failed",
        "The result could not be generated.",
    ) == {
        "type": "provider_generation_failed",
        "code": "generation_failed",
        "message": "The video could not be generated. Retry the request or adjust the input.",
        "retryable": True,
    }
