"""cli.sse 单测（帧格式权威=docs/api/02 §2；主干波 11 事件渲染=§3；全程内存字节流，禁真连）。"""

from __future__ import annotations

import io

from cli.sse import SseFrameParser, TrunkEventRenderer, iter_frames

TWO_FRAMES = (
    b"id: 1740\n"
    b"event: RUN_STARTED\n"
    b'data: {"run_id":"r_01","session_id":"s_01","task_id":"t_01"}\n\n'
    b"id: 1741\n"
    b"event: TEXT_MESSAGE_START\n"
    b'data: {"message_id":"m_01"}\n\n'
)


# ================================================================ 帧解析


def test_帧解析_标准三行帧解出id_event_data():
    # Act
    frames = iter_frames([TWO_FRAMES])
    # Assert
    assert len(frames) == 2
    assert frames[0].id == "1740"
    assert frames[0].event == "RUN_STARTED"
    assert '"run_id":"r_01"' in frames[0].data
    assert frames[1].event == "TEXT_MESSAGE_START"


def test_帧解析_字节任意分片到达仍完整成帧():
    # Arrange：模拟网络 7 字节乱刀分片（增量解析器不得丢帧错帧）
    chunks = [TWO_FRAMES[i : i + 7] for i in range(0, len(TWO_FRAMES), 7)]
    # Act
    frames = iter_frames(chunks)
    # Assert
    assert [f.event for f in frames] == ["RUN_STARTED", "TEXT_MESSAGE_START"]


def test_帧解析_心跳注释帧不产出事件():
    # Act
    frames = iter_frames([b": ping\n\n", TWO_FRAMES])
    # Assert：心跳不计入事件序列（api/02 §2）
    assert len(frames) == 2


def test_帧解析_CRLF与多data行容忍():
    # Arrange：CRLF 行结束 + 一个块内两行 data（按 \n 拼接）
    raw = b"id: 1\r\nevent: A\r\ndata: line1\r\ndata: line2\r\n\r\n"
    # Act
    frames = iter_frames([raw])
    # Assert
    assert len(frames) == 1
    assert frames[0].data == "line1\nline2"


def test_帧解析_流末未完结帧宽松收口():
    # Arrange：服务端未以空行收尾的尾帧
    parser = SseFrameParser()
    parser.feed(b"event: RUN_FINISHED\n")
    # Act
    tail = parser.flush()
    # Assert
    assert len(tail) == 1
    assert tail[0].event == "RUN_FINISHED"


# ================================================================ 主干波渲染


def _render(frames_bytes: bytes) -> tuple[TrunkEventRenderer, str, str]:
    out, err = io.StringIO(), io.StringIO()
    renderer = TrunkEventRenderer(out, err)
    for frame in iter_frames([frames_bytes]):
        renderer.render(frame)
    renderer.finish()
    return renderer, out.getvalue(), err.getvalue()


def test_渲染_TEXT增量即打并在END补换行():
    # Arrange：TEXT_MESSAGE_START/CONTENT*2/END 主干波序列
    raw = (
        b'id: 1\nevent: TEXT_MESSAGE_START\ndata: {"message_id":"m1"}\n\n'
        b'id: 2\nevent: TEXT_MESSAGE_CONTENT\ndata: {"message_id":"m1","delta":"\\u4f60\\u597d"}\n\n'
        b'id: 3\nevent: TEXT_MESSAGE_CONTENT\ndata: {"message_id":"m1","delta":"\\u672c\\u4f53"}\n\n'
        b'id: 4\nevent: TEXT_MESSAGE_END\ndata: {"message_id":"m1","finish_reason":"stop"}\n\n'
    )
    # Act
    _renderer, out, _err = _render(raw)
    # Assert：增量即打拼接为整句，END 补换行
    assert out == "你好本体\n"


