from fastapi import FastAPI, Depends, UploadFile, File, Form, HTTPException, status
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from sqlalchemy import func, text
import pandas as pd
import io
import os
import sys
import re
import docx
import json
from urllib.parse import quote
from difflib import SequenceMatcher
from datetime import datetime, timedelta
import jwt
from passlib.context import CryptContext

from database import engine, Base, get_db
from models import Signage, ActionHistory, User
from utils import process_dataframe_pipeline, get_sort_key

# ================= 安全与鉴权配置 =================
SECRET_KEY = "your-super-secret-key-change-this-in-production"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 30 

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login")

def verify_password(plain_password, hashed_password): return pwd_context.verify(plain_password, hashed_password)
def get_password_hash(password): return pwd_context.hash(password)
def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    expire = datetime.utcnow() + (expires_delta if expires_delta else timedelta(minutes=15))
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    credentials_exception = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="凭证无效或已过期，请重新登录", headers={"WWW-Authenticate": "Bearer"})
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None: raise credentials_exception
    except jwt.PyJWTError: raise credentials_exception
    user = db.query(User).filter(User.username == username).first()
    if user is None: raise credentials_exception
    return user

def require_editor(current_user: User = Depends(get_current_user)):
    if current_user.role not in ["admin", "editor"]: raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足：只有编辑或管理员可执行此操作")
    return current_user

def require_admin(current_user: User = Depends(get_current_user)):
    if current_user.role != "admin": raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="权限不足：需要管理员权限")
    return current_user

# ================= 系统初始化 =================
Base.metadata.create_all(bind=engine)

try:
    with engine.connect() as conn:
        conn.execute(text("ALTER TABLE action_history ADD COLUMN username VARCHAR"))
        conn.commit()
except Exception:
    pass

try:
    with engine.connect() as conn:
        # 修改：新增列时默认值为空字符串，不再显示"未标定"
        conn.execute(text("ALTER TABLE signs ADD COLUMN source_tag VARCHAR DEFAULT ''"))
        conn.commit()
except Exception:
    pass

app = FastAPI(title="公示语多语言词库系统")

@app.on_event("startup")
def create_default_admin():
    db = next(get_db())
    if not db.query(User).first():
        admin_user = User(username="admin", hashed_password=get_password_hash("admin123"), role="admin")
        db.add(admin_user)
        db.commit()
    db.close()

def get_resource_path(relative_path):
    if getattr(sys, 'frozen', False): base_path = sys._MEIPASS
    else: base_path = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base_path, relative_path)

# ================= 工具函数 =================
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

def extract_en_suggestions_list(text: str) -> list:
    if not text: return []
    text = str(text).replace("：", ":")
    if "英文:" in text: 
        text = text.split("英文:")[1].strip()

    parts = re.split(r'\s*或\s*|\s+\bor\b\s+', text, flags=re.IGNORECASE)
    
    results = []
    for part in parts:
        part = part.strip()
        if not part: continue
        
        brackets = re.findall(r'\([^)]*[\u4e00-\u9fa5][^)]*\)|（[^）]*[\u4e00-\u9fa5][^）]*）|\[[^\]]*[\u4e00-\u9fa5][^\]]*\]|【[^】]*[\u4e00-\u9fa5][^】]*】', part)
        
        masked_text = part
        for i, b in enumerate(brackets):
            masked_text = masked_text.replace(b, f" __BRK_{i}__ ")
            
        masked_text = re.sub(r'[\u4e00-\u9fa5。，、；：“”‘’！？]+', ' ', masked_text)
        
        for i, b in enumerate(brackets):
            masked_text = masked_text.replace(f" __BRK_{i}__ ", b).replace(f"__BRK_{i}__", b)
            
        cleaned = re.sub(r'\s+', ' ', masked_text).strip()
        cleaned = re.sub(r'^[,.!?;:]+|[,.!?;:]+$', '', cleaned).strip()
        
        if cleaned:
            results.append(cleaned)
            
    return results

