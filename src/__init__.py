# src/__init__.py
"""RAG Chatbot Package"""
__version__ = "1.0.0"

class Config:
    # ... existing ...
    
    # UI Configuration
    USE_BUTTONS = True
    MAX_LIST_ITEMS = 10
    RESPONSE_FORMAT = 'structured'  # 'structured' atau 'plain'