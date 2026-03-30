from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func
from database import Base

class Signage(Base):
    __tablename__ = "signs"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    # ⚠️ 删除了这里的 index=True，解决超长文本报错，且不影响模糊搜索
    zh_term = Column(String, nullable=False)                    
    en_translation = Column(String, nullable=False)             
    sequence_id = Column(String, index=True, nullable=True)     # 序列ID很短，保留索引以加速查询
    sequence_order = Column(Integer, nullable=True)             
    category = Column(String, nullable=True)                    
    created_at = Column(DateTime, server_default=func.now())    

class ActionHistory(Base):
    __tablename__ = "action_history"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    action_type = Column(String, nullable=False)                
    zh_term = Column(String, nullable=False)                    
    en_translation = Column(String, nullable=False)             
    category = Column(String, nullable=True)                    
    created_at = Column(DateTime, server_default=func.now())
