"""Opt-in synthetic benchmark; model calls use configured embedding/reranking services."""
import copy
import json
from pathlib import Path
from docqa.models import config
from docqa.docbase import DocBase
from docqa.benchmark import compare


def main():
    cfg = copy.deepcopy(config())
    cfg["data_dir"] = "data/evaluation-cloud"
    cfg["chat"] = {"enabled":False}
    cfg["vision"] = {"enabled":False}
    cfg["sparse_model_path"] = ""
    base = DocBase(cfg)
    for path in sorted(Path("examples/evaluation/documents").glob("*.md")):
        result = base.build(path,"合成评测")
        print(json.dumps({"build":path.name,"status":result["status"]},ensure_ascii=True),flush=True)
    cases = json.loads(Path("examples/evaluation/cases.json").read_text(encoding="utf-8"))
    def progress(mode,index,total):
        if index % 5 == 0 or index == total:
            print(json.dumps({"mode":mode,"completed":index,"total":total}),flush=True)
    result = compare(base,cases,1,progress)
    Path("docs/retrieval-comparison.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({mode:{k:v for k,v in report.items() if k != "cases"} for mode,report in result["modes"].items()},ensure_ascii=True),flush=True)


if __name__ == "__main__":
    main()
