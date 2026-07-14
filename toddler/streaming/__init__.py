"""Streaming — real-time token-by-token output with Rich Live display.

Phase 6 implements the full streaming pipeline: SSE → StreamEvent →
AgentEvent → Rich Live dual-panel display.
"""

from __future__ import annotations

from toddler.streaming.display import StreamDisplay
from toddler.streaming.handler import IncrementalJSONParser, StreamHandler

__all__ = ["IncrementalJSONParser", "StreamHandler", "StreamDisplay"]
