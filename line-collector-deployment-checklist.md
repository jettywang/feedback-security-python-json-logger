# JSON 日志接入按行收集器：部署判断与核对清单

适用版本：python-json-logger 4.3.0.dev3（仓库源码，`src/pythonjsonlogger/`），Python 3.12。
结论对 3.x 同样成立（默认 `ensure_ascii=True`、compact 输出的行为未变），但 4.x 的模块路径是
`pythonjsonlogger.json.JsonFormatter`（旧路径 `pythonjsonlogger.jsonlogger` 仅留 DeprecationWarning 桩）。

实证脚本：`line-collector-evidence.py`（纯内存流 `io.StringIO`，无网络、无外部依赖）。
完整输出：`line-collector-evidence.output.txt`。复跑：`python3 line-collector-evidence.py`。

---

## 1. 一句话结论

在 **formatter 配置正确**（`json_indent=None`、`prefix` 不含换行、消息全部经 JSON 序列化）且
**handler/进程侧没有旁路写入** 的前提下，业务消息里的换行、制表符、任意非 ASCII 内容都不会破坏
“一条记录 = 一个物理行”的约定，按行收集可以直接上。风险几乎全部集中在 formatter 之外
（handler、其它日志源、进程/容器管道、收集器自身配置）。

## 2. 为什么转义够用：数据流上的不变量

调用链：`logger.info()` → `BaseJsonFormatter.format()`（`core.py:248`）→
`JsonFormatter.jsonify_log_record()`（`json.py:100`，即 `json.dumps(..., ensure_ascii=..., indent=...)`）
→ `StreamHandler.emit()` 里单次 `stream.write(msg + self.terminator)`，`terminator="\n"`。

由此得到一个可验证的充要条件：

- JSON 字符串内 U+0000–U+001F 控制字符**必然**被转义（`\n` `\t` `\r` 或 `\uXXXX`），
  字节流中唯一的真实 `0x0A` 只可能来自 handler 的 `terminator`。
- 默认 `json_ensure_ascii=True`（`json.py:75`）：非 ASCII 也变成 `\uXXXX`（含 emoji 代理对），
  整条记录纯 ASCII，任何编码链路都不会改变字节数。
- 设 `json_ensure_ascii=False`：非 ASCII 以原始 UTF-8 落盘，但控制字符转义规则不变，
  物理行数量不变（实证第 2 节断言通过）。前提是全链路 UTF-8。
- 异常路径同样安全：`format()` 先把 `exc_info`/`stack_info` 格式化成**普通字符串字段**再序列化
  （`core.py:275-285`），traceback 的换行是 JSON 字符串里的 `\n`，不是真实换行（实证第 3、5 节）。
  `exc_info_as_array=True` / `stack_info_as_array=True` 时变为 JSON 数组，结构上更干净（实证第 4 节）。
- `StreamHandler.emit` 是**一次** `write(msg + terminator)`（stdlib `logging/__init__.py`），
  配合 handler 上的 `RLock`，同进程多线程不会产生交错半行（实证：8 线程 × 200 条 = 1600 行，0 解析失败）。

## 3. 实证证据索引（原始字节 ↔ 解析结果对应关系）

| # | 场景 | 原始字节关键证据 | 逐行 `json.loads` 结果 |
|---|------|------------------|------------------------|
| 1 | 消息含 LF/TAB/CR/NUL/BEL/中文/emoji | `line1\\nline2\\ttabbed\\r\\r...\\u0000...\\u4e2d\\u6587...\\ud83d\\ude00` | 1 物理行，OK，还原出真实换行/tab/中文/emoji |
| 2 | `json_ensure_ascii=False` | 中文为原始 UTF-8 字节 `\\xe4\\xbd\\xa0...`，LF 仍是 `\\n` 转义 | 1 物理行，OK；断言 UTF-8 原样存在 |
| 3 | `exc_info=True` 异常栈 | `exc_info` 字段内全部 `\\n`、`\\"` 转义 | 1 物理行，OK；解析后 `splitlines()` 还原完整 traceback |
| 4 | `exc_info_as_array=True` | `exc_info` 为 JSON 数组，栈帧是数组元素 | 1 物理行，OK |
| 5 | `stack_info=True` | 同 #3，多行栈被转义进字符串 | 1 物理行，OK |
| 6 | 连续 3 条记录（含 dict 消息） | 恰好 3 个 `0x0A` 终止 | 3 物理行全部 OK |
| 7-A | `json_indent=2`（错误配置） | 单条记录占 4 个物理行 | 4 行全部 `JSONDecodeError` |
| 7-B | `prefix="...\\n"` | JSON 外出现真实换行 | 第 1 行 FAIL，第 2 行 OK |
| 7-C | 同流混入 `print`/裸 `write` | 非 JSON 行、无终止符的半行与 JSON 拼接 | 混入行及被拼接行 FAIL |
| 7-D | `emit` 抛错（磁盘满/管道断） | `handleError` 向 stderr 输出 `--- Logging error ---` 纯文本 | 若 stderr 与收集流合并（容器 `2>&1`）即污染 |
| 7-E | `terminator="\\r\\n"` | 行尾 `}\\r\\n` | 按 `\\n` 或 `\\r\\n` 切都可解析，但收集器行界约定必须明确 |
| 8 | 字节级转义对照表 | U+000A→`\\n`、U+0009→`\\t`、U+4E2D→`\\u4e2d`/UTF-8 `\\xe4\\xb8\\xad` 等 | 印证“真实 LF 仅来自 terminator” |

