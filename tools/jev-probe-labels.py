"""一次性标签措辞探针（评测A 批，E1 接线前置）：两种英文措辞 × 6 条金标查询。

产物写 stdout + flush（前台跑，无管道缓冲）；结论只用于冻结 jev 标签族，不入库。
"""

import os
import sys

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

import jieba  # noqa: E402
from gliner import GLiNER  # noqa: E402


def zh(t: str) -> str:
    return " ".join(w for w in jieba.cut(t) if w.strip())

NOUN = {  # 名词短语措辞
 "ask_user": "user clarification question", "chat_answer": "direct answer",
 "file_edit": "file editing", "file_glob": "file name search",
 "file_grep": "file content search", "file_read": "file reading",
 "file_write": "file writing", "run_terminal": "terminal command",
 "spill_get": "tool result retrieval", "subagent_interrupt": "subagent interruption",
 "subagent_spawn": "subagent spawn", "subagent_wait": "subagent wait",
 "todo_read": "todo list reading", "todo_update": "todo update",
 "todo_write": "todo creation", "web_fetch": "web page fetch", "web_search": "web search",
}
VERB = {  # 动宾措辞
 "ask_user": "ask the user a question", "chat_answer": "answer a question",
 "file_edit": "edit a file", "file_glob": "find files by name",
 "file_grep": "search file contents", "file_read": "read a file",
 "file_write": "write a file", "run_terminal": "run a shell command",
 "spill_get": "fetch a stored result", "subagent_interrupt": "stop a subagent",
 "subagent_spawn": "create a subagent", "subagent_wait": "wait for a subagent",
 "todo_read": "show the todo list", "todo_update": "update a todo",
 "todo_write": "add a todo", "web_fetch": "open a web page", "web_search": "search the web",
}
QUERIES = [  # 金标期望：file_read / todo_write / web_search / out_of_scope / clarify / file_grep
 ("帮我读一下 README.md 的内容", "file_read"),
 ("新建 notes/meeting.txt，内容是「周三例会纪要」", "file_write"),
 ("搜一下 GLiNER 的 GitHub 仓库地址", "web_search"),
 ("今天上证指数多少", "out_of_scope"),
 ("这个需求信息不全，你说怎么办", "clarify/ask_user"),
 ("在 src 里找出所有用到 retry 的地方", "file_grep"),
]

def load_local_first(model_id: str):
    """缓存命中→本地路径直载（local_files_only 全链免 hub）；未命中→镜像在线下载后载。"""
    from huggingface_hub import snapshot_download
    try:
        local = snapshot_download(model_id, local_files_only=True)
        print(f"[load] 缓存命中: {local}", flush=True)
        return GLiNER.from_pretrained(local, local_files_only=True)
    except Exception as exc:  # noqa: BLE001 ——未缓存：镜像下载（首跑一次性成本）
        print(f"[load] 缓存未命中（{type(exc).__name__}），走镜像下载", flush=True)
        return GLiNER.from_pretrained(model_id)


def main() -> int:
    model = load_local_first("urchade/gliner_multi-v2.1").to("cuda").eval()
    for name, fam in (("noun", NOUN), ("verb", VERB), ("demo5", None)):
        if fam is None:
            fam = {"weather query": "weather query", "meeting scheduling": "meeting scheduling",
                   "knowledge base search": "knowledge base search", "code search": "code search",
                   "casual chat": "casual chat"}
        labels = list(fam.values())
        inv = {v: k for k, v in fam.items()}
        for q, expect in QUERIES:
            text = f"Classify the user request into one of the action categories. Request: {zh(q)}"
            hits = model.predict_entities(text, labels, threshold=0.0)
            hits.sort(key=lambda e: -e["score"])
            top = [
                (inv.get(h["label"], h["label"]), round(h["score"], 2), h["text"].replace(" ", "")[:12])
                for h in hits[:3]
            ]
            print(f"{name:6} expect={expect:18} -> {top}", flush=True)
    return 0

if __name__ == "__main__":
    sys.exit(main())
