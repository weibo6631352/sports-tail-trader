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

    # 预算相关默认值保持 0，避免在未明确配置前进入自动交易；其余风控阈值对齐示例推荐值。
    portfolio_budget_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    max_order_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    max_market_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    max_total_usdc: Decimal = Field(default=Decimal("0"), ge=Decimal("0"))
    market_sync_interval_seconds: int = Field(default=60, ge=1)
    order_retry_limit: int = Field(default=2, ge=0)
    max_open_orders: int = Field(default=0, ge=0)

    # 外部体育直播状态源只提供入场前事实，不承载策略阈值或交易参数。
    sports_live_state_enabled: bool = False
    sports_live_state_sources: str = "espn,nba,nhl,mlb,sofascore,thesportsdb"
    sports_live_state_espn_base_url: str = "https://site.api.espn.com"
    sports_live_state_nba_base_url: str = "https://cdn.nba.com"
    sports_live_state_nhl_base_url: str = "https://api-web.nhle.com"
    sports_live_state_mlb_base_url: str = "https://statsapi.mlb.com"
    sports_live_state_sofascore_base_url: str = "https://www.sofascore.com"
    sports_live_state_sofascore_lookback_days: int = Field(default=1, ge=0, le=3)
    sports_live_state_sofascore_lookahead_days: int = Field(default=3, ge=0, le=3)
    sports_live_state_thesportsdb_base_url: str = "https://www.thesportsdb.com/api/v1/json/3"
    sports_live_state_leagues: str = "nba,nhl,nfl,mlb,tennis,sports"
    sports_live_state_interval_seconds: int = Field(default=5, ge=5)
    sports_live_state_timeout_s: float = Field(default=5.0, ge=0.1)
    sports_live_state_publish_entry_signals: bool = True

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

    # 密钥类配置绝不入仓；空值只作为示例，真值必须来自安全环境变量或 secret manager。
    polymarket_api_key: SecretStr | None = None
    polymarket_api_secret: SecretStr | None = None
    polymarket_api_passphrase: SecretStr | None = None
    wallet_private_key: SecretStr | None = None
    signer_private_key: SecretStr | None = None
    builder_attribution_credentials: SecretStr | None = None

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
        "builder_attribution_credentials",
        "database_password",
        "database_url_override",
    )
    _STARTUP_REQUIRED_SECRET_FIELDS: ClassVar[tuple[str, ...]] = (
        "wallet_private_key",
    )
    _SUPPORTED_SPORTS_LIVE_STATE_SOURCES: ClassVar[tuple[str, ...]] = (
        "espn",
        "nba",
        "nhl",
        "mlb",
        "sofascore",
        "thesportsdb",
    )
    _ESPN_SUPPORTED_LEAGUES: ClassVar[tuple[str, ...]] = (
        "nba",
        "wnba",
        "ncaamb",
        "ncaawb",
        "nfl",
        "ncaaf",
        "nhl",
        "mlb",
    )
    _SOFASCORE_SUPPORTED_LEAGUES: ClassVar[tuple[str, ...]] = (
        "sports",
        "nba",
        "wnba",
        "ncaamb",
        "ncaawb",
        "basketball",
        "nhl",
        "ice-hockey",
        "hockey",
        "mlb",
        "baseball",
        "nfl",
        "ncaaf",
        "american-football",
        "soccer",
        "epl",
        "premier-league",
        "football",
        "tennis",
    )
    _THESPORTSDB_SUPPORTED_LEAGUES: ClassVar[tuple[str, ...]] = (
        "nhl",
        "ice-hockey",
        "hockey",
        "mlb",
        "baseball",
    )

    @field_validator(
        "polymarket_api_key",
        "polymarket_api_secret",
        "polymarket_api_passphrase",
        "wallet_private_key",
        "signer_private_key",
        "builder_attribution_credentials",
        "polymarket_funder_address",
        "database_password",
        "database_url_override",
        "extension_module",
        "extension_config_path",
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
    def sports_live_state_source_codes(self) -> tuple[str, ...]:
        """返回启用的外部体育直播状态源代码。"""

        return _csv_codes(self.sports_live_state_sources)

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

        # 资金边界不允许默认为 0 进入真实交易，否则虽然能启动，但不会形成明确的风险上限。
        for field_name, label in (
            ("portfolio_budget_usdc", "组合预算"),
            ("max_order_usdc", "单笔下单上限"),
            ("max_market_usdc", "单市场上限"),
            ("max_total_usdc", "总仓上限"),
            ("max_open_orders", "最大未完成订单数"),
        ):
            value = getattr(self, field_name)
            if value <= 0:
                blocking_issues.append(
                    ConfigIssue(
                        field=field_name,
                        code="non_positive_limit",
                        message=f"{label} 必须大于 0，启动阶段禁止自动下单",
                        value=value,
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
            source_codes = self.sports_live_state_source_codes
            unsupported_sources = tuple(
                source for source in source_codes if source not in self._SUPPORTED_SPORTS_LIVE_STATE_SOURCES
            )
            if not source_codes:
                blocking_issues.append(
                    ConfigIssue(
                        field="sports_live_state_sources",
                        code="missing_sources",
                        message="启用体育直播状态源时至少需要配置一个 SPORTS_LIVE_STATE_SOURCES",
                    )
                )
            if unsupported_sources:
                blocking_issues.append(
                    ConfigIssue(
                        field="sports_live_state_sources",
                        code="unsupported_source",
                        message="SPORTS_LIVE_STATE_SOURCES 仅支持 espn,nba,nhl,mlb,sofascore,thesportsdb",
                        value=",".join(unsupported_sources),
                    )
                )
            if "espn" in source_codes and not self.sports_live_state_espn_base_url.strip():
                blocking_issues.append(
                    ConfigIssue(
                        field="sports_live_state_espn_base_url",
                        code="missing_endpoint",
                        message="启用 ESPN 体育直播状态源时必须配置 SPORTS_LIVE_STATE_ESPN_BASE_URL",
                    )
                )
            if "sofascore" in source_codes and not self.sports_live_state_sofascore_base_url.strip():
                blocking_issues.append(
                    ConfigIssue(
                        field="sports_live_state_sofascore_base_url",
                        code="missing_endpoint",
                        message="启用 SofaScore 体育直播状态源时必须配置 SPORTS_LIVE_STATE_SOFASCORE_BASE_URL",
                    )
                )
            if "thesportsdb" in source_codes and not self.sports_live_state_thesportsdb_base_url.strip():
                blocking_issues.append(
                    ConfigIssue(
                        field="sports_live_state_thesportsdb_base_url",
                        code="missing_endpoint",
                        message="启用 TheSportsDB 体育直播状态源时必须配置 SPORTS_LIVE_STATE_THESPORTSDB_BASE_URL",
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
            league_codes = set(self.sports_live_state_league_codes)
            has_source_league_overlap = (
                (
                    "espn" in source_codes
                    and any(
                        league in self._ESPN_SUPPORTED_LEAGUES or league.startswith("soccer:")
                        for league in league_codes
                    )
                )
                or ("nba" in source_codes and "nba" in league_codes)
                or ("nhl" in source_codes and "nhl" in league_codes)
                or ("mlb" in source_codes and "mlb" in league_codes)
                or (
                    "sofascore" in source_codes
                    and bool(league_codes.intersection(self._SOFASCORE_SUPPORTED_LEAGUES))
                )
                or (
                    "thesportsdb" in source_codes
                    and bool(league_codes.intersection(self._THESPORTSDB_SUPPORTED_LEAGUES))
                )
            )
            if source_codes and league_codes and not has_source_league_overlap:
                blocking_issues.append(
                    ConfigIssue(
                        field="sports_live_state_sources",
                        code="source_league_mismatch",
                        message="SPORTS_LIVE_STATE_SOURCES 与 SPORTS_LIVE_STATE_LEAGUES 没有可拉取的交集",
                        value=f"sources={','.join(source_codes)} leagues={','.join(self.sports_live_state_league_codes)}",
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
