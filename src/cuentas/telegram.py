"""Cliente mínimo de la Bot API de Telegram (sin dependencias externas)."""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any

from . import config

log = logging.getLogger(__name__)
API = "https://api.telegram.org"


class TelegramError(RuntimeError):
    pass


class Telegram:
    def __init__(self, token: str | None = None):
        self.token = token or config.bot_token()

    def call(self, method: str, **params: Any) -> Any:
        body = json.dumps({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(f"{API}/bot{self.token}/{method}", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as exc:
            data = json.load(exc)
        if not data.get("ok"):
            raise TelegramError(f"{method}: {data.get('description')}")
        return data["result"]

    def send(self, chat_id: int, text: str, keyboard: list[list[dict[str, Any]]] | None = None,
             reply_to: int | None = None) -> Any:
        markup = {"inline_keyboard": keyboard} if keyboard else None
        try:
            return self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                             reply_markup=markup, link_preview_options={"is_disabled": True},
                             reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True}
                             if reply_to else None)
        except TelegramError as exc:
            # Si el modelo generó HTML inválido, reintentamos como texto plano.
            if "parse entities" not in str(exc):
                raise
            log.warning("HTML inválido, reenviando sin formato: %s", exc)
            return self.call("sendMessage", chat_id=chat_id, text=text, reply_markup=markup)

    def edit(self, chat_id: int, message_id: int, text: str,
             keyboard: list[list[dict[str, Any]]] | None = None) -> Any:
        markup = {"inline_keyboard": keyboard} if keyboard else None
        return self.call("editMessageText", chat_id=chat_id, message_id=message_id, text=text,
                         parse_mode="HTML", reply_markup=markup)

    def typing(self, chat_id: int, action: str = "typing") -> None:
        try:
            self.call("sendChatAction", chat_id=chat_id, action=action)
        except TelegramError:
            pass

    def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text)

    def download_file(self, file_id: str) -> bytes:
        info = self.call("getFile", file_id=file_id)
        url = f"{API}/file/bot{self.token}/{info['file_path']}"
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read()

    def set_menu_button(self, chat_id: int, url: str) -> None:
        self.call("setChatMenuButton", chat_id=chat_id,
                  menu_button={"type": "web_app", "text": "Dashboard", "web_app": {"url": url}})


def webapp_button(text: str = "📊 Abrir dashboard") -> list[list[dict[str, Any]]] | None:
    if not config.WEBAPP_URL:
        return None
    return [[{"text": text, "web_app": {"url": config.WEBAPP_URL}}]]
