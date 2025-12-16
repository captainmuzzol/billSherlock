from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime

class Suspect(Base):
    __tablename__ = "suspects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True, unique=True)
    password = Column(String) # Simple storage for this demo, usually should hash
    created_at = Column(DateTime, default=datetime.now)
    
    # AI Analysis Cache
    ai_analysis = Column(String, nullable=True)
    analysis_signature = Column(String, nullable=True)
    
    transactions = relationship("Transaction", back_populates="suspect")

class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    suspect_id = Column(Integer, ForeignKey("suspects.id"))
    transaction_id = Column(String, index=True) 
    transaction_time = Column(DateTime, index=True)
    transaction_type = Column(String)
    category = Column(String) # 收/支/其他
    method = Column(String)
    amount = Column(Float)
    counterparty = Column(String, index=True)
    merchant_id = Column(String)
    source_file = Column(String)
    
    suspect = relationship("Suspect", back_populates="transactions")
