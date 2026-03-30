from fastapi import FastAPI, Depends, UploadFile, File, Form
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func
import pandas as pd
import io
import os
import sys
import re
import docx
from urllib.parse import quote
from difflib import SequenceMatcher

from database import engine, Base, get_db
from models import Signage, ActionHistory
from utils import process_dataframe_pipeline, get_sort_key

# 建立数据库表
Base.metadata.create_all(bind=engine)
app = FastAPI(title="公示语多语言词库系统")

def get_resource_path(relative_path):
    """获取资源的绝对路径"""
    if getattr(sys, 'frozen', False):
        base_path = sys._MEIPASS
    else:
        base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

# ================= 工具函数：Word 文本提取 =================

def extract_zh_sign(text: str) -> str:
    if not text: return ""
    text = text.replace("：", ":")
    if "英文:" in text: text = text.split("英文:")[0]
    text = text.replace("中文:", "").strip()
    
    if re.search(r'[\u4e00-\u9fa5]', text):
        match = re.search(r'[A-Za-z][^\u4e00-\u9fa5]*$', text)
        if match:
            eng_part = match.group(0)
            if re.search(r'[A-Za-z]{2,}', eng_part) or len(re.findall(r'[A-Za-z]', eng_part)) > 2:
                text = text[:match.start()]
    return text.strip()

def extract_en_suggestion(text: str) -> str:
    if not text: return ""
    text = text.replace("：", ":")
    if "英文:" in text: return text.split("英文:")[1].strip()
        
    lines = text.split('\n')
    en_lines = []
    for line in lines:
        if re.search(r'[A-Za-z]', line) and not re.search(r'[\u4e00-\u9fa5]', line):
            en_lines.append(line.strip())
    if en_lines: return "\n".join(en_lines)
        
    match = re.search(r'[A-Za-z][^\u4e00-\u9fa5]*$', text)
    if match:
        eng_part = match.group(0)
        if re.search(r'[A-Za-z]{2,}', eng_part) or len(re.findall(r'[A-Za-z]', eng_part)) > 2:
            return eng_part.strip()
        
    cleaned = re.sub(r'[\u4e00-\u9fa5：，。！？、\n]+', ' ', text)
    return cleaned.strip()

# ================= 核心路由接口 =================

@app.get("/")
async def read_root():
    html_path = get_resource_path(os.path.join("templates", "index.html"))
    return FileResponse(html_path)

@app.get("/api/search")
def search_signage(keyword: str = "", db: Session = Depends(get_db)):
    if not keyword.strip(): return {"data": []}
    
    clean_keyword = keyword.replace('\n', '').replace('\r', '').replace(' ', '')
    all_signs = db.query(Signage).all()
    grouped = {}
    
    for sign in all_signs:
        # 【核心修复】：拦截所有的空值、NaN 等异常空白数据
        if not sign.zh_term or str(sign.zh_term).lower() == 'nan':
            continue
            
        clean_zh = str(sign.zh_term).replace('\n', '').replace('\r', '').replace(' ', '')
        if not clean_zh:
            continue
            
        if clean_keyword == clean_zh:
            score = 100.0
        else:
            score = SequenceMatcher(None, clean_keyword, clean_zh).ratio() * 100.0
            if clean_keyword in clean_zh or clean_zh in clean_keyword:
                # 【核心修复】：引入长度惩罚因子，解决 "人" > "人行横道" 的问题
                len_ratio = min(len(clean_keyword), len(clean_zh)) / max(len(clean_keyword), len(clean_zh))
                # 长度相差越大，加分越少
                score = max(score, 70.0 + 29.0 * len_ratio)
        
        if score >= 40.0:
            zh = sign.zh_term
            if zh not in grouped:
                grouped[zh] = {
                    "zh_text": zh, "category": sign.category,
                    "translations": set(), "seq_id": sign.sequence_id,
                    "associated_items": [], "show_assoc": False,
                    "match_score": score
                }
            grouped[zh]["translations"].add(sign.en_translation)
            if score > grouped[zh]["match_score"]:
                grouped[zh]["match_score"] = score

    if not grouped: return {"data": []}

    seq_ids = [data["seq_id"] for data in grouped.values() if data["seq_id"]]
    associated = db.query(Signage).filter(Signage.sequence_id.in_(seq_ids)).all() if seq_ids else []
    all_associated_items = {item.id: item for item in associated}.values()
        
    for zh, data in grouped.items():
        data["translations"] = list(data["translations"])
        if data["seq_id"]:
            bros = [i for i in all_associated_items if i.sequence_id == data["seq_id"] and i.zh_term != zh]
            bros.sort(key=lambda x: x.sequence_order or 0)
            seen_bro_zh = set()
            for b in bros:
                if b.zh_term not in seen_bro_zh:
                    data["associated_items"].append({
                        "zh_text": b.zh_term, 
                        "en_texts": [x.en_translation for x in bros if x.zh_term == b.zh_term],
                        "order": b.sequence_order
                    })
                    seen_bro_zh.add(b.zh_term)

    result_list = list(grouped.values())
    result_list.sort(key=lambda x: (-x["match_score"], get_sort_key(x["zh_text"])))
    
    return {"data": result_list}

