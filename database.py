import sqlite3
import pandas as pd
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

# ==========================================
# 1. 智能适配云端与本地的数据库引擎配置
# ==========================================

# 尝试从系统的环境变量中获取 DATABASE_URL（云平台如 Render、Heroku 会自动提供这个变量）
# 如果没有获取到（说明你在本地电脑运行），则默认使用本地的 SQLite 文件
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./signs_glossary.db")

# 【非常重要】: 很多云数据库(如 Neon、Heroku)提供的 URL 是以 "postgres://" 开头的。
# 但是 SQLAlchemy 最新版本要求必须以 "postgresql://" 开头，这里做一个自动修正。
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

# 如果是 SQLite，必须加 check_same_thread=False 以支持 FastAPI 多线程
if "sqlite" in DATABASE_URL:
    engine = create_engine(
        DATABASE_URL, connect_args={"check_same_thread": False}
    )
else:
    # 如果是云端的 PostgreSQL/MySQL，直接创建连接（连接池会自动处理）
    engine = create_engine(DATABASE_URL)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ==========================================
# 2. 本地原生数据导入逻辑 (云端不需要运行这部分)
# ==========================================
# 注意：一旦部署到公开网站，你不需要在服务器上执行下面的代码。
# 甲方可以直接在网页的【数据录入与管理】Tab 页，把那份 Excel 拖拽进去【批量上传】即可。
# 下面的代码主要用于你在本地开发时，快速灌入 6000 词测试数据。

def init_db(db_name="signs_glossary.db"):
    """
    初始化本地 SQLite 数据库并创建核心表结构。
    """
    is_new = not os.path.exists(db_name)
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    
    if is_new:
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS signs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                zh_term TEXT NOT NULL,
                en_translation TEXT NOT NULL,
                sequence_id TEXT,
                sequence_order INTEGER,
                category TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX idx_zh_term ON signs(zh_term);')
        cursor.execute('CREATE INDEX idx_sequence_id ON signs(sequence_id);')
        cursor.execute('CREATE INDEX idx_created_at ON signs(created_at);')
        conn.commit()
        print(f"本地数据库 {db_name} 及表结构已成功创建。")
    else:
        print(f"本地数据库 {db_name} 已存在，跳过初始化。")
        
    conn.close()

def import_excel_to_db(excel_path, db_name="signs_glossary.db"):
    """
    将包含4个Sheet的Excel文件数据导入到本地 SQLite 数据库中。
    """
    if not os.path.exists(excel_path):
        print(f"错误：找不到 Excel 文件 {excel_path}")
        return

    if not os.path.exists(db_name):
        print(f"错误：找不到数据库文件 {db_name}，请先运行初始化数据库的脚本。")
        return

    print(f"正在读取 Excel 文件: {excel_path} ...")
    try:
        sheets_dict = pd.read_excel(excel_path, sheet_name=None, engine='openpyxl')
    except Exception as e:
        print(f"读取 Excel 失败: {e}")
        return

    sheet_names = list(sheets_dict.keys())[:4]
    print(f"即将导入以下 {len(sheet_names)} 个工作表的数据: {sheet_names}")

    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()

    insert_sql = '''
        INSERT INTO signs (zh_term, en_translation, sequence_id, sequence_order, category)
        VALUES (?, ?, ?, ?, ?)
    '''

    total_inserted = 0

    for sheet_name in sheet_names:
        df = sheets_dict[sheet_name]
        print(f"\n开始处理工作表: {sheet_name} (共 {len(df)} 行)")

        if '公示语中文' not in df.columns or '英文译文' not in df.columns:
            print(f"跳过 {sheet_name}：缺少必要的 '公示语中文' 或 '英文译文' 列。")
            continue

        df = df.where(pd.notnull(df), None)
        records_to_insert = []
        
        for index, row in df.iterrows():
            zh_term = row.get('公示语中文')
            en_translation = row.get('英文译文')
            
            if not zh_term or not en_translation:
                continue
                
            sequence_id = row.get('关联序列ID')
            sequence_order = row.get('序列排序')
            category = row.get('公示语分类')

            if sequence_order is not None:
                try:
                    sequence_order = int(float(sequence_order))
                except ValueError:
                    sequence_order = None

            records_to_insert.append((
                str(zh_term).strip(), 
                str(en_translation).strip(), 
                str(sequence_id).strip() if sequence_id else None, 
                sequence_order, 
                str(category).strip() if category else None
            ))

        if records_to_insert:
            try:
                cursor.executemany(insert_sql, records_to_insert)
                conn.commit()
                inserted_count = len(records_to_insert)
                total_inserted += inserted_count
                print(f"  -> 成功从 '{sheet_name}' 导入 {inserted_count} 条记录。")
            except sqlite3.Error as e:
                print(f"  -> 插入 '{sheet_name}' 数据时发生数据库错误: {e}")
                conn.rollback()
        else:
            print(f"  -> '{sheet_name}' 中没有提取到有效数据。")

    conn.close()
    print(f"\n导入流程结束，共向数据库插入了 {total_inserted} 条记录。")

# 当直接运行 `python database.py` 时，执行本地 SQLite 测试导入
if __name__ == "__main__":
    init_db()
    # 填入你实际的原始词库 Excel 路径
    TARGET_EXCEL_FILE = 'marked_processed_file.xlsx' 
    TARGET_DB_NAME = 'signs_glossary.db'
    if os.path.exists(TARGET_EXCEL_FILE):
        import_excel_to_db(TARGET_EXCEL_FILE, TARGET_DB_NAME)
    else:
        print(f"提示：未在当前目录找到 {TARGET_EXCEL_FILE}，已跳过数据导入。Web端可正常使用。")