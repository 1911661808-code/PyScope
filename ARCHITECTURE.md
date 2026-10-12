# PyScope 实现约定

本地 Flask 应用，仅绑定 127.0.0.1。Python 3.10+。前端所有资源本地提供，不依赖 CDN。功能覆盖编辑代码、断点、真正按行单步调试、变量与对象引用、调用栈、历史回放、AST、性能分析与本地记录。

## 引擎契约

`SessionManager` 在 `session_manager.py`，方法：`start(code, breakpoints=[]) -> session_id`；`get(session_id, after=-1) -> dict`；`command(session_id, command, breakpoints=None) -> dict`；`close()`。

get 返回 `{id, status, code, breakpoints, events, total_events, error, created_at}`。状态为 `starting/running/paused/completed/error/stopped/limited`。events 是 index > after 的增量；不存在抛 KeyError。command 允许 `step/run/pause/stop`，失败抛 ValueError。

每个事件：`{index,event,line,function,depth,stack,globals,objects,output,elapsed_ms}`。event 为 line/call/return/exception。stack 从最外层到最内层，元素 `{name,line,locals}`。locals/globals 是名字到 value 的映射。value 是 `{kind:'value',type:'int',text:'3'}` 或 `{kind:'ref',type:'list',id:'o1',text:'list (3)'}`。objects 数组元素 `{id,type,preview,items:[{label,value}]}`。稳定对象编号在同一运行中保持一致，处理循环引用、列表、元组、字典、集合、用户对象。输出限制、事件数限制、子进程超时均需实现。超时终止不等于安全沙箱。仅支持可信的单进程单线程同步代码。

line 事件在执行该行之前采集。初次运行暂停在第一行。step 暂停到下一个可执行行，run 到下一断点或结束。return 事件可添加 return_value，exception 可添加 exception 文本。历史记录是快照回放，不回滚副作用。

## HTTP API

- GET /api/examples -> `{examples:[{id,title,description,category,code}]}`。
- POST /api/analyze `{code}` -> `{tree:{label,line,children},counts:{lines,functions,classes,branches,loops},functions:[{name,line}],warnings:[]}`，语法错误 400 `{error,line,offset}`。
- POST /api/sessions `{code,breakpoints}` -> get 格式的会话。
- GET /api/sessions/<id>?after=-1 -> get 格式。
- POST /api/sessions/<id>/command `{command,breakpoints}` -> get 格式。
- GET /api/sessions/<id>/export -> 下载包含全部 events 的 JSON。
- POST /api/profile `{code}` -> `{elapsed_ms,functions:[{name,line,calls,total_ms,cumulative_ms}],output,exception}`，这是独立重新执行。
- GET /api/history -> `{history:[{id,title,status,event_count,created_at}]}`。
- GET /api/history/<id> -> 完整历史会话 get 格式。
- DELETE /api/history/<id> -> `{ok:true}`。

本地历史 SQLite 保存已结束运行，最多100次。前端可编辑、选择示例、设置行断点、启动调试、单步/继续/暂停/停止、选择栈帧、回放滑块、查看AST、性能分析、导入导出.py、导出执行记录、历史载入/删除。所有操作须有真实实现及错误态。

## 模块说明

- `app.py`：Flask 页面与 HTTP API、请求校验、本地运行历史的 SQLite 持久化。
- `session_manager.py`：调试子进程的创建、指令传递、事件缓存、运行时限与会话生命周期管理。
- `trace_worker.py`：Python 执行事件跟踪、断点与单步暂停、作用域和对象快照、标准输出采集。
- `analysis_engine.py`：源码校验、抽象语法树解析、结构统计与代码提示。
- `profile_worker.py`：独立执行待分析代码，通过 cProfile 采集真实的函数调用与耗时数据。
- `examples.py`：排序、递归、共享引用、循环引用、异常和性能对比等可运行示例。
- `templates/`、`static/`：中文交互界面、调试状态展示、SVG 对象引用图、历史回放、语法结构和性能报告；编辑器依赖及许可证均本地保存。
- `tests/`：调试引擎及 HTTP API 的自动化验证。

使用与运行方法见 README；配套说明材料介绍系统设计、实验方法和答辩演示流程。
