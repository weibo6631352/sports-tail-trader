from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, ClassVar
from urllib.parse import quote

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True, slots=True)
class ConfigIssue:
    field: str
    code: str
    message: str
    value: Any | None = None

    def as_dict(self) -> dict[str, Any]:
        data = {"field": self.field, "code": self.code, "message": self.message}
        if self.value is not None:
            data["value"] = self.value
        return data


@dataclass(frozen=True, slots=True)
class StartupReadiness:
    ready_to_trade: bool
    blocking_issues: tuple[ConfigIssue, ...]
    warnings: tuple[ConfigIssue, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready_to_trade": self.ready_to_trade,
            "blocking_issues": [issue.as_dict() for issue in self.blocking_issues],
            "warnings": [issue.as_dict() for issue in self.warnings],
        }


class ConfigLoadError(RuntimeError):
    def __init__(self, issues: list[ConfigIssue]) -> None:
        self.issues = tuple(issues)
        super().__init__(json.dumps(self.as_dict(), ensure_ascii=False, default=str))

    def as_dict(self) -> dict[str, Any]:
        return {"error": "config_load_failed", "issues": [issue.as_dict() for issue in self.issues]}

    @classmethod
    def from_validation_error(cls, error: ValidationError) -> "ConfigLoadError":
        issues: list[ConfigIssue] = []
        for item in error.errors():
            location = item.get("loc", ())
            if isinstance(location, tuple):
                field = ".".join(str(part) for part in location) or "settings"
            else:
                field = str(location)
            issues.append(
                ConfigIssue(
                    field=field,
                    code=str(item.get("type", "validation_error")),
                    message=str(item.get("msg", "配置校验失败")),
                    value=item.get("input"),
                )
            )
        return cls(issues)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="forbid",
        populate_by_name=True,
    )

    # Polymarket 端点配置只描述外部服务地址，不携带任何密钥。
    polymarket_clob_host: str = "https://clob.polymarket.com"
    polymarket_gamma_host: str = "https://gamma-api.polymarket.com"
    polymarket_data_host: str = "https://data-api.polymarket.com"
    polymarket_market_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_user_ws: str = "wss://ws-subscriptions-clob.polymarket.com/ws/user"
    polymarket_chain_id: int = Field(default=137, ge=1)
    # 默认按 EOA 签名处理；如果使用代理钱包或 Safe，需要显式覆盖 signature type / funder。
    polymarket_signature_type: int = Field(default=0, ge=0, le=2)
    polymarket_funder_address: str | None = None
    extension_module: str | None = None
    extension_config_path: str | None = None

    # portfolio_budget_usdc 语义：bankroll 软上限。实际 bankroll = min(链上可用 USDC,
    # portfolio_budget_usdc)。设 0 时 Kelly 拒新仓（启动安全态）。Kelly 引擎在
    # ``domain/kelly.py``——见该模块 docstring 公式。
    portfolio_budget_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    market_sync_interval_seconds: int = Field(default=60, ge=1)
    order_retry_limit: int = Field(default=2, ge=0)

    # audit_events 表保留期（天）。实测 sports_live_state_recorded + market_discovered
    # 每天累积百万级 row，长期运行会让查询变慢且占用大量磁盘。retention job 每天跑
    # 一次 ``DELETE WHERE created_at < now() - interval N days``，让 audit 表稳态
    # 在 ~14 天数据量内。改 0 关闭 retention（不推荐——仅用于离线分析临时保留）。
    audit_retention_days: int = Field(default=14, ge=0, le=365)
    audit_retention_interval_seconds: int = Field(default=86_400, ge=300)
    # 单次 purge 的批量上限——避免一次 DELETE 锁表过久。10k 在 PG 上约几百 ms。
    audit_retention_purge_batch_size: int = Field(default=10_000, ge=100, le=200_000)

    # 外部体育直播状态源只提供入场前事实，不承载策略阈值或交易参数。
    # 默认 ``True``：strategies/current 的入场链路依赖直播状态，关闭后整个 funnel
    # 在 candidates 阶段卡死（实测 25k discovered / 0 filtered_in）。所以默认开 +
    # supervisor 启动期对"trading 已就绪但 sports_live_state 关闭"发告警。
    sports_live_state_enabled: bool = True
    sports_live_state_leagues: str = "nba,nhl,nfl,mlb,tennis,sports,atp,wta,itf,bkbbl,bkseriea"
    sports_live_state_interval_seconds: int = Field(default=3, ge=1)
    sports_live_state_timeout_s: float = Field(default=8.0, ge=0.1)
    sports_live_state_publish_entry_signals: bool = True
    sports_live_state_health_cooldown_base_s: float = Field(default=60.0, ge=1.0)
    sports_live_state_health_eviction_s: float = Field(default=1800.0, ge=60.0)

    # Goalserve inplay WebSocket 配置。认证方式：JWT token（API key 换取）。
    # proxy 仅用于开发环境（本机 Clash 代理）；生产留空直连。
    goalserve_sports: str = "basketball,soccer,hockey,baseball,tennis,esports,amfootball,volleyball"
    goalserve_proxy: str | None = None

    # Goalserve getfeed livescore 配置。认证方式：API key 嵌入 URL。
    # 覆盖 inplay feed 没有的运动：cricket/handball/rugby/boxing/mma/golf/horse_racing/f1/motogp。
    goalserve_api_key: SecretStr | None = None
    goalserve_livescore_enabled: bool = True
    # 覆盖所有支持 livescore getfeed 的运动。
    # nba/mlb/nhl/tennis/basketball/baseball/hockey 通过 XML 路径拉取，
    # 与 inplay feed 互为补充（inplay IP 被封时这些路径作为主要数据源）。
    goalserve_livescore_sports: str = (
        "soccer,"  # soccernew/home covers all leagues incl Copa Libertadores/Sudamericana
        "nba,mlb,nhl,wnba,basketball,baseball,hockey,tennis,"
        "cricket,handball,rugby,boxing,mma,"
        "golf_pga,golf_dp,golf_liv,golf_lpga,"
        "horse_racing_us,horse_racing_uk,horse_racing_au,horse_racing_hk,"
        "f1,motogp"
    )
    goalserve_livescore_base_url: str = "http://www.goalserve.com/getfeed"
    goalserve_livescore_timeout_s: float = Field(default=10.0, ge=1.0)

    # Goalserve 赛前赔率（Pregame Odds）配置。认证方式：API key 嵌入 URL，GZIP 压缩。
    # 数据量极大（>100MB），默认关闭；按需启用并配置 GOALSERVE_API_KEY。
    goalserve_pregame_enabled: bool = False
    goalserve_pregame_sports: str = "soccer,basketball,tennis,hockey,baseball,amfootball,esports,mma,cricket,rugby,volleyball,handball,boxing,darts,table_tennis,futsal,rugbyleague"
    goalserve_pregame_base_url: str = "http://www.goalserve.com"
    goalserve_pregame_timeout_s: float = Field(default=30.0, ge=1.0)
    # ts 增量拉取；每次只拿变化部分，300s 足以在 ts 未超期前更新一次。
    goalserve_pregame_interval_seconds: int = Field(default=300, ge=60)

    # 赛季级状态子系统：服务于 outright 反向定价。cadence 小时级。
    sports_season_state_enabled: bool = False
    sports_season_state_sources: str = "espn"
    sports_season_state_leagues: str = "nba,nhl,nfl,mlb"
    sports_season_state_interval_seconds: int = Field(default=1800, ge=300)
    sports_season_state_timeout_s: float = Field(default=10.0, ge=0.5)
    sports_season_odds_provider: str = "theoddsapi"
    sports_season_odds_api_key: SecretStr | None = None
    sports_season_odds_base_url: str = "https://api.the-odds-api.com"
    sports_season_odds_regions: str = "us,eu"
    sports_season_odds_interval_seconds: int = Field(default=1800, ge=300)
    sports_season_odds_ttl_seconds: int = Field(default=1800, ge=60)

    # 系列赛热态子系统：服务于 series WINNER 实盘定价。
    # interval=60s：每分钟发一次入场信号（无比赛时 WS 不推盘口更新，需此信号补驱动）。
    # ttl=600s：ESPN scoreboard 每 10 分钟重新拉取一次（减少外部请求）。
    sports_series_state_enabled: bool = False
    sports_series_state_base_url: str = "https://site.api.espn.com"
    sports_series_state_interval_seconds: int = Field(default=60, ge=60)
    sports_series_state_ttl_seconds: int = Field(default=600, ge=60)
    sports_series_state_timeout_s: float = Field(default=5.0, ge=0.5)

    # 单场 h2h 赔率源：TheOddsAPI v4 markets=h2h，driving series winner p_per_game。
    sports_game_odds_provider: str = "theoddsapi"
    sports_game_odds_api_key: SecretStr | None = None
    sports_game_odds_base_url: str = "https://api.the-odds-api.com"
    sports_game_odds_regions: str = "us,eu"
    sports_game_odds_interval_seconds: int = Field(default=1800, ge=300)
    sports_game_odds_ttl_seconds: int = Field(default=1800, ge=60)

    # 性能与优先级字段必须始终有限制，避免无界队列、无界等待和热路径阻塞。
    enable_uvloop: bool = True
    trading_event_queue_max_size: int = Field(default=1000, ge=1)
    maintenance_event_queue_max_size: int = Field(default=1000, ge=1)
    persistence_event_queue_max_size: int = Field(default=5000, ge=1)
    trading_worker_threads: int = Field(default=4, ge=1)
    maintenance_worker_threads: int = Field(default=4, ge=1)
    maintenance_process_workers: int = Field(default=2, ge=1)
    order_submit_timeout_ms: int = Field(default=3000, ge=1)
    order_sign_timeout_ms: int = Field(default=1000, ge=1)
    critical_lock_timeout_ms: int = Field(default=20, ge=1)
    trading_queue_warn_depth: int = Field(default=100, ge=0)
    entry_signal_to_submit_warn_ms: int = Field(default=500, ge=1)

    # SSE fan-out 订阅者并发上限；超过返回 429 Retry-After=5。
    sse_subscriber_cap: int = Field(default=32, ge=1)

    # === API 服务暴露面（Admin 路由是高危写入入口，必须能在 prod 关掉 Swagger 文档
    # 和强制 token 鉴权）===
    # 默认 True 便于本机开发；prod 必须显式 EXPOSE_OPENAPI_DOCS=false。
    expose_openapi_docs: bool = True
    # 非 None 时所有 admin 写接口要求 X-Admin-Token 头匹配；None 表示禁用 token 校验
    # （仅适合本机/受信网络）。生产环境 None 等同于 admin 接口裸跑，应在启动阶段告警。
    admin_api_token: SecretStr | None = None
    # CORS 白名单——逗号分隔。生产配置应只列前端实际域名，不留 localhost。
    cors_allowed_origins: str = "http://127.0.0.1:5173,http://127.0.0.1:5174,http://localhost:5173,http://localhost:5174"

    # 密钥类配置绝不入仓；空值只作为示例，真值必须来自安全环境变量或 secret manager。
    polymarket_api_key: SecretStr | None = None
    polymarket_api_secret: SecretStr | None = None
    polymarket_api_passphrase: SecretStr | None = None
    wallet_private_key: SecretStr | None = None
    signer_private_key: SecretStr | None = None

    # 数据库连接支持完整 URL 覆盖，也支持拆分字段；数据库密码同样不得写入仓库。
    database_url_override: str | None = Field(default=None, validation_alias="DATABASE_URL")
    database_driver: str = "postgresql+asyncpg"
    database_host: str = "localhost"
    database_port: int = Field(default=5432, ge=1, le=65535)
    database_name: str = "trader"
    database_user: str = "trader"
    database_password: SecretStr | None = None

    _SECRET_FIELDS: ClassVar[tuple[str, ...]] = (
        "polymarket_api_key",
        "polymarket_api_secret",
        "polymarket_api_passphrase",
        "wallet_private_key",
        "signer_private_key",
        "database_password",
        "database_url_override",
        "goalserve_api_key",
    )
    _STARTUP_REQUIRED_SECRET_FIELDS: ClassVar[tuple[str, ...]] = (
        "wallet_private_key",
    )

    @field_validator(
        "polymarket_api_key",
        "polymarket_api_secret",
        "polymarket_api_passphrase",
        "wallet_private_key",
        "signer_private_key",
        "polymarket_funder_address",
        "database_password",
        "database_url_override",
        "extension_module",
        "extension_config_path",
        "goalserve_api_key",
        mode="before",
    )
    @classmethod
    def _blank_string_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @property
    def database_url(self) -> str:
        if self.database_url_override:
            return self.database_url_override
        return self._compose_database_url(mask_password=False)

    @property
    def masked_database_url(self) -> str:
        if self.database_url_override:
            try:
                from sqlalchemy.engine import make_url

                return make_url(self.database_url_override).render_as_string(hide_password=True)
            except Exception:
                return "***"
        return self._compose_database_url(mask_password=True)

    @property
    def sports_live_state_league_codes(self) -> tuple[str, ...]:
        """返回外部体育直播状态源需要拉取的联赛代码。"""

        return _csv_codes(self.sports_live_state_leagues)

    @property
    def goalserve_sport_codes(self) -> tuple[str, ...]:
        """返回 Goalserve inplay 启用的运动列表。"""

        return _csv_codes(self.goalserve_sports)

    @property
    def goalserve_livescore_sport_codes(self) -> tuple[str, ...]:
        """返回 Goalserve livescore getfeed 启用的运动列表。"""

        return _csv_codes(self.goalserve_livescore_sports)

    @property
    def goalserve_pregame_sport_codes(self) -> tuple[str, ...]:
        """返回 Goalserve 赛前赔率启用的运动列表。"""

        return _csv_codes(self.goalserve_pregame_sports)

    def _compose_database_url(self, *, mask_password: bool) -> str:
        password = self._secret_value(self.database_password)
        auth = self.database_user
        if password:
            auth = f"{auth}:{'***' if mask_password else quote(password, safe='')}"
        return f"{self.database_driver}://{auth}@{self.database_host}:{self.database_port}/{self.database_name}"

    @staticmethod
    def _secret_value(value: SecretStr | None) -> str:
        if value is None:
            return ""
        return value.get_secret_value()

    def _is_secret_missing(self, field_name: str) -> bool:
        raw_value = getattr(self, field_name)
        if isinstance(raw_value, SecretStr):
            return raw_value.get_secret_value().strip() == ""
        return raw_value is None or str(raw_value).strip() == ""

    def _masked_secret_value(self, field_name: str) -> Any:
        raw_value = getattr(self, field_name)
        if isinstance(raw_value, SecretStr):
            secret = raw_value.get_secret_value()
            return "***" if secret else ""
        if raw_value is None:
            return None
        return "***" if str(raw_value).strip() else ""

    def sanitized_dump(self) -> dict[str, Any]:
        data = self.model_dump(mode="python", exclude_none=True)
        for field_name in self._SECRET_FIELDS:
            if field_name in data:
                data[field_name] = self._masked_secret_value(field_name)
        data["database_url"] = self.masked_database_url
        return data

    def validate_startup_readiness(self) -> StartupReadiness:
        blocking_issues: list[ConfigIssue] = []
        warnings: list[ConfigIssue] = []

        # 启动阶段只允许检查配置是否足够安全进入交易态，任何密钥缺失都必须阻止自动下单。
        for field_name in self._STARTUP_REQUIRED_SECRET_FIELDS:
            if self._is_secret_missing(field_name):
                blocking_issues.append(
                    ConfigIssue(
                        field=field_name,
                        code="missing_secret",
                        message="密钥未配置，启动阶段禁止自动下单",
                    )
                )

        if self.extension_module is None or not self.extension_module.strip():
            blocking_issues.append(
                ConfigIssue(
                    field="extension_module",
                    code="missing_extension_module",
                    message="必须显式配置二次开发业务扩展模块。",
                )
            )

        api_cred_fields = (
            "polymarket_api_key",
            "polymarket_api_secret",
            "polymarket_api_passphrase",
        )
        configured_api_cred_count = sum(
            0 if self._is_secret_missing(field_name) else 1 for field_name in api_cred_fields
        )
        if 0 < configured_api_cred_count < len(api_cred_fields):
            blocking_issues.append(
                ConfigIssue(
                    field="polymarket_api_credentials",
                    code="partial_api_credentials",
                    message="POLYMARKET_API_KEY / SECRET / PASSPHRASE 需要同时提供，或全部留空并在运行时派生",
                )
            )

        if self.polymarket_signature_type not in {0, 1, 2}:
            blocking_issues.append(
                ConfigIssue(
                    field="polymarket_signature_type",
                    code="invalid_signature_type",
                    message="POLYMARKET_SIGNATURE_TYPE 仅支持 0(EOA) / 1(proxy) / 2(safe)",
                    value=self.polymarket_signature_type,
                )
            )
        if self.polymarket_signature_type in {1, 2} and not self.polymarket_funder_address:
            blocking_issues.append(
                ConfigIssue(
                    field="polymarket_funder_address",
                    code="missing_funder_address",
                    message="代理钱包或 Safe 模式需要配置 POLYMARKET_FUNDER_ADDRESS",
                )
            )

        # bankroll 软上限不允许 0 进入真实交易；Kelly 引擎在 bankroll=0 时拒新仓但不报错，
        # 启动期把它升级为 blocking issue，避免运行中"看起来在跑但永远不下单"的迷惑。
        if self.portfolio_budget_usdc <= 0:
            blocking_issues.append(
                ConfigIssue(
                    field="portfolio_budget_usdc",
                    code="non_positive_limit",
                    message="bankroll 软上限 portfolio_budget_usdc 必须大于 0，启动阶段禁止自动下单",
                    value=self.portfolio_budget_usdc,
                )
            )

        if self.order_retry_limit < 0:
            blocking_issues.append(
                ConfigIssue(
                    field="order_retry_limit",
                    code="negative_limit",
                    message="ORDER_RETRY_LIMIT 不能为负数",
                    value=self.order_retry_limit,
                )
            )

        if self.market_sync_interval_seconds <= 0:
            blocking_issues.append(
                ConfigIssue(
                    field="market_sync_interval_seconds",
                    code="non_positive_limit",
                    message="MARKET_SYNC_INTERVAL_SECONDS 必须大于 0",
                    value=self.market_sync_interval_seconds,
                )
            )

        if self.sports_live_state_enabled:
            if self.goalserve_api_key is None:
                blocking_issues.append(
                    ConfigIssue(
                        field="goalserve_api_key",
                        code="missing_api_key",
                        message="启用体育直播状态源时必须配置 GOALSERVE_API_KEY（inplay WS 认证）",
                    )
                )
            if not self.goalserve_sports.strip():
                blocking_issues.append(
                    ConfigIssue(
                        field="goalserve_sports",
                        code="missing_sports",
                        message="启用体育直播状态源时 GOALSERVE_SPORTS 至少配置一个运动",
                    )
                )
            if not self.sports_live_state_league_codes:
                blocking_issues.append(
                    ConfigIssue(
                        field="sports_live_state_leagues",
                        code="missing_leagues",
                        message="启用体育直播状态源时至少需要配置一个联赛代码",
                    )
                )

        if self.trading_queue_warn_depth < 0:
            warnings.append(
                ConfigIssue(
                    field="trading_queue_warn_depth",
                    code="negative_warning_threshold",
                    message="TRADING_QUEUE_WARN_DEPTH 为负数没有意义，建议改回非负数",
                    value=self.trading_queue_warn_depth,
                )
            )

        return StartupReadiness(
            ready_to_trade=not blocking_issues,
            blocking_issues=tuple(blocking_issues),
            warnings=tuple(warnings),
        )


def load_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as exc:
        raise ConfigLoadError.from_validation_error(exc) from exc


def _csv_codes(value: str) -> tuple[str, ...]:
    """解析逗号分隔的小写代码并去重。"""

    seen: set[str] = set()
    result: list[str] = []
    for item in value.split(","):
        code = item.strip().lower()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append(code)
    return tuple(result)
