"""Conservative deterministic entry router; the planner handles tool selection."""

import re

from obsai.agent.state import Intent


def route_intent(query: str) -> Intent:
    value = query.strip().lower()
    if re.match(r"^(search|find|搜索|查找|检索)\s*[:： ]", value):
        return "direct_search"
    if re.search(r"(整理|归档|organize|重构笔记)", value):
        return "organization_task"
    if re.search(r"(创建|新建|修改|更新|移动|重命名|删除|移到回收站|create|update|move|trash)", value):
        return "write_operation"
    if re.search(r"(读取|打开|查看笔记|read note|show note|反向链接|出链)", value):
        return "read_operation"
    return "rag_question"
