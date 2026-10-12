# PyScope 服务器部署模板

本目录用于带登录和 Docker 任务后端的服务器模式。主机软件和 Docker daemon 均由专用非 root 账户运行。没有提供服务器信息，因此以下是可审查的部署模板，尚未在实际服务器执行。

| 文件 | 用途 |
| --- | --- |
| `pyscope.service` | 专用账户的 systemd 用户服务 |
| `server.env.example` | 可选宿主环境示例，无口令或密钥 |
| `server.ssh.example.json` | SSH 回环来源的字段示例，密钥和用户故意留空，不能直接启动 |
| `server.https.example.json` | HTTPS 字段示例，使用文档保留 IP，不能直接启动 |
| `nginx-https.conf.example` | 有效 TLS 证书准备好后使用的 443 反代模板 |

完整边界见 [服务器部署与安全边界](../docs/服务器部署与安全边界.md)。旧的 `app.py` 本地演示入口不作为公网部署入口。

## 1. 主机与账户准备

由管理员按实际 Linux 发行版安装 Git、Python 3.10 或更新版本、venv、pip、Docker CLI 与 rootless Docker 所需组件。创建专用账户 `pyscope`，分配独立 home、subuid/subgid 范围和工作目录；该账户的 rootless daemon 专供本实例，不与其他业务共用。不要加入 rootful `docker` 组。

本模板约定以下路径，改动时要同步修改 unit 和实际配置：

| 路径 | 权限与内容 |
| --- | --- |
| `/opt/pyscope` | 程序和 `.venv`；发布后由管理员维护，服务只读 |
| `/etc/pyscope/server.json` | 专用账户拥有、0600 的真实配置，包含敏感密钥与密码哈希 |
| `/etc/pyscope/server.env` | 可选环境文件，管理员维护，不保存明文口令 |
| `/var/lib/pyscope` | 服务账户拥有、0700 的运行数据目录，数据库权限为 0600 |
| 专用账户 home | rootless Docker 状态及 `.config/systemd/user` |

确认专用账户已经建立后，管理员可按本模板建立目录：

```bash
sudo install -d -m 0750 -o pyscope -g pyscope /opt/pyscope
sudo install -d -m 0700 -o pyscope -g pyscope /etc/pyscope /var/lib/pyscope
```

后续初始化、用户管理和应用启动都使用 `pyscope` 账户，保持文件权限一致。服务代码安装完成后可由管理员调整为只读发布目录，但真实配置和运行数据仍需由专用账户维护。不要在共享可写目录中放服务代码、配置或 Docker socket。

## 2. 配置 rootless Docker

通过管理员批准的 Docker 安装流程补齐 `newuidmap`、`newgidmap` 与 subordinate UID/GID，然后以 `pyscope` 的真实登录会话执行：

```bash
dockerd-rootless-setuptool.sh install
systemctl --user enable --now docker.service
docker info
```

