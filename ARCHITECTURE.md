# PyScope 实现约定

Flask 应用，提供本地与服务器两种入口，均仅绑定 127.0.0.1。Python 3.10+。前端所有资源本地提供，不依赖 CDN。功能覆盖编辑代码、断点、真正按行单步调试、变量与对象引用、调用栈、历史回放、AST、性能分析与运行记录。

## 服务器认证与隔离

`app.py` 默认使用 LocalBackend，只供本机可信代码。`server.py` 为 Linux 服务器入口：读取私有配置，验证非 root 账户、rootless Docker 与实际 cgroup 限制，使用单进程 Waitress。服务启动失败时绝不改用 LocalBackend。

`auth.py` 将随机会话令牌的 HMAC 摘要、CSRF 令牌和到期时间保存在 SQLite；浏览器 cookie 只含随机令牌。密码使用 scrypt 哈希，登录后轮换会话，退出或到期后令牌失效。全部写接口需要 CSRF，Host/Origin 按部署配置校验。历史和内存运行记录均检查当前账号归属，跨账号请求返回 404。旧本地历史的 owner 为空，不会自动分配给服务器用户。

`execution_backend.py` 实现 LocalBackend 和 DockerBackend。服务器的调试、性能和 AST 均通过独立容器执行，宿主只接收经过大小、类型、Unicode 校验的白名单协议。容器没有宿主挂载、宿主环境变量或 Docker socket，禁用网络、使用只读根文件系统及临时工作区，并施加 CPU/内存/进程上限。预检在容器内读取有效 cgroup 限额。容器共享内核；合法形状的事件不具备防篡改保证。

正常完成、错误、停止、超时和服务退出都会显式清理容器；Docker 失联时报告清理失败，下次启动仅在专用 rootless daemon 内按本应用标签回收遗留容器。数据目录进程锁防止同一实例重复启动。部署步骤见 `deploy/README.md`。

## 引擎契约

`SessionManager` 在 `session_manager.py`，方法：`start(code, breakpoints=[]) -> session_id`；`get(session_id, after=-1) -> dict`；`command(session_id, command, breakpoints=None) -> dict`；`close()`。

get 返回 `{id, status, code, breakpoints, events, total_events, error, created_at}`。状态为 `starting/running/paused/completed/error/stopped/limited`。events 是 index > after 的增量；不存在抛 KeyError。command 允许 `step/run/pause/stop`，失败抛 ValueError。

每个事件：`{index,event,line,function,depth,stack,globals,objects,output,elapsed_ms}`。event 为 line/call/return/exception。stack 从最外层到最内层，元素 `{name,line,locals}`。locals/globals 是名字到 value 的映射。value 是 `{kind:'value',type:'int',text:'3'}` 或 `{kind:'ref',type:'list',id:'o1',text:'list (3)'}`。objects 数组元素 `{id,type,preview,items:[{label,value}]}`。稳定对象编号在同一运行中保持一致，处理循环引用、列表、元组、字典、集合、用户对象。输出限制、事件数限制、子进程超时均需实现。超时终止不等于安全沙箱。仅支持可信的单进程单线程同步代码。

line 事件在执行该行之前采集。初次运行暂停在第一行。step 暂停到下一个可执行行，run 到下一断点或结束。return 事件可添加 return_value，exception 可添加 exception 文本。历史记录是快照回放，不回滚副作用。

## HTTP API

- GET /login：服务器登录页；GET /api/auth/me：当前账号与 CSRF 状态。
- POST /auth/login `{username,password}`：使用登录页 CSRF 验证后创建登录会话。
- POST /auth/logout `{}`：注销当前会话。浏览器退出前先停止当前调试。
- 服务器模式所有 /api/ 路由要求登录；POST/DELETE 还需 `X-CSRF-Token` 和 JSON 请求体。

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