def test_渲染_工具四联_参数聚合一次输出与结果摘要():
    # Arrange：TOOL_CALL_START → ARGS*2 → END → RESULT 完整工具循环
    raw = (
        b'id: 1\nevent: TOOL_CALL_START\ndata: {"tool_call_id":"tc1","tool_name":"knowledge.search"}\n\n'
        b'id: 2\nevent: TOOL_CALL_ARGS\ndata: {"tool_call_id":"tc1","delta":"{\\"query\\":"}\n\n'
        b'id: 3\nevent: TOOL_CALL_ARGS\ndata: {"tool_call_id":"tc1","delta":"\\"\\u505c\\u7535\\"}"}\n\n'
        b'id: 4\nevent: TOOL_CALL_END\ndata: {"tool_call_id":"tc1"}\n\n'
        b'id: 5\nevent: TOOL_CALL_RESULT\ndata: {"tool_call_id":"tc1","ok":true,'
        b'"summary":"\\u68c0\\u7d22\\u5230 8 \\u4e2a\\u5207\\u7247","cost_ms":612}\n\n'
    )
    # Act
    _renderer, out, _err = _render(raw)
    # Assert：参数只在 END 聚合输出一次（不逐增量刷屏），RESULT 展示 summary 与耗时
    assert out.count("query") == 1
    assert "knowledge.search" in out
    assert "检索到 8 个切片" in out
    assert "612ms" in out


def test_渲染_证据事件折叠为计数并标注降级():
    # Arrange：RETRIEVAL_EVIDENCE（citations 全字段但只渲染计数）
    raw = (
        b'id: 1\nevent: RETRIEVAL_EVIDENCE\ndata: {"chunks":[{"chunk_id":"c1"},{"chunk_id":"c2"}],'
        b'"graph_paths":[{"nodes":["A","B"]}],"citations":[{"doc_id":"d1"},{"doc_id":"d2"},{"doc_id":"d3"}],'
        b'"degraded":true}\n\n'
    )
    # Act
    _renderer, out, _err = _render(raw)
    # Assert：折叠引用数 + degraded 显式标注（api/02 §3 degraded=true 须标注证据链暂缺）
    assert "切片 2" in out
    assert "图谱路径 1" in out
    assert "引用 3 条" in out
    assert "证据链暂缺" in out


def test_渲染_RUN_FINISHED显示usage():
    # Arrange
    raw = b'id: 1\nevent: RUN_FINISHED\ndata: {"run_id":"r1","usage":{"tokens":123,"cost":0.02}}\n\n'
    # Act
    _renderer, out, _err = _render(raw)
    # Assert
    assert "123" in out
    assert "0.02" in out


def test_渲染_RUN_STARTED输出run与task():
    # Act
    _renderer, out, _err = _render(TWO_FRAMES)
    # Assert
    assert "r_01" in out
    assert "t_01" in out


def test_渲染_RUN_ERROR写入stderr并置run_error():
    # Arrange
    raw = (
        b'id: 1\nevent: RUN_ERROR\ndata: {"run_id":"r1","code":5001,'
        b'"message":"\\u6a21\\u578b\\u8d85\\u65f6","retryable":true}\n\n'
    )
    # Act
    renderer, out, err = _render(raw)
    # Assert：错误走 stderr（含 code/message/可重试提示），渲染器记录终态
    assert out == ""
    assert "5001" in err
    assert "模型超时" in err
    assert "（可重试）" in err
    assert renderer.run_error is not None
    assert renderer.run_error["code"] == 5001


def test_渲染_未知与扩展波事件静默忽略():
    # Arrange：GATE_VERDICT/STATE_DELTA 属 M4+ 扩展波，未知事件一律忽略（api/02 §7 规则 2）
    raw = (
        b'id: 1\nevent: GATE_VERDICT\ndata: {"ok":false,"violations":[]}\n\n'
        b'id: 2\nevent: STATE_DELTA\ndata: {"patch":[]}\n\n'
        b'id: 3\nevent: MESSAGE_BRANCH_CREATED\ndata: {"x":1}\n\n'
    )
    # Act
    _renderer, out, _err = _render(raw)
    # Assert
    assert out == ""