确认 `docker info` 的安全选项包含 `rootless`，Cgroup Version 为 2、Cgroup Driver 为 systemd，并确认 CPU、memory、pids 控制器已委派。Ubuntu 也需实际检查控制器委派，不能仅凭发行版名称判断。只看命令包含资源参数不够：环境不满足时 rootless Docker 可能忽略限制。应由部署检查实际确认；`doctor` 不通过时停止，不去掉限制继续部署。[Docker 官方安装说明](https://docs.docker.com/engine/security/rootless/)、[资源限制前提](https://docs.docker.com/engine/security/rootless/tips/#limiting-resources)

如需退出登录后与开机持续运行，由管理员显式执行：

```bash
sudo loginctl enable-linger pyscope
```

Rootless Docker 使用 systemd 用户服务，不要把 daemon 改成 `/etc/systemd/system` 下带 `User=` 的系统服务。以下应用 unit 也装到同一账户的用户服务目录。通过 SSH 登录该账户后操作 `systemctl --user`，避免在没有用户会话环境的 `sudo` shell 中误用另一个用户的 daemon。

## 3. 安装应用与任务镜像

以下命令在管理员准备好的 `/opt/pyscope` 仓库目录执行，使用专用账户和其 rootless Docker：

```bash
git clone https://github.com/1911661808-code/PyScope.git /opt/pyscope
cd /opt/pyscope
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-server.txt
docker build -f sandbox/Dockerfile -t pyscope-worker:1.1.0 .
docker image inspect pyscope-worker:1.1.0
```

若目录已经是有效仓库，不重复执行 clone；按选定发布版本更新后再构建。镜像仅供本地任务执行，不公开 Docker API，不把宿主 socket 或源码挂载给 worker。每次 worker 代码升级后重新构建并记录镜像 ID，不能凭相同 tag 假定镜像内容已更新。

可选 gVisor 应安装在这一个 rootless daemon 的配置中，随后明确指定 `runsc` runtime 并完成实际任务检查。参照 [gVisor rootless 文档](https://gvisor.dev/docs/user_guide/rootless/)，不要照搬 rootful daemon 的配置路径，也不要给 Docker runtime 盲加 `--rootless` 参数。指定的 runtime 不可用时，服务应停止而不是改用另一种 runtime。

## 4. 初始化配置

真实配置通过服务器 CLI 交互式初始化，口令不得出现在命令参数中。以下使用 SSH 方案，并将浏览器来源设为本机隧道实际使用的 18765；服务后台仍为 9000。以 `pyscope` 账户执行：

```bash
cd /opt/pyscope
umask 077
.venv/bin/python server.py --config /etc/pyscope/server.json init --username admin --origin http://localhost:18765 --data-dir /var/lib/pyscope
chmod 0600 /etc/pyscope/server.json
.venv/bin/python server.py --config /etc/pyscope/server.json doctor
```

按隐藏输入提示设置自己的密码。`init` 仅在配置不存在时创建，生成随机签名密钥并保存密码哈希；没有通用初始密码，不要把空密钥示例复制为真实配置。`doctor` 使用同一配置检查 Docker、镜像、rootless 和资源限制；失败时排除原因，不改用 `app.py` 上线。

需要增加登录用户时，同一账户执行并交互输入新密码：

```bash
.venv/bin/python server.py --config /etc/pyscope/server.json add-user --username another_user
systemctl --user restart pyscope.service
```

若用户名已存在，`add-user` 更新其密码；重启后新配置生效，该用户原有会话失效。

`users` 保存用户名到 scrypt 哈希的映射，`secret_key` 是随机签名密钥；两者均属于秘密。`allowed_origins` 是包含 scheme、主机名与必要端口的精确列表，`secure_cookie` 在 SSH 回环 HTTP 情况下显式为 false，在 HTTPS 公网来源下必须为 true。`data_dir` 为持久化数据目录，`port` 为回环后台端口。`docker_image` 指定管理员构建的镜像，`docker_runtime` 可设为已验证的 `runsc` 或保持 null 使用 Docker 默认 runtime；`require_rootless` 保持 true。`session_hours` 控制登录有效期。

如选择 HTTPS，初始化时把 `--origin` 改为证书覆盖的真实 `https://` 地址，随后确认生成配置的 `secure_cookie` 为 true。`server.https.example.json` 中 `192.0.2.10` 是文档保留地址，不能当作自己的公网 IP。不要在已有配置上再次运行 init 期待覆盖密钥或用户。

## 5. 启动用户服务

完成初始化和环境检查后，以专用账户执行：

```bash
mkdir -p ~/.config/systemd/user
install -m 0644 /opt/pyscope/deploy/pyscope.service ~/.config/systemd/user/pyscope.service
systemctl --user daemon-reload
systemctl --user enable --now pyscope.service
systemctl --user status pyscope.service
journalctl --user -u pyscope.service -n 100 --no-pager
```

模板通过 `DOCKER_HOST=unix://%t/docker.sock` 使用当前用户运行目录里的 socket，仅宿主 Docker CLI 使用。若 Docker CLI 装在其他目录，复制并修改 `server.env.example` 的 PATH；不要指向 rootful socket或未经保护的 TCP daemon。

服务入口为：

```bash
/opt/pyscope/.venv/bin/python /opt/pyscope/server.py --config /etc/pyscope/server.json serve
```

该入口由 Waitress 启动单个应用进程，并独占数据目录的进程锁。不要自行替换成多个 Gunicorn worker 或多实例负载均衡，当前内存会话不会跨进程共享。该实例使用专属 rootless daemon，并通过自己的容器标签清理遗留任务；不要与其他实例共用 daemon 或复制实例配置并行启动。

## 6. 选择浏览器入口

自己使用时只需既有 SSH 规则，不开放 9000。本机另开终端：

```bash
ssh -N -L 127.0.0.1:18765:127.0.0.1:9000 用户@服务器公网IP
```

浏览器访问 `http://localhost:18765`，与真实配置中的允许来源保持一致。关闭本机 SSH 隧道只中断访问，systemd 管理的服务器应用继续运行。

需要公网 IP 或域名浏览器入口时，先取得覆盖该 IP 或域名且客户端信任的 TLS 证书，再调整 `nginx-https.conf.example`。真实配置应仅允许相应 `https://` 来源并启用 Secure cookie。模板不包含明文 HTTP 登录入口，不开放后端 9000。当前 Waitress 清除未配置为可信的转发头，认证以配置中的精确来源和 Host 为准；模板仍在代理层覆盖转发头，避免保留客户端伪造的链。若以后启用代理头解析，只能信任明确的同机代理地址。若现有主机还承载其他站点，先检查端口和默认虚拟主机安排。

当前来源 IP 登录限流在同机反代后会把客户端合并为回环地址，共享每 5 分钟 15 次的限额，成功登录尝试也计数；另有用户名与全局限流。该限制不等同于每个公网客户端独立计数。需要扩展代理识别时，应同步复核登录限流与真实代理链。

Nginx 模板的占位地址及证书路径必须替换；安装位置按实际发行版决定。先运行 `nginx -t`，通过后由管理员加载配置。不要在 TLS 验证失败时让用户忽略证书错误。

## 7. 运维与更新

```bash
systemctl --user restart pyscope.service
systemctl --user stop pyscope.service
```

更新前保留当前源码/镜像版本及数据备份，停止服务后更新代码、依赖和任务镜像，再执行部署检查及课程样例回归。数据库含源码与输出，应按敏感文件备份，不能推送 GitHub。正在执行的会话在服务重启时结束，已保存历史以数据库为准。

未收到实际服务器信息前，本目录只提供模板；systemd、rootless Docker、gVisor、TLS 及 Linux 主机上的真实隔离行为都需要在目标环境验收后再声明通过。
