"""Create a source-only release from an explicit allowlist. Never copy local data."""
from datetime import datetime, timezone, timedelta
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile
from urllib.parse import urlparse


def build(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.is_relative_to(source):
        raise ValueError("发布目录必须位于项目目录之外")
    defaults = json.loads((source/"config.example.json").read_text(encoding="utf-8"))
    roles = ("chat","vision","embedding","reranker","bge","visual_embedding")
    if defaults.get("allow_remote_models") is not False or any(defaults.get(role,{}).get("enabled") is not False or any(defaults[role].get(field) for field in ("api_key_store","api_key","key","token","access_token")) for role in roles):
        raise ValueError("发布默认配置必须关闭模型并清空凭证；可保留公开接口与模型名")
    for role in roles:
        for field in ("base_url", "url"):
            value = defaults[role].get(field)
            if value:
                parsed = urlparse(value)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                    raise ValueError("发布接口不能含密钥、认证信息或查询参数")
    def check_bindings(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"api_key_store", "api_key_env", "api_key", "key", "token", "access_token"} and value:
                    raise ValueError("发布配置示例不能含密钥或凭证绑定")
                check_bindings(value)
        elif isinstance(node, list):
            for value in node:
                check_bindings(value)
    for example in source.glob("config.*example.json"):
        check_bindings(json.loads(example.read_text(encoding="utf-8")))
    destination.mkdir(parents=True, exist_ok=False)
    stage = destination / "DocQA"
    stage.mkdir()
    files = [source / name for name in (".gitignore","AGENTS.md","README.md","pyproject.toml","start.ps1")]
    files += list(source.glob("config.*example.json"))
    files += [source/"docs"/name for name in ("需求规格说明书.md","发布说明.md","部署手册.md","验收测试清单.md")]
    for folder in ("docqa","examples","scripts","tests"):
        files += [p for p in (source/folder).rglob("*") if p.is_file() and "__pycache__" not in p.parts and p.suffix not in {".pyc",".pyo"}]
    manifest = []
    key_pattern = re.compile(rb"(?:sk-[A-Za-z0-9_-]{24,}|Bearer [A-Za-z0-9_-]{24,})")
    for path in sorted(set(files)):
        name = path.relative_to(source)
        if path.suffix in {".dpapi",".sqlite",".sqlite3",".log",".zip"} or path.name == "config.json":
            raise ValueError("发布白名单包含私人或生成文件")
        raw = path.read_bytes()
        if key_pattern.search(raw):
            raise ValueError("检测到疑似密钥，请先清理："+str(name))
        target = stage/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(path,target)
        manifest.append({"path":name.as_posix(),"bytes":len(raw),"sha256":hashlib.sha256(raw).hexdigest()})
    report = {"source_files":len(manifest),"default_models_disabled":True,"personal_data_included":False,"files":manifest}
    (stage/"RELEASE-MANIFEST.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    archive = destination/"DocQA-source-release.zip"
    with zipfile.ZipFile(archive,"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6) as zipped:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                zipped.write(path,"DocQA/"+path.relative_to(stage).as_posix())
    with zipfile.ZipFile(archive) as zipped:
        if zipped.testzip():
            raise RuntimeError("发布包完整性检查失败")
    result = {"archive":str(archive),"folder":str(stage),"files":len(manifest)+1,"bytes":archive.stat().st_size,"sha256":hashlib.sha256(archive.read_bytes()).hexdigest()}
    (destination/"release-info.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    return result


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    parser.add_argument("--out",default=str(root.parent/"DocQA-releases"/stamp))
    args = parser.parse_args()
    print(json.dumps(build(root,args.out),ensure_ascii=False,indent=2))
