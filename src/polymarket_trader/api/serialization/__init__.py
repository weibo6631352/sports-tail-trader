"""【api/serialization】operator API 路由层专用序列化器。

`ApiSerializer` 把 domain 对象（Market / Order / Fill / Position / Allocation /
AuditEvent / Outbox 等）投影成前端契约形态。位置在 `api/` 下因为它是 HTTP
wire format 的实现（domain 不知道 JSON），不是 domain 规则。
"""

from .api_serializer import ApiSerializer

__all__ = ["ApiSerializer"]