补充实证（第二轮脚本，未入库，结论如下）：

- `FileHandler` 默认 encoding 就是 UTF-8、`errors="strict"`、文本模式 `newline=None`；
  文本模式下消息里的真实 CR 会被翻译（实测 `x\r\ny` → 磁盘 `x\r\ny`，macOS 上 `\n` 不被改写；
  Windows 文本模式会把 `\n` 写成 `\r\n`）——但 JSON 内的 CR/LF 都已转义，**翻译只作用于 terminator**，
  因此不影响 JSON 完整性，只影响行分隔符形态。
- `RotatingFileHandler.shouldRollover` 在写入**之前**判断并轮转（stdlib 源码实证），
  实测 maxBytes=180 下所有归档文件都以完整 `}\n` 收尾，无半行切割。
- handler 锁是 `threading.RLock`，**不跨进程**。多进程写同一文件没有 stdlib 级保护。

## 4. formatter 以外的风险清单（按发生概率排序）

1. **旁路文本写入同一目标**：业务代码或第三方库的 `print`、`warnings`、C 扩展裸 `write`、
   gunicorn/uwsgi access log、未接管的 root logger（实证 7-C）。这是最常见的污染来源。
2. **stderr/stdout 合流**：容器里 `2>&1` 后，`handleError` 的 `--- Logging error ---`
   纯文本堆栈（实证 7-D）、解释器崩溃信息、OOM killer 信息都会进同一条流。
3. **多进程共写一个文件**：`RLock` 只在线程内有效；POSIX 下 `O_APPEND` 单次 write 有
   `PIPE_BUF`/管道原子性边界，超长记录仍可能交错。应改为每进程独立文件、或走
   SocketHandler/系统日志采集端。
4. **formatter 被改错**：`json_indent` 非 None（7-A）、`prefix` 含换行（7-B）、
   自定义 `json_serializer` 输出多行、自定义 `process_log_record` 引入不可序列化对象后
   走兜底 `unknown_default`（输出字符串 `"__could_not_encode__"`，不炸但会丢字段语义）。
5. **编码链路不一致**：`json_ensure_ascii=False` 时，收集器/文件/管道任一环不是 UTF-8
   （或被 Latin-1 转码），中文/emoji 会乱码；`errors="strict"` 下写不出去还会触发 7-D。
6. **行分隔符约定**：Windows 文本模式或自定义 `terminator="\r\n"`（7-E）时，收集器若只认 `\n`
   一般仍可解析（`\r` 成为行尾空白），但要在收集器侧显式确认；NUL 字符（已转义为 `\u0000`）
   对部分 C 系采集 agent 仍需验证其长行/特殊字符处理。
7. **单条记录过大**：收集器常有行长度上限（如 journald 默认 48KiB/条、各类 agent 256KiB–1MiB），
   大 traceback、大 payload 会被截断成无法解析的半行。需在应用侧限制字段长度或开采样。
8. **SocketHandler/DatagramHandler 不走 terminator 语义**：前者按长度分帧、后者受 UDP 64KiB
   限制且可能丢包，不能套用文件行采模式；HTTP/网络 handler 的失败重试也不保证幂等。
9. **轮转与采集的竞态**：`mv` 式轮转（logrotate 未配 `copytruncate` 或 rename signal 未送达）
   期间，进程仍写旧 inode，采集器跟随新文件名会丢一段；优先用 `RotatingFileHandler` 内建轮转
   或让采集器按 inode 跟踪。