# 【全新接口】：支持全面修改中文、英文和分类，并自动抹平换行符差异
@app.post("/api/edit_signage")
def edit_signage(
    old_zh: str = Form(...), old_en: str = Form(...),
    new_zh: str = Form(...), new_en: str = Form(...), category: str = Form(""),
    db: Session = Depends(get_db)
):
    # 清洗所有的前端传入换行符
    clean_old_zh = old_zh.replace('\r\n', '\n').replace('\r', '')
    clean_old_en = old_en.replace('\r\n', '\n').replace('\r', '')
    clean_new_zh = new_zh.replace('\r\n', '\n').replace('\r', '')
    clean_new_en = new_en.replace('\r\n', '\n').replace('\r', '')

    record = db.query(Signage).filter(
        func.replace(Signage.zh_term, '\r', '') == clean_old_zh,
        func.replace(Signage.en_translation, '\r', '') == clean_old_en
    ).first()

    if not record: 
        return {"status": "error", "msg": "在数据库中找不到原始词条，修改失败。"}
    
    if (clean_old_zh != clean_new_zh or clean_old_en != clean_new_en):
        if db.query(Signage).filter(
            func.replace(Signage.zh_term, '\r', '') == clean_new_zh,
            func.replace(Signage.en_translation, '\r', '') == clean_new_en
        ).first():
            return {"status": "error", "msg": "修改后的中英文组合已在词库中存在！"}

    record.zh_term = clean_new_zh
    record.en_translation = clean_new_en
    record.category = category
    
    db.add(ActionHistory(action_type="编辑", zh_term=clean_new_zh, en_translation=clean_new_en, category=category))
    db.commit()
    return {"status": "success", "msg": "词条全方位修改成功！"}

@app.post("/api/add_single")
def add_single(zh_text: str = Form(...), en_text: str = Form(...), db: Session = Depends(get_db)):
    clean_zh = zh_text.replace('\r\n', '\n').replace('\r', '')
    clean_en = en_text.replace('\r\n', '\n').replace('\r', '')
    
    df = pd.DataFrame([{"公示语中文": clean_zh, "英文译文": clean_en}])
    processed_list = process_dataframe_pipeline(df)
    
    added_count = 0
    for item in processed_list:
        if not db.query(Signage).filter_by(zh_term=item['zh_text'], en_translation=item['en_text']).first():
            seq_id = None if pd.isna(item['seq_id']) else str(item['seq_id'])
            seq_order = None if pd.isna(item['seq_order']) else int(item['seq_order'])
            
            db.add(Signage(
                zh_term=item['zh_text'], en_translation=item['en_text'],
                sequence_id=seq_id, sequence_order=seq_order, category=item['category']
            ))
            db.add(ActionHistory(action_type="新增", zh_term=item['zh_text'], en_translation=item['en_text'], category=item['category']))
            added_count += 1
    db.commit()
    return {"status": "success", "msg": f"录入成功，受影响条数：{added_count}"}

@app.post("/api/upload_excel")
async def upload_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):
    contents = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(contents), engine='openpyxl')
        if '公示语中文' not in df.columns or '英文译文' not in df.columns:
            return {"status": "error", "msg": "Excel必须包含 '公示语中文' 和 '英文译文' 两列！"}
            
        processed_list = process_dataframe_pipeline(df)
        added_count, skip_count = 0, 0
        for item in processed_list:
            if not db.query(Signage).filter_by(zh_term=item['zh_text'], en_translation=item['en_text']).first():
                seq_id = None if pd.isna(item['seq_id']) else str(item['seq_id'])
                seq_order = None if pd.isna(item['seq_order']) else int(item['seq_order'])
                
                db.add(Signage(
                    zh_term=item['zh_text'], en_translation=item['en_text'],
                    sequence_id=seq_id, sequence_order=seq_order, category=item['category']
                ))
                db.add(ActionHistory(action_type="Excel导入", zh_term=item['zh_text'], en_translation=item['en_text'], category=item['category']))
                added_count += 1
            else:
                skip_count += 1
        db.commit()
        return {"status": "success", "msg": f"解析完毕！新增 {added_count} 条，由于重复跳过 {skip_count} 条。"}
    except Exception as e:
        return {"status": "error", "msg": str(e)}

