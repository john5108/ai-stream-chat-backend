#FastAPI + Pydantic 相关
from fastapi import FastAPI, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import List
from fastapi.responses import StreamingResponse, FileResponse, JSONResponse
from openai import AsyncOpenAI
from dotenv import load_dotenv
import os
import json
import logging

# 数据库相关
from sqlalchemy import create_engine, desc
from sqlalchemy.orm import sessionmaker, Mapped, mapped_column, relationship
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy import String, DateTime, ForeignKey, Text, Integer
import datetime
from pathlib import Path

# 日志配置：应用自己的日志统一走这个 logger
logger = logging.getLogger("chat_app")
logger.setLevel(logging.INFO)
if not logger.handlers:
    log_handler = logging.StreamHandler()
    log_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    )
    logger.addHandler(log_handler)
    logger.propagate = False


#1、数据库配置区(database)
BASE_DIR = Path(__file__).resolve().parent
SQLALCHEMY_DATABASE_URI = f"sqlite:///{(BASE_DIR / 'chat.db').as_posix()}"

#-创建数据库引擎(SQLite 必须加 connect_args={"check_same_thread": False}，因为SQLite默认单线程，FastAPI是多线程的)
engine = create_engine(
    SQLALCHEMY_DATABASE_URI, 
    connect_args={"check_same_thread": False}
)
#-创建会话工厂
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# -建立基类
# -后面 models.py 里所有的表结构，都要继承这个 Base，SQLAlchemy才能识别到
Base = declarative_base()

# 依赖注入函数 get_db（给 FastAPI 接口用的）
def get_db():
    db = SessionLocal()  # 从工厂拿一个新的数据库会话
    try:
        yield db  # 把会话交给接口用
    finally:
        db.close()  # 接口处理完，自动关闭会话

# 1.5 大模型配置
# 明确从 main.py 所在目录加载 .env，避免启动目录不同导致读不到
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# 创建异步大模型客户端
llm_client = AsyncOpenAI(
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY")
)
# 读取模型名称
LLM_MODEL = os.getenv("OPENAI_MODEL")




#2、数据模型区（对应 models.py）
# 写Session表、Message表的类，继承Base
class Session(Base):
    __tablename__ = "sessions"  # 表名
    
    id:Mapped[int] = mapped_column(primary_key=True, index=True, comment="会话id") 
    title:Mapped[str] = mapped_column(String(255), index=True, comment="会话标题") 
    created_at:Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow, comment="创建时间") 
    updated_at:Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow, comment="更新时间")    
    
    # 关联消息（可选，级联删除）
    messages: Mapped[list["Message"]] = relationship(
        back_populates="session", cascade="all, delete"
    )

class Message(Base):
    __tablename__ = "messages"  # 表名
    
    id:Mapped[int] = mapped_column(Integer,primary_key=True, index=True, comment="消息id") 
    session_id:Mapped[int] = mapped_column(ForeignKey("sessions.id"), nullable=False, comment="会话id") 
    role:Mapped[str] = mapped_column(String(32), nullable=False, comment="角色") 
    content:Mapped[str] = mapped_column(Text, nullable=False, comment="消息内容") 
    created_at:Mapped[datetime.datetime] = mapped_column(DateTime, default=datetime.datetime.utcnow, comment="创建时间") 
    seq:Mapped[int] = mapped_column(nullable=False, comment="消息序号")          
    
    # 反向关联会话
    session: Mapped["Session"] = relationship(back_populates="messages")

# 两张表都定义完成后才能建表；如果提前调用，metadata 还是空的
Base.metadata.create_all(bind=engine)


# 3、数据校验区（对应 schemas.py）
# 写Pydantic模型，校验接口输入输出

# 新建会话的「请求体」
# 前端调用 POST /sessions 新建会话时，只需要传 title 就行
# 给个默认值，不传也能创建，叫“新会话”
class SessionCreate(BaseModel):
    title: str = "新会话"

