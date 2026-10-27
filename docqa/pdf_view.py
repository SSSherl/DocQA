"""Render actual PDF pages and locate verbatim chunk text, never inferred highlights."""
import hashlib
import math
from pathlib import Path
import re
import pymupdf


def source(base, doc_id):
    doc = next((d for d in base.documents() if d["id"] == doc_id), None)
    if not doc or Path(doc["source"]).suffix.lower() != ".pdf":
        raise ValueError("该来源不是可预览的 PDF")
    path = Path(doc["source"])
    if not path.is_file():
        raise ValueError("PDF 原文件不存在")
    if hashlib.sha256(path.read_bytes()).hexdigest() != doc["hash"]:
        raise ValueError("PDF 原文件已变化，请重新导入后定位")
    return doc, path


def locate(base, doc_id, number, chunk_id=""):
    doc, path = source(base, doc_id)
    with pymupdf.open(path) as pdf:
        if not 1 <= number <= len(pdf):
            raise ValueError("PDF 页码超出范围")
        page = pdf[number-1]
        rects = []
        note = "已定位原文页面。"
        if chunk_id:
            with base.connect() as db:
                chunk = db.execute("SELECT text,page,image FROM chunks WHERE id=? AND doc_id=?", (chunk_id,doc_id)).fetchone()
            if not chunk:
                raise ValueError("引用片段不存在或不属于该文档")
            if chunk["page"] == number and not chunk["image"]:
                # Match complete original lines. No semantic guessing for diagram descriptions.
                phrases = [t.strip() for t in chunk["text"].splitlines() if len(re.sub(r"\s+", " ", t).strip()) >= 12]
                for phrase in phrases[:24]:
                    if len(phrase) > 300:
                        phrase = phrase[:200]
                    for rect in page.search_for(phrase):
                        rect = rect * page.rotation_matrix
                        key = tuple(round(v,2) for v in rect)
                        if key not in rects:
                            rects.append(key)
                note = "已高亮匹配到的原文文字。" if rects else "已定位页面，未找到可精确高亮的文字。"
            elif chunk["image"] and chunk["page"] == number:
                note = "该引用来自图表解读，已定位原图所在页。"
            else:
                note = "当前浏览页不是该引用的原始页。"
        origin = page.rect
        return {"doc_id":doc_id,"name":doc["name"],"page":number,"pages":len(pdf),"highlights":[[(x0-origin.x0)/origin.width,(y0-origin.y0)/origin.height,(x1-x0)/origin.width,(y1-y0)/origin.height] for x0,y0,x1,y1 in rects],"note":note}


def render_page(base, doc_id, number):
    _, path = source(base, doc_id)
    with pymupdf.open(path) as pdf:
        if not 1 <= number <= len(pdf):
            raise ValueError("PDF 页码超出范围")
        page = pdf[number-1]
        scale = min(2., math.sqrt(20_000_000 / max(1,page.rect.width*page.rect.height)))
        return page.get_pixmap(matrix=pymupdf.Matrix(scale,scale),alpha=False).tobytes("png")
