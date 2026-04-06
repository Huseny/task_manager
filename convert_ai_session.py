#!/usr/bin/env python3
"""
AI Session 统一转换工具

支持自动识别并转换以下格式的 AI 对话历史为 OpenAI 标准格式:
- Claude JSONL (Claude Desktop/API session)
- Codex JSONL (Codex CLI session)
- Gemini JSON (Gemini CLI session)
- Kilocode JSON (Kilocode API conversation history)
- OpenCode JSON (OpenCode session)

输出格式:
- 符合 OPENAI_FORMAT_SPEC.md 规范
- 包含 messages 数组和 meta 元数据
- 支持 reasoning (推理内容)、tool_call (工具调用)、tool_output (工具结果) 等内容类型
- Token 统计信息统一存储在 meta.token_counts 数组中

特性:
- 自动检测输入文件格式
- 支持多种编码格式 (UTF-8, UTF-16, GBK 等)
- 保留完整的元数据和时间戳信息
- 完全独立运行，不依赖任何项目内其他脚本
- 支持批量转换指定目录下的所有会话文件

使用示例:
    # 单文件转换 - 自动检测格式
    python convert_ai_session.py -i session.json
    
    # 单文件转换 - 指定输出文件
    python convert_ai_session.py -i session.jsonl -o output.json
    
    # 单文件转换 - 强制指定格式
    python convert_ai_session.py -i session.jsonl --format claude
    
    # 批量转换 - 转换指定目录下所有会话文件(仅处理一层目录) 固定输出到convert目录下 
    python convert_ai_session.py -d script/session/test

    # 批量转换 - 转换当前目录下所有会话文件(仅处理一层目录) 固定输出到convert目录下 
    python convert_ai_session.py -d .

    
    
    # 批量转换 - 指定文件匹配模式
    python convert_ai_session.py -d script/session/test --pattern "*.json" --exclude "*_converted.json"

批量转换说明:
    - 仅扫描指定目录下的所有 .json 和 .jsonl 文件(不递归子目录)
    - 默认跳过已转换的文件 (*_converted.json)
    - 转换后的文件命名为: 原文件名_converted.json
    - 可通过 --pattern 和 --exclude 参数自定义文件过滤规则
    - 转换失败的文件会记录错误信息并继续处理其他文件
    - 转换完成后输出统计信息: 成功数/失败数/跳过数

作者: liufei
版本: 1.3.0
更新日期: 2026-03-18
"""
from __future__ import annotations

import json
import sys
import argparse
import re
from pathlib import Path
from typing import Dict, Any, List, Optional, TextIO
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ============================================================================
# 格式检测
# ============================================================================

