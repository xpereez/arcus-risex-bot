from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path


SUPPORTED_MARKETS = ("BTC", "LIT", "HYPE", "SOL", "SPCX", "NVDA", "TSLA")
LIVE_ACK = "I_UNDERSTAND_THIS_SENDS_REAL_ORDERS"


def load_env_file(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


@dataclass(frozen=True, slots=True)
class Settings:
    mode: str
    auto_start: bool
    markets: tuple[str, ...]
    min_notional_usd: float
    max_notional_usd: float
    order_timeout_seconds: int
    order_poll_interval_seconds: float
    hold_min_minutes: int
    hold_max_minutes: int
    cycle_pause_min_minutes: int
    cycle_pause_max_minutes: int
    max_slippage_bps: int
    max_unhedged_seconds: int
    max_daily_loss_usd: float
    paper_time_scale: float
    enable_live_trading: bool
    live_trading_ack: str
    arcus_api_url: str
    arcus_address: str
    arcus_account_index: int
    arcus_api_key: str
    arcus_api_private_key: str
    lighter_api_url: str
    lighter_chain_id: int
    lighter_account_index: int | None
    lighter_api_key_index: int
    lighter_api_private_key: str
    database_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        requested = tuple(
            item.strip().upper()
            for item in os.getenv("BOT_MARKETS", ",".join(SUPPORTED_MARKETS)).split(",")
            if item.strip()
        )
        unknown = sorted(set(requested) - set(SUPPORTED_MARKETS))
        if unknown:
            raise ValueError(f"Unsupported markets: {', '.join(unknown)}")
        account_raw = os.getenv("LIGHTER_ACCOUNT_INDEX", "").strip()
        lighter_api_url = os.getenv("LIGHTER_API_URL", "https://api.rh.lighter.xyz").rstrip("/")
        default_lighter_chain_id = 466324 if "api.rh.lighter.xyz" in lighter_api_url else 300
        settings = cls(
            mode=os.getenv("BOT_MODE", "paper").strip().lower(),
            auto_start=_bool("BOT_AUTO_START", True),
            markets=requested,
            min_notional_usd=_float("BOT_MIN_NOTIONAL_USD", 75),
            max_notional_usd=_float("BOT_MAX_NOTIONAL_USD", 225),
            order_timeout_seconds=_int("BOT_ORDER_TIMEOUT_SECONDS", 60),
            order_poll_interval_seconds=_float("BOT_ORDER_POLL_INTERVAL_SECONDS", 0.5),
            hold_min_minutes=_int("BOT_HOLD_MIN_MINUTES", 20),
            hold_max_minutes=_int("BOT_HOLD_MAX_MINUTES", 40),
            cycle_pause_min_minutes=_int("BOT_CYCLE_PAUSE_MIN_MINUTES", 5),
            cycle_pause_max_minutes=_int("BOT_CYCLE_PAUSE_MAX_MINUTES", 10),
            max_slippage_bps=_int("BOT_MAX_SLIPPAGE_BPS", 15),
            max_unhedged_seconds=_int("BOT_MAX_UNHEDGED_SECONDS", 8),
            max_daily_loss_usd=_float("BOT_MAX_DAILY_LOSS_USD", 35),
            paper_time_scale=_float("BOT_PAPER_TIME_SCALE", 60),
            enable_live_trading=_bool("ENABLE_LIVE_TRADING", False),
            live_trading_ack=os.getenv("LIVE_TRADING_ACK", ""),
            arcus_api_url=os.getenv("ARCUS_API_URL", "https://api.arcus.xyz").rstrip("/"),
            arcus_address=os.getenv("ARCUS_ADDRESS", "").strip(),
            arcus_account_index=_int("ARCUS_ACCOUNT_INDEX", 0),
            arcus_api_key=os.getenv("ARCUS_API_KEY", "").strip(),
            arcus_api_private_key=os.getenv("ARCUS_API_PRIVATE_KEY", "").strip(),
            lighter_api_url=lighter_api_url,
            lighter_chain_id=_int("LIGHTER_CHAIN_ID", default_lighter_chain_id),
            lighter_account_index=int(account_raw) if account_raw else None,
            lighter_api_key_index=_int("LIGHTER_API_KEY_INDEX", 0),
            lighter_api_private_key=os.getenv("LIGHTER_API_PRIVATE_KEY", "").strip(),
            database_path=os.getenv("BOT_DATABASE_PATH", "data/bot.sqlite3"),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.mode not in {"paper", "live"}:
            raise ValueError("BOT_MODE must be paper or live")
        if self.min_notional_usd <= 0 or self.max_notional_usd < self.min_notional_usd:
            raise ValueError("Invalid notional range")
        if self.hold_max_minutes < self.hold_min_minutes:
            raise ValueError("Invalid hold-time range")
        if self.cycle_pause_max_minutes < self.cycle_pause_min_minutes:
            raise ValueError("Invalid cycle-pause range")
        if self.order_timeout_seconds < 2:
            raise ValueError("Order timeout must be at least 2 seconds")
        if self.order_poll_interval_seconds < 0.25:
            raise ValueError("Order poll interval must be at least 0.25 seconds")
        if self.mode == "live":
            missing = [
                name
                for name, value in {
                    "ARCUS_ADDRESS": self.arcus_address,
                    "ARCUS_API_KEY": self.arcus_api_key,
                    "ARCUS_API_PRIVATE_KEY": self.arcus_api_private_key,
                    "LIGHTER_ACCOUNT_INDEX": self.lighter_account_index,
                    "LIGHTER_API_PRIVATE_KEY": self.lighter_api_private_key,
                }.items()
                if value in {None, ""}
            ]
            if not self.enable_live_trading or self.live_trading_ack != LIVE_ACK:
                raise ValueError("Live mode is locked; set both live-trading safeguards")
            if missing:
                raise ValueError(f"Missing live credentials: {', '.join(missing)}")

    def public_dict(self) -> dict[str, object]:
        data = asdict(self)
        for key in list(data):
            if "private_key" in key or key in {"arcus_api_key", "live_trading_ack"}:
                data.pop(key)
        data["live_unlocked"] = (
            self.enable_live_trading and self.live_trading_ack == LIVE_ACK
        )
        return data

    def scaled_seconds(self, seconds: float) -> float:
        return seconds / self.paper_time_scale if self.mode == "paper" else seconds
