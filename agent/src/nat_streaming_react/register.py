# SPDX-FileCopyrightText: Copyright (c) 2025-2026, NVIDIA CORPORATION & AFFILIATES.
# SPDX-License-Identifier: Apache-2.0
"""ReAct agent workflow that preserves native-tool final-answer streaming.

NAT 1.8's built-in ReAct stream buffers output until it sees the textual
``Final Answer:`` marker. Native tool calling returns a normal assistant
message instead, so the built-in fallback emits the complete answer as one
chunk. This local component keeps NAT's ReAct graph and MCP tooling but streams
content chunks immediately when native tool calling is enabled. NAT owns the
canonical request trace; Guardrails joins it through the runner context bridge.
"""

import logging
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from nat.builder.builder import Builder
from nat.builder.framework_enum import LLMFrameworkEnum
from nat.builder.function_info import FunctionInfo
from nat.cli.register_workflow import register_function
from nat.data_models.api_server import ChatRequest
from nat.data_models.api_server import ChatRequestOrMessage
from nat.data_models.api_server import ChatResponse
from nat.data_models.api_server import ChatResponseChunk
from nat.data_models.api_server import Usage
from nat.plugins.langchain.agent.react_agent.register import ReActAgentWorkflowConfig
from nat.utils.io.model_processing import remove_r1_think_tags
from nat.utils.type_converter import GlobalTypeConverter

# Import the middleware registration for its NAT component side effect.
from nat_streaming_react.otel_setup import configure_opentelemetry
from nat_streaming_react.text_guardrails import text_guardrails_middleware as _text_guardrails_middleware

configure_opentelemetry()
logger = logging.getLogger(__name__)


class StreamingReActAgentWorkflowConfig(
    ReActAgentWorkflowConfig,
    name="streaming_react_agent",
):
    """NAT ReAct configuration with native final-answer token streaming."""


def _content_to_text(content: Any) -> str:
    """Normalize LangChain string or multimodal content into text."""

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return "" if content is None else str(content)


