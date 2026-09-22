"""
The async-encoder / worker-pool infrastructure has been removed
(AsyncEncoder, LlamaAsyncEncoder, NonWorkerAsyncEncoder,
autodetectTemplateType, IS_BINARY branch, llamaTokenizer fallback).
Core token counting and message compilation logic are preserved.
"""

import math
import json
import asyncio
import tiktoken
from typing import Any, Dict, List, Optional, Union
from functools import lru_cache


# Importing a bunch of tokenizers can be very resource intensive (MB-scale per tokenizer)
# Using token counting APIs (e.g. for anthropic) can be complicated and unreliable in many environments
# So for now we will just use super fast gpt-tokenizer and apply safety buffers
# I'm using rough estimates from this article to apply safety buffers to common tokenizers
# which will have HIGHER token counts than gpt. Roughly using token ratio from article + 10%
# https://medium.com/@disparate-ai/not-all-tokens-are-created-equal-7347d549af4d

ANTHROPIC_TOKEN_MULTIPLIER = 1.23
GEMINI_TOKEN_MULTIPLIER = 1.18
MISTRAL_TOKEN_MULTIPLIER = 1.26


def _get_adjusted_token_count_from_model(base_tokens: int, model_name: str) -> int:
    """
    Adjusts token count based on model-specific tokenizer differences.
    Since we use llama tokenizer (~= gpt tokenizer) for all models, we apply
    multipliers for models known to have higher token counts.

    :param base_tokens: Token count from llama/gpt tokenizer
    :param model_name: Name of the model
    :returns: Adjusted token count with safety buffer
    """
    multiplier = 1.0
    lower_model_name = (model_name or "").lower()

    if "claude" in lower_model_name:
        multiplier = ANTHROPIC_TOKEN_MULTIPLIER
    elif "gemini" in lower_model_name:
        multiplier = GEMINI_TOKEN_MULTIPLIER
    elif "stral" in lower_model_name or "mixtral" in lower_model_name:
        # Mistral family models: mistral, mixtral, codestral, devstral, etc
        multiplier = MISTRAL_TOKEN_MULTIPLIER

    return math.ceil(base_tokens * multiplier)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

MessageContent = Union[str, List[Dict[str, Any]]]
ChatMessage = Dict[str, Any]


DEFAULT_PRUNING_LENGTH = 128_000


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

_gpt_encoding = None


def _encoding_for_model(model_name: str):
    """Return a tiktoken encoding.

    The original used autodetectTemplateType() to pick between tiktoken and
    llamaTokenizer. That branch was part of the removed infra, so we use
    gpt-4's encoding for every model.
    """
    global _gpt_encoding
    if _gpt_encoding is None:
        _gpt_encoding = tiktoken.encoding_for_model("gpt-4")
    return _gpt_encoding


def _count_image_tokens(content: Dict[str, Any]) -> int:
    if content.get("type") == "imageUrl":
        return 1024
    raise ValueError("Non-image content type")


# ---------------------------------------------------------------------------
# Core counting
# ---------------------------------------------------------------------------

def count_tokens(content: MessageContent, model_name: str = "llama2") -> int:
    encoding = _encoding_for_model(model_name)

    if isinstance(content, list):
        base_tokens = 0
        for part in content:
            if part.get("type") == "text":
                base_tokens += len(encoding.encode(
                        part.get("text") or "",
                        disallowed_special=(),
                    )
                )
            else:
                try:
                    base_tokens += _count_image_tokens(part)
                except (KeyError, TypeError):
                    # Unknown part type — skip rather than crash.
                    pass
    else:   
        base_tokens = len(encoding.encode(content or "", disallowed_special=()))

    return _get_adjusted_token_count_from_model(base_tokens, model_name)


async def count_tokens_async__(content: MessageContent, model_name: str = "llama2") -> int:
    
    # Delegate to the sync implementation on a worker thread so the event
    # loop is not blocked by tiktoken's CPU-bound encode. This guarantees
    # parity with count_tokens() — including the model-specific multiplier
    # applied by _get_adjusted_token_count_from_model.
    return await asyncio.to_thread(count_tokens, content, model_name)
    
