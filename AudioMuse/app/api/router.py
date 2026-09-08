"""v1 API 路由聚合。

业务路由在此按阶段挂载（路径与笔试题目契约一致）：
- POST   /v1/recordings                上传录音（P0-03）
- GET    /v1/tasks/{task_id}           查询任务状态（后续阶段）
- GET    /v1/recordings                录音列表（后续阶段）（分页）
- GET    /v1/recordings/{id}           录音详情（后续阶段）
- POST   /v1/tasks/{task_id}/retry     失败任务重试（后续阶段）
- DELETE /v1/recordings/{id}           删除录音（后续阶段）
"""

from fastapi import APIRouter

from app.api.recordings import router as recordings_router

api_router = APIRouter(prefix="/v1")
api_router.include_router(recordings_router)
