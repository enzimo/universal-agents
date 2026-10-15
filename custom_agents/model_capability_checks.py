"""Read-only model capability lookup helpers.

These checks intentionally return candidate facts with source metadata. They do
not write configuration and should not be treated as authoritative without user
review.

Sources, checked in this order by default:

- ``litellm_proxy``: the team's own LiteLLM server (``GET <api base>/model/info``),
  which knows the models it actually routes, their context window and input
  kinds. The agent process already calls this server with its key for every
  model request; the key goes only into the request header and never into a
  result, a log line or an error.
- ``openrouter``: OpenRouter's public model directory.
- ``litellm``: the LiteLLM package's built-in model data.
- ``huggingface``: Hugging Face Hub metadata, through the broker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import requests

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
LITELLM_PROXY_MODEL_INFO_PATH = "/model/info"
DEFAULT_METHODS = ("litellm_proxy", "openrouter", "litellm", "huggingface")

_logger = logging.getLogger(__name__)

_IMAGE_MODALITIES = {"image", "vision"}
_AUDIO_MODALITIES = {"audio", "speech"}
_FILE_MODALITIES = {"document", "file", "pdf"}
_OPENROUTER_PREFIXES = ("openrouter/",)
_HUGGINGFACE_PREFIXES = ("huggingface/", "hf/")

_HF_IMAGE_PIPELINES = {
    "image-text-to-text",
    "visual-question-answering",
    "document-question-answering",
    "image-to-text",
}
_HF_AUDIO_PIPELINES = {
    "automatic-speech-recognition",
    "audio-text-to-text",
    "audio-to-audio",
    "audio-classification",
}
_HF_BOTH_PIPELINES = {"any-to-any"}
_HF_IMAGE_TAGS = {
    "image-text-to-text",
    "visual-question-answering",
    "document-question-answering",
    "image-to-text",
    "vision",
}
_HF_AUDIO_TAGS = {
    "automatic-speech-recognition",
    "audio-text-to-text",
    "audio-to-text",
    "speech-to-text",
    "audio",
}
_HF_FILE_PIPELINES = {"document-question-answering"}
_HF_FILE_TAGS = {"document-question-answering", "document-ai", "pdf"}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_method_names(methods: str | Iterable[str] | None) -> list[str]:
    if methods is None:
        return list(DEFAULT_METHODS)
    if isinstance(methods, str):
        raw_items = methods.replace(";", ",").split(",")
    else:
        raw_items = list(methods)
    normalized: list[str] = []
    aliases = {
        "or": "openrouter",
        "openrouter_models_api": "openrouter",
        "lite": "litellm",
        "proxy": "litellm_proxy",
        "litellm_server": "litellm_proxy",
        "hf": "huggingface",
        "hugging_face": "huggingface",
    }
    for item in raw_items:
        value = aliases.get(_clean_text(item).lower(), _clean_text(item).lower())
        if value and value not in normalized:
            normalized.append(value)
    return normalized or list(DEFAULT_METHODS)


def _strip_prefix(value: str, prefixes: tuple[str, ...]) -> str:
    lowered = value.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            return value[len(prefix) :]
    return value


def default_openrouter_model_id(model_id: str) -> str:
    return _strip_prefix(_clean_text(model_id), _OPENROUTER_PREFIXES)


def default_huggingface_repo_id(provider: str, model_id: str) -> str:
    clean_model = _clean_text(model_id)
    clean_provider = _clean_text(provider).lower()
    if clean_provider in {"huggingface", "hf"}:
        return _strip_prefix(clean_model, _HUGGINGFACE_PREFIXES)
    lowered = clean_model.lower()
    if lowered.startswith(_HUGGINGFACE_PREFIXES):
        return _strip_prefix(clean_model, _HUGGINGFACE_PREFIXES)
    return ""


def _modalities_from_modality_text(modality_text: Any) -> list[str]:
    text = _clean_text(modality_text).lower()
    if not text:
        return []
    input_side = text.split("->", 1)[0]
    return [
        item.strip()
        for item in input_side.replace(",", "+").replace("/", "+").split("+")
        if item.strip()
    ]


def _coerce_modalities(value: Any) -> list[str]:
    if isinstance(value, str):
        return _modalities_from_modality_text(value)
    if isinstance(value, list):
        return [_clean_text(item).lower() for item in value if _clean_text(item)]
    return []


def _bool_from_modalities(modalities: list[str], targets: set[str]) -> bool:
    return any(item in targets for item in modalities)


def _result(
    *,
    method: str,
    status: str,
    supports_image_input: bool | None = None,
    supports_audio_input: bool | None = None,
    supports_file_input: bool | None = None,
    confidence: str = "none",
    evidence: list[str] | None = None,
    source: dict[str, Any] | None = None,
    raw: dict[str, Any] | None = None,
    error: str | None = None,
    context_window_tokens: int | None = None,
    supports_reasoning: bool | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "method": method,
        "status": status,
        "supports_image_input": supports_image_input,
        "supports_audio_input": supports_audio_input,
        "supports_file_input": supports_file_input,
        "context_window_tokens": context_window_tokens,
        "supports_reasoning": supports_reasoning,
        "confidence": confidence,
        "evidence": evidence or [],
        "source": source or {},
        "raw": raw or {},
    }
    if error:
        payload["error"] = error
    return payload


def check_openrouter_model_capabilities(
    model_id: str,
    *,
    session: Any | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Check OpenRouter's model registry for input modality metadata."""

    candidate_id = default_openrouter_model_id(model_id)
    if not candidate_id:
        return _result(
            method="openrouter",
            status="skipped",
            evidence=["No OpenRouter model id was provided."],
        )

    source = {"url": OPENROUTER_MODELS_URL, "model_id": candidate_id}
    client = session or requests
    try:
        response = client.get(
            OPENROUTER_MODELS_URL,
            params={"output_modalities": "all"},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        models = response.json().get("data", [])
    except Exception as exc:
        _logger.debug("OpenRouter model capability lookup failed for %s: %s", candidate_id, exc)
        return _result(
            method="openrouter",
            status="error",
            source=source,
            error=str(exc),
        )

    if not isinstance(models, list):
        return _result(
            method="openrouter",
            status="error",
            source=source,
            error="OpenRouter models response did not contain a data list.",
        )

    model = next((item for item in models if isinstance(item, dict) and item.get("id") == candidate_id), None)
    if model is None:
        return _result(
            method="openrouter",
            status="not_found",
            source=source,
            evidence=["Model id was not present in the OpenRouter model directory response."],
        )

    architecture = model.get("architecture") if isinstance(model.get("architecture"), dict) else {}
    input_modalities = _coerce_modalities(architecture.get("input_modalities"))
    if not input_modalities:
        input_modalities = _modalities_from_modality_text(architecture.get("modality"))
    image = _bool_from_modalities(input_modalities, _IMAGE_MODALITIES)
    audio = _bool_from_modalities(input_modalities, _AUDIO_MODALITIES)
    file_input = _bool_from_modalities(input_modalities, _FILE_MODALITIES)
    evidence = [f"OpenRouter input modalities: {', '.join(input_modalities) or 'not listed'}."]
    return _result(
        method="openrouter",
        status="found",
        supports_image_input=image,
        supports_audio_input=audio,
        supports_file_input=file_input,
        confidence="high",
        evidence=evidence,
        source=source,
        raw={
            "id": model.get("id"),
            "architecture": {
                "modality": architecture.get("modality"),
                "input_modalities": architecture.get("input_modalities"),
                "output_modalities": architecture.get("output_modalities"),
            },
        },
    )


def _optional_bool_from_mapping(mapping: dict[str, Any], key: str) -> bool | None:
    value = mapping.get(key)
    if isinstance(value, bool):
        return value
    return None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _proxy_entry(entries: list[Any], model_id: str) -> dict[str, Any] | None:
    """The server's entry for a model: its public name first, then the model it routes to."""

    candidates = [entry for entry in entries if isinstance(entry, dict)]
    for entry in candidates:
        if entry.get("model_name") == model_id:
            return entry
    for entry in candidates:
        params = entry.get("litellm_params") if isinstance(entry.get("litellm_params"), dict) else {}
        if params.get("model") == model_id:
            return entry
    return None


def check_litellm_proxy_model_info(
    model_id: str,
    *,
    api_base: str,
    api_key: str | None = None,
    session: Any | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Ask the team's LiteLLM server what it knows about a model it routes.

    Reads only the entry's ``model_info`` and the name of the model it routes
    to; the rest of ``litellm_params`` (upstream addresses, masked keys) never
    enters the result.
    """

    candidate_id = _clean_text(model_id)
    base = _clean_text(api_base).rstrip("/")
    if not candidate_id or not base:
        return _result(
            method="litellm_proxy",
            status="skipped",
            evidence=["No LiteLLM server address is set for this agent." if not base else "No model id was provided."],
        )
    url = f"{base}{LITELLM_PROXY_MODEL_INFO_PATH}"
    source = {"url": url, "model_id": candidate_id}
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    client = session or requests
    try:
        response = client.get(url, headers=headers, timeout=timeout_seconds)
        response.raise_for_status()
        entries = response.json().get("data", [])
    except Exception as exc:
        # Neither the request's error text nor this log line carries the key:
        # it is only in the header.
        _logger.info(
            "The LiteLLM server model lookup failed. event=litellm_proxy_model_info_failed url=%s model_id=%s error=%s",
            url,
            candidate_id,
            type(exc).__name__,
        )
        return _result(method="litellm_proxy", status="error", source=source, error=type(exc).__name__)
    entry = _proxy_entry(entries if isinstance(entries, list) else [], candidate_id)
    if entry is None:
        return _result(
            method="litellm_proxy",
            status="not_found",
            source=source,
            evidence=["The LiteLLM server has no model with this name."],
        )
    info = entry.get("model_info") if isinstance(entry.get("model_info"), dict) else {}
    routed = (entry.get("litellm_params") or {}).get("model") if isinstance(entry.get("litellm_params"), dict) else None
    context_window = _positive_int(info.get("max_input_tokens"))
    evidence = [f"The LiteLLM server routes {candidate_id} to {routed or 'an unnamed model'}."]
    if context_window:
        evidence.append(f"Context window: {context_window} tokens.")
    return _result(
        method="litellm_proxy",
        status="found",
        supports_image_input=_optional_bool_from_mapping(info, "supports_vision"),
        supports_audio_input=_optional_bool_from_mapping(info, "supports_audio_input"),
        supports_file_input=_optional_bool_from_mapping(info, "supports_pdf_input"),
        context_window_tokens=context_window,
        supports_reasoning=_optional_bool_from_mapping(info, "supports_reasoning"),
        confidence="high",
        evidence=evidence,
        source=source,
        raw={
            "model_name": entry.get("model_name"),
            "routed_model": routed,
            "mode": info.get("mode"),
            "max_input_tokens": info.get("max_input_tokens"),
            "max_output_tokens": info.get("max_output_tokens"),
            "supports_vision": info.get("supports_vision"),
            "supports_audio_input": info.get("supports_audio_input"),
            "supports_pdf_input": info.get("supports_pdf_input"),
            "supports_reasoning": info.get("supports_reasoning"),
        },
    )


def check_litellm_model_capabilities(model_id: str) -> dict[str, Any]:
    """Check LiteLLM's installed model info map."""

    candidate_id = _clean_text(model_id)
    if not candidate_id:
        return _result(
            method="litellm",
            status="skipped",
            evidence=["No LiteLLM model id was provided."],
        )
    try:
        import litellm

        info = litellm.get_model_info(model=candidate_id)
    except Exception as exc:
        _logger.debug("LiteLLM model capability lookup failed for %s: %s", candidate_id, exc)
        return _result(
            method="litellm",
            status="not_found",
            source={"package": "litellm", "model_id": candidate_id},
            error=str(exc),
        )

    if not isinstance(info, dict):
        return _result(
            method="litellm",
            status="error",
            source={"package": "litellm", "model_id": candidate_id},
            error="litellm.get_model_info did not return a mapping.",
        )
    image = _optional_bool_from_mapping(info, "supports_vision")
    audio = _optional_bool_from_mapping(info, "supports_audio_input")
    file_input = _optional_bool_from_mapping(info, "supports_pdf_input")
    evidence = []
    if image is None and hasattr(litellm, "supports_vision"):
        try:
            image = bool(litellm.supports_vision(model=candidate_id))
            evidence.append(f"LiteLLM supports_vision()={image!r}.")
        except Exception as exc:
            evidence.append(f"LiteLLM supports_vision() unavailable: {exc}.")
    if audio is None and hasattr(litellm, "supports_audio_input"):
        try:
            audio = bool(litellm.supports_audio_input(model=candidate_id))
            evidence.append(f"LiteLLM supports_audio_input()={audio!r}.")
        except Exception as exc:
            evidence.append(f"LiteLLM supports_audio_input() unavailable: {exc}.")
    if "supports_vision" in info:
        evidence.append(f"LiteLLM supports_vision={info.get('supports_vision')!r}.")
    if "supports_audio_input" in info:
        evidence.append(f"LiteLLM supports_audio_input={info.get('supports_audio_input')!r}.")
    if "supports_pdf_input" in info:
        evidence.append(f"LiteLLM supports_pdf_input={info.get('supports_pdf_input')!r}.")
    return _result(
        method="litellm",
        status="found",
        supports_image_input=image,
        supports_audio_input=audio,
        supports_file_input=file_input,
        context_window_tokens=_positive_int(info.get("max_input_tokens")),
        supports_reasoning=_optional_bool_from_mapping(info, "supports_reasoning"),
        confidence="medium",
        evidence=evidence or ["LiteLLM returned model info without modality-specific fields."],
        source={"package": "litellm", "model_id": candidate_id},
        raw={
            "supports_vision": info.get("supports_vision"),
            "supports_audio_input": info.get("supports_audio_input"),
            "supports_audio_output": info.get("supports_audio_output"),
            "supports_pdf_input": info.get("supports_pdf_input"),
            "mode": info.get("mode"),
        },
    )


@dataclass(frozen=True)
class HubModelInfo:
    """The model metadata the capability check reads."""

    pipeline_tag: str | None
    tags: tuple[str, ...]


class HuggingFaceAccountUnavailable(Exception):
    """The linked Hugging Face account could not be used; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Looks a model up with the team's (or the person's) linked Hugging Face
# account through the secure tool broker (``cli:huggingface`` ``model.info``),
# so gated and private models are found without a token in this process.
HuggingFaceAccountLookup = Callable[[str], HubModelInfo]


def _anonymous_model_info(repo_id: str, api: Any | None) -> HubModelInfo:
    if api is None:
        from huggingface_hub import HfApi

        # token=False: never an ambient HF_TOKEN or a cached login.
        api = HfApi(token=False)
    info = api.model_info(repo_id)
    return HubModelInfo(
        pipeline_tag=getattr(info, "pipeline_tag", None),
        tags=tuple(str(tag) for tag in (getattr(info, "tags", None) or [])),
    )


def _huggingface_model_info(
    repo_id: str, *, api: Any | None, account_lookup: HuggingFaceAccountLookup | None
) -> tuple[HubModelInfo, str, str | None]:
    """The metadata, which account read it ("linked" or "none"), and a note when none did."""

    if account_lookup is None:
        return _anonymous_model_info(repo_id, api), "none", None
    try:
        return account_lookup(repo_id), "linked", None
    except HuggingFaceAccountUnavailable as exc:
        _logger.info(
            "Hugging Face model lookup ran without an account. event=model_capability_hf_anonymous_lookup "
            "repo_id=%s reason=%s",
            repo_id,
            exc.reason,
        )
        note = (
            f"Looked up without a Hugging Face account ({exc.reason}); gated or private models are not "
            "visible this way. A team administrator can link one with /link cli:huggingface team."
        )
        return _anonymous_model_info(repo_id, api), "none", note


def _huggingface_modalities(
    pipeline_tag: str, tags: set[str]
) -> tuple[bool | None, bool | None, bool | None]:
    """Image, audio and file input hints from the pipeline tag, then the tags."""

    image = True if pipeline_tag in _HF_BOTH_PIPELINES | _HF_IMAGE_PIPELINES or tags & _HF_IMAGE_TAGS else None
    audio = True if pipeline_tag in _HF_BOTH_PIPELINES | _HF_AUDIO_PIPELINES or tags & _HF_AUDIO_TAGS else None
    file_input = True if pipeline_tag in _HF_FILE_PIPELINES or tags & _HF_FILE_TAGS else None
    return image, audio, file_input


def check_huggingface_model_capabilities(
    repo_id: str,
    *,
    api: Any | None = None,
    account_lookup: HuggingFaceAccountLookup | None = None,
) -> dict[str, Any]:
    """Check Hugging Face Hub model metadata for modality hints."""

    candidate_id = _clean_text(repo_id)
    if not candidate_id:
        return _result(
            method="huggingface",
            status="skipped",
            evidence=["No Hugging Face repo id was provided."],
        )
    try:
        info, account, note = _huggingface_model_info(candidate_id, api=api, account_lookup=account_lookup)
    except Exception as exc:
        _logger.debug("Hugging Face model capability lookup failed for %s: %s", candidate_id, exc)
        return _result(
            method="huggingface",
            status="not_found",
            source={"package": "huggingface_hub", "repo_id": candidate_id},
            error=str(exc),
        )

    pipeline_tag = _clean_text(info.pipeline_tag).lower()
    tags = {_clean_text(tag).lower() for tag in info.tags if _clean_text(tag)}
    hint_tags = _HF_IMAGE_TAGS | _HF_AUDIO_TAGS | _HF_FILE_TAGS | {"multimodal"}
    evidence: list[str] = []
    if pipeline_tag:
        evidence.append(f"Hugging Face pipeline_tag={pipeline_tag!r}.")
    matched_tags = sorted(tags & hint_tags)
    if matched_tags:
        evidence.append(f"Hugging Face matched tags: {', '.join(matched_tags)}.")
    image, audio, file_input = _huggingface_modalities(pipeline_tag, tags)
    specific = image is not None or audio is not None or file_input is not None
    if "multimodal" in tags and not specific:
        evidence.append(
            "A generic multimodal tag was present, but image/audio/file input support was not specific."
        )
    if note:
        evidence.append(note)

    return _result(
        method="huggingface",
        status="found",
        supports_image_input=image,
        supports_audio_input=audio,
        supports_file_input=file_input,
        confidence="medium" if specific else "low",
        evidence=evidence or ["Hugging Face metadata did not include modality-specific hints."],
        source={"package": "huggingface_hub", "repo_id": candidate_id, "account": account},
        raw={"pipeline_tag": pipeline_tag or None, "matched_tags": matched_tags},
    )


def _first_found(results: list[dict[str, Any]], field: str) -> Any:
    return next(
        (result[field] for result in results if result.get("status") == "found" and result.get(field) is not None),
        None,
    )


def _consensus_for_field(results: list[dict[str, Any]], field: str) -> tuple[bool | None, str]:
    values = [
        result.get(field)
        for result in results
        if result.get("status") == "found" and isinstance(result.get(field), bool)
    ]
    if not values:
        return None, "insufficient_evidence"
    unique = set(values)
    if len(unique) == 1:
        return values[0], "unanimous"
    return None, "conflict"


def _capability_option(
    option_id: str,
    label: str,
    *,
    supports_image_input: bool | None,
    supports_audio_input: bool | None,
    supports_file_input: bool | None,
    description: str,
    recommended: bool = False,
    enabled: bool = True,
) -> dict[str, Any]:
    return {
        "id": option_id,
        "label": label,
        "description": description,
        "recommended": recommended,
        "enabled": enabled,
        "changes": {
            "supports_image_input": supports_image_input,
            "supports_audio_input": supports_audio_input,
            "supports_file_input": supports_file_input,
        },
    }


def _feedback_options(consensus: dict[str, Any]) -> list[dict[str, Any]]:
    image = consensus.get("supports_image_input")
    audio = consensus.get("supports_audio_input")
    file_input = consensus.get("supports_file_input")
    has_recommendation = (
        isinstance(image, bool)
        or isinstance(audio, bool)
        or isinstance(file_input, bool)
    )
    options = [
        _capability_option(
            "accept_lookup_recommendation",
            "Use lookup recommendation",
            supports_image_input=image if isinstance(image, bool) else None,
            supports_audio_input=audio if isinstance(audio, bool) else None,
            supports_file_input=file_input if isinstance(file_input, bool) else None,
            description="Write the non-null values supported by the lookup evidence and leave unknown fields unchanged.",
            recommended=has_recommendation,
            enabled=has_recommendation,
        ),
        _capability_option(
            "mark_text_only",
            "Text only",
            supports_image_input=False,
            supports_audio_input=False,
            supports_file_input=False,
            description="Record that this model should only receive plain text inputs.",
        ),
        _capability_option(
            "mark_image_only",
            "Image input only",
            supports_image_input=True,
            supports_audio_input=False,
            supports_file_input=False,
            description="Record image input support and audio input unsupported.",
        ),
        _capability_option(
            "mark_audio_only",
            "Audio input only",
            supports_image_input=False,
            supports_audio_input=True,
            supports_file_input=False,
            description="Record audio input support and image input unsupported.",
        ),
        _capability_option(
            "mark_image_and_audio",
            "Image and audio",
            supports_image_input=True,
            supports_audio_input=True,
            supports_file_input=False,
            description="Record support for both image and audio input.",
        ),
        _capability_option(
            "mark_file_only",
            "File input only",
            supports_image_input=False,
            supports_audio_input=False,
            supports_file_input=True,
            description="Record native file/document input support only.",
        ),
        _capability_option(
            "mark_all_inputs",
            "Images, audio, and files",
            supports_image_input=True,
            supports_audio_input=True,
            supports_file_input=True,
            description="Record support for all three optional input types.",
        ),
        _capability_option(
            "keep_unknown",
            "Keep unknown",
            supports_image_input=None,
            supports_audio_input=None,
            supports_file_input=None,
            description="Do not write capability metadata yet.",
        ),
    ]
    return options


def lookup_model_capability_candidates(
    *,
    provider: str = "",
    model_id: str = "",
    openrouter_model_id: str = "",
    huggingface_repo_id: str = "",
    methods: str | Iterable[str] | None = None,
    timeout_seconds: float = 10.0,
    huggingface_account_lookup: HuggingFaceAccountLookup | None = None,
    litellm_api_base: str = "",
    litellm_api_key: str | None = None,
) -> dict[str, Any]:
    """Run selected read-only checks and return user-reviewable candidates."""

    provider_text = _clean_text(provider)
    model_text = _clean_text(model_id)
    requested_methods = _normalize_method_names(methods)
    results: list[dict[str, Any]] = []

    for method in requested_methods:
        if method == "litellm_proxy":
            results.append(
                check_litellm_proxy_model_info(
                    model_text,
                    api_base=litellm_api_base,
                    api_key=litellm_api_key,
                    timeout_seconds=timeout_seconds,
                )
            )
        elif method == "openrouter":
            results.append(
                check_openrouter_model_capabilities(
                    _clean_text(openrouter_model_id) or default_openrouter_model_id(model_text),
                    timeout_seconds=timeout_seconds,
                )
            )
        elif method == "litellm":
            results.append(check_litellm_model_capabilities(model_text))
        elif method == "huggingface":
            repo_id = _clean_text(huggingface_repo_id) or default_huggingface_repo_id(provider_text, model_text)
            results.append(
                check_huggingface_model_capabilities(repo_id, account_lookup=huggingface_account_lookup)
            )
        else:
            results.append(
                _result(
                    method=method,
                    status="skipped",
                    evidence=[f"Unknown capability lookup method: {method}."],
                )
            )

    image, image_status = _consensus_for_field(results, "supports_image_input")
    audio, audio_status = _consensus_for_field(results, "supports_audio_input")
    file_input, file_status = _consensus_for_field(results, "supports_file_input")
    consensus = {
        "supports_image_input": image,
        "supports_audio_input": audio,
        "supports_file_input": file_input,
        # The first source that knows it, in method order (the team's own server first).
        "context_window_tokens": _first_found(results, "context_window_tokens"),
        "supports_reasoning": _first_found(results, "supports_reasoning"),
        "image_evidence_status": image_status,
        "audio_evidence_status": audio_status,
        "file_evidence_status": file_status,
        "overall_status": (
            "conflict"
            if "conflict" in {image_status, audio_status, file_status}
            else "insufficient_evidence"
            if (
                image_status == "insufficient_evidence"
                and audio_status == "insufficient_evidence"
                and file_status == "insufficient_evidence"
            )
            else "candidate"
        ),
    }
    return {
        "status": "ok",
        "provider": provider_text,
        "model_id": model_text,
        "methods_requested": requested_methods,
        "results": results,
        "consensus": consensus,
        "user_feedback_options": _feedback_options(consensus),
        "write_policy": (
            "Read-only lookup; it changes nothing. When switching a model, stage only what "
            "the person asked for plus the context window and input kinds this evidence "
            "supports; when the "
            "sources conflict or say nothing, ask the person instead of guessing."
        ),
    }
