"""Structured demo data for the read-only ops console panels (sre-agent-ui).

The `/api/ui/*` endpoints read live data where a real source exists — the Qdrant
runbook index — and fall back to these fixtures where the current environment has
no backing store (no kubectl context, DATABASE_URL unset, so no findings / audit /
approvals). Every payload carries ``"source": "demo"`` so the UI can label what is
real versus illustrated, and the routes override individual fields with live values
(e.g. the pending-approvals count from in-memory sessions) where one is available.
"""

DASHBOARD = {
    "source": "demo",
    "health_score": "94",
    "health_score_delta": "较上次 +2",
    "pods_running": "1,247",
    "pods_pending": "32 个 Pending",
    "alerts_open": "3",
    "alerts_critical": "2 个 CRITICAL",
    "pending_approvals": "2",
    "pending_approvals_delta": "等待人工确认",
    "recent_alerts": [
        {
            "title": "default / Deployment / api-gateway 内存使用超过 85%",
            "meta": "2 分钟前 · 持续 12 分钟",
            "severity": "critical",
        },
        {
            "title": "kube-system / Node / ip-10-0-4-7 磁盘压力阈值触发",
            "meta": "15 分钟前 · 持续 1 小时",
            "severity": "warning",
        },
        {
            "title": "monitoring / Pod / prometheus-0 重启 2 次",
            "meta": "32 分钟前 · 已恢复",
            "severity": "info",
        },
    ],
    "resources": {"cpu": 62, "memory": 78, "storage": 45},
}

HEALTH = {
    "source": "demo",
    "health_score": "94",
    "health_score_delta": "较昨日 +2",
    "checks_passed": "156",
    "checks_total": "162",
    "checks_delta": "6 项异常",
    "critical_count": "3",
    "critical_delta": "需立即处理",
    "checks": [
        {"name": "API Server 可用性", "target": "kube-apiserver", "last_checked": "2 分钟前", "status": "pass"},
        {"name": "节点磁盘压力", "target": "ip-10-0-4-7", "last_checked": "5 分钟前", "status": "fail"},
        {"name": "Pod 内存使用率", "target": "default/api-gateway", "last_checked": "8 分钟前", "status": "warn"},
        {"name": "ETCD 集群状态", "target": "etcd-0,1,2", "last_checked": "12 分钟前", "status": "pass"},
        {"name": "证书过期时间", "target": "ingress-tls", "last_checked": "1 小时前", "status": "warn"},
    ],
}

APPROVALS = {
    "source": "demo",
    "pending": "2",
    "pending_delta": "平均等待 12 分钟",
    "approved_today": "18",
    "approved_delta": "+3 较昨日",
    "rejected_today": "1",
    "rejected_delta": "风险操作",
    "sla": "98%",
    "sla_delta": "目标 95%",
    "items": [
        {
            "title": "扩容 api-gateway Deployment",
            "meta": "replicas: 3 → 5 · namespace: default",
            "applicant": "zhangsan",
            "time": "10:23",
            "risk": "中风险",
            "risk_level": "warning",
            "expired": False,
        },
        {
            "title": "清理节点 docker 镜像缓存",
            "meta": "node: ip-10-0-4-7 · Runbook: node-disk-pressure-cleanup",
            "applicant": "lisi",
            "time": "09:45",
            "risk": "低风险",
            "risk_level": "success",
            "expired": False,
        },
        {
            "title": "修改 ConfigMap nginx-conf",
            "meta": "namespace: ingress-nginx · key: proxy-timeout",
            "applicant": "wangwu",
            "time": "昨天 18:30",
            "risk": "高风险",
            "risk_level": "error",
            "expired": True,
        },
    ],
}

AUDIT = {
    "source": "demo",
    "count": 5,
    "items": [
        {"time": "2026-09-09 10:23:18", "action": "执行 Runbook", "resource": "pod-crash-loop-restart / default", "user": "zhangsan", "result": "成功", "result_level": "success"},
        {"time": "2026-09-09 10:20:05", "action": "审批通过", "resource": "api-gateway 扩容申请", "user": "admin", "result": "成功", "result_level": "success"},
        {"time": "2026-09-09 09:58:42", "action": "执行健康检查", "resource": "prod-cluster-01", "user": "lisi", "result": "6 项异常", "result_level": "warning"},
        {"time": "2026-09-09 09:45:11", "action": "登录系统", "resource": "控制台", "user": "wangwu", "result": "成功", "result_level": "success"},
        {"time": "2026-09-09 09:12:33", "action": "修改 ConfigMap", "resource": "nginx-conf / ingress-nginx", "user": "wangwu", "result": "失败", "result_level": "error"},
    ],
}
