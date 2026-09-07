# AudioMuse：录音转写与智能摘要服务

后端实习生笔试项目。客户端上传音频文件，服务端**异步**完成「转写」（Mock ASR）
与「智能摘要」（真实 LLM），客户端可查询任务状态与结果。

> 当前状态：**项目骨架（P0-01 工程与契约部分）**。业务接口与异步流水线按
> [`docs/需求分析与10小时开发测试计划.md`](docs/需求分析与10小时开发测试计划.md) 分阶段实现。

## 目录结构

```
AudioMuse/
├── app/
│   ├── main.py            # FastAPI 应用入口（工厂 + /healthz）
│   ├── config.py          # 配置（环境变量前缀 AUDIOMUSE_，支持 .env）
│   ├── api/               # v1 HTTP 路由（业务接口后续挂载）
│   ├── core/              # 统一错误结构、日志
│   ├── db/                # SQLite 连接与迁移（P0-02 实现）
│   ├── services/          # 上传存储、ASR/LLM 适配（后续阶段）
│   └── worker/            # 后台异步消费者（后续阶段）
├── docs/                  # 笔试题目与需求分析/开发计划
├── scripts/start.sh       # 一键启动
├── tests/                 # pytest 用例
├── .env.example           # 环境变量示例
└── pyproject.toml         # 依赖（uv 管理）
```

## 运行方式

依赖管理：标准库 `venv` + `pip`，需要本机 Python >= 3.9。

```bash
cp .env.example .env        # 按需修改配置
scripts/start.sh            # 一键启动（首次自动建 .venv 并安装依赖），默认 http://0.0.0.0:8000
# 或开发热重载：
scripts/start.sh --reload
```

手动运行：

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/uvicorn app.main:app --port 8000
```

验证：

```bash
curl http://127.0.0.1:8000/healthz
# => {"code":"OK","message":"ok","data":{"status":"ok","version":"0.1.0"},"request_id":"..."}
```

测试：

```bash
.venv/bin/pytest -q
```

## 设计要点（骨架阶段）

- **技术栈**：Python + FastAPI + SQLite + 本地磁盘，单进程内多个 asyncio 消费者
  异步处理任务（详见 `docs/需求分析与10小时开发测试计划.md`）。
- **统一成功响应**：`{"code":"OK","message":"ok","data":{...},"request_id":"..."}`；
  `data` 内容按接口自定义（如上传接口返回 `recording_id/task_id/status`），见 `app/core/responses.py`。
- **统一错误响应**：`{"error":{"code","message","request_id"}}`，见 `app/core/errors.py`。
  每个请求通过 `X-Request-ID` 头关联日志与响应（成功与错误共用同一编号）。
- **配置外置**：全部经 `AUDIOMUSE_*` 环境变量 / `.env` 注入，密钥不入库不入日志。

## 路线图

按 `docs/需求分析与10小时开发测试计划.md` 第 9 节推进：

- [ ] P0-01 工程与契约（本次：骨架、配置、错误结构、启动入口）— **骨架已就绪**
- [ ] P0-02 数据持久化（recordings/tasks、迁移、条件状态更新）
- [ ] P0-03 上传接口（multipart、限 50MB、扩展名校验、立即返回 202）
- [ ] P0-04 全局调度（单进程锁、3 协程消费者、原子领取）
- [ ] P0-05 ASR Mock 与真实 LLM（超时、结构校验）
- [ ] P0-06 任务查询 / 分页列表 / 录音详情
- [ ] P0-07 失败任务手动重试（attempt_no、防重复）
- [ ] P0-08 删除录音（含文件与关联数据）
- [ ] P0-09 验收与提交材料（测试、README 架构图、API 调试文件）

## 远程仓库

- 仓库：`git@github.com:lvxq333/AudioMuse.git`
- 策略：分阶段提交，保留完整 commit 历史（笔试要求禁止一次性提交全部代码）。