@lru_cache(maxsize=32768)
def _count_cached(text: str, model_name: str) -> int:
    if model_name == "llama2":
        # Indexer path: no multiplier, straight cl100k count.
        return len(_encoding_for_model(model_name).encode(text, disallowed_special=()))
    return count_tokens(text, model_name)

async def count_tokens_async(content: MessageContent, model_name: str = "llama2") -> int:
    if isinstance(content, str):
        return _count_cached(content, model_name)
    # List content (chat messages with parts) — rare in the indexer path.
    return await asyncio.to_thread(count_tokens, content, model_name)    
    
    
    
    
# async def count_tokens_async(content: MessageContent, model_name: str = "llama2") -> int:
#     # Original used a worker pool for parallelism. In Python we just
#     # delegate to the encoding directly.
#     encoding = _encoding_for_model(model_name)

#     if isinstance(content, list):
#         total = 0
#         for part in content:
#             if part.get("type") == "imageUrl":
#                 total += _count_image_tokens(part)
#             else:
#                 total += len(encoding.encode(part.get("text") or "", disallowed_special=()))
#         return total
#     return len(encoding.encode(content or "", disallowed_special=()))


# https://community.openai.com/t/how-to-calculate-the-tokens-when-using-function-call/266573/10
def count_tools_tokens(tools: List[Dict[str, Any]], model_name: str) -> int:
    encoding = _encoding_for_model(model_name)

    def _count(value: str) -> int:
        return len(encoding.encode(value, disallowed_special=()))

    num_tokens = 12

    for tool in tools:
        function = tool.get("function", {}) or {}
        function_tokens = _count(function.get("name", "") or "")

        if function.get("description"):
            function_tokens += _count(function["description"])

        props = (function.get("parameters") or {}).get("properties")
        if props:
            for key in props:
                function_tokens += _count(key)
                fields = props[key]
                if fields:
                    field_type = fields.get("type")
                    field_desc = fields.get("description")
                    field_enum = fields.get("enum")

                    if field_type and isinstance(field_type, str):
                        function_tokens += 2
                        function_tokens += _count(field_type)

                    if field_desc and isinstance(field_desc, str):
                        function_tokens += 2
                        function_tokens += _count(field_desc)

                    if field_enum and isinstance(field_enum, list):
                        function_tokens -= 3
                        for e in field_enum:
                            function_tokens += 3
                            function_tokens += _count(e) if isinstance(e, str) else 5

        num_tokens += function_tokens

    return num_tokens + 12


def count_chat_message_tokens(model_name: str, chat_message: ChatMessage) -> int:
    # Simpler, safer version of:
    # https://github.com/openai/openai-cookbook/blob/main/examples/How_to_count_tokens_with_tiktoken.ipynb
    BASE_TOKENS = 4
    TOOL_CALL_EXTRA_TOKENS = 10
    TOOL_OUTPUT_EXTRA_TOKENS = 10

    tokens = BASE_TOKENS

    content = chat_message.get("content")
    if content:
        tokens += count_tokens(content, model_name)

    if chat_message.get("toolCalls"):
        for call in chat_message["toolCalls"]:
            tokens += TOOL_CALL_EXTRA_TOKENS
            tokens += count_tokens(json.dumps(call), model_name)

    role = chat_message.get("role")
    if role == "thinking":
        if chat_message.get("redactedThinking"):
            tokens += count_tokens(chat_message["redactedThinking"], model_name)
        if chat_message.get("signature"):
            tokens += count_tokens(chat_message["signature"], model_name)

    if role == "tool":
        tokens += TOOL_OUTPUT_EXTRA_TOKENS
        if chat_message.get("toolCallId"):
            tokens += count_tokens(chat_message["toolCallId"], model_name)

    return tokens


# ---------------------------------------------------------------------------
# Message helpers (originally imported from messages.js)
# ---------------------------------------------------------------------------

def _message_has_tool_call_id(assistant_msg: ChatMessage, tool_call_id: Optional[str]) -> bool:
    for call in assistant_msg.get("toolCalls") or []:
        if call.get("id") == tool_call_id:
            return True
    return False


def _chat_message_is_empty(msg: ChatMessage) -> bool:
    content = msg.get("content")
    if content is None:
        return True
    if isinstance(content, str):
        return len(content.strip()) == 0
    if isinstance(content, list):
        if not content:
            return True
        for part in content:
            if part.get("type") == "text":
                if (part.get("text") or "").strip():
                    return False
            else:
                return False
        return True
    return False


