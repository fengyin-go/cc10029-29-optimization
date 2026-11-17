"""存储落地业务规则：状态流转、字段校验与筛选口径都收在这里。

同时承载落地批次回执：存储落地读取流程关系导出数据包，为通过校验的
完整导出版本生成回执，并提供回执与数据包的差异核对，顺着存储落地的
数据流定位两边对不上的地方。
"""
from __future__ import annotations

import copy
import threading
from datetime import datetime, timezone
from typing import Any

from app.services.flowline import ERR_PACKAGE_INCOMPLETE, ERR_PACKAGE_NOT_FOUND, flowline_export_service
from app.store import store

MODULE = "storage"
LIST_FIELDS = ["配置编号", "存储类型", "连接信息", "分区策略", "压缩格式", "保存天数", "备份副本", "清理策略"]
REQUIRED_FIELDS = ["配置编号", "存储类型", "连接信息"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

RECEIPT_TABLE = "storage_landing_receipts"
ERR_RECEIPT_NOT_FOUND = "RECEIPT_NOT_FOUND"

_receipt_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StorageService:
    def list_entries(
        self,
        *,
        filters: dict[str, str] | None = None,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        for field, expected in (filters or {}).items():
            rows = [row for row in rows if expected.casefold() in str(row.get(field, "")).casefold()]
        if keyword:
            rows = [row for row in rows if keyword.casefold() in str(row.get("配置编号", "")).casefold()]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = (page - 1) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in LIST_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"存储配置 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于存储落地可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"存储配置已{action}"

    # ---- 落地批次回执 ----
    def _receipts(self) -> list[dict[str, Any]]:
        return store.rows(RECEIPT_TABLE)

    def list_landing_receipts(self) -> list[dict[str, Any]]:
        """落地批次回执清单：流程关系导出数据包的落地结论同步到这里。"""
        return list(self._receipts())

    def create_landing_receipt(self, package_id: str) -> tuple[dict[str, Any] | None, str, str | None]:
        """读取流程关系导出数据包生成落地批次回执。

        回执必须指向一份通过校验的完整导出版本；同一版本重复落地只保留一份。
        返回 (回执, 提示信息, 错误码)。
        """
        package = flowline_export_service.get_package(package_id)
        if package is None:
            return None, f"流程关系导出数据包 {package_id} 不存在", ERR_PACKAGE_NOT_FOUND
        if package.get("status") != "已完成":
            return None, (
                f"流程关系导出数据包 {package_id} 未通过完整校验（状态：{package.get('status')}），"
                f"落地批次回执必须指向通过校验的完整导出版本"
            ), ERR_PACKAGE_INCOMPLETE
        with _receipt_lock:
            receipts = self._receipts()
            for receipt in receipts:
                if (
                    receipt.get("package_id") == package_id
                    and receipt.get("package_revision") == package.get("revision")
                ):
                    return receipt, f"数据包 {package_id} 该版本已落地，仅保留一份回执 {receipt['receipt_id']}", None
            ok_nodes = [node for node in package["nodes"] if node["state"] == "ok"]
            ok_edges = [edge for edge in package["edges"] if edge["state"] == "ok"]
            schema_version = package.get("schema_version", "v2")
            receipt = {
                "id": max((int(item.get("id", 0)) for item in receipts), default=0) + 1,
                "receipt_id": f"BATCH-{len(receipts) + 1:04d}",
                "module": MODULE,
                "package_id": package_id,
                "package_revision": package.get("revision", 1),
                "schema_version": schema_version,
                "flow_version": package.get("flow_version"),
                "mode": "整包" if schema_version == "v1" else "增量补写",
                "node_count": len(ok_nodes),
                "edge_count": len(ok_edges),
                "backfilled_nodes": [node["node_id"] for node in ok_nodes if node.get("backfilled")],
                "version_summary": copy.deepcopy(package.get("version_summary", {})),
                "validated": True,
                "conclusion": f"流程关系导出数据包 {package_id} 已完整落地",
                "created_at": _now(),
            }
            receipts.append(receipt)
            return receipt, f"落地批次回执 {receipt['receipt_id']} 已生成，指向数据包 {package_id} 第 {receipt['package_revision']} 版", None

    def get_landing_receipt(self, receipt_id: str) -> dict[str, Any] | None:
        for receipt in self._receipts():
            if receipt.get("receipt_id") == receipt_id:
                return receipt
        return None

    def landing_receipt_diff(self, receipt_id: str) -> tuple[dict[str, Any] | None, str | None]:
        """顺着存储落地的数据流，核对落地批次回执与流程关系导出数据包的差异。"""
        receipt = self.get_landing_receipt(receipt_id)
        if receipt is None:
            return None, ERR_RECEIPT_NOT_FOUND
        package = flowline_export_service.get_package(receipt["package_id"])
        differences: list[str] = []
        if package is None:
            differences.append(f"流程关系导出数据包 {receipt['package_id']} 已不存在")
        else:
            if package.get("revision") != receipt.get("package_revision"):
                differences.append(
                    f"数据包版本已推进到第 {package.get('revision')} 版，回执指向第 {receipt.get('package_revision')} 版"
                )
            if package.get("flow_version") != receipt.get("flow_version"):
                differences.append(
                    f"边界版本不一致：回执 {receipt.get('flow_version')}，数据包 {package.get('flow_version')}"
                )
            ok_nodes = [node for node in package["nodes"] if node["state"] == "ok"]
            ok_edges = [edge for edge in package["edges"] if edge["state"] == "ok"]
            if len(ok_nodes) != receipt.get("node_count"):
                differences.append(f"节点数对不上：回执 {receipt.get('node_count')}，数据包 {len(ok_nodes)}")
            if len(ok_edges) != receipt.get("edge_count"):
                differences.append(f"边数对不上：回执 {receipt.get('edge_count')}，数据包 {len(ok_edges)}")
            current_summary = package.get("version_summary", {})
            recorded_summary = receipt.get("version_summary", {})
            for key in ("nodes", "edges"):
                current_items = current_summary.get(key, {})
                recorded_items = recorded_summary.get(key, {})
                for item_key, recorded_version in recorded_items.items():
                    current_version = current_items.get(item_key)
                    if current_version != recorded_version:
                        differences.append(
                            f"{key} {item_key} 版本摘要不一致：回执 {recorded_version}，数据包 {current_version}"
                        )
        return {
            "receipt_id": receipt_id,
            "package_id": receipt["package_id"],
            "matched": not differences,
            "differences": differences,
            "checked_at": _now(),
        }, None
