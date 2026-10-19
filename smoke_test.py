"""Week 1 API smoke test. Run: python smoke_test.py"""

import json
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
os.chdir(BASE_DIR)
sys.path.insert(0, str(BASE_DIR))

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def check(condition, label):
    if not condition:
        raise AssertionError(label)
    print(f"PASS: {label}")


def message_pairs(messages):
    return [(item["role"], item["content"], item["seq"]) for item in messages]


async def fake_event_stream(messages):
    main.fake_calls.append(messages)
    reply = f"reply-{messages[-1]['content']}"
    yield f"data: {json.dumps({'delta': reply}, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def main_test():
    main.fake_calls = []
    main.event_stream = fake_event_stream
    client = TestClient(main.app)
    created_ids = []
    try:
        page = client.get("/")
        check(page.status_code == 200 and "Chat API 调试台" in page.text, "HTML 调试页可访问")

        docs = client.get("/docs")
        check(docs.status_code == 200, "Swagger /docs 可访问")

        session_a = client.post("/sessions", json={"title": "smoke-A"}).json()["id"]
        created_ids.append(session_a)
        session_b = client.post("/sessions", json={"title": "smoke-B"}).json()["id"]
        created_ids.append(session_b)

        sessions = client.get("/sessions").json()
        check(
            {session_a, session_b}.issubset({item["id"] for item in sessions}),
            "GET /sessions 返回新建会话",
        )

        empty = client.get(f"/sessions/{session_a}/messages")
        check(empty.status_code == 200 and empty.json() == [], "空会话返回空历史")
        check(
            client.delete(f"/sessions/999999").status_code == 404,
            "删除不存在的会话返回 404",
        )

        stream_a = client.post(
            f"/sessions/{session_a}/chat",
            json={"content": "测试"},
        )
        check(stream_a.status_code == 200, "POST chat 返回 200")
        check(
            stream_a.headers["content-type"].startswith("text/event-stream"),
            "chat 返回 SSE Content-Type",
        )
        check("data: [DONE]" in stream_a.text, "SSE 包含 DONE 结束标记")

        history_a = client.get(f"/sessions/{session_a}/messages").json()
        check(
            message_pairs(history_a)
            == [
                ("user", "测试", 1),
                ("assistant", "reply-测试", 2),
            ],
            "用户消息与完整 AI 回复按 seq 落库",
        )

        client.post(f"/sessions/{session_b}/chat", json={"content": "另一个会话"})
        history_b = client.get(f"/sessions/{session_b}/messages").json()
        check(
            [item["content"] for item in history_b]
            == ["另一个会话", "reply-另一个会话"],
            "会话 B 不会混入会话 A 的历史",
        )

        client.post(f"/sessions/{session_a}/chat", json={"content": "继续"})
        check(
            [item["content"] for item in main.fake_calls[-1]]
            == ["测试", "reply-测试", "继续"],
            "会话 A 的上下文只来自会话 A",
        )

        session_c = client.post("/sessions", json={"title": "smoke-C"}).json()["id"]
        created_ids.append(session_c)
        db = main.SessionLocal()
        try:
            for seq in range(1, 26):
                main.create_message(db, session_c, "user", f"m{seq}", seq)
        finally:
            db.close()

        client.post(f"/sessions/{session_c}/chat", json={"content": "当前"})
        check(
            len(main.fake_calls[-1]) == 21
            and main.fake_calls[-1][0]["content"] == "m6"
            and main.fake_calls[-1][-1]["content"] == "当前",
            "模型上下文只取最近 20 条并追加当前消息",
        )

        history_c = client.get(f"/sessions/{session_c}/messages").json()
        check(
            history_c[-2]["seq"] == 26
            and history_c[-2]["content"] == "当前"
            and history_c[-1]["seq"] == 27
            and history_c[-1]["content"] == "reply-当前",
            "超过 20 条后 seq 仍连续递增",
        )

        deleted = client.delete(f"/sessions/{session_a}")
        check(deleted.status_code == 200, "DELETE /sessions/{id} 删除成功")
        check(
            client.get(f"/sessions/{session_a}/messages").status_code == 404,
            "删除后历史接口返回 404",
        )

        db = main.SessionLocal()
        try:
            leftover = (
                db.query(main.Message)
                .filter(main.Message.session_id == session_a)
                .count()
            )
        finally:
            db.close()
        check(leftover == 0, "删除会话级联删除消息")

    finally:
        for session_id in created_ids:
            try:
                client.delete(f"/sessions/{session_id}")
            except Exception:
                pass

    print("ALL_SMOKE_TESTS_PASSED")


if __name__ == "__main__":
    main_test()
