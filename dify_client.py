"""Dify API 客户端（聊天助手 / 文本生成 / 工作流）。"""

from __future__ import annotations

import json
import re

import requests

APP_CHAT = "chat"
APP_COMPLETION = "completion"
APP_WORKFLOW = "workflow"


class DifyError(RuntimeError):
    pass


class DifyClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        app_type: str = APP_CHAT,
        user: str = "zto-kefu-bot",
        timeout: int = 120,
    ):
        self.base_url = self._normalize_base(base_url)
        self.api_key = (api_key or "").strip()
        self.app_type = app_type or APP_CHAT
        self.user = (user or "").strip() or "zto-kefu-bot"
        self.timeout = timeout
        self.session = requests.Session()

    @staticmethod
    def _normalize_base(url: str) -> str:
        url = (url or "").strip().rstrip("/")
        if not url:
            raise DifyError("请填写 Dify Base URL，例如 https://api.dify.ai/v1")
        if not re.search(r"/v\d+$", url):
            url += "/v1"
        return url

    @property
    def headers(self) -> dict:
        if not self.api_key:
            raise DifyError("请填写 Dify API Key")
        return {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}

    def _raise_for_status(self, resp: requests.Response) -> None:
        if resp.status_code < 400:
            return
        detail = ""
        try:
            data = resp.json()
            detail = data.get("message") or data.get("error") or json.dumps(data, ensure_ascii=False)
        except ValueError:
            detail = (resp.text or "")[:300]
        hints = {
            401: "API Key 无效或未授权",
            403: "当前 Key 无接口权限",
            404: "接口不存在，请检查 Base URL 与应用类型是否匹配",
        }
        raise DifyError(f"Dify 请求失败（HTTP {resp.status_code}）：{detail or hints.get(resp.status_code, '')}")

    def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            resp = self.session.request(
                method,
                self.base_url + path,
                headers=self.headers,
                timeout=kwargs.pop("timeout", self.timeout),
                **kwargs,
            )
        except requests.RequestException as exc:
            raise DifyError(f"无法连接 Dify：{exc}") from exc
        self._raise_for_status(resp)
        try:
            return resp.json()
        except ValueError as exc:
            raise DifyError("Dify 返回内容不是合法 JSON") from exc

    def test_connection(self) -> str:
        data = self._request("GET", "/parameters", params={"user": self.user}, timeout=20)
        name = data.get("app_name") if isinstance(data.get("app_name"), str) else ""
        return name or "连接成功"

    def generate(self, query: str, inputs: dict | None = None, conversation_id: str | None = None) -> tuple[str, str | None]:
        """调用 Dify 生成一条回复，返回 (文本, conversation_id)。"""
        inputs = inputs or {}
        if self.app_type == APP_COMPLETION:
            payload = {
                "inputs": {**inputs, "query": query},
                "response_mode": "blocking",
                "user": self.user,
            }
            data = self._request("POST", "/completion-messages", json=payload)
            return (data.get("answer") or "").strip(), conversation_id

        if self.app_type == APP_WORKFLOW:
            data = self._request(
                "POST",
                "/workflows/run",
                json={"inputs": inputs, "response_mode": "blocking", "user": self.user},
            )
            outputs = ((data.get("data") or {}).get("outputs")) or {}
            return self._stringify_outputs(outputs), conversation_id

        payload = {
            "inputs": inputs,
            "query": query,
            "response_mode": "blocking",
            "user": self.user,
        }
        if conversation_id:
            payload["conversation_id"] = conversation_id
        data = self._request("POST", "/chat-messages", json=payload)
        answer = (data.get("answer") or "").strip()
        new_conversation = data.get("conversation_id") or conversation_id
        return answer, new_conversation

    @staticmethod
    def _stringify_outputs(outputs: dict) -> str:
        if not isinstance(outputs, dict):
            return str(outputs).strip()
        for key in ("text", "answer", "output", "result", "content"):
            if key in outputs and isinstance(outputs[key], str):
                return outputs[key].strip()
        parts = [v for v in outputs.values() if isinstance(v, str) and v.strip()]
        if parts:
            return "\n".join(parts).strip()
        return json.dumps(outputs, ensure_ascii=False)