def detect_format(file_path: Path) -> str:
    """
    自动检测文件格式
    
    返回: 'claude_jsonl' | 'codex_jsonl' | 'kilocode' | 'opencode' | 'gemini' | 'unknown'
    """
    # JSONL 格式检测
    if file_path.suffix == '.jsonl':
        return detect_jsonl_format(file_path)
    
    # JSON 格式检测
    data = None
    for encoding in ['utf-8-sig', 'utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'gbk', 'gb2312']:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                data = json.load(f)
            break
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
    
    if data is None:
        return 'unknown'
    
    # Gemini 格式: {"sessionId": "...", "messages": [...], "startTime": "..."}
    if isinstance(data, dict) and 'sessionId' in data and 'messages' in data:
        messages = data.get('messages', [])
        if isinstance(messages, list) and len(messages) > 0:
            first_msg = messages[0]
            if isinstance(first_msg, dict) and 'type' in first_msg and first_msg.get('type') in ('user', 'gemini'):
                return 'gemini'
    
    # OpenCode 格式: {"info": {...}, "messages": [...]}
    if isinstance(data, dict) and 'info' in data and 'messages' in data:
        info = data.get('info', {})
        if isinstance(info, dict) and 'id' in info:
            return 'opencode'
    
    # Kilocode 格式: [{"role": "user", "content": [...], "ts": 123}]
    if isinstance(data, list) and len(data) > 0:
        first_item = data[0]
        if isinstance(first_item, dict) and 'role' in first_item and 'content' in first_item and 'ts' in first_item:
            content = first_item.get('content', [])
            if isinstance(content, list) and len(content) > 0:
                if isinstance(content[0], dict) and 'type' in content[0]:
                    return 'kilocode'
    
    return 'unknown'


def detect_jsonl_format(file_path: Path) -> str:
    """
    检测 JSONL 文件的具体格式
    
    返回: 'codex_jsonl' | 'claude_jsonl' | 'unknown'
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = []
            for i, line in enumerate(f):
                if i >= 10:
                    break
                line = line.strip()
                if line:
                    lines.append(line)
            
            if not lines:
                return 'unknown'
            
            first_obj = json.loads(lines[0])
            
            # Claude 格式特征
            if 'sessionId' in first_obj:
                return 'claude_jsonl'
            
            event_type = first_obj.get('type')
            if event_type in ('user', 'assistant', 'progress', 'file-history-snapshot', 'system'):
                if 'message' in first_obj or 'parentUuid' in first_obj or 'isSidechain' in first_obj:
                    return 'claude_jsonl'
            
            # Codex 格式特征
            if 'payload' in first_obj:
                return 'codex_jsonl'
            
            if event_type in ('session_meta', 'turn_context', 'event_msg', 'response_item'):
                return 'codex_jsonl'
            
            # 检查更多行
            claude_indicators = 0
            codex_indicators = 0
            
            for line in lines[1:]:
                try:
                    obj = json.loads(line)
                    if any(k in obj for k in ('sessionId', 'parentUuid', 'isSidechain', 'userType')):
                        claude_indicators += 1
                    if 'payload' in obj or obj.get('type') in ('session_meta', 'turn_context'):
                        codex_indicators += 1
                except json.JSONDecodeError:
                    continue
            
            if claude_indicators > codex_indicators:
                return 'claude_jsonl'
            elif codex_indicators > claude_indicators:
                return 'codex_jsonl'
            
            return 'codex_jsonl'
    
    except Exception as e:
        print(f"警告: 检测 JSONL 格式时出错: {str(e)}")
        return 'unknown'


# ============================================================================
# Claude JSONL 转换器 (原 claude_jsonl_to_openai_messages.py)
# ============================================================================

def _claude_read_jsonl(stream: TextIO) -> Iterable[dict]:
    """读取 JSONL 文件，每行一个 JSON 对象"""
    for line_no, line in enumerate(stream, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON at line {line_no}") from exc
        if not isinstance(obj, dict):
            raise ValueError(f"Expected object at line {line_no}, got {type(obj).__name__}")
        yield obj


@dataclass
class ClaudeConverterOptions:
    """Claude 转换器选项配置"""
    include_thinking: bool = True
    include_toolcall_content: bool = True
    include_token_count: bool = True
    messages_only: bool = False


@dataclass
class ClaudeConverterState:
    """Claude 转换器状态"""
    session_id: str | None = None
    token_counts: list = field(default_factory=list)
    session_meta: dict = field(default_factory=dict)
    skipped_events: list = field(default_factory=list)


def convert_claude_jsonl_to_messages(
    events: Iterable[dict],
    *,
    options: ClaudeConverterOptions,
) -> dict:
    """
    将 Claude session JSONL 转换为 OpenAI 消息格式

    Args:
        events: JSONL 事件迭代器
        options: 转换选项

    Returns:
        包含 messages 和 meta 的字典
    """
    state = ClaudeConverterState()
    messages: list = []

    for obj in events:
        event_type = obj.get("type")
        timestamp = obj.get("timestamp")

        # 提取 session 元数据
        if event_type == "user" and state.session_id is None:
            state.session_id = obj.get("sessionId")
            state.session_meta = {
                "session_id": obj.get("sessionId"),
                "version": obj.get("version"),
                "git_branch": obj.get("gitBranch"),
                "cwd": obj.get("cwd"),
            }

        # 处理用户消息
        if event_type == "user":
            message = obj.get("message", {})
            role = message.get("role")
            content = message.get("content")

            if role == "user" and isinstance(content, str):
                user_msg = {
                    "role": "user",
                    "content": [{"type": "text", "text": content}],
                }
                if timestamp:
                    user_msg["_metadata"] = {"timestamp": timestamp}
                messages.append(user_msg)
            elif role == "user" and isinstance(content, list):
                # 处理工具结果
                user_msg = {
                    "role": "user",
                    "content": []
                }
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "tool_result":
                            tool_msg = {
                                "role": "tool",
                                "tool_call_id": item.get("tool_use_id", ""),
                                "content": [{"type": "tool_output", "text": item.get("content", "")}]
                            }
                            if timestamp:
                                tool_msg["_metadata"] = {"timestamp": timestamp}
                            messages.append(tool_msg)
                        else:
                            user_msg["content"].append(item)

                # 如果有非工具结果的内容，添加用户消息
                if user_msg["content"]:
                    if timestamp:
                        user_msg["_metadata"] = {"timestamp": timestamp}
                    messages.append(user_msg)

        # 处理助手消息
        elif event_type == "assistant":
            message = obj.get("message", {})
            role = message.get("role")
            content = message.get("content")
            usage = message.get("usage")

            if role == "assistant" and isinstance(content, list):
                assistant_msg = {
                    "role": "assistant",
                    "content": [],
                }

                tool_calls = []

                for item in content:
                    if not isinstance(item, dict):
                        continue

                    item_type = item.get("type")

                    # 处理思考过程
                    if item_type == "thinking" and options.include_thinking:
                        thinking_text = item.get("thinking", "")
                        if thinking_text:
                            assistant_msg["content"].append({
                                "type": "reasoning",
                                "text": thinking_text
                            })

                    # 处理文本内容
                    elif item_type == "text":
                        text = item.get("text", "")
                        if text:
                            assistant_msg["content"].append({
                                "type": "text",
                                "text": text
                            })

                    # 处理工具调用
                    elif item_type == "tool_use":
                        tool_id = item.get("id", "")
                        tool_name = item.get("name", "")
                        tool_input = item.get("input", {})

                        tool_call = {
                            "id": tool_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": json.dumps(tool_input, ensure_ascii=False)
                            }
                        }
                        tool_calls.append(tool_call)

                        # 可选：在 content 中也包含工具调用信息
                        if options.include_toolcall_content:
                            assistant_msg["content"].append({
                                "type": "tool_call",
                                "tool_call_id": tool_id,
                                "name": tool_name,
                                "arguments": json.dumps(tool_input, ensure_ascii=False)
                            })

                # 添加工具调用字段
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls

                # 添加时间戳和元数据
                if timestamp:
                    assistant_msg["_metadata"] = {"timestamp": timestamp}

                # 只有当消息有内容或工具调用时才添加
                if assistant_msg["content"] or tool_calls:
                    messages.append(assistant_msg)

                # 收集 token 统计信息
                if usage and options.include_token_count:
                    token_entry = {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": usage.get("input_tokens", 0),
                                "cached_input_tokens": usage.get("cache_read_input_tokens", 0),
                                "output_tokens": usage.get("output_tokens", 0),
                                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                            },
                            "last_token_usage": {
                                "input_tokens": usage.get("input_tokens", 0),
                                "cached_input_tokens": usage.get("cache_read_input_tokens", 0),
                                "output_tokens": usage.get("output_tokens", 0),
                                "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                            }
                        },
                        "rate_limits": {
                            "primary": None,
                            "secondary": None,
                            "credits": None,
                            "plan_type": None
                        }
                    }
                    if timestamp:
                        token_entry["_timestamp"] = timestamp
                    state.token_counts.append(token_entry)

        # 记录其他类型的事件
        elif event_type in ("progress", "system", "file-history-snapshot"):
            if options.include_token_count:
                state.skipped_events.append({
                    "type": event_type,
                    "timestamp": timestamp,
                    "data": obj.get("data") or obj.get("subtype")
                })

    # 构建结果
    result: dict = {"messages": messages}
    if not options.messages_only:
        result["meta"] = {
            "session_meta": state.session_meta,
            "token_counts": state.token_counts if options.include_token_count else None,
            "skipped_events_count": len(state.skipped_events),
            "skipped_events": state.skipped_events[:10] if state.skipped_events else []
        }

    return result


# ============================================================================
# Codex JSONL 转换器 (原 codex_jsonl_to_openai_messages.py)
# ============================================================================

def _codex_looks_like_agents_instructions(text: str) -> bool:
    """判断文本是否为 AGENTS.md 指令"""
    t = text.lstrip()
    return t.startswith("# AGENTS.md instructions") or ("## Skills" in t and "<INSTRUCTIONS>" in t)


def _codex_looks_like_environment_context(text: str) -> bool:
    """判断文本是否为环境上下文"""
    t = text.lstrip()
    return t.startswith("<environment_context>") and "</environment_context>" in t


def _codex_as_text_parts(content: Any) -> list:
    """将 content 转换为文本 parts 列表"""
    if not isinstance(content, list):
        return []
    out = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if "text" in part and isinstance(part["text"], str):
            out.append({"type": "text", "text": part["text"]})
            continue
        out.append(part)
    return out


def _codex_concat_text(content: Any) -> str:
    """拼接 content 中所有文本"""
    if not isinstance(content, list):
        return ""
    chunks = []
    for part in content:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            chunks.append(part["text"])
    return "".join(chunks)


def _codex_maybe_parse_json_string(value: str) -> Any:
    """尝试将字符串解析为 JSON"""
    s = value.strip()
    if not s:
        return None
    if not (s.startswith("{") or s.startswith("[")):
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


@dataclass
class CodexConverterOptions:
    """Codex 转换器选项配置"""
    promote_harness_messages: bool = True
    emit_session_instructions: bool = True
    include_toolcall_content: bool = True
    include_token_count: bool = True
    include_turn_context: bool = True
    messages_only: bool = False


@dataclass
class CodexConverterState:
    """Codex 转换器状态"""
    pending_reasoning: list = field(default_factory=list)
    last_reasoning: str | None = None
    session_instructions: str | None = None
    token_counts: list = field(default_factory=list)
    turn_contexts: list = field(default_factory=list)
    session_meta: dict | None = None

    def add_reasoning(self, text: str) -> None:
        """添加推理文本"""
        t = text.strip()
        if not t:
            return
        if self.last_reasoning == t:
            return
        self.pending_reasoning.append(t)
        self.last_reasoning = t

    def take_reasoning_parts(self) -> list:
        """取出并清空待处理推理内容"""
        if not self.pending_reasoning:
            return []
        parts = [{"type": "reasoning", "text": t} for t in self.pending_reasoning]
        self.pending_reasoning.clear()
        self.last_reasoning = None
        return parts


def convert_codex_jsonl_to_messages(
    events: Iterable[dict],
    *,
    options: CodexConverterOptions,
) -> dict:
    """将 Codex CLI session JSONL 转换为 OpenAI 消息格式"""
    state = CodexConverterState()
    messages: list = []

    for obj in events:
        timestamp = obj.get("timestamp")
        outer_type = obj.get("type")
        payload = obj.get("payload")

        # 处理 session_meta 事件
        if outer_type == "session_meta" and isinstance(payload, dict):
            state.session_meta = payload
            instr = payload.get("instructions")
            if isinstance(instr, str):
                state.session_instructions = instr
                if options.emit_session_instructions and instr.strip():
                    messages.append({
                        "role": "developer",
                        "content": [{"type": "text", "text": instr}],
                    })
            continue

        # 处理 turn_context 事件
        if outer_type == "turn_context" and isinstance(payload, dict):
            if options.include_turn_context:
                ctx = dict(payload)
                if timestamp:
                    ctx["_timestamp"] = timestamp
                state.turn_contexts.append(ctx)
            continue

        # 处理 event_msg 事件
        if outer_type == "event_msg" and isinstance(payload, dict):
            ptype = payload.get("type")
            if ptype == "agent_reasoning":
                text = payload.get("text")
                if isinstance(text, str):
                    state.add_reasoning(text)
                continue
            if ptype == "token_count":
                if options.include_token_count:
                    entry = dict(payload)
                    if timestamp:
                        entry["_timestamp"] = timestamp
                    state.token_counts.append(entry)
                continue
            continue

        if outer_type != "response_item" or not isinstance(payload, dict):
            continue

        ptype = payload.get("type")

        # 处理推理摘要
        if ptype == "reasoning":
            summary = payload.get("summary")
            if isinstance(summary, list):
                for item in summary:
                    if isinstance(item, dict) and isinstance(item.get("text"), str):
                        state.add_reasoning(item["text"])
            continue

        # 处理普通消息
        if ptype == "message":
            role = payload.get("role")
            content = payload.get("content")
            content_parts = _codex_as_text_parts(content)
            content_text = _codex_concat_text(content)

            if role == "assistant":
                assistant_msg = {"role": "assistant", "content": []}
                assistant_msg["content"].extend(state.take_reasoning_parts())
                assistant_msg["content"].extend(content_parts)
                if timestamp:
                    assistant_msg["_metadata"] = {"timestamp": timestamp}
                messages.append(assistant_msg)
                continue

            if role == "user":
                out_role = "user"
                if options.promote_harness_messages:
                    if _codex_looks_like_environment_context(content_text):
                        out_role = "system"
                    elif _codex_looks_like_agents_instructions(content_text):
                        out_role = "developer"

                # 避免重复 session 指令
                if (
                    out_role == "developer"
                    and state.session_instructions
                    and state.session_instructions.strip() == content_text.strip()
                    and options.emit_session_instructions
                ):
                    continue

                user_msg = {"role": out_role, "content": content_parts}
                if timestamp:
                    user_msg["_metadata"] = {"timestamp": timestamp}
                messages.append(user_msg)
                continue

            # 其他角色，原样保留
            if isinstance(role, str) and role:
                other_msg = {"role": role, "content": content_parts}
                if timestamp:
                    other_msg["_metadata"] = {"timestamp": timestamp}
                messages.append(other_msg)
            continue

        # 处理函数调用
        if ptype in ("function_call", "custom_tool_call"):
            call_id = payload.get("call_id")
            name = payload.get("name")
            if not isinstance(call_id, str) or not isinstance(name, str):
                continue

            if ptype == "function_call":
                arguments = payload.get("arguments")
                if isinstance(arguments, dict):
                    args_str = json.dumps(arguments, ensure_ascii=False)
                elif isinstance(arguments, str):
                    args_str = arguments
                else:
                    args_str = ""
            else:
                tool_input = payload.get("input")
                args_str = json.dumps({"input": tool_input}, ensure_ascii=False)

            tool_call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": args_str},
            }

            assistant_msg: dict = {
                "role": "assistant",
                "content": [],
                "tool_calls": [tool_call],
            }
            assistant_msg["content"].extend(state.take_reasoning_parts())
            if options.include_toolcall_content:
                assistant_msg["content"].append({
                    "type": "tool_call",
                    "tool_call_id": call_id,
                    "name": name,
                    "arguments": args_str,
                })
            if timestamp:
                assistant_msg["_metadata"] = {"timestamp": timestamp}
            messages.append(assistant_msg)
            continue

        # 处理函数调用输出
        if ptype in ("function_call_output", "custom_tool_call_output"):
            call_id = payload.get("call_id")
            output = payload.get("output")
            if not isinstance(call_id, str):
                continue
            if not isinstance(output, str):
                output = "" if output is None else str(output)

            tool_msg: dict = {"role": "tool", "tool_call_id": call_id, "content": []}
            parsed = _codex_maybe_parse_json_string(output)
            if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
                tool_msg["content"].append({"type": "tool_output", "text": parsed["output"]})
                if isinstance(parsed.get("metadata"), dict):
                    tool_msg["metadata"] = parsed["metadata"]
            else:
                tool_msg["content"].append({"type": "tool_output", "text": output})
            if timestamp:
                tool_msg["_metadata"] = {"timestamp": timestamp}
            messages.append(tool_msg)
            continue

    result: dict = {"messages": messages}
    if not options.messages_only:
        result["meta"] = {
            "session_meta": state.session_meta,
            "turn_contexts": state.turn_contexts,
            "token_counts": state.token_counts if options.include_token_count else None,
        }
    return result


# ============================================================================
# OpenCode JSON 转换器 (原 opencode_jsonl_to_openai_messages.py)
# ============================================================================

@dataclass
class OpenCodeConverterOptions:
    """OpenCode 转换器选项配置"""
    include_reasoning: bool = True          # 是否包含推理过程
    include_toolcall_content: bool = True   # 是否在 content 中包含工具调用
    include_token_count: bool = True        # 是否包含 token 统计
    include_session_info: bool = True       # 是否包含会话信息
    messages_only: bool = False             # 是否只输出 messages 数组
    include_timestamps: bool = True         # 是否在每条消息中包含时间戳
    include_full_tool_metadata: bool = True # 是否包含工具调用的完整元数据


@dataclass
class OpenCodeConverterState:
    """OpenCode 转换器状态"""
    session_info: dict | None = None
    token_counts: list = field(default_factory=list)

    def add_token_count(self, tokens: dict, timestamp: int | None = None) -> None:
        """添加 token 统计信息 (Codex 嵌套格式)"""
        input_tokens = tokens.get('input', 0)
        output_tokens = tokens.get('output', 0)
        reasoning_tokens = tokens.get('reasoning', 0)
        cache_read = tokens.get('cache', {}).get('read', 0) if isinstance(tokens.get('cache'), dict) else 0

        entry = {
            'type': 'token_count',
            'info': {
                'total_token_usage': {
                    'input_tokens': input_tokens,
                    'cached_input_tokens': cache_read,
                    'output_tokens': output_tokens,
                    'reasoning_output_tokens': reasoning_tokens,
                    'total_tokens': input_tokens + output_tokens
                },
                'last_token_usage': {
                    'input_tokens': input_tokens,
                    'cached_input_tokens': cache_read,
                    'output_tokens': output_tokens,
                    'reasoning_output_tokens': reasoning_tokens,
                    'total_tokens': input_tokens + output_tokens
                }
            },
            'rate_limits': {
                'primary': None,
                'secondary': None,
                'credits': None,
                'plan_type': None
            }
        }
        if timestamp:
            entry["_timestamp"] = timestamp
        self.token_counts.append(entry)


def _opencode_format_timestamp(timestamp_ms: int | None) -> str | None:
    """将毫秒时间戳转换为 ISO8601 格式"""
    if timestamp_ms is None:
        return None
    dt = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
    return dt.isoformat()


def _opencode_convert_tool_call(part: dict, include_full_metadata: bool = True):
    """
    将 OpenCode 的工具调用转换为 OpenAI 格式

    返回: (tool_call_dict, original_data_dict) 或 None
    """
    call_id = part.get("callID")
    tool_name = part.get("tool")
    state_obj = part.get("state", {})

    if not call_id or not tool_name:
        return None

    # 获取输入参数
    input_data = state_obj.get("input", {})
    if isinstance(input_data, dict):
        args_str = json.dumps(input_data, ensure_ascii=False)
    else:
        args_str = json.dumps({"input": input_data}, ensure_ascii=False)

    # 标准的 tool_call 格式
    tool_call = {
        "id": call_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": args_str
        }
    }

    # 原始数据，用于后续生成 metadata
    original_data = {
        "part_id": part.get("id"),
        "tool": tool_name,
        "state": state_obj
    } if include_full_metadata else {}

    return tool_call, original_data


def _opencode_convert_message(
    message: dict,
    options: OpenCodeConverterOptions,
    state: OpenCodeConverterState
) -> list:
    """将 OpenCode 的单个 message 转换为 OpenAI 格式的 messages 列表"""
    info = message.get("info", {})
    parts = message.get("parts", [])
    role = info.get("role")

    # 提取时间戳和 token 信息
    timestamp = info.get("time", {}).get("created")
    tokens = info.get("tokens")
    message_id = info.get("id")

    # 记录 token 统计
    if options.include_token_count and tokens:
        state.add_token_count(tokens, timestamp)

    # 用户消息
    if role == "user":
        content_parts = []
        for part in parts:
            if part.get("type") == "text":
                content_parts.append({
                    "type": "text",
                    "text": part.get("text", "")
                })

        if content_parts:
            user_msg = {
                "role": "user",
                "content": content_parts
            }
            if options.include_timestamps:
                user_msg["_metadata"] = {
                    "message_id": message_id,
                    "timestamp": timestamp,
                    "tokens": tokens
                }
            return [user_msg]
        return []

    # 助手消息
    if role == "assistant":
        result_messages = []

        text_parts = []
        tool_calls = []
        tool_outputs = []
        tool_call_original_data = {}

        for part in parts:
            part_type = part.get("type")

            # 推理内容
            if part_type == "reasoning":
                if options.include_reasoning:
                    reasoning_text = part.get("text", "")
                    if reasoning_text.strip():
                        text_parts.append({
                            "type": "reasoning",
                            "text": reasoning_text
                        })

            # 文本内容
            elif part_type == "text":
                text = part.get("text", "")
                if text.strip():
                    text_parts.append({
                        "type": "text",
                        "text": text
                    })

            # 工具调用
            elif part_type == "tool":
                state_obj = part.get("state", {})
                status = state_obj.get("status")

                # 工具调用请求
                if status in ("pending", "running", "completed"):
                    result = _opencode_convert_tool_call(part, options.include_full_tool_metadata)
                    if result:
                        tool_call, original_data = result
                        tool_calls.append(tool_call)

                        if original_data:
                            tool_call_original_data[tool_call["id"]] = original_data

                        if options.include_toolcall_content:
                            tool_call_content = {
                                "type": "tool_use",
                                "tool_call_id": tool_call["id"],
                                "name": tool_call["function"]["name"],
                                "arguments": tool_call["function"]["arguments"]
                            }
                            text_parts.append(tool_call_content)

                # 工具调用结果
                if status == "completed":
                    call_id = part.get("callID")
                    output = state_obj.get("output", "")

                    if call_id:
                        tool_outputs.append({
                            "call_id": call_id,
                            "output": output,
                            "state": state_obj
                        })

        # 构建助手消息
        if text_parts or tool_calls:
            assistant_msg: dict = {
                "role": "assistant",
                "content": text_parts
            }

            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls

            if options.include_timestamps:
                assistant_msg["_metadata"] = {
                    "message_id": message_id,
                    "timestamp": timestamp,
                    "tokens": tokens
                }

            result_messages.append(assistant_msg)

        # 添加工具输出消息
        for tool_output in tool_outputs:
            output_text = tool_output["output"]
            if not isinstance(output_text, str):
                output_text = str(output_text) if output_text is not None else ""

            tool_msg = {
                "role": "tool",
                "tool_call_id": tool_output["call_id"],
                "content": [{
                    "type": "tool_output",
                    "text": output_text
                }]
            }

            # 如果启用完整元数据，保留所有 metadata 信息
            if options.include_full_tool_metadata and "state" in tool_output:
                s = tool_output["state"]
                time_info = s.get("time", {})
                metadata_info = s.get("metadata", {})

                tool_msg["metadata"] = {}

                # 保留完整的 metadata (包括 diff, files, diagnostics 等)
                if isinstance(metadata_info, dict):
                    tool_msg["metadata"] = dict(metadata_info)
                    
                    # 如果 metadata 中有 exit 字段，也添加 exit_code 别名
                    if "exit" in metadata_info:
                        tool_msg["metadata"]["exit_code"] = metadata_info["exit"]

                # 添加 duration_seconds
                if time_info and "start" in time_info and "end" in time_info:
                    duration_ms = time_info["end"] - time_info["start"]
                    tool_msg["metadata"]["duration_seconds"] = round(duration_ms / 1000, 3)

            result_messages.append(tool_msg)

        return result_messages

    return []


def convert_opencode_to_messages(
    session_data: dict,
    *,
    options: OpenCodeConverterOptions
) -> dict:
    """
    将 OpenCode session 数据转换为 OpenAI messages 格式

    参数:
        session_data: OpenCode session JSON 数据
        options: 转换选项

    返回:
        包含 messages 和 meta 的字典
    """
    state = OpenCodeConverterState()
    messages: list = []

    # 提取会话信息
    session_info = session_data.get("info", {})

    # 处理所有消息
    opencode_messages = session_data.get("messages", [])
    for msg in opencode_messages:
        converted = _opencode_convert_message(msg, options, state)
        messages.extend(converted)

    # 构建结果
    result: dict = {"messages": messages}

    if not options.messages_only:
        # 构建 session_meta
        session_meta = {
            "id": session_info.get("id"),
            "timestamp": _opencode_format_timestamp(session_info.get("time", {}).get("created")),
            "cwd": session_info.get("directory"),
            "originator": "ide",
            "cli_version": session_info.get("version"),
            "source": "opencode",
            "model_provider": None,
            "base_instructions": {
                "text": None
            },
            "git": {}
        }

        # 从第一条助手消息中提取模型信息
        for msg in opencode_messages:
            info = msg.get("info", {})
            if info.get("role") == "assistant":
                model_info = info.get("model", {})
                session_meta["model_provider"] = model_info.get("providerID")
                break

        # 构建 turn_contexts
        turn_contexts = []
        for msg in opencode_messages:
            info = msg.get("info", {})
            if info.get("role") == "assistant":
                model_info = info.get("model", {})
                if not isinstance(model_info, dict):
                    model_info = {}
                path_info = info.get("path", {})
                if not isinstance(path_info, dict):
                    path_info = {}
                summary_info = info.get("summary", {})
                if not isinstance(summary_info, dict):
                    summary_info = {}
                time_info = info.get("time", {})
                if not isinstance(time_info, dict):
                    time_info = {}

                turn_context = {
                    "cwd": path_info.get("cwd"),
                    "approval_policy": "auto",
                    "sandbox_policy": {"type": "local"},
                    "model": model_info.get("modelID"),
                    "personality": info.get("agent"),
                    "collaboration_mode": {"mode": "single"},
                    "effort": info.get("mode"),
                    "summary": summary_info.get("title"),
                    "user_instructions": None,
                    "truncation_policy": {"mode": "auto", "limit": 100000},
                    "_timestamp": _opencode_format_timestamp(time_info.get("created"))
                }
                turn_contexts.append(turn_context)

        result["meta"] = {
            "session_meta": session_meta,
            "turn_contexts": turn_contexts,
            "token_counts": state.token_counts if options.include_token_count else None
        }

    return result


# ============================================================================
# Gemini JSON 转换器
# ============================================================================

def convert_gemini(file_path: Path) -> Dict[str, Any]:
    """转换 Gemini CLI JSON 格式"""
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    messages = []
    user_messages = 0
    assistant_messages = 0
    token_counts = []

    for msg in data.get('messages', []):
        msg_type = msg.get('type', '')
        msg_id = msg.get('id', '')
        timestamp = msg.get('timestamp', '')
        content_text = msg.get('content', '')

        # 确定角色
        if msg_type == 'user':
            role = 'user'
            user_messages += 1
        elif msg_type == 'gemini':
            role = 'assistant'
            assistant_messages += 1
        else:
            continue

        # 构建内容数组
        content_blocks = []

        # 处理 thoughts (推理内容)
        thoughts = msg.get('thoughts', [])
        if thoughts and role == 'assistant':
            reasoning_parts = []
            for thought in thoughts:
                subject = thought.get('subject', '')
                description = thought.get('description', '')
                if subject and description:
                    reasoning_parts.append(f"**{subject}**\n{description}")

            if reasoning_parts:
                content_blocks.append({
                    'type': 'reasoning',
                    'reasoning': '\n\n'.join(reasoning_parts)
                })

        # 处理主要内容
        if content_text:
            content_blocks.append({
                'type': 'text',
                'text': content_text
            })

        # 处理工具调用
        tool_calls_data = msg.get('toolCalls', [])
        tool_calls = []

        for tool_call in tool_calls_data:
            tool_id = tool_call.get('id', '')
            tool_name = tool_call.get('name', '')
            tool_args = tool_call.get('args', {})

            content_blocks.append({
                'type': 'tool_call',
                'tool_call_id': tool_id,
                'name': tool_name,
                'arguments': json.dumps(tool_args, ensure_ascii=False)
            })

            tool_calls.append({
                'id': tool_id,
                'type': 'function',
                'function': {
                    'name': tool_name,
                    'arguments': json.dumps(tool_args, ensure_ascii=False)
                }
            })

        # 构建消息对象
        message = {
            'role': role,
            'content': content_blocks
        }

        if tool_calls:
            message['tool_calls'] = tool_calls

        metadata = {}
        if timestamp:
            metadata['timestamp'] = timestamp
        if msg.get('model'):
            metadata['model'] = msg['model']

        if metadata:
            message['_metadata'] = metadata

        messages.append(message)

        # 处理工具结果消息
        for tool_call in tool_calls_data:
            tool_id = tool_call.get('id', '')
            tool_result = tool_call.get('result', [])

            if tool_result:
                output_text = ''
                for result_item in tool_result:
                    if isinstance(result_item, dict):
                        func_response = result_item.get('functionResponse', {})
                        response_data = func_response.get('response', {})
                        output_text = response_data.get('output', '')
                        break

                tool_message = {
                    'role': 'tool',
                    'tool_call_id': tool_id,
                    'content': [{
                        'type': 'tool_output',
                        'text': output_text
                    }]
                }
                messages.append(tool_message)

        # 收集 token 统计
        tokens = msg.get('tokens', {})
        if tokens:
            token_count = {
                'type': 'token_count',
                'input_tokens': tokens.get('input', 0),
                'output_tokens': tokens.get('output', 0),
                '_timestamp': timestamp
            }

            if 'cached' in tokens:
                token_count['cache_read_input_tokens'] = tokens['cached']
            if 'thoughts' in tokens:
                token_count['reasoning_tokens'] = tokens['thoughts']
            if 'tool' in tokens:
                token_count['tool_tokens'] = tokens['tool']
            if 'total' in tokens:
                token_count['total_tokens'] = tokens['total']

            token_counts.append(token_count)

    # 构建会话元数据
    session_meta = {
        'source': 'gemini',
        'session_id': data.get('sessionId', ''),
        'message_count': len(messages),
        'user_messages': user_messages,
        'assistant_messages': assistant_messages,
    }

    if data.get('startTime'):
        session_meta['created_at'] = data['startTime']
    if data.get('lastUpdated'):
        session_meta['last_updated_at'] = data['lastUpdated']
    if data.get('projectHash'):
        session_meta['project_hash'] = data['projectHash']

    # 计算会话时长
    if data.get('startTime') and data.get('lastUpdated'):
        try:
            start = datetime.fromisoformat(data['startTime'].replace('Z', '+00:00'))
            end = datetime.fromisoformat(data['lastUpdated'].replace('Z', '+00:00'))
            duration = (end - start).total_seconds()
            session_meta['duration_seconds'] = round(duration, 2)
        except Exception:
            pass

    return {
        'messages': messages,
        'meta': {
            'session_meta': session_meta,
            'token_counts': token_counts
        }
    }


# ============================================================================
# Kilocode JSON 转换器
# ============================================================================

def parse_tool_calls_from_text(text: str) -> List[Dict[str, Any]]:
    """从文本中解析工具调用 (XML 格式)"""
    tool_calls = []
    pattern = r'<(\w+)>(.*?)</\1>'
    matches = re.finditer(pattern, text, re.DOTALL)

    for idx, match in enumerate(matches):
        tool_name = match.group(1)
        tool_content = match.group(2).strip()

        arguments = {}
        param_pattern = r'<(\w+)>(.*?)</\1>'
        param_matches = re.finditer(param_pattern, tool_content, re.DOTALL)

        for param_match in param_matches:
            param_name = param_match.group(1)
            param_value = param_match.group(2).strip()
            arguments[param_name] = param_value

        tool_call_id = f"call_{tool_name}_{idx}"

        tool_calls.append({
            'id': tool_call_id,
            'name': tool_name,
            'arguments': arguments
        })

    return tool_calls


def parse_kilocode_content_block(block: Dict[str, Any], timestamp: Optional[int] = None) -> Dict[str, Any]:
    """解析 Kilocode 内容块"""
    block_type = block.get('type', '')

    if block_type == 'text':
        return {'type': 'text', 'text': block.get('text', '')}
    elif block_type == 'reasoning':
        return {'type': 'reasoning', 'reasoning': block.get('text', '')}
    else:
        text = block.get('text', json.dumps(block, ensure_ascii=False))
        return {'type': 'text', 'text': text}


def parse_kilocode_content_array(content: List[Dict[str, Any]], timestamp: Optional[int] = None) -> tuple:
    """解析 Kilocode 内容数组"""
    content_blocks = []
    tool_calls = []

    for block in content:
        parsed_block = parse_kilocode_content_block(block, timestamp)

        if parsed_block['type'] == 'text':
            text = parsed_block['text']
            extracted_tools = parse_tool_calls_from_text(text)

            if extracted_tools:
                for tool in extracted_tools:
                    tool_call_block = {
                        'type': 'tool_call',
                        'tool_call_id': tool['id'],
                        'name': tool['name'],
                        'arguments': json.dumps(tool['arguments'], ensure_ascii=False)
                    }
                    content_blocks.append(tool_call_block)

                    tool_calls.append({
                        'id': tool['id'],
                        'type': 'function',
                        'function': {
                            'name': tool['name'],
                            'arguments': json.dumps(tool['arguments'], ensure_ascii=False)
                        }
                    })
            else:
                content_blocks.append(parsed_block)
        else:
            content_blocks.append(parsed_block)

    return content_blocks, tool_calls


def convert_kilocode(file_path: Path) -> Dict[str, Any]:
    """转换 Kilocode 格式"""
    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    messages = []
    user_messages = 0
    assistant_messages = 0
    first_timestamp = None
    last_timestamp = None

    for item in data:
        role = item.get('role', '')
        content = item.get('content', [])
        timestamp = item.get('ts', 0)

        if first_timestamp is None:
            first_timestamp = timestamp
        last_timestamp = timestamp

        if role == 'user':
            user_messages += 1
        elif role == 'assistant':
            assistant_messages += 1

        if isinstance(content, list):
            content_blocks, tool_calls = parse_kilocode_content_array(content, timestamp)
        elif isinstance(content, str):
            content_blocks = [{'type': 'text', 'text': content}]
            tool_calls = []
        else:
            content_blocks = [{'type': 'text', 'text': json.dumps(content, ensure_ascii=False)}]
            tool_calls = []

        message = {'role': role, 'content': content_blocks}

        if tool_calls:
            message['tool_calls'] = tool_calls

        metadata = {}
        if timestamp:
            try:
                dt = datetime.fromtimestamp(timestamp / 1000)
                metadata['timestamp'] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
            except Exception:
                pass

        if metadata:
            message['_metadata'] = metadata

        messages.append(message)

    session_meta = {
        'source': 'kilocode',
        'message_count': len(data),
        'user_messages': user_messages,
        'assistant_messages': assistant_messages,
    }

    if first_timestamp:
        try:
            dt = datetime.fromtimestamp(first_timestamp / 1000)
            session_meta['created_at'] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        except Exception:
            pass

    if last_timestamp:
        try:
            dt = datetime.fromtimestamp(last_timestamp / 1000)
            session_meta['last_updated_at'] = dt.strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z'
        except Exception:
            pass

    if first_timestamp and last_timestamp:
        duration_ms = last_timestamp - first_timestamp
        session_meta['duration_seconds'] = round(duration_ms / 1000, 2)

    return {
        'messages': messages,
        'meta': {'session_meta': session_meta}
    }


# ============================================================================
# 各格式入口函数（对外统一接口）
# ============================================================================

def convert_claude_jsonl(file_path: Path) -> Dict[str, Any]:
    """转换 Claude JSONL 格式"""
    with open(file_path, 'r', encoding='utf-8') as f:
        events = [json.loads(line) for line in f if line.strip()]

    options = ClaudeConverterOptions(messages_only=False)
    return convert_claude_jsonl_to_messages(events, options=options)


def convert_codex_jsonl(file_path: Path) -> Dict[str, Any]:
    """转换 Codex JSONL 格式"""
    with open(file_path, 'r', encoding='utf-8') as f:
        events = [json.loads(line) for line in f if line.strip()]

    options = CodexConverterOptions(messages_only=False)
    return convert_codex_jsonl_to_messages(events, options=options)


def convert_opencode(file_path: Path) -> Dict[str, Any]:
    """转换 OpenCode 格式"""
    data = None
    for encoding in ['utf-8-sig', 'utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'gbk', 'gb2312']:
        try:
            with open(file_path, 'r', encoding=encoding) as f:
                data = json.load(f)
            break
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue

    if data is None:
        raise ValueError(f"无法读取文件 {file_path}，尝试了多种编码都失败")

    options = OpenCodeConverterOptions(messages_only=False)
    return convert_opencode_to_messages(data, options=options)


# ============================================================================
# 主程序
# ============================================================================

def convert_single_file(input_path: Path, output_path: Path, format_type: str) -> bool:
    """
    转换单个文件
    
    返回: 转换是否成功
    """
    # 显示检测到的格式
    format_names = {
        'claude_jsonl': 'Claude JSONL',
        'codex_jsonl': 'Codex JSONL',
        'gemini': 'Gemini JSON',
        'kilocode': 'Kilocode JSON',
        'opencode': 'OpenCode JSON'
    }
    
    # 转换数据
    try:
        if format_type == 'claude_jsonl':
            result = convert_claude_jsonl(input_path)
        elif format_type == 'codex_jsonl':
            result = convert_codex_jsonl(input_path)
        elif format_type == 'gemini':
            result = convert_gemini(input_path)
        elif format_type == 'kilocode':
            result = convert_kilocode(input_path)
        elif format_type == 'opencode':
            result = convert_opencode(input_path)
        else:
            print(f"  ❌ 不支持的格式: {format_type}")
            return False
    except Exception as e:
        print(f"  ❌ 转换失败: {str(e)}")
        return False

    # 写入输出文件
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  ❌ 写入输出文件失败: {str(e)}")
        return False

    return True


def process_directory(directory: Path, format_type: str = 'auto') -> None:
    """
    批量处理目录下的所有 JSON/JSONL 文件
    
    参数:
        directory: 目录路径
        format_type: 格式类型 ('auto' 为自动检测)
    """
    # 扫描目录下的所有 JSON 和 JSONL 文件
    json_files = list(directory.glob('*.json'))
    jsonl_files = list(directory.glob('*.jsonl'))
    all_files = json_files + jsonl_files
    
    # 过滤掉已经转换过的文件
    files_to_process = [f for f in all_files if not f.stem.endswith('_converted')]
    
    if not files_to_process:
        print(f"❌ 目录中没有找到需要转换的文件: {directory}")
        print("   (已忽略 *_converted.json 文件)")
        return
    
    # 创建 converted 输出目录
    output_dir = directory / 'converted'
    try:
        output_dir.mkdir(exist_ok=True)
        print(f"输出目录: {output_dir}")
    except Exception as e:
        print(f"❌ 错误: 无法创建输出目录: {str(e)}")
        return
    
    print(f"找到 {len(files_to_process)} 个文件待处理")
    print()
    
    success_count = 0
    failed_count = 0
    skipped_count = 0
    
    for idx, input_path in enumerate(files_to_process, 1):
        print(f"[{idx}/{len(files_to_process)}] 处理: {input_path.name}")
        
        # 生成输出文件名 (放到 converted 子目录下)
        output_path = output_dir / f"{input_path.stem}_converted.json"
        
        # 检查输出文件是否已存在
        if output_path.exists():
            print(f"  ⚠️  输出文件已存在,跳过: {output_path.name}")
            skipped_count += 1
            continue
        
        # 检测格式
        if format_type == 'auto':
            detected_format = detect_format(input_path)
        else:
            format_map = {
                'claude': 'claude_jsonl',
                'codex': 'codex_jsonl',
                'gemini': 'gemini',
                'kilocode': 'kilocode',
                'opencode': 'opencode'
            }
            detected_format = format_map.get(format_type, format_type)
        
        if detected_format == 'unknown':
            print(f"  ❌ 无法识别的文件格式,跳过")
            failed_count += 1
            continue
        
        format_names = {
            'claude_jsonl': 'Claude JSONL',
            'codex_jsonl': 'Codex JSONL',
            'gemini': 'Gemini JSON',
            'kilocode': 'Kilocode JSON',
            'opencode': 'OpenCode JSON'
        }
        print(f"  格式: {format_names.get(detected_format, detected_format)}")
        
        # 转换文件
        if convert_single_file(input_path, output_path, detected_format):
            file_size = output_path.stat().st_size / 1024
            print(f"  ✅ 转换成功 ({file_size:.2f} KB) -> {output_path.name}")
            success_count += 1
        else:
            failed_count += 1
        
        print()
    
    # 显示汇总信息
    print("="*80)
    print("批量转换完成!")
    print("="*80)
    print(f"成功: {success_count} 个")
    print(f"失败: {failed_count} 个")
    print(f"跳过: {skipped_count} 个")
    print(f"总计: {len(files_to_process)} 个")


def main():
    parser = argparse.ArgumentParser(
        description='AI Session 统一转换工具 - 自动识别格式并转换为 OpenAI 标准格式',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
支持的输入格式:
  1. Claude JSONL - Claude Desktop/API session 事件流
  2. Codex JSONL - Codex CLI session 事件流
  3. Gemini JSON - Gemini CLI session 数据
  4. Kilocode JSON - Kilocode API 对话历史数组
  5. OpenCode JSON - OpenCode session 数据

输出格式:
  - OpenAI 标准消息格式
  - 符合 OPENAI_FORMAT_SPEC.md 规范
  - 包含 messages 数组和 meta 元数据

使用示例:
  # 单文件转换 (自动检测格式)
  python convert_ai_session.py -i session.json
  
  # 单文件转换 (指定输出文件)
  python convert_ai_session.py -i session.jsonl -o output.json
  
  # 单文件转换 (强制指定格式)
  python convert_ai_session.py -i session.jsonl --format claude
  
  # 批量转换指定目录下所有文件
  python convert_ai_session.py -d ./sessions
  
  # 批量转换当前目录下所有文件
  python convert_ai_session.py -d .
  
  # 批量转换 (强制指定格式)
  python convert_ai_session.py -d ./sessions --format claude

批量处理说明:
  - 批量模式会扫描目录下所有 .json 和 .jsonl 文件
  - 自动创建 converted/ 子目录存放转换后的文件
  - 输出文件命名规则: converted/<原文件名>_converted.json
  - 自动跳过已存在的输出文件和 *_converted.json 文件
  - 使用 -d . 可以处理当前目录下的所有文件
        """
    )

    # 创建互斥组: -i 和 -d 只能选一个
    input_group = parser.add_mutually_exclusive_group(required=True)
    
    input_group.add_argument(
        '-i', '--input',
        help='输入文件路径 (单文件模式)'
    )
    
    input_group.add_argument(
        '-d', '--directory',
        help='输入目录路径 (批量处理模式,会扫描目录下所有 .json 和 .jsonl 文件)'
    )

    parser.add_argument(
        '-o', '--output',
        help='输出文件路径 (仅单文件模式有效,默认: <输入文件名>_converted.json)'
    )

    parser.add_argument(
        '--format',
        choices=['claude', 'codex', 'gemini', 'kilocode', 'opencode', 'auto'],
        default='auto',
        help='强制指定输入格式 (默认: auto 自动检测)'
    )

    args = parser.parse_args()

    print("="*80)
    print("AI SESSION 统一转换工具")
    print("="*80)
    print()

    # 批量处理模式
    if args.directory:
        directory_path = Path(args.directory)
        
        if not directory_path.exists():
            print(f"❌ 错误: 目录不存在: {args.directory}")
            sys.exit(1)
        
        if not directory_path.is_dir():
            print(f"❌ 错误: 不是一个目录: {args.directory}")
            sys.exit(1)
        
        if args.output:
            print("⚠️  警告: 批量处理模式下 -o 参数无效,将使用默认命名规则")
            print()
        
        print(f"批量处理模式")
        print(f"输入目录: {directory_path}")
        print(f"输出规则: converted/<原文件名>_converted.json")
        print()
        
        process_directory(directory_path, args.format)
        return

    # 单文件处理模式
    input_path = Path(args.input)

    if not input_path.exists():
        print(f"❌ 错误: 输入文件不存在: {args.input}")
        sys.exit(1)

    # 确定输出文件名
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = input_path.parent / f"{input_path.stem}_converted.json"

    print(f"单文件处理模式")
    print(f"输入文件: {input_path}")
    print(f"输出文件: {output_path}")
    print()

    # 检测格式
    if args.format == 'auto':
        print("正在检测文件格式...")
        format_type = detect_format(input_path)
    else:
        format_map = {
            'claude': 'claude_jsonl',
            'codex': 'codex_jsonl',
            'gemini': 'gemini',
            'kilocode': 'kilocode',
            'opencode': 'opencode'
        }
        format_type = format_map[args.format]
        print(f"使用指定格式: {args.format}")

    if format_type == 'unknown':
        print("❌ 错误: 无法识别的文件格式")
        print()
        print("支持的格式:")
        print("  - Claude JSONL (*.jsonl)")
        print("  - Codex JSONL (*.jsonl)")
        print("  - Gemini JSON (*.json)")
        print("  - Kilocode JSON (*.json)")
        print("  - OpenCode JSON (*.json)")
        print()
        print("提示: 使用 --format 参数强制指定格式")
        sys.exit(1)

    # 显示检测到的格式
    format_names = {
        'claude_jsonl': 'Claude JSONL',
        'codex_jsonl': 'Codex JSONL',
        'gemini': 'Gemini JSON',
        'kilocode': 'Kilocode JSON',
        'opencode': 'OpenCode JSON'
    }
    print(f"✅ 检测到格式: {format_names.get(format_type, format_type)}")
    print()

    # 转换数据
    print("正在转换数据...")
    try:
        if format_type == 'claude_jsonl':
            result = convert_claude_jsonl(input_path)
        elif format_type == 'codex_jsonl':
            result = convert_codex_jsonl(input_path)
        elif format_type == 'gemini':
            result = convert_gemini(input_path)
        elif format_type == 'kilocode':
            result = convert_kilocode(input_path)
        elif format_type == 'opencode':
            result = convert_opencode(input_path)
        else:
            print(f"❌ 错误: 不支持的格式: {format_type}")
            sys.exit(1)
    except Exception as e:
        print(f"❌ 错误: 转换失败: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

    # 写入输出文件
    print("正在写入输出文件...")
    try:
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"❌ 错误: 写入输出文件失败: {str(e)}")
        sys.exit(1)

    # 显示统计信息
    print()
    print("="*80)
    print("✅ 转换完成!")
    print("="*80)
    print()

    if 'meta' in result and 'session_meta' in result['meta']:
        meta = result['meta']['session_meta']
        print("统计信息:")
        if 'message_count' in meta:
            print(f"  总消息数: {meta['message_count']}")
        if 'user_messages' in meta:
            print(f"  用户消息: {meta['user_messages']}")
        if 'assistant_messages' in meta:
            print(f"  助手消息: {meta['assistant_messages']}")
        if 'created_at' in meta:
            print(f"  开始时间: {meta['created_at']}")
        if 'last_updated_at' in meta:
            print(f"  结束时间: {meta['last_updated_at']}")
        if 'duration_seconds' in meta:
            print(f"  会话时长: {meta['duration_seconds']} 秒")
        print()

    print("输出格式: 完整格式 (包含 meta)")
    file_size = output_path.stat().st_size / 1024
    print(f"文件大小: {file_size:.2f} KB")
    print(f"输出文件: {output_path}")


if __name__ == "__main__":
    main()
