"""Compare retrieval modes on the same human-labelled cases, without generating answers."""
from pathlib import Path
import statistics
from .models import configured


def resolve_cases(base, cases):
    documents = base.documents()
    output = []
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("question"),str) or not case["question"].strip():
            raise ValueError("每个评测样例都需要非空 question")
        relevant = set(case.get("relevant_ids", []))
        for name in case.get("relevant_sources", []):
            matches = [d["id"] for d in documents if d["name"] == name and (not case.get("category") or d["category"] == case["category"])]
            if len(matches) != 1:
                raise ValueError(f"评测来源 {name} 不存在或不唯一；请导入资料并指定分类")
            relevant.add(matches[0])
        if not relevant and not case.get("expected_empty", False):
            raise ValueError("正例必须提供 relevant_ids 或 relevant_sources")
        if relevant and case.get("expected_empty", False):
            raise ValueError("无答案样例不能同时提供相关来源")
        output.append(dict(case, relevant_ids=sorted(relevant)))
    if not output:
        raise ValueError("评测集不能为空")
    return output


def compare(base, cases, top_k=5, progress=None):
    cases = resolve_cases(base, cases)
    reports = {}
    modes = ["bm25", "hybrid", "reranked"]
    if configured(base.cfg,"bge"):
        modes.append("expanded")
    for mode in modes:
        if mode != "bm25" and not configured(base.cfg,"embedding"):
            reports[mode] = {"status":"not_configured", "reason":"Embedding 尚未启用"}
            continue
        if mode in {"reranked","expanded"} and not configured(base.cfg,"reranker"):
            reports[mode] = {"status":"not_configured", "reason":"Reranker 尚未启用"}
            continue
        records = []
        for index, case in enumerate(cases,1):
            try:
                result = base.query(case["question"],top_k,case.get("category"),mode=mode)
                if mode != "bm25" and "dense" not in result["routes"] and not (result.get("literal_miss") and case.get("expected_empty")):
                    raise ValueError("Embedding 未实际成功，不能算作混合检索效果")
                if mode == "expanded" and "BGE-M3-dense" not in result["routes"] and not (result.get("literal_miss") and case.get("expected_empty")):
                    raise ValueError("BGE-M3 云端通道未实际成功")
                if mode in {"reranked","expanded"} and result["results"] and not result["reranked"]:
                    raise ValueError("精排未实际成功，不能算作精排效果")
                relevant = set(case["relevant_ids"])
                hits = result["results"]
                found = {k for h in hits for k in (h["id"],h["doc_id"]) if k in relevant}
                rank = next((i for i,h in enumerate(hits,1) if h["id"] in relevant or h["doc_id"] in relevant),None)
                records.append({"question":case["question"],"status":"ok","negative":bool(case.get("expected_empty")),"recall":len(found)/len(relevant) if relevant else None,"hit_rate":int(bool(found)) if relevant else None,"mrr":1/rank if rank else 0 if relevant else None,"no_hit":int(not hits),"elapsed_ms":result["elapsed_ms"],"warnings":result["warnings"],"returned_ids":[h["id"] for h in hits]})
            except Exception as exc:
                records.append({"question":case["question"],"status":"failed","error":str(exc)})
            if progress:
                progress(mode,index,len(cases))
        success = [r for r in records if r["status"] == "ok"]
        positives = [r for r in success if not r["negative"]]
        negatives = [r for r in success if r["negative"]]
        failed = len(records)-len(success)
        # Failed requests invalidate the headline metrics; partial success is not presented as a clean gain.
        metrics = {k:sum(r[k] for r in positives)/len(positives) if positives and not failed else None for k in ("recall","hit_rate","mrr")}
        reports[mode] = {"status":"complete" if not failed else "incomplete","count":len(records),"failed":failed,**metrics,"no_hit_accuracy":sum(r["no_hit"] for r in negatives)/len(negatives) if negatives and not failed else None,"mean_ms":statistics.mean(r["elapsed_ms"] for r in success) if success else None,"cases":records}
    return {"top_k":top_k,"case_count":len(cases),"modes":reports,"note":"检索评测不调用 Chat；无答案项衡量是否返回片段，不等同于生成回答的拒答准确率。合成样例结果不能代表真实资料效果。"}