10. **模块路径漂移**：4.x 旧 import 路径 `pythonjsonlogger.jsonlogger` 每次发 `DeprecationWarning`，
    且 warnings 默认可能打到 stderr——升级时统一改为 `pythonjsonlogger.json`。

## 5. 部署同事逐项核对清单

### A. Formatter（python-json-logger 侧）

- [ ] 类路径为 `pythonjsonlogger.json.JsonFormatter`（orjson/msgspec 后端分别是
      `pythonjsonlogger.orjson.OrjsonFormatter` / `pythonjsonlogger.msgspec.MsgspecFormatter`）。
- [ ] `json_indent` 未设置或显式为 `None`（orjson 后端 `json_indent=False`）。
- [ ] 未设置 `prefix`，或 `prefix` 经检查不含 `\n`/`\r` 且本身是合法 JSON 前缀策略。
- [ ] 非 ASCII 策略二选一并记录在案：默认 `json_ensure_ascii=True`（最省心，纯 ASCII）；
      若设 `False`，则 B 节所有环节必须验证 UTF-8。
- [ ] 异常字段保持默认字符串，或显式启用 `exc_info_as_array=True`、`stack_info_as_array=True`
      （下游更易结构化消费）；不要在 `process_log_record` 里把异常拼成多行裸文本。
- [ ] 自定义 `json_serializer`/`json_default` 评审过：输出必须是单行合法 JSON；
      知晓兜底值 `"__could_not_encode__"` 的含义。
- [ ] 用 `line-collector-evidence.py` 在目标镜像/venv 里复跑，第 1–6 节全部 OK、7 节行为与本报告一致。

### B. Handler 与进程

- [ ] 优先 `StreamHandler(sys.stdout)` 交给容器运行时采集，或 `FileHandler(..., encoding="utf-8")`；
      显式指定 encoding，不依赖平台默认。
- [ ] `terminator` 保持默认 `"\n"`；如确需 `"\r\n"`，在收集器侧同步确认分隔符配置。
- [ ] 同一 stdout/stderr 流内没有非 JSON 来源：`print` 全部替换为 logging；
      `warnings.simplefilter("ignore")` 或把 warnings 重定向到 logging（`logging.captureWarnings(True)`）；
      gunicorn/uwsgi/uvicorn access log 改用同一 JSON formatter 或独立文件。
- [ ] 容器场景评估 stderr 合流：应用自身异常退出信息无法 JSON 化，收集器需配“解析失败行”的
      兜底存储与告警，不能静默丢弃。
- [ ] 多进程不共写普通文件：每进程独立文件名（带 PID），或统一走 socket/sidecar/采集 agent。
- [ ] 轮转使用 `RotatingFileHandler`/`TimedRotatingFileHandler`（写入前轮转，不切半行）；
      外部 logrotate 用 `copytruncate` 或保证 postrotate 信号被进程接收；采集器按 inode 跟踪。
- [ ] 压测最大单条记录体积（含异常栈），与收集器行长度上限对齐，超限字段在应用侧裁剪。

### C. 收集器/平台侧

- [ ] 输入编码明确配置为 UTF-8（仅当 `json_ensure_ascii=False` 时为硬性要求；True 时为防御性要求）。
- [ ] 行分隔符与应用 terminator 一致（默认 `\n`）；开启多行/合并选项时要知道它会掩盖污染问题，不建议依赖。
- [ ] 解析失败行进入独立死信通道并计数告警（用于发现 7-C/7-D 类旁路污染和截断）。
- [ ] 时间字段、级别字段映射与 `rename_fields` 后的实际字段名对齐（示例见 `docs/cookbook.md:253`）。
- [ ] UDP/socket 类 handler 不纳入行采；网络发送失败的缓冲与重试策略已评估。
- [ ] 灰度期对“采集行数 ≈ 应用 `emit` 计数”做对账，差异即为丢失/污染量。

### D. 参考 dictConfig 片段（可直接作为基线改）

```yaml
version: 1
disable_existing_loggers: false
formatters:
  json:
    "()": pythonjsonlogger.json.JsonFormatter
    format: "%(asctime)s %(levelname)s %(name)s %(message)s"
    rename_fields:
      asctime: timestamp
      levelname: level
    # 不设置 json_indent；不设置 prefix；json_ensure_ascii 保持默认 true
    exc_info_as_array: true
    stack_info_as_array: true
handlers:
  stdout:
    class: logging.StreamHandler
    stream: ext://sys.stdout
    formatter: json
    # terminator 保持默认 "\n"，dictConfig 不覆盖
root:
  level: INFO
  handlers: [stdout]
```
