"""Reviewed public video examples, without private procurement metadata."""

import json
from html import escape

REVIEWED_VIDEO_MODELS = {"kling-4.0", "minimax-h3", "wan-3", "wan-3-prime"}
EXAMPLES = {
    "kling-4.0": [
        {"model": "kling-4.0", "prompt": "A slow aerial shot over a foggy pine forest at sunrise",
         "duration": 5, "resolution": "720p", "aspect_ratio": "16:9", "generate_audio": True},
        {"model": "kling-4.0", "prompt": "Let @Image 1 follow the motion from @Video 1",
         "duration": 5, "resolution": "1080p", "aspect_ratio": "adaptive", "generate_audio": False,
         "reference_images": [{"url": "https://media.example.com/subject.png"}],
         "reference_videos": [{"url": "https://media.example.com/motion.mp4"}]},
    ],
    "minimax-h3": [
        {"model": "minimax-h3", "prompt": "A slow camera move around a ceramic cup", "duration": 5,
         "resolution": "768p", "aspect_ratio": "16:9"},
        {"model": "minimax-h3", "prompt": "Follow the movement in @Video 1", "duration": 5,
         "resolution": "2k", "reference_videos": [{"url": "https://media.example.com/reference.mp4"}]},
        {"model": "minimax-h3", "prompt": "Move smoothly between the frames", "duration": 5,
         "start_image": {"url": "https://media.example.com/first.png"},
         "end_image": {"url": "https://media.example.com/last.png"}, "aspect_ratio": "adaptive"},
    ],
    "wan-3": [
        {"model": "wan-3", "prompt": "A slow camera move around a ceramic cup", "duration": 5,
         "resolution": "720p", "generate_audio": False, "seed": 42},
        {"model": "wan-3", "prompt": "Make the colors in @Video 1 warmer",
         "omni_reference_task_type": "edit", "resolution": "720p",
         "reference_videos": [{"url": "https://media.example.com/source.mp4"}]},
    ],
    "wan-3-prime": [
        {"model": "wan-3-prime", "prompt": "Animate the subject in @Image 1", "duration": 5,
         "resolution": "720p", "aspect_ratio": "16:9",
         "reference_images": [{"url": "https://media.example.com/reference.jpg"}]},
    ],
}

LIMITS = {
    "kling-4.0": (
        "3–30 сек; 720p / 1080p; до 10 изображений и 5 видео, всего 15; reference_audios не "
        "поддерживается. Промпт обязателен: до 26 000 английских или 11 000 других символов. "
        "Пропорции: 21:9, 16:9, 1:1, 9:16; с референсами также adaptive. Видеореференсы: "
        "3–30 сек каждый, до 30 сек суммарно. Кадры, seed и watermark не поддерживаются. "
        "generate_audio по умолчанию true; false создаёт видео без звука. Видеореференс выбирает отдельный "
        "тариф video_input; итоговое списание идёт по подтверждённому usage.billed_seconds.",
        "3–30 seconds; 720p / 1080p; up to 10 images and 5 videos, 15 total; reference_audios is "
        "unsupported. A prompt is required: up to 26,000 English or 11,000 other characters. Ratios: "
        "21:9, 16:9, 1:1, 9:16; reference requests also support adaptive. Reference videos: 3–30 "
        "seconds each, 30 seconds total. Frames, seed and watermark are unsupported. generate_audio "
        "defaults to true; false creates a silent result. A video reference selects the separate video_input "
        "rate; confirmed usage.billed_seconds determines the final charge.",
    ),
    "minimax-h3": (
        "4–15 сек; 768p / 2k; до 9 изображений, 3 видео, 3 аудио, всего 12. "
        "Промпт обязателен, до 7000 символов. Входные видео и аудио: 2–15 сек, суммарно до 15 сек на тип. "
        "Аудио требует изображение или видео. Первый и последний кадры нельзя смешивать с референсами; "
        "aspect_ratio для кадров: adaptive или не указан. Seed и отключение звука не поддерживаются.",
        "4–15 seconds; 768p / 2k; up to 9 images, 3 videos, 3 audios, 12 total. "
        "Required prompt: at most 7000 characters. Video/audio references: 2–15 seconds, 15 total per type. "
        "Audio needs an image or video. Frames cannot mix with references; frame aspect_ratio is adaptive "
        "or omitted. No seed or silent output.",
    ),
    "wan-3": (
        "2–30 сек; 480p / 720p / 1080p; до 10 изображений, 5 видео, 5 аудио, всего 20. "
        "Промпт обязателен, до 20000 символов. Первый/последний кадр и edit поддерживаются. "
        "В edit не передавайте duration, aspect_ratio, size: размеры берутся из первого видео. "
        "Поддерживаются seed от 0 до 4294967295 и generate_audio: false.",
        "2–30 seconds; 480p / 720p / 1080p; up to 10 images, 5 videos, 5 audios, 20 total. "
        "Required prompt: at most 20000 characters. Frames and edit are supported. In edit omit duration, "
        "aspect_ratio and size: the first video determines dimensions. Seed 0–4294967295 and "
        "generate_audio: false are supported.",
    ),
    "wan-3-prime": (
        "4–30 сек; 480p / 720p / 1080p; до 10 изображений, 5 видео, 5 аудио, всего 20. "
        "Промпт обязателен, до 20000 символов. Пропорции: 16:9, 1:1, 9:16. "
        "Первый/последний кадр, edit, seed, adaptive и отключение звука не поддерживаются. "
        "Аудиореференс требует изображение или видео.",
        "4–30 seconds; 480p / 720p / 1080p; up to 10 images, 5 videos, 5 audios, 20 total. "
        "Required prompt: at most 20000 characters. Ratios: 16:9, 1:1, 9:16. No frames, edit, seed, "
        "adaptive ratio or silent output. Audio references need an image or video.",
    ),
}


