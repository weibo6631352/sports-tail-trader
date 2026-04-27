from __future__ import annotations

from contextvars import ContextVar
from contextlib import contextmanager
from uuid import uuid4

trace_id_var: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    # trace 需要沿着 discovery -> signal -> order -> fill 一路传播，所以这里既生成也写入上下文。
    trace_id = uuid4().hex
    trace_id_var.set(trace_id)
    return trace_id


def current_trace_id() -> str | None:
    return trace_id_var.get()


def bind_trace_id(trace_id: str | None) -> str:
    if trace_id:
        trace_id_var.set(trace_id)
        return trace_id
    return new_trace_id()


def ensure_trace_id() -> str:
    trace_id = current_trace_id()
    if trace_id:
        return trace_id
    return new_trace_id()


@contextmanager
def trace_scope(trace_id: str | None = None):
    if trace_id is None:
        trace_id = current_trace_id()
    if trace_id is None:
        trace_id = uuid4().hex
    token = trace_id_var.set(trace_id)
    try:
        yield trace_id
    finally:
        trace_id_var.reset(token)
