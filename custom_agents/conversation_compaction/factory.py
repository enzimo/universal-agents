import logging

from default_values import CONVERSATION_COMPACTION_DEFAULTS
from custom_agents.conversation_compaction.artifact_offloader import (
    ConversationArtifactOffloader,
    RecursiveLargeTextArtifactOffloader,
)
from custom_agents.conversation_compaction.base import (
    ConversationCompactionConfig,
    ConversationCompactor,
)
from custom_agents.conversation_compaction.heuristic_compactor import HeuristicSlidingWindowCompactor
from custom_agents.conversation_compaction.llm_compactor import LLMSummarizingConversationCompactor
from custom_agents.conversation_compaction.noop_compactor import NoOpConversationCompactor


def build_conversation_compactor(
    compactor_type: str,
    *,
    config: ConversationCompactionConfig,
    logger: logging.Logger,
) -> ConversationCompactor:
    normalized = (compactor_type or CONVERSATION_COMPACTION_DEFAULTS.compactor_type.value).strip().lower()
    if normalized in {"heuristic", "heuristic_sliding_window"}:
        return HeuristicSlidingWindowCompactor(config=config, logger=logger)
    if normalized in {"llm", "llm_summarizing", "llm_backed"}:
        return LLMSummarizingConversationCompactor(config=config, logger=logger)
    if normalized in {"noop", "none"}:
        return NoOpConversationCompactor(config=config, logger=logger)
    raise ValueError(f"Unsupported conversation compactor type: {compactor_type}")


def build_conversation_artifact_offloader(
    logger: logging.Logger,
) -> ConversationArtifactOffloader:
    del logger
    return RecursiveLargeTextArtifactOffloader()
