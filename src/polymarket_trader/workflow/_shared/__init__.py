"""跨 family 共享的纯函数工具。

放在 ``current/_shared/`` 下而不是 domain：只服务当前策略包内部，不暴露成
框架级 API；与 §5 改动落点一致——策略相关、可复用的小工具。
"""

from polymarket_trader.workflow._shared.team_normalize import normalize_team_name

__all__ = ["normalize_team_name"]