@register_function(
    config_type=StreamingReActAgentWorkflowConfig,
    framework_wrappers=[LLMFrameworkEnum.LANGCHAIN],
)
async def streaming_react_agent_workflow(
    config: StreamingReActAgentWorkflowConfig,
    builder: Builder,
):
    """Build NAT's ReAct graph while fixing native-tool output streaming."""

    from langchain_core.messages import AIMessageChunk
    from langchain_core.messages import BaseMessage
    from langchain_core.messages import trim_messages
    from langgraph.errors import GraphRecursionError
    from langgraph.graph.state import CompiledStateGraph

    from nat.plugins.langchain.agent.base import AGENT_LOG_PREFIX
    from nat.plugins.langchain.agent.react_agent.agent import ReActAgentGraph
    from nat.plugins.langchain.agent.react_agent.agent import ReActGraphState
    from nat.plugins.langchain.agent.react_agent.agent import create_react_agent_prompt
    from nat.plugins.langchain.agent.react_agent.output_parser import FINAL_ANSWER_PATTERN

    prompt = create_react_agent_prompt(config)
    llm = await builder.get_llm(
        config.llm_name,
        wrapper_type=LLMFrameworkEnum.LANGCHAIN,
    )
    tools = await builder.get_tools(
        tool_names=config.tool_names,
        wrapper_type=LLMFrameworkEnum.LANGCHAIN,
    )
    if not tools:
        raise ValueError(f"No tools specified for ReAct Agent '{config.llm_name}'")

    graph: CompiledStateGraph = await ReActAgentGraph(
        llm=llm,
        prompt=prompt,
        tools=tools,
        use_tool_schema=config.include_tool_input_schema_in_tool_description,
        detailed_logs=config.verbose,
        log_response_max_chars=config.log_response_max_chars,
        retry_agent_response_parsing_errors=config.retry_agent_response_parsing_errors,
        parse_agent_response_max_retries=config.parse_agent_response_max_retries,
        tool_call_max_retries=config.tool_call_max_retries,
        pass_tool_call_errors_to_agent=config.pass_tool_call_errors_to_agent,
        normalize_tool_input_quotes=config.normalize_tool_input_quotes,
        raise_on_parsing_failure=config.raise_on_parsing_failure,
        use_native_tool_calling=config.use_native_tool_calling,
    ).build_graph()

    def _messages(chat_request_or_message: ChatRequestOrMessage) -> tuple[ChatRequest, list[BaseMessage]]:
        request = GlobalTypeConverter.get().convert(
            chat_request_or_message,
            to_type=ChatRequest,
        )
        messages: list[BaseMessage] = trim_messages(
            messages=[message.model_dump() for message in request.messages],
            max_tokens=config.max_history,
            strategy="last",
            token_counter=len,
            start_on="human",
            include_system=True,
        )
        return request, messages

    async def _response_fn(
        chat_request_or_message: ChatRequestOrMessage,
    ) -> ChatResponse | str:
        try:
            request, messages = _messages(chat_request_or_message)
            state = ReActGraphState(messages=messages)
            result = await graph.ainvoke(
                state,
                config={"recursion_limit": (config.max_tool_calls + 1) * 2},
            )
            final_state = ReActGraphState(**result)
            content = _content_to_text(final_state.messages[-1].content)

            prompt_tokens = sum(
                len(_content_to_text(message.content).split())
                for message in request.messages
            )
            completion_tokens = len(content.split()) if content else 0
            usage = Usage(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            )
            response = ChatResponse.from_string(content, usage=usage)
            if chat_request_or_message.is_string:
                return GlobalTypeConverter.get().convert(response, to_type=str)
            return response
        except Exception as error:
            logger.error(
                "%s Streaming ReAct Agent failed with exception: %s",
                AGENT_LOG_PREFIX,
                error,
            )
            raise

    async def _stream_fn(
        chat_request_or_message: ChatRequestOrMessage,
    ) -> AsyncGenerator[ChatResponseChunk]:
        chunk_id = str(uuid.uuid4())
        try:
            request, messages = _messages(chat_request_or_message)
            state = ReActGraphState(messages=messages)

            # Native tool calling already separates tool calls from assistant
            # content structurally. There is no need to wait for the textual
            # "Final Answer:" marker; doing so is what collapsed the whole
            # final answer into one fallback chunk in NAT 1.8.
            if config.use_native_tool_calling:
                async for message, metadata in graph.astream(
                    state,
                    config={"recursion_limit": (config.max_tool_calls + 1) * 2},
                    stream_mode="messages",
                ):
                    if not isinstance(message, AIMessageChunk):
                        continue
                    if not isinstance(metadata, dict):
                        continue
                    if metadata.get("langgraph_node") != "agent":
                        continue
                    if message.tool_call_chunks:
                        # Tool calls are shown by NAT intermediate TOOL events.
                        continue

                    text = _content_to_text(message.content)
                    if text:
                        yield ChatResponseChunk.create_streaming_chunk(
                            text,
                            id_=chunk_id,
                        )
                return

            # Preserve NAT's textual-ReAct behavior for models that do not use
            # native function calling.
            buffer = ""
            found_final_answer = False
            async for message, metadata in graph.astream(
                state,
                config={"recursion_limit": (config.max_tool_calls + 1) * 2},
                stream_mode="messages",
            ):
                if not isinstance(message, AIMessageChunk):
                    continue
                if not isinstance(metadata, dict):
                    continue
                if metadata.get("langgraph_node") != "agent":
                    continue
                if message.tool_call_chunks:
                    continue

                text = _content_to_text(message.content)
                if not text:
                    continue

                if found_final_answer:
                    yield ChatResponseChunk.create_streaming_chunk(text, id_=chunk_id)
                    continue

                buffer += text
                cleaned = remove_r1_think_tags(buffer)
                match = FINAL_ANSWER_PATTERN.search(cleaned)
                if match:
                    found_final_answer = True
                    after_marker = cleaned[match.end():]
                    if after_marker:
                        yield ChatResponseChunk.create_streaming_chunk(
                            after_marker,
                            id_=chunk_id,
                        )
                    buffer = ""

            if not found_final_answer and buffer:
                fallback_answer = remove_r1_think_tags(buffer)
                yield ChatResponseChunk.create_streaming_chunk(
                    fallback_answer,
                    id_=chunk_id,
                )


        except GraphRecursionError:
            logger.warning(
                "%s ReAct Agent reached its maximum iteration limit (%d)",
                AGENT_LOG_PREFIX,
                config.max_tool_calls,
            )
            recursion_message = (
                "The agent could not produce a final answer within "
                f"{config.max_tool_calls} tool calls."
            )
            yield ChatResponseChunk.create_streaming_chunk(
                recursion_message,
                id_=chunk_id,
            )
        except Exception as error:
            logger.error(
                "%s Streaming ReAct Agent streaming failed: %s",
                AGENT_LOG_PREFIX,
                error,
            )
            raise

    yield FunctionInfo.create(
        single_fn=_response_fn,
        stream_fn=_stream_fn,
        description=config.description,
    )