@app.post("/api/upload_docx")
async def upload_docx(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if file.filename.endswith('.doc'):
        return {"status": "error", "msg": "不支持旧版 .doc 格式，为防止乱码，请先在本地另存为 .docx 格式后重试！"}
    elif not file.filename.endswith('.docx'):
        return {"status": "error", "msg": "请上传 .docx 格式的 Word 文档！"}

    contents = await file.read()
    try:
        doc = docx.Document(io.BytesIO(contents))
    except Exception as e:
        return {"status": "error", "msg": f"读取 Word 文档失败: {e}"}

    extracted_data = []
    for table in doc.tables:
        if len(table.rows) == 0: continue
        header_row = [cell.text.strip() for cell in table.rows[0].cells]
        try:
            sign_col_idx = header_row.index("标识内容")
            suggestion_col_idx = header_row.index("整改建议")
        except ValueError:
            continue

        for row in table.rows[1:]:
            if len(row.cells) <= max(sign_col_idx, suggestion_col_idx): continue
            
            try:
                raw_sign = row.cells[sign_col_idx].text.strip()
                raw_suggestion = row.cells[suggestion_col_idx].text.strip()
            except Exception:
                continue 
                
            if len(raw_sign) > 1000 or len(raw_suggestion) > 1000:
                continue
                
            if not raw_sign and not raw_suggestion: continue
                
            clean_zh = extract_zh_sign(raw_sign)
            clean_en = extract_en_suggestion(raw_suggestion)
            if clean_zh and clean_en:
                extracted_data.append({"公示语中文": clean_zh, "英文译文": clean_en})

    if not extracted_data:
        return {"status": "error", "msg": "未能在文档中提取到有效的标语数据，请检查表格是否包含'标识内容'和'整改建议'列。"}

    try:
        df = pd.DataFrame(extracted_data)
        processed_list = process_dataframe_pipeline(df)
        
        added_count, skip_count = 0, 0
        for item in processed_list:
            if not db.query(Signage).filter_by(zh_term=item['zh_text'], en_translation=item['en_text']).first():
                seq_id = None if pd.isna(item['seq_id']) else str(item['seq_id'])
                seq_order = None if pd.isna(item['seq_order']) else int(item['seq_order'])
                
                db.add(Signage(
                    zh_term=item['zh_text'], en_translation=item['en_text'],
                    sequence_id=seq_id, sequence_order=seq_order, category=item['category']
                ))
                db.add(ActionHistory(action_type="Word导入", zh_term=item['zh_text'], en_translation=item['en_text'], category=item['category']))
                added_count += 1
            else:
                skip_count += 1
        db.commit()
        return {"status": "success", "msg": f"Word 解析完毕！新增 {added_count} 条，由于重复跳过 {skip_count} 条。"}
    except Exception as e:
        return {"status": "error", "msg": f"数据处理过程中发生错误: {str(e)}"}

@app.post("/api/match_docx")
async def match_docx(file: UploadFile = File(...), db: Session = Depends(get_db)):
    if file.filename.endswith('.doc'):
        return {"status": "error", "msg": "不支持旧版 .doc 格式，请另存为 .docx 格式后重试！"}
    elif not file.filename.endswith('.docx'):
        return {"status": "error", "msg": "请上传 .docx 格式的 Word 文档！"}

    contents = await file.read()
    try:
        doc = docx.Document(io.BytesIO(contents))
    except Exception as e:
        return {"status": "error", "msg": f"读取 Word 文档失败: {e}"}

    extracted_zh_terms = set()
    for table in doc.tables:
        if len(table.rows) == 0: continue
        header_row = [cell.text.strip() for cell in table.rows[0].cells]
        try:
            sign_col_idx = header_row.index("标识内容")
        except ValueError:
            continue

        for row in table.rows[1:]:
            if len(row.cells) <= sign_col_idx: continue
            try:
                raw_sign = row.cells[sign_col_idx].text.strip()
            except Exception:
                continue
                
            if len(raw_sign) > 1000 or not raw_sign:
                continue
                
            clean_zh = extract_zh_sign(raw_sign)
            # 【核心修复】：防止空值进入提取集合
            if clean_zh and clean_zh.lower() != 'nan':
                extracted_zh_terms.add(clean_zh)

    if not extracted_zh_terms:
        return {"status": "error", "msg": "未能在文档中提取到有效的中文标识（请确保表格包含'标识内容'列）。"}

    clean_extracted = {term.replace('\n', '').replace('\r', '').replace(' ', '') for term in extracted_zh_terms}
    db_zh_clean = func.replace(func.replace(func.replace(Signage.zh_term, '\n', ''), '\r', ''), ' ', '')
    matched = db.query(Signage).filter(db_zh_clean.in_(clean_extracted)).all()

    seq_ids = [m.sequence_id for m in matched if m.sequence_id]
    associated = db.query(Signage).filter(Signage.sequence_id.in_(seq_ids)).all() if seq_ids else []
        
    grouped = {}
    for item in matched:
        if item.zh_term not in grouped:
            grouped[item.zh_term] = {
                "zh_text": item.zh_term, "category": item.category,
                "translations": set(), "seq_id": item.sequence_id,
                "associated_items": [], "show_assoc": False
            }
        grouped[item.zh_term]["translations"].add(item.en_translation)

    all_associated_items = {item.id: item for item in associated}.values()
    for zh, data in grouped.items():
        data["translations"] = list(data["translations"])
        if data["seq_id"]:
            bros = [i for i in all_associated_items if i.sequence_id == data["seq_id"] and i.zh_term != zh]
            bros.sort(key=lambda x: x.sequence_order or 0)
            seen_bro_zh = set()
            for b in bros:
                if b.zh_term not in seen_bro_zh:
                    data["associated_items"].append({
                        "zh_text": b.zh_term, 
                        "en_texts": [x.en_translation for x in bros if x.zh_term == b.zh_term],
                        "order": b.sequence_order
                    })
                    seen_bro_zh.add(b.zh_term)

    result_list = list(grouped.values())
    result_list.sort(key=lambda x: get_sort_key(x["zh_text"]))
    
    return {
        "status": "success", 
        "data": result_list, 
        "extracted_count": len(extracted_zh_terms)
    }

@app.get("/api/history")
def get_history(category: str = "", limit: int = 100, db: Session = Depends(get_db)):
    query = db.query(ActionHistory)
    if category: query = query.filter(ActionHistory.category == category)
    records = query.order_by(ActionHistory.created_at.desc()).limit(limit).all()
    return {"data": records}

@app.post("/api/history/update")
def update_history(id: int = Form(...), zh_term: str = Form(...), en_translation: str = Form(...), category: str = Form(...), db: Session = Depends(get_db)):
    clean_zh = zh_term.replace('\r\n', '\n').replace('\r', '')
    clean_en = en_translation.replace('\r\n', '\n').replace('\r', '')

    record = db.query(ActionHistory).filter(ActionHistory.id == id).first()
    if not record:
        return {"status": "error", "msg": "找不到该条历史记录"}

    sign = db.query(Signage).filter(Signage.zh_term == record.zh_term, Signage.en_translation == record.en_translation).first()
    
    record.zh_term = clean_zh
    record.en_translation = clean_en
    record.category = category
    record.action_type = "记录修改"
    
    if sign:
        sign.zh_term = clean_zh
        sign.en_translation = clean_en
        sign.category = category

    db.commit()
    return {"status": "success", "msg": "词条及分类已成功更新！"}

@app.post("/api/history/delete")
def delete_history(id: int = Form(...), db: Session = Depends(get_db)):
    record = db.query(ActionHistory).filter(ActionHistory.id == id).first()
    if not record:
        return {"status": "error", "msg": "找不到该条历史记录"}

    sign = db.query(Signage).filter(Signage.zh_term == record.zh_term, Signage.en_translation == record.en_translation).first()
    if sign:
        db.delete(sign)

    db.delete(record)
    db.commit()
    return {"status": "success", "msg": "词条及其历史记录已被永久删除。"}

@app.get("/api/export_history")
def export_history(category: str = "", db: Session = Depends(get_db)):
    query = db.query(ActionHistory)
    if category: query = query.filter(ActionHistory.category == category)
    records = query.order_by(ActionHistory.created_at.desc()).all()
    
    data = [{
        "操作类型": r.action_type, "公示语中文": r.zh_term, "英文译文": r.en_translation,
        "公示语分类": r.category, "操作时间": r.created_at.strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""
    } for r in records]
    
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='历史记录')
    output.seek(0)
    
    filename = quote("词库新增与编辑记录.xlsx")
    return StreamingResponse(
        output, 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename}"}
    )