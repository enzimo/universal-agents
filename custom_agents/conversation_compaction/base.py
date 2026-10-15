import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from default_values import CONVERSATION_COMPACTION_DEFAULTS, LLM_DEFAULTS
from custom_agents.conversation_store import ConversationStore


@dataclass
class ConversationCompactionConfig:
    context_window_tokens: int = 128000
    trigger_ratio: float = CONVERSATION_COMPACTION_DEFAULTS.trigger_ratio.value
    target_ratio: float = CONVERSATION_COMPACTION_DEFAULTS.target_ratio.value
    min_recent_turns: int = CONVERSATION_COMPACTION_DEFAULTS.min_recent_turns.value
    max_recent_turns: int = CONVERSATION_COMPACTION_DEFAULTS.max_recent_turns.value
    trigger_turn_count: Optional[int] = None
    trigger_message_count: Optional[int] = None
    compaction_model_provider: Optional[str] = None
    compaction_model_id: Optional[str] = None
    default_model_provider: str = LLM_DEFAULTS.provider.value
    default_model_id: str = LLM_DEFAULTS.model_id.value
    temperature: float = LLM_DEFAULTS.temperature.value
    litellm_api_base: Optional[str] = None
    litellm_api_mode: str = LLM_DEFAULTS.litellm_api_mode.value
    openai_codex: Optional[Mapping[str, Any]] = None
    xai_oauth: Optional[Mapping[str, Any]] = None
    instance_root: Optional[str] = None
    workspace_dir: Optional[str] = None


@dataclass
class CompactionResult:
    compacted: bool
    token_estimate: int
    prompt_context: str
    context_epoch_id: Optional[str] = None
    through_turn_index: Optional[int] = None
    preserved_suffix_start_turn: Optional[int] = None


class ConversationCompactor(ABC):
    compactor_type = "base"

    def __init__(
        self,
        config: ConversationCompactionConfig,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.config = config
        self.logger = logger or logging.getLogger(__name__)

    @abstractmethod
    def build_prompt_context(
        self,
        *,
        store: ConversationStore,
        conversation_id: str,
        latest_request_text: str,
        exclude_task_id: Optional[str] = None,
    ) -> str:
        pass

    @abstractmethod
    def compact_if_needed(
        self,
        *,
        store: ConversationStore,
        conversation_id: str,
        latest_request_text: str = "",
        exclude_task_id: Optional[str] = None,
    ) -> CompactionResult:
        pass
