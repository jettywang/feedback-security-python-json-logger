# -*- coding: utf-8 -*-
"""按行收集场景下 python-json-logger 的实证脚本（全部使用内存流）。"""
from __future__ import annotations
import io, json, logging, sys, traceback

sys.path.insert(0, "src")
from pythonjsonlogger.json import JsonFormatter

SEP = "=" * 78

def make_logger(formatter):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)          # 与生产 FileHandler 同为字符流 + terminator="\n"
    h.setFormatter(formatter)
    lg = logging.getLogger("demo")
    lg.handlers = [h]
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    return lg, buf, h

def show(title, buf, note=""):
    print(SEP)
    print(f"# {title}")
    if note:
        print(note)
    raw_text = buf.getvalue()
    raw_bytes = raw_text.encode("utf-8")
    print("\n[原始字节 repr]")
    for line in raw_bytes.split(b"\n"):
        print("  ", line)
    print("[原始文本]")
    print(raw_text, end="" if raw_text.endswith("\n") else "\n")
    print("[逐行 json.loads 验证]")
    lines = raw_text.splitlines()
    for i, ln in enumerate(lines, 1):
        try:
            obj = json.loads(ln)
            print(f"  line{i}: OK ->", json.dumps(obj, ensure_ascii=False)[:160])
        except Exception as e:
            print(f"  line{i}: FAIL ({type(e).__name__}: {e})")
    print(f"[物理行数 splitlines()={len(lines)}]  [内嵌真实 LF 计数(含终止符)={raw_text.count(chr(10))}]")
    return lines

# ---------------------------------------------------------------- 1. 业务消息：换行/制表符/控制字符/非 ASCII
fmt = JsonFormatter("%(levelname)s %(name)s %(message)s")
lg, buf, h = make_logger(fmt)
lg.info("line1\nline2\ttabbed\r\rcrlf-end\u0000nul\u0007bell 中文/emoji: 你好😀\t结束")
show("1. 默认 JsonFormatter：换行/制表符/CR/NUL/BEL/非 ASCII 全部进 JSON 字符串", buf,
     "默认 json_ensure_ascii=True, json_indent=None")

# ---------------------------------------------------------------- 2. ensure_ascii=False
fmt2 = JsonFormatter("%(levelname)s %(message)s", json_ensure_ascii=False)
lg, buf, h = make_logger(fmt2)
lg.info("你好，世界\n第二行\ttab😀")
lines2 = show("2. json_ensure_ascii=False：非 ASCII 以原始 UTF-8 落盘，控制字符仍被转义", buf)
# 关键断言：原始字节里出现 UTF-8 中文，但物理行仍只有 1 行
raw = buf.getvalue().encode("utf-8")
assert len(raw.split(b"\n")) == 2, raw          # 1 条记录 + 终止换行
assert "你好".encode("utf-8") in raw
assert b"\xe4\xbd\xa0\xe5\xa5\xbd" in raw
print("  断言: 物理行=1 且 UTF-8 中文原样存在 ->", True)

# ---------------------------------------------------------------- 3. 异常 traceback（多行）
lg, buf, h = make_logger(JsonFormatter("%(levelname)s %(message)s"))
try:
    raise RuntimeError("boom\n第二行异常信息")
except RuntimeError:
    lg.error("任务失败", exc_info=True)
lines3 = show("3. exc_info 异常栈：traceback 内大量换行，作为 JSON 字符串被整体转义", buf)
obj = json.loads(lines3[0])
print("[解析后 exc_info 还原出的前 3 行]")
for l in obj["exc_info"].splitlines()[:3]:
    print("   |", l)

# ---------------------------------------------------------------- 4. exc_info_as_array=True
lg, buf, h = make_logger(JsonFormatter("%(levelname)s %(message)s", exc_info_as_array=True))
try:
    raise ValueError("数组化异常")
except ValueError:
    lg.error("任务失败", exc_info=True)
show("4. exc_info_as_array=True：栈帧变成 JSON 数组，换行变成数组元素边界", buf)

# ---------------------------------------------------------------- 5. stack_info
lg, buf, h = make_logger(JsonFormatter("%(levelname)s %(message)s"))
lg.warning("带堆栈的告警", stack_info=True)
show("5. stack_info 同样是多行字符串，同样在 JSON 内部转义", buf)

# ---------------------------------------------------------------- 6. 多条连续记录：一条物理行一条记录
lg, buf, h = make_logger(JsonFormatter("%(levelname)s %(message)s"))
lg.info("第一条\n内含换行")
lg.info({"event": "dict-msg", "detail": "a\tb\nc"})
lg.info("第三条 日本語: テスト")
lines6 = show("6. 连续 3 条记录：收集器按 \\n 切分恰好得到 3 个完整 JSON", buf)
assert len(lines6) == 3
assert all(json.loads(l) for l in lines6)
print("  断言: 3 条记录 == 3 个可解析物理行 ->", True)

