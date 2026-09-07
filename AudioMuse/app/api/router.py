"""v1 API 路由聚合。

骨架阶段暂无业务路由；后续按开发计划在此挂载：
- POST   /v1/recordings                上传录音
- GET    /v1/tasks/{task_id}           查询任务状态
- GET    /v1/recordings                录音列表（分页）
- GET    /v1/recordings/{id}           录音详情
- POST   /v1/tasks/{task_id}/retry     失败任务重试
- DELETE /v1/recordings/{id}           删除录音
"""

from fastapi import APIRouter

api_router = APIRouter(prefix="/v1")