def _add_space_to_any_empty_messages(msgs: List[ChatMessage]) -> List[ChatMessage]:
    out = []
    for msg in msgs:
        if msg.get("content") == "":
            msg = {**msg, "content": " "}
        out.append(msg)
    return out


def _render_chat_message(msg: ChatMessage) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            (part.get("text") or "")
            for part in content
            if part.get("type") == "text"
        )
    return ""


# ---------------------------------------------------------------------------
# Tool sequence extraction
# ---------------------------------------------------------------------------

def extract_tool_sequence(messages: List[ChatMessage]) -> List[ChatMessage]:
    """Extracts and validates the tool call sequence from the end of a
    message array.

    Tool sequences consist of:
      [assistant_with_tool_calls, tool_response_1, tool_response_2, ...]
    or just a single user message.
    """
    if not messages:
        raise ValueError("Error parsing chat history: no user/tool message found")

    last_msg = messages.pop()
    tool_sequence: List[ChatMessage] = []

    if last_msg.get("role") == "tool":
        tool_sequence.append(last_msg)

        # Collect all consecutive tool messages from the end
        while messages and messages[-1].get("role") == "tool":
            tool_sequence.insert(0, messages.pop())

        # Get the assistant message with tool calls
        if messages:
            assistant_msg = messages.pop()
            tool_sequence.insert(0, assistant_msg)

            # Validate every tool message has a matching tool call ID
            for tool_msg in tool_sequence[1:]:
                if tool_msg.get("role") == "tool" and not _message_has_tool_call_id(
                    assistant_msg, tool_msg.get("toolCallId")
                ):
                    raise ValueError(
                        f'Error parsing chat history: no tool call found to match '
                        f'tool output for id "{tool_msg.get("toolCallId")}"'
                    )

    elif last_msg.get("role") in ("assistant", "thinking"):
        tool_sequence.append(last_msg)
        while messages and messages[-1].get("role") in ("thinking", "assistant"):
            tool_sequence.insert(0, messages.pop())

    else:
        # Single user message
        tool_sequence.append(last_msg)

    return tool_sequence


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def prune_lines_from_top(prompt: str, max_tokens: int, model_name: str) -> str:
    lines = prompt.split("\n")
    line_tokens = [count_tokens(line, model_name) for line in lines]
    total_tokens = sum(line_tokens)
    start = 0
    current_lines = len(lines)

    total_tokens += max(0, current_lines - 1)

    while total_tokens > max_tokens and start < current_lines:
        total_tokens -= line_tokens[start]
        if current_lines - start > 1:
            total_tokens -= 1
        start += 1

    return "\n".join(lines[start:])


def prune_lines_from_bottom(prompt: str, max_tokens: int, model_name: str) -> str:
    lines = prompt.split("\n")
    line_tokens = [count_tokens(line, model_name) for line in lines]
    total_tokens = sum(line_tokens)
    end = len(lines)

    total_tokens += max(0, end - 1)

    while total_tokens > max_tokens and end > 0:
        end -= 1
        total_tokens -= line_tokens[end]
        if end > 0:
            total_tokens -= 1

    return "\n".join(lines[:end])


def prune_string_from_bottom(model_name: str, max_tokens: int, prompt: str) -> str:
    encoding = _encoding_for_model(model_name)
    tokens = encoding.encode(prompt, disallowed_special=())
    if len(tokens) <= max_tokens:
        return prompt
    return encoding.decode(tokens[:max_tokens])


def prune_string_from_top(model_name: str, max_tokens: int, prompt: str) -> str:
    encoding = _encoding_for_model(model_name)
    tokens = encoding.encode(prompt, disallowed_special=())
    if len(tokens) <= max_tokens:
        return prompt
    return encoding.decode(tokens[len(tokens) - max_tokens:])


MAX_TOKEN_SAFETY_BUFFER = 1000
TOKEN_SAFETY_PROPORTION = 0.02


def get_token_counting_buffer_safety(context_length: int) -> int:
    return int(min(MAX_TOKEN_SAFETY_BUFFER, context_length * TOKEN_SAFETY_PROPORTION))


