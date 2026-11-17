"""采集流程 DAG 拓扑接口：返回节点、连线图结构。

新增流程关系导出数据包接口：部分失败可导出、失败节点补写、并发收敛，
旧有图结构与 CSV 导出契约保持不变。
"""
from __future__ import annotations

import csv
import io
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.schemas import ActionPayload, ActionResult, EntryPayload, ExportPackagePayload, PageResult
from app.services.flowline import FlowlineService, csv_cell, flowline_export_service

router = APIRouter(prefix="/api/flowline", tags=["采集流程"])
service = FlowlineService()
LIST_FIELDS = ["流程编号", "流程名称", "步骤序列", "输入站点", "输出目标", "失败处理", "触发条件", "版本号"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("")
def get_graph() -> dict[str, Any]:
    """返回节点与边组成的 DAG 图数据。"""
    return service.get_graph()


@router.get("/export")
def export_entries() -> StreamingResponse:
    nodes, edges = service.raw_data()
    buf = io.StringIO(newline="")
    writer = csv.writer(buf)
    writer.writerow(["type", "id", "name", "from", "to", "label", "status"])
    for node in nodes:
        writer.writerow(["node", node.get("id",""), csv_cell(node.get("name","")), "", "", "", node.get("status","")])
    for edge in edges:
        writer.writerow(["edge", "", "", edge.get("from",""), edge.get("to",""), edge.get("label",""), ""])
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=flowline.csv"})


@router.post("/export-packages")
def create_export_package(payload: ExportPackagePayload) -> dict[str, Any]:
    """生成流程关系导出数据包：失败节点带错误码留在包里，可用关系先可下载。"""
    package, message, error_code = flowline_export_service.create_package(payload.schema_version)
    return {"ok": error_code is None, "message": message, "error_code": error_code, "package": package}


@router.get("/export-packages")
def list_export_packages() -> dict[str, Any]:
    """导出数据包清单；is_current 标记唯一保留的成功版本。"""
    packages = flowline_export_service.list_packages()
    return {"items": packages, "total": len(packages)}


@router.get("/export-packages/current")
def current_export_package() -> dict[str, Any]:
    """读取端只保留一个带边界版本的完整导出包。"""
    return {"package": flowline_export_service.current_package()}


@router.get("/export-packages/{package_id}")
def get_export_package(package_id: str) -> dict[str, Any]:
    package = flowline_export_service.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"流程关系导出数据包 {package_id} 不存在")
    return package


@router.get("/export-packages/{package_id}/download")
def download_export_package(package_id: str) -> StreamingResponse:
    """下载数据包里可用关系；失败节点带错误码一并列出，不静默省略。"""
    package = flowline_export_service.get_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=f"流程关系导出数据包 {package_id} 不存在")
    content = flowline_export_service.package_csv(package)
    return StreamingResponse(iter([content]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": f"attachment; filename={package_id}.csv"})


@router.post("/export-packages/{package_id}/retry")
def retry_export_package(package_id: str) -> dict[str, Any]:
    """补写失败节点：已校验通过的关系记录保持原样、不被覆盖。"""
    package, message, error_code, detail = flowline_export_service.retry_package(package_id)
    if package is None:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": error_code is None, "message": message, "error_code": error_code, "package": package, "detail": detail}


@router.get("/export-repairs")
def export_repair_logs() -> dict[str, Any]:
    """修复证据：重复提交去重、并发冲突收敛、失败补写等处置记录。"""
    logs = flowline_export_service.repair_logs()
    return {"items": logs, "total": len(logs)}


@router.get("/{entry_id}")
def get_entry(entry_id: int) -> dict:
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"流程编排 {entry_id} 不存在")
    return entry


@router.post("")
def create_entry(payload: EntryPayload) -> ActionResult:
    entry_or_none, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="流程编排已登记", entry=entry_or_none)


@router.post("/{entry_id}/actions")
def run_action(entry_id: int, payload: ActionPayload) -> ActionResult:
    entry_or_none, message = service.run_action(entry_id, payload.action)
    if entry_or_none is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry_or_none)