# ================= 高级排序规则 =================
def advanced_sort_key(text):
    clean_text = str(text).strip()
    match = re.search(r'([\u4e00-\u9fa5a-zA-Z0-9])', clean_text)
    
    if not match:
        return (4, get_sort_key(clean_text))
    
    first_char = match.group(1)
    
    if '\u4e00' <= first_char <= '\u9fa5':
        priority = 1
    elif first_char.isdigit():
        priority = 2
    else:  
        priority = 3
        
    sort_content = clean_text[match.start():]
    return (priority, get_sort_key(sort_content))


# ================= 认证与用户管理路由 =================
@app.post("/api/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password): raise HTTPException(status_code=400, detail="用户名或密码错误")
    access_token = create_access_token(data={"sub": user.username, "role": user.role}, expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    return {"access_token": access_token, "token_type": "bearer", "role": user.role, "username": user.username}

@app.post("/api/register")
def register(username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == username).first(): return {"status": "error", "msg": "用户名已被占用，请换一个试试！"}
    if len(password) < 6: return {"status": "error", "msg": "密码长度不能少于6位！"}
    new_user = User(username=username, hashed_password=get_password_hash(password), role="viewer")
    db.add(new_user)
    db.commit()
    return {"status": "success", "msg": "注册成功！请使用新账号登录。"}

@app.get("/api/verify_token")
def verify_token(current_user: User = Depends(get_current_user)):
    return {"status": "success", "role": current_user.role, "username": current_user.username}

@app.post("/api/users/update_password")
def update_password(old_password: str = Form(...), new_password: str = Form(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not verify_password(old_password, current_user.hashed_password): return {"status": "error", "msg": "原密码错误！"}
    if len(new_password) < 6: return {"status": "error", "msg": "新密码长度不能少于6位！"}
    current_user.hashed_password = get_password_hash(new_password)
    db.commit()
    return {"status": "success", "msg": "密码修改成功，请妥善保管！"}

@app.get("/api/users")
def get_users(db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    users = db.query(User).all()
    return {"data": [{"id": u.id, "username": u.username, "role": u.role, "created_at": u.created_at + timedelta(hours=8) if u.created_at else None} for u in users]}

@app.post("/api/users/role")
def update_user_role(user_id: int = Form(...), role: str = Form(...), db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    if role not in ["admin", "editor", "viewer"]: return {"status": "error", "msg": "非法的角色！"}
    user = db.query(User).filter(User.id == user_id).first()
    if not user: return {"status": "error", "msg": "找不到该用户"}
    if user.username == "admin" and role != "admin": return {"status": "error", "msg": "禁止修改初始 admin 的权限！"}
    user.role = role
    db.commit()
    return {"status": "success", "msg": f"已将 {user.username} 的权限更新为 {role}"}

@app.post("/api/users/delete")
def delete_user(user_id: int = Form(...), db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user: return {"status": "error", "msg": "用户不存在"}
    if user.username == "admin": return {"status": "error", "msg": "无法删除系统初始管理员！"}
    db.delete(user)
    db.commit()
    return {"status": "success", "msg": "用户已删除"}

# ================= 核心业务路由 =================

@app.get("/")
async def read_root():
    base_path = get_resource_path("index.html")
    template_path = get_resource_path(os.path.join("templates", "index.html"))
    if os.path.exists(base_path): return FileResponse(base_path)
    elif os.path.exists(template_path): return FileResponse(template_path)
    else: raise HTTPException(status_code=404, detail="未找到 index.html")

@app.get("/api/stats")
def get_stats(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    count = db.query(Signage.zh_term).filter(
        Signage.zh_term.isnot(None),
        Signage.zh_term != 'nan',
        Signage.zh_term != ''
    ).distinct().count()
    return {"status": "success", "total": count}

@app.get("/api/search")
def search_signage(keyword: str = "", db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    clean_keyword = keyword.replace('\n', '').replace('\r', '').replace(' ', '')
    all_signs = db.query(Signage).all()
    grouped = {}
    
    if not clean_keyword:
        for sign in all_signs:
            if not sign.zh_term or str(sign.zh_term).lower() == 'nan': continue
            zh = sign.zh_term
            if zh not in grouped: 
                grouped[zh] = {"zh_text": zh, "category": sign.category, "source_tag": sign.source_tag, "translations": set(), "seq_id": sign.sequence_id, "associated_items": [], "show_assoc": False, "match_score": 100.0}
            grouped[zh]["translations"].add(sign.en_translation)
    else:
        for sign in all_signs:
            if not sign.zh_term or str(sign.zh_term).lower() == 'nan': continue
            clean_zh = str(sign.zh_term).replace('\n', '').replace('\r', '').replace(' ', '')
            if not clean_zh: continue
            if clean_keyword == clean_zh: score = 100.0
            else:
                score = SequenceMatcher(None, clean_keyword, clean_zh).ratio() * 100.0
                if clean_keyword in clean_zh or clean_zh in clean_keyword:
                    len_ratio = min(len(clean_keyword), len(clean_zh)) / max(len(clean_keyword), len(clean_zh))
                    score = max(score, 70.0 + 29.0 * len_ratio)
            if score >= 40.0:
                zh = sign.zh_term
                if zh not in grouped: 
                    grouped[zh] = {"zh_text": zh, "category": sign.category, "source_tag": sign.source_tag, "translations": set(), "seq_id": sign.sequence_id, "associated_items": [], "show_assoc": False, "match_score": score}
                grouped[zh]["translations"].add(sign.en_translation)
                if score > grouped[zh]["match_score"]: grouped[zh]["match_score"] = score

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
                    data["associated_items"].append({"zh_text": b.zh_term, "en_texts": [x.en_translation for x in bros if x.zh_term == b.zh_term], "order": b.sequence_order})
                    seen_bro_zh.add(b.zh_term)
                    
    result_list = list(grouped.values())

    if not clean_keyword:
        result_list.sort(key=lambda x: advanced_sort_key(x["zh_text"]))
    else:
        result_list.sort(key=lambda x: (-x["match_score"], advanced_sort_key(x["zh_text"])))
        
    return {"data": result_list}

@app.post("/api/edit_signage")
def edit_signage(old_zh: str = Form(...), old_en: str = Form(...), new_zh: str = Form(...), new_en: str = Form(...), category: str = Form(""), db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    clean_old_zh = old_zh.replace('\r\n', '\n').replace('\r', '')
    clean_old_en = old_en.replace('\r\n', '\n').replace('\r', '')
    clean_new_zh = new_zh.replace('\r\n', '\n').replace('\r', '')
    clean_new_en = new_en.replace('\r\n', '\n').replace('\r', '')
    
    records = db.query(Signage).filter(func.replace(Signage.zh_term, '\r', '') == clean_old_zh, func.replace(Signage.en_translation, '\r', '') == clean_old_en).all()
    if not records: return {"status": "error", "msg": "在数据库中找不到原始词条，修改失败。"}
    
    if (clean_old_zh != clean_new_zh or clean_old_en != clean_new_en):
        if db.query(Signage).filter(func.replace(Signage.zh_term, '\r', '') == clean_new_zh, func.replace(Signage.en_translation, '\r', '') == clean_new_en).first(): 
            return {"status": "error", "msg": "修改后的组合已存在！"}
            
    for r in records:
        r.zh_term = clean_new_zh
        r.en_translation = clean_new_en
        if category: r.category = category
        
    db.add(ActionHistory(action_type="编辑", zh_term=clean_new_zh, en_translation=clean_new_en, category=category, username=current_user.username))
    db.commit()
    return {"status": "success", "msg": "词条修改成功！"}

@app.post("/api/delete_signage")
def delete_signage(zh_term: str = Form(...), en_translation: str = Form(...), db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    clean_zh = zh_term.replace('\r\n', '\n').replace('\r', '')
    clean_en = en_translation.replace('\r\n', '\n').replace('\r', '')
    
    records = db.query(Signage).filter(
        func.replace(Signage.zh_term, '\r', '') == clean_zh, 
        func.replace(Signage.en_translation, '\r', '') == clean_en
    ).all()
    
    if not records: 
        return {"status": "error", "msg": "找不到该词条，可能包含不可见字符或已被删除"}
    
    category = records[0].category if records[0].category else ""
    
    for r in records:
        db.delete(r)
        
    db.add(ActionHistory(action_type="删除译文", zh_term=clean_zh, en_translation=clean_en, category=category, username=current_user.username))
    db.commit()
    return {"status": "success", "msg": "词条已成功删除！"}

@app.post("/api/add_single")
def add_single(zh_text: str = Form(...), en_text: str = Form(...), db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    clean_zh = zh_text.replace('\r\n', '\n').replace('\r', '')
    raw_en = en_text.replace('\r\n', '\n').replace('\r', '')
    
    en_list = extract_en_suggestions_list(raw_en)
    if not en_list:
        en_list = [raw_en]
        
    data_to_add = [{"公示语中文": clean_zh, "英文译文": en} for en in en_list]
    df = pd.DataFrame(data_to_add)
    processed_list = process_dataframe_pipeline(df)
    
    added_count = 0
    for item in processed_list:
        if not db.query(Signage).filter_by(zh_term=item['zh_text'], en_translation=item['en_text']).first():
            seq_id = None if pd.isna(item['seq_id']) else str(item['seq_id'])
            seq_order = None if pd.isna(item['seq_order']) else int(item['seq_order'])
            db.add(Signage(zh_term=item['zh_text'], en_translation=item['en_text'], sequence_id=seq_id, sequence_order=seq_order, category=item['category']))
            db.add(ActionHistory(action_type="单条录入", zh_term=item['zh_text'], en_translation=item['en_text'], category=item['category'], username=current_user.username))
            added_count += 1
            
    db.commit()
    return {"status": "success", "msg": f"录入成功，受影响条数：{added_count}"}

@app.post("/api/upload_excel")
async def upload_excel(
    file: UploadFile = File(...), 
    source_tag: str = Form(""), # 修改：默认空字符串
    db: Session = Depends(get_db), 
    current_user: User = Depends(require_editor)
):
    contents = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(contents), engine='openpyxl')
        if '公示语中文' not in df.columns or '英文译文' not in df.columns: return {"status": "error", "msg": "Excel必须包含 '公示语中文' 和 '英文译文' 两列！"}
        
        expanded_rows = []
        for _, row in df.iterrows():
            en_val = str(row.get('英文译文', ''))
            if pd.isna(row.get('英文译文')) or not en_val.strip() or en_val.lower() == 'nan':
                expanded_rows.append(row.to_dict())
                continue
                
            en_list = extract_en_suggestions_list(en_val)
            if not en_list:
                expanded_rows.append(row.to_dict())
            else:
                for en in en_list:
                    new_row = row.to_dict()
                    new_row['英文译文'] = en
                    expanded_rows.append(new_row)
                    
        expanded_df = pd.DataFrame(expanded_rows)
        processed_list = process_dataframe_pipeline(expanded_df)
        
        added_count, skip_count = 0, 0
        for item in processed_list:
            if not db.query(Signage).filter_by(zh_term=item['zh_text'], en_translation=item['en_text']).first():
                seq_id = None if pd.isna(item['seq_id']) else str(item['seq_id'])
                seq_order = None if pd.isna(item['seq_order']) else int(item['seq_order'])
                db.add(Signage(
                    zh_term=item['zh_text'], 
                    en_translation=item['en_text'], 
                    sequence_id=seq_id, 
                    sequence_order=seq_order, 
                    category=item['category'],
                    source_tag=source_tag
                ))
                db.add(ActionHistory(action_type="Excel导入", zh_term=item['zh_text'], en_translation=item['en_text'], category=item['category'], username=current_user.username))
                added_count += 1
            else: skip_count += 1
        db.commit()
        return {"status": "success", "msg": f"解析完毕！新增 {added_count} 条，由于重复跳过 {skip_count} 条。"}
    except Exception as e: return {"status": "error", "msg": str(e)}

@app.post("/api/upload_docx")
async def upload_docx(
    file: UploadFile = File(...), 
    source_tag: str = Form(""), # 修改：默认空字符串
    db: Session = Depends(get_db), 
    current_user: User = Depends(require_editor)
):
    if file.filename.endswith('.doc'): return {"status": "error", "msg": "请先在本地另存为 .docx 格式后重试！"}
    elif not file.filename.endswith('.docx'): return {"status": "error", "msg": "请上传 .docx 文档！"}
    contents = await file.read()
    try: doc = docx.Document(io.BytesIO(contents))
    except Exception as e: return {"status": "error", "msg": f"读取失败: {e}"}
    extracted_data = []
    for table in doc.tables:
        if len(table.rows) == 0: continue
        header_row = [cell.text.strip() for cell in table.rows[0].cells]
        try: sign_col_idx = header_row.index("标识内容"); suggestion_col_idx = header_row.index("整改建议")
        except ValueError: continue
        for row in table.rows[1:]:
            if len(row.cells) <= max(sign_col_idx, suggestion_col_idx): continue
            try: raw_sign = row.cells[sign_col_idx].text.strip(); raw_suggestion = row.cells[suggestion_col_idx].text.strip()
            except Exception: continue 
            if len(raw_sign) > 1000 or len(raw_suggestion) > 1000: continue
            if not raw_sign and not raw_suggestion: continue
            
            clean_zh = extract_zh_sign(raw_sign)
            clean_en_list = extract_en_suggestions_list(raw_suggestion)
            if clean_zh and clean_en_list: 
                for clean_en in clean_en_list:
                    extracted_data.append({"公示语中文": clean_zh, "英文译文": clean_en})
                    
    if not extracted_data: return {"status": "error", "msg": "未能在文档中提取到有效数据。"}
    try:
        df = pd.DataFrame(extracted_data)
        processed_list = process_dataframe_pipeline(df)
        added_count, skip_count = 0, 0
        for item in processed_list:
            if not db.query(Signage).filter_by(zh_term=item['zh_text'], en_translation=item['en_text']).first():
                seq_id = None if pd.isna(item['seq_id']) else str(item['seq_id'])
                seq_order = None if pd.isna(item['seq_order']) else int(item['seq_order'])
                db.add(Signage(
                    zh_term=item['zh_text'], 
                    en_translation=item['en_text'], 
                    sequence_id=seq_id, 
                    sequence_order=seq_order, 
                    category=item['category'],
                    source_tag=source_tag
                ))
                db.add(ActionHistory(action_type="Word导入", zh_term=item['zh_text'], en_translation=item['en_text'], category=item['category'], username=current_user.username))
                added_count += 1
            else: skip_count += 1
        db.commit()
        return {"status": "success", "msg": f"Word 解析完毕！新增 {added_count} 条，由于重复跳过 {skip_count} 条。"}
    except Exception as e: return {"status": "error", "msg": str(e)}

@app.post("/api/match_docx")
async def match_docx(file: UploadFile = File(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if file.filename.endswith('.doc'): return {"status": "error", "msg": "请另存为 .docx 格式后重试！"}
    elif not file.filename.endswith('.docx'): return {"status": "error", "msg": "请上传 .docx 格式文档！"}
    contents = await file.read()
    try: doc = docx.Document(io.BytesIO(contents))
    except Exception as e: return {"status": "error", "msg": f"读取失败: {e}"}
    extracted_zh_terms = set()
    for table in doc.tables:
        if len(table.rows) == 0: continue
        header_row = [cell.text.strip() for cell in table.rows[0].cells]
        try: sign_col_idx = header_row.index("标识内容")
        except ValueError: continue
        for row in table.rows[1:]:
            if len(row.cells) <= sign_col_idx: continue
            try: raw_sign = row.cells[sign_col_idx].text.strip()
            except Exception: continue
            if len(raw_sign) > 1000 or not raw_sign: continue
            clean_zh = extract_zh_sign(raw_sign)
            if clean_zh and clean_zh.lower() != 'nan': extracted_zh_terms.add(clean_zh)
    if not extracted_zh_terms: return {"status": "error", "msg": "未能提取到有效的中文标识。"}
    clean_extracted = {term.replace('\n', '').replace('\r', '').replace(' ', '') for term in extracted_zh_terms}
    db_zh_clean = func.replace(func.replace(func.replace(Signage.zh_term, '\n', ''), '\r', ''), ' ', '')
    matched = db.query(Signage).filter(db_zh_clean.in_(clean_extracted)).all()
    seq_ids = [m.sequence_id for m in matched if m.sequence_id]
    associated = db.query(Signage).filter(Signage.sequence_id.in_(seq_ids)).all() if seq_ids else []
    grouped = {}
    for item in matched:
        if item.zh_term not in grouped: grouped[item.zh_term] = {"zh_text": item.zh_term, "category": item.category, "source_tag": item.source_tag, "translations": set(), "seq_id": item.sequence_id, "associated_items": [], "show_assoc": False}
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
                    data["associated_items"].append({"zh_text": b.zh_term, "en_texts": [x.en_translation for x in bros if x.zh_term == b.zh_term], "order": b.sequence_order})
                    seen_bro_zh.add(b.zh_term)
    result_list = list(grouped.values())
    result_list.sort(key=lambda x: advanced_sort_key(x["zh_text"]))
    return {"status": "success", "data": result_list, "extracted_count": len(extracted_zh_terms)}

@app.post("/api/match_excel")
async def match_excel(file: UploadFile = File(...), db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    if not file.filename.endswith('.xlsx'): return {"status": "error", "msg": "请上传 .xlsx 格式！"}
    contents = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(contents), engine='openpyxl')
        if '公示语中文' not in df.columns: return {"status": "error", "msg": "Excel文档必须包含名为 '公示语中文' 的表头列！"}
    except Exception as e: return {"status": "error", "msg": f"读取失败: {e}"}
    extracted_zh_terms = set()
    for zh_val in df['公示语中文'].dropna():
        raw_sign = str(zh_val).strip()
        if len(raw_sign) > 1000 or not raw_sign: continue
        clean_zh = extract_zh_sign(raw_sign)
        if clean_zh and clean_zh.lower() != 'nan': extracted_zh_terms.add(clean_zh)
    if not extracted_zh_terms: return {"status": "error", "msg": "未能提取到有效的标识文本。"}
    clean_extracted = {term.replace('\n', '').replace('\r', '').replace(' ', '') for term in extracted_zh_terms}
    db_zh_clean = func.replace(func.replace(func.replace(Signage.zh_term, '\n', ''), '\r', ''), ' ', '')
    matched = db.query(Signage).filter(db_zh_clean.in_(clean_extracted)).all()
    seq_ids = [m.sequence_id for m in matched if m.sequence_id]
    associated = db.query(Signage).filter(Signage.sequence_id.in_(seq_ids)).all() if seq_ids else []
    grouped = {}
    for item in matched:
        if item.zh_term not in grouped: grouped[item.zh_term] = {"zh_text": item.zh_term, "category": item.category, "source_tag": item.source_tag, "translations": set(), "seq_id": item.sequence_id, "associated_items": [], "show_assoc": False}
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
                    data["associated_items"].append({"zh_text": b.zh_term, "en_texts": [x.en_translation for x in bros if x.zh_term == b.zh_term], "order": b.sequence_order})
                    seen_bro_zh.add(b.zh_term)
    result_list = list(grouped.values())
    result_list.sort(key=lambda x: advanced_sort_key(x["zh_text"]))
    return {"status": "success", "data": result_list, "extracted_count": len(extracted_zh_terms)}

# ================= 历史记录与管理接口 =================

@app.get("/api/history")
def get_history(category: str = "", username: str = "", limit: int = 100, db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    query = db.query(ActionHistory)
    if category: query = query.filter(ActionHistory.category == category)
    if username: query = query.filter(ActionHistory.username == username)
    records = query.order_by(ActionHistory.created_at.desc()).limit(limit).all()
    
    result = []
    for r in records:
        bj_time = r.created_at + timedelta(hours=8) if r.created_at else None
        result.append({
            "id": r.id,
            "action_type": r.action_type,
            "zh_term": r.zh_term,
            "en_translation": r.en_translation,
            "category": r.category,
            "created_at": bj_time,
            "username": r.username
        })
    return {"data": result}

@app.post("/api/history/update")
def update_history(id: int = Form(...), zh_term: str = Form(...), en_translation: str = Form(...), category: str = Form(...), db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    clean_zh = zh_term.replace('\r\n', '\n').replace('\r', '')
    clean_en = en_translation.replace('\r\n', '\n').replace('\r', '')
    record = db.query(ActionHistory).filter(ActionHistory.id == id).first()
    if not record: return {"status": "error", "msg": "找不到该条历史记录"}
    sign = db.query(Signage).filter(Signage.zh_term == record.zh_term, Signage.en_translation == record.en_translation).first()
    record.zh_term = clean_zh; record.en_translation = clean_en; record.category = category; record.action_type = "记录修改"; record.username = current_user.username
    if sign: sign.zh_term = clean_zh; sign.en_translation = clean_en; sign.category = category
    db.commit()
    return {"status": "success", "msg": "词条已成功更新！"}

@app.post("/api/history/delete")
def delete_history(id: int = Form(...), db: Session = Depends(get_db), current_user: User = Depends(require_admin)):
    record = db.query(ActionHistory).filter(ActionHistory.id == id).first()
    if not record: return {"status": "error", "msg": "找不到该条历史记录"}
    
    sign_records = db.query(Signage).filter(Signage.zh_term == record.zh_term, Signage.en_translation == record.en_translation).all()
    for s in sign_records: 
        db.delete(s)
        
    db.delete(record)
    db.commit()
    return {"status": "success", "msg": "已永久删除。"}

@app.post("/api/history/batch_delete")
def batch_delete_history(id_list: str = Form(...), db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    try:
        ids = json.loads(id_list)
        if not ids: return {"status": "error", "msg": "未选中任何记录"}
        records = db.query(ActionHistory).filter(ActionHistory.id.in_(ids)).all()
        count = 0
        for r in records:
            if current_user.role != 'admin' and r.username != current_user.username:
                continue
            
            sign_records = db.query(Signage).filter(Signage.zh_term == r.zh_term, Signage.en_translation == r.en_translation).all()
            for s in sign_records: 
                db.delete(s)
                
            db.delete(r)
            count += 1
        db.commit()
        return {"status": "success", "msg": f"成功批量删除了 {count} 条记录！"}
    except Exception as e:
        db.rollback()
        return {"status": "error", "msg": f"批量删除失败: {str(e)}"}

@app.post("/api/history/batch_tag")
def batch_tag_history(id_list: str = Form(...), tag_value: str = Form(...), db: Session = Depends(get_db), current_user: User = Depends(require_editor)):
    try:
        ids = json.loads(id_list)
        if not ids: return {"status": "error", "msg": "未选中任何记录"}
        
        records = db.query(ActionHistory).filter(ActionHistory.id.in_(ids)).all()
        count = 0
        
        for r in records:
            if current_user.role != 'admin' and r.username != current_user.username:
                continue
            
            sign_records = db.query(Signage).filter(Signage.zh_term == r.zh_term, Signage.en_translation == r.en_translation).all()
            
            for s in sign_records: 
                s.source_tag = tag_value  
                count += 1
                
        db.commit()
        return {"status": "success", "msg": f"成功操作了 {count} 个词条！"}
    except Exception as e:
        db.rollback()
        return {"status": "error", "msg": f"操作失败: {str(e)}"}

@app.get("/api/export_history")
def export_history(category: str = "", username: str = "", db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    query = db.query(ActionHistory)
    if category: query = query.filter(ActionHistory.category == category)
    if username: query = query.filter(ActionHistory.username == username)
    records = query.order_by(ActionHistory.created_at.desc()).all()
    
    data = [{"操作类型": r.action_type, "操作人": r.username or "系统", "公示语中文": r.zh_term, "英文译文": r.en_translation, "公示语分类": r.category, "操作时间": (r.created_at + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S") if r.created_at else ""} for r in records]
    
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer: df.to_excel(writer, index=False, sheet_name='历史与审计记录')
    output.seek(0)
    filename = quote("词库新增与编辑记录.xlsx")
    return StreamingResponse(output, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": f"attachment; filename*=utf-8''{filename}"})