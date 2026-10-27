"""Validate a restricted graph schema and emit Mermaid without executable directives."""
import json
import re


def graph_description(raw):
    content = raw.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
    try:
        graph = json.loads(content)
    except ValueError:
        # Text-only model responses remain useful evidence, but are not labelled as diagrams.
        return raw, None
    if not isinstance(graph, dict) or not isinstance(graph.get("description"), str):
        raise ValueError("视觉 JSON 缺少 description 字符串")
    nodes, edges = graph.get("nodes", []), graph.get("edges", [])
    if not isinstance(nodes,list) or not isinstance(edges,list) or len(nodes)>200 or len(edges)>500:
        raise ValueError("图表节点或连线格式/数量无效")
    ids = {}
    for node in nodes:
        if not isinstance(node,dict) or not isinstance(node.get("id"),str) or not isinstance(node.get("label"),str):
            raise ValueError("图表节点必须具有 id 和 label 字符串")
        if node["id"] in ids:
            raise ValueError("图表节点 ID 重复")
        ids[node["id"]] = f"n{len(ids)}"
    def quote(text):
        if not isinstance(text,str):
            raise ValueError("连线标签必须是字符串")
        # Mermaid entity escaping; output contains no user-controlled node IDs or directives.
        return "".join(f"#{ord(c)};" if c in '"<>[]{}|`\\&\n\r' else c for c in text)
    lines = ["flowchart TD"]
    for node in nodes:
        lines.append(f'  {ids[node["id"]]}["{quote(node["label"])}"]')
    for edge in edges:
        if not isinstance(edge,dict) or not isinstance(edge.get("source"), str) or not isinstance(edge.get("target"), str) or edge["source"] not in ids or edge["target"] not in ids:
            raise ValueError("图表连线引用不存在的节点")
        label = quote(edge.get("label", ""))
        arrow = f'-->|"{label}"|' if label else "-->"
        lines.append(f'  {ids[edge["source"]]} {arrow} {ids[edge["target"]]}')
    description = graph["description"]
    if nodes:
        description += "\n\n节点：\n" + "\n".join(f"- {n['id']}: {n['label']}" for n in nodes)
        description += "\n\n连线：\n" + "\n".join(f"- {e['source']} → {e['target']}: {e.get('label','')}" for e in edges)
        description += "\n\n```mermaid\n" + "\n".join(lines) + "\n```\n\n[模型解读：结构检查通过，语义及箭头方向仍需对照原图人工确认。]"
    return description, {"description":graph["description"],"nodes":nodes,"edges":edges,"structure_checked":True,"human_verified":False}