# ---------------------------------------------------------------- 7. 风险演示 A：json_indent 打开（formatter 配置错误）
lg, buf, h = make_logger(JsonFormatter("%(levelname)s %(message)s", json_indent=2))
lg.info("缩进输出\n内含换行")
show("7-A. 风险：json_indent=2 时单条记录跨多个物理行，按行收集器全部解析失败", buf)

# ---------------------------------------------------------------- 8. 风险演示 B：prefix 带换行
lg, buf, h = make_logger(JsonFormatter("%(message)s", prefix="NOT-JSON-PREFIX\n"))
lg.info("prefix 注入换行")
show("7-B. 风险：prefix 在 JSON 之外拼接，prefix 中的换行不受 JSON 转义保护", buf)

# ---------------------------------------------------------------- 9. 风险演示 C：formatter 之外 —— print/第三方写同一文件流
class SharedStream(io.StringIO):
    pass
shared = SharedStream()
h = logging.StreamHandler(shared)
h.setFormatter(JsonFormatter("%(levelname)s %(message)s"))
lg = logging.getLogger("mixed"); lg.handlers=[h]; lg.propagate=False; lg.setLevel(logging.DEBUG)
lg.info("结构化记录")
print("某库直接 print 的纯文本\t不含JSON", file=shared)   # 模拟业务/库绕过 logging
shared.write("第三方 C 扩展 write 的半行")                 # 无终止换行
lg.info("又一条结构化记录")
show("7-C. 风险（formatter 之外）：同一目标流混入 print/write 的非 JSON 文本", shared)

# ---------------------------------------------------------------- 10. 风险演示 D：handleError 路径（emit 失败后 logging 往 stderr 打纯文本）
class BrokenStream(io.StringIO):
    def write(self, s):
        raise OSError("模拟磁盘满/管道关闭")
err_buf = io.StringIO()
h = logging.StreamHandler(BrokenStream())
h.setFormatter(JsonFormatter("%(message)s"))
old_stderr = sys.stderr
sys.stderr = err_buf
lg = logging.getLogger("broken"); lg.handlers=[h]; lg.propagate=False; lg.setLevel(logging.DEBUG)
lg.info("这条写不进去")
sys.stderr = old_stderr
print(SEP); print("# 7-D. 风险（handler 路径）：emit 异常 -> handleError -> stderr 纯文本堆栈")
print("[stderr 原始内容 repr]")
print(repr(err_buf.getvalue()[:300]))
print("  说明: 若 stderr 与日志收集目标相同（容器 2>&1 常见），非 JSON 行会混入流")

# ---------------------------------------------------------------- 11. 风险演示 E：terminator 被改
buf = io.StringIO()
h = logging.StreamHandler(buf)
h.terminator = "\r\n"                 # 有些团队为了 syslog/Windows 改这个
h.setFormatter(JsonFormatter("%(message)s"))
lg = logging.getLogger("crlf"); lg.handlers=[h]; lg.propagate=False; lg.setLevel(logging.DEBUG)
lg.info("CRLF 终止符")
print(SEP); print("# 7-E. handler.terminator='\\r\\n'：行边界变化")
print("[原始字节]", buf.getvalue().encode("utf-8"))
for sep_name, sepb in [("b'\\n'", b"\n"), ("b'\\r\\n'", b"\r\n")]:
    parts = [p for p in buf.getvalue().encode("utf-8").split(sepb) if p]
    print(f"  按 {sep_name} 切分得到 {len(parts)} 段；json 可解析:", end=" ")
    try:
        json.loads(parts[0].decode()); print(True)
    except Exception as e:
        print(False, type(e).__name__)

# ---------------------------------------------------------------- 12. 字节级对照表
print(SEP); print("# 8. 字节级转义对照表（json.dumps 保证）")
cases = ["\n", "\t", "\r", "\u0000", "\u001f", " ", "A", "中", "😀", "\\", '"']
for c in cases:
    enc = json.dumps(c, ensure_ascii=True)[1:-1]
    raw_utf8 = c.encode("unicode_escape").decode()
    print(f"  U+{ord(c):04X}  字符={c!r:6} ensure_ascii转义={enc!r:10} 原始UTF8字节={c.encode('utf-8')!r}")
print(SEP)
print("结论: 0x00-0x1F 控制字符在 JSON 字符串内必然以 \\uXXXX/\\n/\\t/\\r 形式出现，")
print("      唯一的真实 0x0A 只能来自 handler 的 terminator —— 这就是按行切分成立的充要条件。")
