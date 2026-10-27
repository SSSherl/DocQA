import argparse
import json
from pathlib import Path
from .models import config
from .docbase import DocBase
from .doc2md import SUPPORTED, parse_document
from .agent import answer_question


def main():
    parser = argparse.ArgumentParser(description="DocQA 本地文档智能问答")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("build")
    p.add_argument("path")
    p.add_argument("--category", default="默认")
    p.add_argument("--force", action="store_true")
    for cmd in ("query", "ask"):
        p = sub.add_parser(cmd)
        p.add_argument("question")
        p.add_argument("--top-k", type=int, default=5)
        p.add_argument("--category")
    p = sub.add_parser("parse")
    p.add_argument("path")
    p.add_argument("--out", default="data/converted")
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=8765)
    p = sub.add_parser("test")
    p.add_argument("cases", help="JSON 文件：question + relevant_ids 列表")
    p.add_argument("--top-k", type=int, default=5)
    p = sub.add_parser("compare", help="对比 BM25、混合检索、混合检索＋精排")
    p.add_argument("cases")
    p.add_argument("--top-k", type=int, default=5)
    p.add_argument("--out")
    p = sub.add_parser("setup-retrieval", help="验证向量和精排服务，成功重建后才激活")
    p.add_argument("settings", help="只包含 embedding/reranker 等设置的 JSON，不含明文密钥")
    sub.add_parser("rebuild", help="重建已有文档，保留分类")
    p = sub.add_parser("setup-bge",help="验证并启用 BGE-M3 云端补充稠密向量")
    p.add_argument("settings")
    p = sub.add_parser("setup-visual", help="验证图文向量并建立已有图片索引")
    p.add_argument("settings")
    sub.add_parser("refresh-images", help="仅重建图片向量")
    sub.add_parser("watch")
    sub.add_parser("list")
    p = sub.add_parser("doctor", help="验证已配置模型，仅发送最小测试内容，不发送文档")
    p.add_argument("--role", choices=["chat","vision","embedding","reranker","bge","visual_embedding"])
    p = sub.add_parser("credential-set", help="交互式写入 Windows 当前用户加密凭证，不回显密钥")
    p.add_argument("name")
    p = sub.add_parser("delete")
    p.add_argument("id")
    args = parser.parse_args()
    cfg = config(args.config)
    base = DocBase(cfg)
    def show(obj):
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    if args.command == "credential-set":
        import getpass
        from .credentials import save_secret
        save_secret(args.name, getpass.getpass("API key (hidden): "))
        show({"status":"stored", "name":args.name})
    elif args.command == "doctor":
        from .diagnostics import diagnose
        show(diagnose(cfg, [args.role] if args.role else None))
    elif args.command == "build":
        path = Path(args.path)
        files = sorted(p for p in path.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED) if path.is_dir() else [path]
        failed = False
        for i,file in enumerate(files,1):
            try:
                show({"progress":f"{i}/{len(files)}", "file":str(file), **base.build(file,args.category,args.force)})
            except Exception as exc:
                show({"file":str(file),"error":str(exc)})
                failed = True
        if failed:
            raise SystemExit(1)
    elif args.command == "parse":
        show(parse_document(args.path,args.out,cfg))
    elif args.command == "query":
        show(base.query(args.question,args.top_k,args.category))
    elif args.command == "ask":
        show(answer_question(base,args.question,args.top_k,args.category))
    elif args.command == "serve":
        from .server import serve
        serve(base,args.port,args.config)
    elif args.command == "test":
        show(base.evaluate(json.loads(Path(args.cases).read_text(encoding="utf-8")),args.top_k))
    elif args.command == "compare":
        from .benchmark import compare
        report = compare(base,json.loads(Path(args.cases).read_text(encoding="utf-8")),args.top_k)
        if args.out:
            Path(args.out).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        show(report)
    elif args.command == "setup-retrieval":
        from .retrieval_setup import prepare_upgrade
        result = prepare_upgrade(base,json.loads(Path(args.settings).read_text(encoding="utf-8")),args.config)
        show(result)
        if result["status"] != "activated":
            raise SystemExit(1)
    elif args.command == "rebuild":
        result = base.rebuild_all()
        show(result)
        if result["failed"]:
            raise SystemExit(1)
    elif args.command == "setup-bge":
        from .retrieval_setup import prepare_bge_upgrade
        endpoint = json.loads(Path(args.settings).read_text(encoding="utf-8"))["bge"]
        result = prepare_bge_upgrade(base,endpoint,args.config)
        show(result)
        if result["status"] != "activated":
            raise SystemExit(1)
    elif args.command == "setup-visual":
        from .retrieval_setup import prepare_visual_upgrade
        settings = json.loads(Path(args.settings).read_text(encoding="utf-8"))
        show(prepare_visual_upgrade(base, settings["visual_embedding"], args.config))
    elif args.command == "refresh-images":
        from .visual_index import refresh_images
        show(refresh_images(base))
    elif args.command == "list":
        show(base.documents())
    elif args.command == "delete":
        base.delete(args.id)
    elif args.command == "watch":
        from .service import Watcher
        watcher = Watcher(base)
        try:
            watcher.run()
        except KeyboardInterrupt:
            watcher.stop.set()


if __name__ == "__main__":
    main()
