from pydantic import BaseModel
from typing import List, Optional

class Message(BaseModel):
    role: str # 'user' or 'ai'
    content: str

class ChatRequest(BaseModel):
    message: str
    history: List[Message] = []
    watched_list: List[str] = []

class Movie(BaseModel):
    id: str
    title: str
    year: str
    genre: str
    rating: str
    tags: List[str]
    image: str

class ChatResponse(BaseModel):
    id: str
    role: str = "ai"
    content: str
    recommendations: Optional[List[Movie]] = []
    suggestions: Optional[List[str]] = None