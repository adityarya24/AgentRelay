"""AgentRelay Telegram plugin."""
from .models import TelegramBotRegistration, TelegramGroupRegistration

__all__ = [
    "TelegramBotRegistration",
    "TelegramGroupRegistration",
    "TelegramBot",
    "TelegramSupervisorRunner",
    "supervisor_from_env",
    "TelegramBotRunner",
    "TelegramClient",
    "TelegramHTTPClient",
    "TelegramTransport",
]


def __getattr__(name: str):
    if name == "TelegramBot":
        from .telegram import TelegramBot
        return TelegramBot
    if name in ("TelegramSupervisorRunner", "supervisor_from_env"):
        from . import telegram_supervisor
        return getattr(telegram_supervisor, name)
    if name in ("TelegramBotRunner", "TelegramClient", "TelegramHTTPClient", "TelegramTransport"):
        from . import telegram_transport
        return getattr(telegram_transport, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