def render_native_video_reference(lang: str, enabled_models: set[str]) -> str:
    parts = []
    for model in sorted(enabled_models & REVIEWED_VIDEO_MODELS):
        parts.append(f'<section><h3 id="{escape(model)}">{escape(model)}</h3>')
        parts.append("<p>" + escape(LIMITS[model][0 if lang == "ru" else 1]) + "</p>")
        for example in EXAMPLES[model]:
            parts.append("<pre><code>POST /v1/videos/generations\n" +
                         escape(json.dumps(example, ensure_ascii=False, indent=2)) + "</code></pre>")
        media_billing_ru = (
            "Изображения отдельно не оплачиваются; аудиореференсы не поддерживаются."
            if model == "kling-4.0"
            else "Изображения и аудио отдельно не оплачиваются."
        )
        media_billing_en = (
            "Images have no extra fee; audio references are unsupported."
            if model == "kling-4.0"
            else "Images and audio have no extra fee."
        )
        ru = (
            "Референсы передаются как HTTPS URL. Предварительного удержания с партнёрского баланса нет. "
            "Списание выполняется после успешной генерации по подтверждённым usage.billed_seconds. "
            f"{media_billing_ru} Один запрос — одно видео. "
            "Сохраняйте request_id из HTTP 202, опрашивайте GET /v1/videos/{request_id}; "
            "при done скачайте /v1/videos/{request_id}/content. Не повторяйте POST с новым ключом при задержке."
        )
        en = (
            "References are HTTPS URLs. No upfront hold is placed on partner funds. "
            f"Confirmed usage.billed_seconds determines the final charge. {media_billing_en} "
            "One request produces one video. Save request_id from HTTP 202, poll GET /v1/videos/{request_id}; "
            "on done download /v1/videos/{request_id}/content. Do not replay a slow POST with a new key."
        )
        parts.append("<p>" + escape(ru if lang == "ru" else en) + "</p>")
        if model.startswith("wan-"):
            parts.append("<p>" + escape(
                "Входные клипы: 1–15 сек, до 15 сек суммарно. Результат и видеореференсы вместе: до 30 сек."
                if lang == "ru" else
                "Input clips: 1–15 seconds, 15 total. Output and video references together: at most 30 seconds."
            ) + "</p>")
        parts.append("</section>")
    return "".join(parts)
