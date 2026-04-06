from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from database import Base

class Signage(Base):
    __tablename__ = "signs"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    zh_term = Column(String, nullable=False)                    
    en_translation = Column(String, nullable=False)             
    sequence_id = Column(String, index=True, nullable=True)     
    sequence_order = Column(Integer, nullable=True)             
    category = Column(String, nullable=True)
    
    # 【新增字段】：来源规范性标签，默认给个初始状态
    source_tag = Column(String, default="未标定", nullable=True) 
    created_at = Column(DateTime, server_default=func.now())

class ActionHistory(Base):
    __tablename__ = "action_history"
    __table_args__ = {'extend_existing': True}

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    action_type = Column(String, nullable=False)                
    zh_term = Column(String, nullable=False)                    
    en_translation = Column(String, nullable=False)             
    category = Column(String, nullable=True)                    
    created_at = Column(DateTime, server_default=func.now())
    # 新增字段：记录操作人，用于个人数据中心和全局审计溯源
    username = Column(String, nullable=True)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), default="viewer", nullable=False) # 可选值: admin, editor, viewer
    created_at = Column(DateTime(timezone=True), server_default=func.now())