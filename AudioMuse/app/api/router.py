"""v1 API 路由聚合。

业务路由在此按阶段挂载（路径与笔试题目契约一致）：
- POST   /v1/recordings                上传录音（P0-03）
- GET    /v1/tasks/{task_id}           查询任务状态（P0-06）
- GET    /v1/recordings                录音列表，分页倒序（P0-06）
- GET    /v1/recordings/{id}           录音详情，含 transcript/摘要（P0-06）
- POST   /v1/tasks/{task_id}/retry     失败任务重试（P0-07）
- DELETE /v1/recordings/{id}           删除录音（P0-08）
"""

from fastapi import APIRouter

from app.api.recordings import router as recordings_router
from app.api.tasks import router as tasks_router

api_router = APIRouter(prefix="/v1")
api_router.include_router(recordings_router)
api_router.include_router(tasks_router)
