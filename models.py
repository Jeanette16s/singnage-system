from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from database import Base

class Signage(Base):
    __tablename__ = "signs"  # 映射现有的 signs 表

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    zh_term = Column(String, index=True, nullable=False)        # 公示语中文
    en_translation = Column(String, nullable=False)             # 英文译文
    sequence_id = Column(String, index=True, nullable=True)     # 关联序列ID
    sequence_order = Column(Integer, nullable=True)             # 序列排序
    category = Column(String, nullable=True)                    # 公示语分类
    created_at = Column(DateTime, server_default=func.now())    # 创建时间

class ActionHistory(Base):
    __tablename__ = "action_history" # 新增的操作记录表

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    action_type = Column(String, nullable=False)                # 操作类型: 新增, 编辑, 导入
    zh_term = Column(String, nullable=False)                    # 中文
    en_translation = Column(String, nullable=False)             # 英文
    category = Column(String, nullable=True)                    # 分类
    created_at = Column(DateTime, server_default=func.now())    # 操作时间