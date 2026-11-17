"""采集流程 DAG 图服务：生成节点、边示例结构与流转规则。

同时承载流程关系导出数据包（flowline export package）的业务规则：

- 节点状态不一致时不再整包报错重做，而是生成带版本摘要的导出数据包，
  可用关系先形成可下载版本，失败节点带错误码留在包里，允许单独补写。
- 重试只补写失败节点，已校验通过的关系记录保持原样、不被覆盖。
- 并发或连续重复提交时只保留一个成功版本；导出期间流程状态被更新造成
  冲突时，读取端收敛为唯一一份带边界版本的完整导出包，并留下修复证据。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Any

from app.store import store

MODULE = "flowline"
REQUIRED_FIELDS = ["流程编号", "流程名称", "步骤序列"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用配置": "已启用", "停用配置": "已停用"}
NEGATIVE_ACTIONS = ["停用配置"]

# 流程关系导出数据包对外错误码（保持稳定，调用方按码处理）。
ERR_NODE_STATE_INCONSISTENT = "NODE_STATE_INCONSISTENT"
ERR_NODE_STATUS_UNKNOWN = "NODE_STATUS_UNKNOWN"
ERR_NODE_VERSION_MISSING = "NODE_VERSION_MISSING"
ERR_EDGE_ENDPOINT_INCONSISTENT = "EDGE_ENDPOINT_INCONSISTENT"
ERR_PACKAGE_NOT_FOUND = "PACKAGE_NOT_FOUND"
ERR_PACKAGE_INCOMPLETE = "PACKAGE_INCOMPLETE"
ERR_FLOW_STATE_CONFLICT = "FLOW_STATE_CONFLICT"
ERR_LEGACY_EXPORT_FAILED = "LEGACY_EXPORT_FAILED"

PACKAGE_TABLE = "flowline_export_packages"
REPAIR_TABLE = "flowline_export_repairs"
SCHEMA_VERSIONS = ("v1", "v2")  # v1 旧版整包重做；v2 新版部分失败可补写


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def csv_cell(value: Any) -> str:
    text = str(value) if value is not None else ""
    if text and text[0] in "=+-@":
        text = "'" + text
    return '"' + text.replace('"', '""') + '"'


def _row_name(row: dict[str, Any]) -> str:
    """节点显示名：优先流程名称，其次流程编号。

    修复前取的是 ``list(row.keys())[1]``，恰好指向 status 字段，导致图上的
    节点名全部显示成「草稿/待校验/已启用」，和流程名称对不上。
    """
    return str(row.get("流程名称") or row.get("流程编号") or row.get("name") or "")


class FlowlineService:
    def get_graph(self) -> dict[str, Any]:
        rows = store.rows(MODULE)
        nodes: list[dict] = []
        edges: list[dict] = []
        for i, row in enumerate(rows):
            nodes.append({"id": int(row.get("id", i+1)), "name": _row_name(row), "status": str(row.get("status", STATUS_ORDER[0])),
                          **{k: row[k] for k in row if k not in ("id","status","pending","abnormal")}})
        for i in range(len(nodes)-1):
            edges.append({"from": nodes[i]["id"], "to": nodes[i+1]["id"], "label": "依赖"})
        return {"nodes": nodes, "edges": edges}

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [f for f in REQUIRED_FIELDS if not str(values.get(f) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(r.get("id",0)) for r in rows), default=0) + 1}
        entry.update({f: values.get(f) for f in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"流程编排 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集流程可执行范围"
        target = ACTION_RULES[action]
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"流程编排已{action}"

    def raw_data(self) -> tuple[list[dict], list[dict]]:
        g = self.get_graph()
        return g["nodes"], g["edges"]


class FlowlineExportService:
    """流程关系导出数据包：部分失败可导出、失败补写、并发收敛。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # ---- 内部存取 ----
    def _packages(self) -> list[dict[str, Any]]:
        return store.rows(PACKAGE_TABLE)

    def _repairs(self) -> list[dict[str, Any]]:
        return store.rows(REPAIR_TABLE)

    def _log_repair(self, kind: str, detail: str, package_id: str | None = None) -> dict[str, Any]:
        logs = self._repairs()
        entry = {
            "id": max((int(log.get("id", 0)) for log in logs), default=0) + 1,
            "kind": kind,
            "detail": detail,
            "package_id": package_id,
            "at": _now(),
        }
        logs.append(entry)
        return entry

    # ---- 版本与校验 ----
    def node_version(self, row: dict[str, Any]) -> str:
        return _digest({
            "id": row.get("id"),
            "status": row.get("status"),
            "流程编号": row.get("流程编号"),
            "流程名称": row.get("流程名称"),
            "步骤序列": row.get("步骤序列"),
            "版本号": row.get("版本号"),
        })

    def edge_version(self, edge: dict[str, Any]) -> str:
        return _digest({"from": edge.get("from"), "to": edge.get("to"), "label": edge.get("label")})

    def flow_version(self) -> str:
        """当前流程编排的边界版本：任一节点/边变化都会改变它。"""
        rows = store.rows(MODULE)
        payload = [
            (int(row.get("id", 0)), self.node_version(row), row.get("status"))
            for row in rows
        ]
        return f"FV-{_digest(payload)}"

    def validate_node(self, row: dict[str, Any]) -> tuple[str | None, str | None]:
        """节点状态一致性校验，返回 (错误码, 说明)；通过时返回 (None, None)。"""
        if row.get("abnormal"):
            return ERR_NODE_STATE_INCONSISTENT, "节点状态标记异常，与流程编排状态不一致"
        if str(row.get("status", "")) not in STATUS_ORDER:
            return ERR_NODE_STATUS_UNKNOWN, f"节点状态「{row.get('status')}」不在允许的状态序列里"
        if not str(row.get("版本号") or "").strip():
            return ERR_NODE_VERSION_MISSING, "节点缺少版本号，无法形成版本摘要"
        return None, None

    def _snapshot(self) -> dict[str, Any]:
        """读取当前流程编排，生成带版本摘要的节点/边快照；失败节点保留在快照里。"""
        rows = store.rows(MODULE)
        nodes: list[dict[str, Any]] = []
        for i, row in enumerate(rows):
            code, message = self.validate_node(row)
            nodes.append({
                "node_id": int(row.get("id", i + 1)),
                "name": _row_name(row),
                "status": str(row.get("status", STATUS_ORDER[0])),
                "version": self.node_version(row),
                "state": "failed" if code else "ok",
                "error_code": code,
                "error": message,
                "backfilled": False,
                **{k: row[k] for k in row if k not in ("id", "status", "pending", "abnormal")},
            })
        ok_ids = {node["node_id"] for node in nodes if node["state"] == "ok"}
        edges: list[dict[str, Any]] = []
        for i in range(len(nodes) - 1):
            frm, to = nodes[i]["node_id"], nodes[i + 1]["node_id"]
            code = None if (frm in ok_ids and to in ok_ids) else ERR_EDGE_ENDPOINT_INCONSISTENT
            edges.append({
                "edge_id": f"{frm}->{to}",
                "from": frm,
                "to": to,
                "label": "依赖",
                "version": self.edge_version({"from": frm, "to": to, "label": "依赖"}),
                "state": "failed" if code else "ok",
                "error_code": code,
                "error": "边的端点节点状态不一致" if code else None,
            })
        return {"flow_version": self.flow_version(), "nodes": nodes, "edges": edges}

    @staticmethod
    def _summary(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
        """节点和边的版本摘要：已校验通过的记录原样保留，补写只追加失败项。"""
        return {
            "nodes": {str(node["node_id"]): node["version"] for node in nodes},
            "edges": {edge["edge_id"]: edge["version"] for edge in edges},
            "node_count": len(nodes),
            "edge_count": len(edges),
            "ok_count": sum(1 for node in nodes if node["state"] == "ok"),
            "failed_count": sum(1 for node in nodes if node["state"] == "failed"),
        }

    def _current(self) -> dict[str, Any] | None:
        current = [pkg for pkg in self._packages() if pkg.get("is_current")]
        return current[-1] if current else None

    def _converge_current(self) -> dict[str, Any] | None:
        """读取端只保留一个带边界版本的完整导出包，其余标记为被取代。"""
        packages = self._packages()
        if not packages:
            return None
        keeper = packages[-1]
        for pkg in packages:
            pkg["is_current"] = pkg is keeper
        return keeper

    # ---- 对外行为 ----
    def create_package(self, schema_version: str = "v2") -> tuple[dict[str, Any] | None, str, str | None]:
        """生成流程关系导出数据包。

        返回 (数据包, 提示信息, 错误码)。错误码非空时数据包为 None 或收敛后的现有包。
        """
        if schema_version not in SCHEMA_VERSIONS:
            return None, f"数据包版本「{schema_version}」不支持，仅支持 v1（旧版整包）/ v2（新版补写）", "SCHEMA_VERSION_UNKNOWN"
        with self._lock:
            snapshot = self._snapshot()
            if snapshot["flow_version"] != self.flow_version():
                # 导出过程中流程状态被更新，节点关系冲突：读取端收敛为唯一边界版本包。
                keeper = self._converge_current()
                if keeper is not None:
                    self._log_repair(
                        "冲突收敛",
                        f"导出期间流程状态被更新，读取端仅保留边界版本 {keeper['flow_version']} 的完整导出包 {keeper['package_id']}",
                        keeper["package_id"],
                    )
                    return keeper, f"导出期间流程状态被更新，已保留边界版本 {keeper['flow_version']} 的完整导出包", ERR_FLOW_STATE_CONFLICT
                return None, "导出期间流程状态被更新，节点关系冲突，请重新导出", ERR_FLOW_STATE_CONFLICT

            failed = [node for node in snapshot["nodes"] if node["state"] == "failed"]
            if schema_version == "v1" and failed:
                # 旧版契约：任一节点状态不一致即报错，整包重做。
                ids = "、".join(str(node["node_id"]) for node in failed)
                return None, f"旧版整包导出失败：节点 {ids} 状态不一致（{ERR_NODE_STATE_INCONSISTENT}），请整包重做", ERR_LEGACY_EXPORT_FAILED

            current = self._current()
            if (
                current is not None
                and current["flow_version"] == snapshot["flow_version"]
                and current["schema_version"] == schema_version
            ):
                # 连续/并发重复提交：内容一致，只保留一份，不生成第二份结果。
                self._log_repair(
                    "重复提交去重",
                    f"连续提交的流程关系导出数据包与 {current['package_id']} 边界版本一致，仅保留一份",
                    current["package_id"],
                )
                return current, f"检测到重复提交，已保留现有导出数据包 {current['package_id']}", None

            packages = self._packages()
            package = {
                "id": max((int(pkg.get("id", 0)) for pkg in packages), default=0) + 1,
                "package_id": f"EXP-{len(packages) + 1:04d}",
                "schema_version": schema_version,
                "flow_version": snapshot["flow_version"],
                "revision": 1,
                "status": "部分成功" if failed else "已完成",
                "is_current": True,
                "nodes": snapshot["nodes"],
                "edges": snapshot["edges"],
                "version_summary": self._summary(snapshot["nodes"], snapshot["edges"]),
                "failed_nodes": [node["node_id"] for node in failed],
                "retry_log": [],
                "created_at": _now(),
                "updated_at": _now(),
            }
            superseded = [pkg["package_id"] for pkg in packages if pkg.get("is_current")]
            for pkg in packages:
                pkg["is_current"] = False
            packages.append(package)
            if superseded:
                self._log_repair(
                    "并发收敛",
                    f"收到新的流程关系导出数据包并发提交，仅保留一个成功版本 {package['package_id']}，取代 {('、'.join(superseded))}",
                    package["package_id"],
                )
            if failed:
                ids = "、".join(str(node["node_id"]) for node in failed)
                return package, f"导出数据包已生成，{len(failed)} 个节点状态不一致（{ERR_NODE_STATE_INCONSISTENT}）：{ids}，可用关系已形成可下载版本，失败节点可单独补写", None
            return package, "导出数据包已生成，全部节点通过校验", None

    def list_packages(self) -> list[dict[str, Any]]:
        return list(self._packages())

    def current_package(self) -> dict[str, Any] | None:
        return self._current()

    def get_package(self, package_id: str) -> dict[str, Any] | None:
        for pkg in self._packages():
            if pkg.get("package_id") == package_id:
                return pkg
        return None

    def retry_package(self, package_id: str) -> tuple[dict[str, Any] | None, str, str | None, dict[str, Any]]:
        """补写失败节点：只更新失败记录，已校验通过的关系记录不被覆盖。

        返回 (数据包, 提示信息, 错误码, 补写明细)。补写明细里 kept 是本次
        没有改写的已校验通过节点，便于调用方核对成功节点没有被重复写入。
        """
        with self._lock:
            package = self.get_package(package_id)
            if package is None:
                return None, f"流程关系导出数据包 {package_id} 不存在", ERR_PACKAGE_NOT_FOUND, {}
            failed_nodes = [node for node in package["nodes"] if node["state"] == "failed"]
            failed_edges = [edge for edge in package["edges"] if edge["state"] == "failed"]
            detail: dict[str, Any] = {"backfilled": [], "kept": [], "still_failed": []}
            if not failed_nodes and not failed_edges:
                detail["kept"] = [node["node_id"] for node in package["nodes"]]
                return package, "没有需要补写的失败节点，已校验通过的关系记录保持原样", None, detail

            rows_by_id = {int(row.get("id", 0)): row for row in store.rows(MODULE)}
            for node in failed_nodes:
                row = rows_by_id.get(node["node_id"])
                if row is None:
                    node["error_code"] = ERR_NODE_STATE_INCONSISTENT
                    node["error"] = "节点在流程编排中已不存在"
                    detail["still_failed"].append(node["node_id"])
                    continue
                code, message = self.validate_node(row)
                if code:
                    node["error_code"] = code
                    node["error"] = message
                    detail["still_failed"].append(node["node_id"])
                    continue
                # 只补写失败节点：版本、名称、状态按当前流程编排重新写入。
                node.update({
                    "name": _row_name(row),
                    "status": str(row.get("status", STATUS_ORDER[0])),
                    "version": self.node_version(row),
                    "state": "ok",
                    "error_code": None,
                    "error": None,
                    "backfilled": True,
                    **{k: row[k] for k in row if k not in ("id", "status", "pending", "abnormal")},
                })
                detail["backfilled"].append(node["node_id"])

            ok_ids = {node["node_id"] for node in package["nodes"] if node["state"] == "ok"}
            for edge in failed_edges:
                if edge["from"] in ok_ids and edge["to"] in ok_ids:
                    edge.update({"state": "ok", "error_code": None, "error": None})

            detail["kept"] = [
                node["node_id"]
                for node in package["nodes"]
                if node["state"] == "ok" and node["node_id"] not in detail["backfilled"]
            ]
            package["version_summary"] = self._summary(package["nodes"], package["edges"])
            package["failed_nodes"] = [node["node_id"] for node in package["nodes"] if node["state"] == "failed"]
            package["revision"] += 1
            package["status"] = "已完成" if not package["failed_nodes"] and all(
                edge["state"] == "ok" for edge in package["edges"]
            ) else "部分成功"
            package["updated_at"] = _now()
            package["retry_log"].append({
                "revision": package["revision"],
                "at": package["updated_at"],
                "backfilled": list(detail["backfilled"]),
                "kept": list(detail["kept"]),
                "still_failed": list(detail["still_failed"]),
            })
            self._log_repair(
                "失败补写",
                f"数据包 {package_id} 补写失败节点 {detail['backfilled'] or '无'}，已校验通过节点 {detail['kept'] or '无'} 未改写",
                package_id,
            )
            if detail["still_failed"]:
                return package, f"已补写 {len(detail['backfilled'])} 个失败节点，仍有 {len(detail['still_failed'])} 个节点状态不一致", ERR_NODE_STATE_INCONSISTENT, detail
            return package, f"已补写 {len(detail['backfilled'])} 个失败节点，数据包校验完整", None, detail

    def package_csv(self, package: dict[str, Any]) -> str:
        """可用关系的可下载版本：保留原导出字段，追加版本/状态/错误码，失败节点不省略。"""
        lines = ["type,id,name,from,to,label,status,version,state,error_code"]
        for node in package["nodes"]:
            lines.append(",".join([
                "node",
                csv_cell(node.get("node_id", "")),
                csv_cell(node.get("name", "")),
                "", "", "",
                csv_cell(node.get("status", "")),
                csv_cell(node.get("version", "")),
                csv_cell(node.get("state", "")),
                csv_cell(node.get("error_code") or ""),
            ]))
        for edge in package["edges"]:
            lines.append(",".join([
                "edge", "", "",
                csv_cell(edge.get("from", "")),
                csv_cell(edge.get("to", "")),
                csv_cell(edge.get("label", "")),
                "",
                csv_cell(edge.get("version", "")),
                csv_cell(edge.get("state", "")),
                csv_cell(edge.get("error_code") or ""),
            ]))
        return "\n".join(lines) + "\n"

    def repair_logs(self) -> list[dict[str, Any]]:
        """修复证据：重复显示、并发冲突、失败补写等处置记录。"""
        return list(self._repairs())


flowline_export_service = FlowlineExportService()
