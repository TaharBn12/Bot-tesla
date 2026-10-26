#!/usr/bin/env python3
"""
كشف معرّف المشروع أو اسم الخدمة من مخرجات Railway CLI (JSON).

    python3 scripts/railway_discover.py projects /tmp/projects.json
    python3 scripts/railway_discover.py services /tmp/svc.json

يطبع القيمة الوحيدة إن وُجدت، أو سطراً فارغاً إن كان هناك أكثر من خيار
(حتى لا يختار المستخدم مشروعاً خاطئاً بالخطأ).

الصيغ المدعومة: قائمة مباشرة، أو كائن فيه مفتاح projects/services/edges/nodes.
"""
import json
import sys


def unwrap(data):
    """يحوّل أي شكل JSON محتمل إلى قائمة عناصر."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("projects", "services", "results", "items", "data"):
            if key in data:
                return unwrap(data[key])
        if "edges" in data:
            return [e.get("node", e) for e in data["edges"] if isinstance(e, dict)]
        if "nodes" in data:
            return data["nodes"]
    return []


def pick(path: str, field: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except Exception:
        return ""
    items = unwrap(data)
    values = [i.get(field) for i in items if isinstance(i, dict) and i.get(field)]
    # بلا تكرار مع الحفاظ على الترتيب
    seen = []
    for v in values:
        if v not in seen:
            seen.append(v)
    return seen[0] if len(seen) == 1 else ""


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("", end="")
        sys.exit(1)
    kind, path = sys.argv[1], sys.argv[2]
    field = "name" if kind == "services" else "id"
    print(pick(path, field), end="")
