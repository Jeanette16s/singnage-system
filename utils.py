#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Thu Mar 12 16:30:12 2026

@author: a111
"""

# utils.py
import pandas as pd
import re
import uuid
from pypinyin import lazy_pinyin, Style

# ================= 原有基础函数 =================
def generate_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:6].upper()}"

def split_by_slash_outside_parens(text):
    if pd.isna(text): return[]
    text = str(text).strip()
    result, current_chunk, depth = [], [], 0
    for char in text:
        if char in '({[（': 
            depth += 1
            current_chunk.append(char)
        elif char in ')}]）': 
            depth = max(0, depth - 1)
            current_chunk.append(char)
        elif char in ['/', '\\']: 
            if depth == 0:
                result.append(''.join(current_chunk).strip())
                current_chunk =[]
            else:
                current_chunk.append(char)
        else:
            current_chunk.append(char)
    if current_chunk:
        result.append(''.join(current_chunk).strip())
    return[r for r in result if r]

def get_start_number(text):
    match = re.match(r'^(\d+)[.、\s]+(.*)', str(text))
    if match: return int(match.group(1)), match.group(2).strip()
    return None, str(text).strip()

def classify_signs_with_priority(zh_text, en_text):
    zh_text = str(zh_text).strip() if pd.notna(zh_text) else ""
    en_text = str(en_text).strip().upper() if pd.notna(en_text) else ""
    
    prompt_zh =['注意', '小心', '当心', '警示', '提醒', '留意', '警告', '危险', '防滑', '防爆']
    prompt_en =[r'\bCAUTION\b', r'\bMIND\b', r'\bBEWARE\b', r'\bREMINDER\b', r'\bNOTICE\b',r'\bGUIDANCE\b',r'\bINSTRUCTION\b']
    if any(kw in zh_text for kw in prompt_zh) or any(re.search(kw, en_text) for kw in prompt_en): return "提示类"

    compulsion_zh =['严禁', '禁止', '必须', '罚款', '违者', '毋', '不准', '一律', '强制']
    compulsion_en =[r'\bFORBIDDEN\b', r'\bMUST\b', r'\bNOT ALLOWED\b']
    if any(kw in zh_text for kw in compulsion_zh) or any(re.search(kw, en_text) for kw in compulsion_en): return "强制类"

    restriction_zh =['限', '仅限', '谢绝', '请勿', '不要', '专供', '专用', '内部', '不对外']
    restriction_en =[r'\bONLY\b', r'\bRESTRICTED\b', r'\bLIMIT\b', r'\bPLEASE DO NOT\b', r'\bDO NOT\b']
    if any(kw in zh_text for kw in restriction_zh) or any(re.search(kw, en_text) for kw in restriction_en): return "限制类"
    return "指示类"

def get_sort_key(text):
    if not isinstance(text, str) or text == '': return 'zzzzzz'
    try:
        return ''.join(lazy_pinyin(text, style=Style.NORMAL))
    except:
        return text.lower()

# ================= 整合的处理流水线 =================
def process_dataframe_pipeline(df: pd.DataFrame):
    """接收 DataFrame，执行拆分、序列检查、分类、拼音提取，返回字典列表"""
    df['公示语中文'] = df['公示语中文'].fillna('').astype(str).str.strip()
    df['英文译文'] = df['英文译文'].fillna('').astype(str).str.strip()
    
    # 彻底去重
    df = df.drop_duplicates(subset=['公示语中文', '英文译文']).reset_index(drop=True)

    # 1. 拆分斜杠
    expanded_rows =[]
    for _, row in df.iterrows():
        zh_parts = split_by_slash_outside_parens(row['公示语中文'])
        en_parts = split_by_slash_outside_parens(row['英文译文'])
        if len(zh_parts) > 1:
            max_len = max(len(zh_parts), len(en_parts))
            zh_parts += [''] * (max_len - len(zh_parts))
            en_parts += [''] * (max_len - len(en_parts))
            group_id = generate_id("SPLIT")
            for idx, (z, e) in enumerate(zip(zh_parts, en_parts), 1):
                expanded_rows.append({'zh': z, 'en': e, 'seq_id': group_id, 'seq_order': idx})
        else:
            expanded_rows.append({'zh': row['公示语中文'], 'en': row['英文译文'], 'seq_id': None, 'seq_order': None})
            
    df_expanded = pd.DataFrame(expanded_rows)

    # 2. 连续序号侦测
    i = 0
    while i < len(df_expanded):
        num_zh, _ = get_start_number(df_expanded.at[i, 'zh'])
        num_en, _ = get_start_number(df_expanded.at[i, 'en'])
        start_num = num_zh if num_zh == 1 else (num_en if num_en == 1 else None)
        
        if start_num == 1:
            j = i + 1; expected = 2
            while j < len(df_expanded):
                n_zh, _ = get_start_number(df_expanded.at[j, 'zh'])
                n_en, _ = get_start_number(df_expanded.at[j, 'en'])
                curr_num = n_zh if n_zh == expected else (n_en if n_en == expected else None)
                if curr_num == expected:
                    expected += 1; j += 1
                else: break
            
            if j > i + 1:
                seq_id = generate_id("SEQ")
                for k in range(i, j):
                    _, df_expanded.at[k, 'zh'] = get_start_number(df_expanded.at[k, 'zh'])
                    _, df_expanded.at[k, 'en'] = get_start_number(df_expanded.at[k, 'en'])
                    df_expanded.at[k, 'seq_id'] = seq_id
                    df_expanded.at[k, 'seq_order'] = (k - i) + 1
                i = j; continue
        i += 1

    # 3. 分类、质检与拼音
    final_data = []
    for _, row in df_expanded.iterrows():
        zh, en = str(row['zh']), str(row['en'])
        is_reviewed = True
        if not zh.strip() or not en.strip() or '\n' in zh or '\n' in en:
            is_reviewed = False # 标记为异常数据
            
        final_data.append({
            "zh_text": zh,
            "en_text": en,
            "category": classify_signs_with_priority(zh, en),
            "seq_id": row['seq_id'],
            "seq_order": row['seq_order'],
            "zh_pinyin": get_sort_key(zh),
            "is_reviewed": is_reviewed
        })
        
    return final_data