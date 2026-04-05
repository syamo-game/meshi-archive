from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Message(Base):
    __tablename__ = 'messages'
    
    # Using String for message_id since Discord IDs can be large and are best handled as strings or BigIntegers.
    message_id = Column(String, primary_key=True)
    is_target = Column(Boolean, default=True, comment="True if it was a restaurant, False if ignored")

class Shop(Base):
    __tablename__ = 'shops'
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    message_id = Column(String, ForeignKey('messages.message_id'), nullable=False)
    shop_name = Column(String, nullable=False)
    area = Column(String, nullable=True)
    category = Column(String, nullable=True)
    url = Column(Text, nullable=True)
    is_visited = Column(Boolean, default=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