MIN_RESPONSE_TOKENS = 1000


def prune_raw_prompt_from_top(
    model_name: str,
    context_length: int,
    prompt: str,
    tokens_for_completion: int,
) -> str:
    max_tokens = (
        context_length
        - tokens_for_completion
        - get_token_counting_buffer_safety(context_length)
    )
    return prune_string_from_top(model_name, max_tokens, prompt)


# ---------------------------------------------------------------------------
# Chat message compilation
# ---------------------------------------------------------------------------

def compile_chat_messages(
    model_name: str,
    msgs: List[ChatMessage],
    known_context_length: Optional[int],
    max_tokens: int,
    supports_images: bool,
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Reconciles chat messages with available context length by pruning older
    messages while preserving critical conversation elements.
    """
    did_prune = False

    msgs_copy: List[ChatMessage] = [dict(m) for m in msgs]

    # If images aren't supported, collapse MessagePart[] to a string
    if not supports_images:
        for msg in msgs_copy:
            if "content" in msg and isinstance(msg["content"], list):
                msg["content"] = _render_chat_message(msg)

    # Extract system message
    system_msg = next((m for m in msgs_copy if m.get("role") == "system"), None)
    msgs_copy = [m for m in msgs_copy if m.get("role") != "system"]

    # Remove empty messages
    msgs_copy = [m for m in msgs_copy if not _chat_message_is_empty(m)]
    msgs_copy = _add_space_to_any_empty_messages(msgs_copy)

    # Extract the tool sequence from the end
    tool_sequence = extract_tool_sequence(msgs_copy)

    last_messages_tokens = sum(
        count_chat_message_tokens(model_name, m) for m in tool_sequence
    )

    system_msg_tokens = (
        count_chat_message_tokens(model_name, system_msg) if system_msg else 0
    )

    tool_tokens = count_tools_tokens(tools, model_name) if tools else 0

    context_length = (
        known_context_length
        if known_context_length is not None
        else DEFAULT_PRUNING_LENGTH
    )
    counting_safety_buffer = get_token_counting_buffer_safety(context_length)
    min_output_tokens = min(MIN_RESPONSE_TOKENS, max_tokens)

    input_tokens_available = context_length
    input_tokens_available -= counting_safety_buffer
    input_tokens_available -= min_output_tokens
    input_tokens_available -= tool_tokens
    input_tokens_available -= system_msg_tokens
    input_tokens_available -= last_messages_tokens

    if known_context_length is not None and input_tokens_available < 0:
        raise ValueError(
            "Not enough context available to include the system message, last "
            "user message, and tools.\n"
            f"  There must be at least {min_output_tokens} tokens remaining for output.\n"
            "  Request had the following token counts:\n"
            f"  - contextLength: {known_context_length}\n"
            f"  - counting safety buffer: {counting_safety_buffer}\n"
            f"  - tools: ~{tool_tokens}\n"
            f"  - system message: ~{system_msg_tokens}\n"
            f"  - max output tokens: {max_tokens}"
        )

    # Remove messages until we're under the limit
    current_total = 0
    history_with_tokens: List[Dict[str, Any]] = []
    for message in msgs_copy:
        tokens = count_chat_message_tokens(model_name, message)
        current_total += tokens
        history_with_tokens.append({**message, "tokens": tokens})

    while history_with_tokens and current_total > input_tokens_available:
        message = history_with_tokens.pop(0)
        current_total -= message["tokens"]
        did_prune = True

        # Make sure no latent tool response without corresponding call
        while history_with_tokens and history_with_tokens[0].get("role") == "tool":
            message = history_with_tokens.pop(0)
            current_total -= message["tokens"]

    # Reassemble
    reassembled: List[ChatMessage] = []
    if system_msg:
        reassembled.append(system_msg)
    for item in history_with_tokens:
        reassembled.append({k: v for k, v in item.items() if k != "tokens"})
    reassembled.extend(tool_sequence)

    input_tokens = (
        current_total + system_msg_tokens + tool_tokens + last_messages_tokens
    )
    available_tokens = context_length - counting_safety_buffer - min_output_tokens
    context_percentage = input_tokens / available_tokens

    return {
        "compiled_chat_messages": reassembled,
        "did_prune": did_prune,
        "context_percentage": context_percentage,
    }