# 会话信息的「返回体」
# 新建会话、查询会话列表时，返回给前端看的信息
# 包含 id、标题、创建时间、更新时间，都是数据库里真实有的字段
class SessionResponse(BaseModel):
    id: int
    title: str
    created_at: datetime.datetime
    updated_at: datetime.datetime

    # 这个配置很重要：告诉Pydantic，直接从数据库模型对象里自动读取字段
    # 后面你从数据库查出来的Session对象，直接就能转成这个返回体
    class Config:
        from_attributes = True

# 单条消息的「返回体」
# 查询聊天历史时，返回给前端的每条消息格式
class MessageResponse(BaseModel):
    id: int
    session_id: int
    role: str
    content: str
    seq: int
    created_at: datetime.datetime

    class Config:
        from_attributes = True

# 发聊天消息的「请求体」
# 前端调用 POST /sessions/{id}/chat 时，只需要传一句话的内容
class ChatRequest(BaseModel):
    content: str


# 4、数据库操作区（对应 crud.py）
# 写增删改查的函数

# 新建对话
def create_session(db, title: str):
    new_session = Session(title=title)
    db.add(new_session)
    db.commit()
    db.refresh(new_session)
    return new_session

# 获取所有会话列表，按更新时间倒序排列
def get_sessions(db):
    return db.query(Session).order_by(desc(Session.updated_at)).all()

# 根据id查询单个会话，不存在就返回None
def get_session_by_id(db, session_id: int):
    return db.query(Session).filter(Session.id == session_id).first()   

# 查询某个会话的所有消息，按序号升序排列（历史记录接口用）
def get_messages_by_session_id(db, session_id: int):
    return db.query(Message)\
        .filter(Message.session_id == session_id)\
        .order_by(Message.seq)\
        .all()

# 查询最近 limit 条消息，返回时仍按序号升序（给模型上下文用）
def get_recent_messages_by_session_id(db, session_id: int, limit: int = 20):
    recent_messages = db.query(Message)\
        .filter(Message.session_id == session_id)\
        .order_by(Message.seq.desc())\
        .limit(limit)\
        .all()
    return list(reversed(recent_messages))

# 新建一条消息
def create_message(db, session_id: int, role: str, content: str, seq: int):
    new_message = Message(
        session_id=session_id, 
        role=role, 
        content=content, 
        seq=seq
    )
    db.add(new_message)
    db.commit()
    db.refresh(new_message)

    #顺便更新会话的更新时间
    session = get_session_by_id(db, session_id)
    if session:
        session.updated_at = datetime.datetime.utcnow()
        db.commit()

    return new_message

# 删除会话（因为设置了级联删除，关联的消息会自动一起删掉）
def delete_session(db, session_id: int):
    session = get_session_by_id(db, session_id)
    if session:
        db.delete(session)
        db.commit()
        return True
    return False

# 4.5 流式对话核心
async def event_stream(messages: list):
    """
    接收历史消息，流式返回AI回复，最后返回完整回复内容
    """
    # 用来拼接完整的AI回复，最后存数据库用
    full_reply = ""

    # 调用大模型的流式接口，stream=True 是关键
    stream = await llm_client.chat.completions.create(
        model=LLM_MODEL,
        messages=messages,
        stream=True
    )

    # 异步遍历返回的流，一块一块地接收内容
    async for chunk in stream:
        # 有些模型会在最后返回 choices 为空的 chunk，直接跳过
        if not chunk.choices:
            continue
        # 提取本次返回的一小段文字
        delta = chunk.choices[0].delta.content or ""
        if delta:
            full_reply += delta
            # 按 SSE 标准格式推给前端：data: 内容 + 两个换行
            yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
    
    # 全部发完后，发结束标记，告诉前端传输完成
    yield "data: [DONE]\n\n"




# 5、API接口区（主逻辑）
# 创建FastAPI实例，写各个接口
app = FastAPI(
    title="流式聊天后端API", 
    description="FastAPI + SQLAlchemy + Pydantic 实现的流式对话接口，带会话存储",
    version="1.0.0")


