"""Slack 봇 (Socket Mode). 질의와 승인은 모두 API(`/query`, `/resume`)를 거친다.

- 봇 멘션과 DM으로 질의한다. Slack 스레드의 ts를 thread_id로 써서 같은 스레드의 대화를 이어간다.
- 클러스터 변경이 필요하면 스레드에 승인 요청과 [승인] [수정] [거절] 버튼을 올린다.
- 승인 권한은 `config/policy.yaml`의 승인자 Slack User ID로 판정한다 (API도 다시 검증한다).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

import httpx
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

from src.config import get_policy, get_settings, load_env

log = logging.getLogger("slack_app")

_MENTION_RE = re.compile(r"<@[A-Z0-9]+>")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_HEADER_RE = re.compile(r"^#{1,6}\s+(.*)$", re.M)
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_SECTION_LIMIT = 2900  # Slack section 블록 텍스트 한도(3000자) 안쪽


def to_mrkdwn(text: str) -> str:
    """Markdown 응답을 Slack mrkdwn으로 바꾼다. 굵게, 제목을 변환하고 표는 코드 블록으로 감싼다."""
    text = _BOLD_RE.sub(r"*\1*", text)
    text = _HEADER_RE.sub(r"*\1*", text)
    out: list[str] = []
    in_table = False
    for line in text.splitlines():
        is_row = line.strip().startswith("|") and line.strip().endswith("|")
        if is_row and not in_table:
            out.append("```")
            in_table = True
        elif not is_row and in_table:
            out.append("```")
            in_table = False
        if is_row and _TABLE_SEP_RE.match(line):
            continue
        out.append(line)
    if in_table:
        out.append("```")
    return "\n".join(out)


def chunk_text(text: str, limit: int) -> list[str]:
    """긴 메시지를 줄 단위로 `limit`자 이하로 나눈다."""
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit and current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current += line
    if current:
        chunks.append(current)
    return chunks or [""]


def approval_blocks(answer: str, thread_id: str) -> list[dict[str, Any]]:
    """승인 요청 메시지 블록(요약 + 승인·수정·거절 버튼)을 만든다."""
    def button(action: str, label: str, style: str | None = None) -> dict[str, Any]:
        b: dict[str, Any] = {"type": "button", "action_id": action, "value": thread_id,
                             "text": {"type": "plain_text", "text": label}}
        if style:
            b["style"] = style
        return b

    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": to_mrkdwn(answer)[:_SECTION_LIMIT]}},
        {"type": "actions", "block_id": "approval_actions",
         "elements": [button("approve", "승인", "primary"), button("edit", "수정"), button("reject", "거절", "danger")]},
    ]


class CopilotBot:
    """API 호출과 Slack 메시지 처리를 묶은 봇."""

    def __init__(self, app: AsyncApp) -> None:
        self.app = app
        s = get_settings()
        self.api = httpx.AsyncClient(
            base_url=s.slack.api_base_url,
            timeout=s.limits.request_timeout_sec + s.verification.initial_delay_sec + s.verification.max_wait_sec + 60,
        )
        self.max_chars = s.slack.max_message_chars
        app.event("app_mention")(self.on_mention)
        app.event("message")(self.on_message)
        app.action("approve")(self.on_approve)
        app.action("reject")(self.on_reject)
        app.action("edit")(self.on_edit_click)
        app.view("edit_submit")(self.on_edit_submit)

    # ------------------------------------------------------------ 질의

    async def on_mention(self, event: dict[str, Any], say: Any, client: Any) -> None:
        """채널에서 봇을 멘션하면 질의로 처리한다."""
        await self.handle_question(event, client)

    async def on_message(self, event: dict[str, Any], client: Any) -> None:
        """DM으로 온 메시지를 질의로 처리한다. 봇 자신의 메시지와 수정·삭제 이벤트는 무시한다."""
        if event.get("channel_type") != "im" or event.get("bot_id") or event.get("subtype"):
            return
        await self.handle_question(event, client)

    async def handle_question(self, event: dict[str, Any], client: Any) -> None:
        """질문을 API로 보내고 응답을 같은 스레드에 올린다."""
        channel = event["channel"]
        thread_ts = event.get("thread_ts") or event["ts"]
        question = _MENTION_RE.sub("", event.get("text", "")).strip()
        if not question:
            await self.post(client, channel, thread_ts, "증상을 알려 주세요. 예: `recommendationservice 파드가 계속 재시작돼`")
            return
        await self.post(client, channel, thread_ts, "조사 중입니다. 잠시만 기다려 주세요.")
        try:
            resp = await self.api.post("/query", json={"question": question, "thread_id": thread_ts})
            resp.raise_for_status()
            await self.reply(client, channel, thread_ts, resp.json())
        except Exception as e:
            log.exception("query failed")
            await self.post(client, channel, thread_ts, f"처리 중 오류가 발생했습니다 ({type(e).__name__}). 잠시 후 다시 시도해 주세요.")

    async def reply(self, client: Any, channel: str, thread_ts: str, data: dict[str, Any]) -> None:
        """API 응답을 올린다. 승인 대기 중이면 버튼이 있는 승인 요청 메시지를 올린다."""
        if data.get("approval"):
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts, text="승인 요청",
                blocks=approval_blocks(data["answer"], thread_ts),
            )
            return
        await self.post(client, channel, thread_ts, data["answer"])

    async def post(self, client: Any, channel: str, thread_ts: str, text: str) -> None:
        """텍스트를 mrkdwn으로 바꿔 스레드에 올린다. 길면 나눠서 올린다."""
        for chunk in chunk_text(to_mrkdwn(text), self.max_chars):
            await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=chunk)

    # ------------------------------------------------------------ 승인

    @staticmethod
    def is_approver(user_id: str) -> bool:
        """등록된 승인자인지 확인한다."""
        return user_id in get_policy().approval.user_ids

    async def deny(self, client: Any, body: dict[str, Any]) -> None:
        """승인 권한이 없는 사용자에게 본인에게만 보이는 안내를 보낸다."""
        await client.chat_postEphemeral(
            channel=body["channel"]["id"], user=body["user"]["id"], thread_ts=body["message"].get("thread_ts"),
            text="승인 권한이 없는 사용자입니다. 등록된 승인자(sre-approvers)만 처리할 수 있습니다.",
        )

    async def resolve(self, client: Any, body: dict[str, Any], decision: str, params: dict[str, Any]) -> None:
        """승인·수정·거절 결정을 API로 전달하고 결과를 스레드에 올린다."""
        channel = body["channel"]["id"]
        message_ts = body["message"]["ts"]
        thread_id = body["actions"][0]["value"]
        user = body["user"]["id"]
        label = {"approve": "승인", "edit": "수정 후 승인", "reject": "거절"}[decision]
        # 중복 클릭을 막기 위해 버튼을 치우고 처리 중임을 표시한다
        await client.chat_update(channel=channel, ts=message_ts, text=f"{label} 처리 중", blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<@{user}>님이 *{label}*했습니다. 처리 중입니다."
                                          + (" 실행 후 정상화를 검증하는 데 최대 5분 걸립니다." if decision != "reject" else "")}}])
        await self.send_resume(client, channel, thread_id, decision, params, user)

    async def send_resume(self, client: Any, channel: str, thread_id: str, decision: str, params: dict[str, Any], user: str) -> None:
        """`/resume`을 호출하고 응답을 올린다."""
        try:
            resp = await self.api.post("/resume", json={"thread_id": thread_id, "decision": decision, "params": params, "approver": user})
            if resp.status_code == 403:
                await self.post(client, channel, thread_id, "승인 권한이 없어 처리하지 않았습니다.")
            elif resp.status_code == 409:
                await self.post(client, channel, thread_id, "이미 처리되었거나 승인 대기가 없는 요청입니다.")
            else:
                resp.raise_for_status()
                await self.reply(client, channel, thread_id, resp.json())
        except Exception as e:
            log.exception("resume failed")
            await self.post(client, channel, thread_id, f"승인 처리 중 오류가 발생했습니다 ({type(e).__name__}).")

    async def on_approve(self, ack: Any, body: dict[str, Any], client: Any) -> None:
        """[승인] 버튼."""
        await ack()
        if not self.is_approver(body["user"]["id"]):
            return await self.deny(client, body)
        await self.resolve(client, body, "approve", {})

    async def on_reject(self, ack: Any, body: dict[str, Any], client: Any) -> None:
        """[거절] 버튼."""
        await ack()
        if not self.is_approver(body["user"]["id"]):
            return await self.deny(client, body)
        await self.resolve(client, body, "reject", {})

    async def on_edit_click(self, ack: Any, body: dict[str, Any], client: Any) -> None:
        """[수정] 버튼. 파라미터를 JSON으로 고칠 수 있는 모달을 연다."""
        await ack()
        if not self.is_approver(body["user"]["id"]):
            return await self.deny(client, body)
        thread_id = body["actions"][0]["value"]
        default = json.dumps(self._params_from_message(body), ensure_ascii=False, indent=1)
        await client.views_open(trigger_id=body["trigger_id"], view={
            "type": "modal", "callback_id": "edit_submit", "title": {"type": "plain_text", "text": "조치 파라미터 수정"},
            "submit": {"type": "plain_text", "text": "수정 후 승인"}, "close": {"type": "plain_text", "text": "취소"},
            "private_metadata": json.dumps({"thread_id": thread_id, "channel": body["channel"]["id"], "message_ts": body["message"]["ts"]}),
            "blocks": [{"type": "input", "block_id": "params", "label": {"type": "plain_text", "text": "params (JSON)"},
                        "element": {"type": "plain_text_input", "action_id": "value", "multiline": True, "initial_value": default}}],
        })

    @staticmethod
    def _params_from_message(body: dict[str, Any]) -> dict[str, Any]:
        """승인 요청 메시지 본문의 `조치: <유형> {JSON}`에서 파라미터를 꺼낸다. 실패하면 빈 객체."""
        text = body["message"]["blocks"][0]["text"]["text"] if body["message"].get("blocks") else ""
        m = re.search(r"조치:\s*\S+\s*(\{.*\})", text)
        try:
            return json.loads(m.group(1)) if m else {}
        except ValueError:
            return {}

    async def on_edit_submit(self, ack: Any, body: dict[str, Any], view: dict[str, Any], client: Any) -> None:
        """수정 모달 제출. JSON이 올바르면 수정한 값으로 승인 처리한다."""
        raw = view["state"]["values"]["params"]["value"]["value"]
        try:
            params = json.loads(raw)
            if not isinstance(params, dict):
                raise ValueError
        except ValueError:
            await ack(response_action="errors", errors={"params": "JSON 객체 형식이어야 합니다"})
            return
        await ack()
        meta = json.loads(view["private_metadata"])
        user = body["user"]["id"]
        if not self.is_approver(user):
            await client.chat_postEphemeral(channel=meta["channel"], user=user, thread_ts=meta["thread_id"], text="승인 권한이 없는 사용자입니다.")
            return
        await client.chat_update(channel=meta["channel"], ts=meta["message_ts"], text="수정 후 승인 처리 중", blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": f"<@{user}>님이 *수정 후 승인*했습니다 (`{json.dumps(params, ensure_ascii=False)}`). 처리 중입니다."}}])
        await self.send_resume(client, meta["channel"], meta["thread_id"], "edit", params, user)


async def main() -> None:
    """Socket Mode로 Slack에 연결한다. API 서버(uvicorn src.api:app)가 먼저 떠 있어야 한다."""
    load_env()
    logging.basicConfig(level=logging.INFO)
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    CopilotBot(app)
    await AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start_async()


if __name__ == "__main__":
    asyncio.run(main())