# 全局异常兜底：未被接口捕获的错误统一记录日志并返回 500
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(
        "Unhandled exception on %s %s: %s",
        request.method,
        request.url.path,
        exc,
        exc_info=(type(exc), exc, exc.__traceback__),
    )
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误"})


# HTML 调试页
@app.get("/", include_in_schema=False)
def debug_page():
    return FileResponse(BASE_DIR / "debug.html")


# 新建会话
@app.post("/sessions", response_model=SessionResponse)
def create_new_session(request: SessionCreate, db=Depends(get_db)):
    new_session = create_session(db, request.title)
    return new_session

# 获取所有会话列表
@app.get("/sessions", response_model=List[SessionResponse])
def list_sessions(db=Depends(get_db)):   
    sessions = get_sessions(db)
    return sessions 

# 获取单个对话的历史消息
@app.get("/sessions/{session_id}/messages", response_model=List[MessageResponse])
def get_session_messages(session_id: int, db=Depends(get_db)):    
    session = get_session_by_id(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    messages = get_messages_by_session_id(db, session_id)
    return messages 

# 删除会话
@app.delete ("/sessions/{session_id}")
def delete_session_endpoint(
    session_id: int, 
    db=Depends(get_db)):    

    session = get_session_by_id(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    
    delete_session(db, session_id)
    return {"success": True, "message": "会话删除成功" } 


# 流式聊天接口
@app.post("/sessions/{session_id}/chat")
async def chat_endpoint(
    session_id: int,
    request: ChatRequest,
    db = Depends(get_db)
):
    logger.info("chat start session_id=%s", session_id)

    # 步骤1：校验会话是否存在
    session = get_session_by_id(db, session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 步骤2：先把用户消息存进数据库（坑点2：用户消息提前写）
    history_msgs = get_recent_messages_by_session_id(db, session_id)
    next_seq = history_msgs[-1].seq + 1 if history_msgs else 1

    # 写入用户消息
    create_message(db, session_id, "user", request.content, next_seq)
    next_seq += 1

    # 步骤3：整理历史消息，只取最近20条（坑点3：上下文截断）
    # 把数据库里的消息，转成大模型要求的 [{"role":"xxx","content":"xxx"}] 格式
    messages_for_llm = [
        {"role": msg.role, "content": msg.content}
        for msg in history_msgs[-20:]  # 只取最近20条，防止上下文无限变长
    ]
    # 加上当前这条用户消息
    messages_for_llm.append({"role": "user", "content": request.content})

    # 步骤4：定义内部生成器，处理AI回复存库（坑点1：数据库生命周期）
    async def sse_generator():
        # 坑点1重点：不要用外面接口注入的db！自己新开一个数据库会话
        # 因为接口返回后外面的db会被关闭，但生成器还在运行，会报错
        db_inner = SessionLocal()
        try:
            # 调用流式生成器，接收每一段内容，直接转发给前端
            full_reply = ""
            try:
                async for data in event_stream(messages_for_llm):
                    yield data
                    # 提取delta拼接完整回复（这里只是拼，不写库）
                    if '"delta":' in data:
                        delta_data = json.loads(data.replace("data: ", ""))
                        full_reply += delta_data["delta"]
            except Exception as exc:
                logger.error(
                    "SSE stream failed session_id=%s: %s",
                    session_id,
                    exc,
                    exc_info=(type(exc), exc, exc.__traceback__),
                )
                yield f"data: {json.dumps({'error': '模型调用失败，请稍后重试'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
                return
            
            # 流结束后，一次性把完整AI回复写入数据库（坑点2：最后一次性写）
            create_message(db_inner, session_id, "assistant", full_reply, next_seq)
            logger.info(
                "chat done session_id=%s chars=%s",
                session_id,
                len(full_reply),
            )
        finally:
            # 手动关闭自己新开的会话
            db_inner.close()

    # 步骤5：返回流式响应
    return StreamingResponse(
        sse_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",  # 禁止缓存
            "X-Accel-Buffering": "no"     # 关闭代理缓冲，保证流式不卡顿
        }
    )









