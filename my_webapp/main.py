import asyncio, os, hmac, hashlib, json, logging
from urllib.parse import parse_qsl
from datetime import datetime
from typing import Optional, List
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from fastapi import FastAPI, Request, Depends, HTTPException
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from dotenv import load_dotenv
from database import engine, async_session, Base, User, Task, Announcement, UserRole, TaskStatus

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID") or 0)
BASE_URL = os.getenv("BASE_URL")
logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])
templates = Jinja2Templates(directory="templates")

# --- Helpers ---
async def send_notify(tg_id: int, text: str):
    try: await bot.send_message(tg_id, text)
    except: pass # User blocked bot or other error

async def broadcast_notify(db: AsyncSession, text: str):
    result = await db.execute(select(User.telegram_id))
    ids = result.scalars().all()
    for tg_id in ids:
        asyncio.create_task(send_notify(tg_id, text))

# --- Models ---
class InitDataSchema(BaseModel): initData: str
class TaskCreate(BaseModel):
    title: str; description: Optional[str] = ""; assignee_id: int; deadline: Optional[datetime] = None
class TaskUpdate(BaseModel):
    status: Optional[TaskStatus] = None; title: Optional[str] = None; description: Optional[str] = None
    deadline: Optional[datetime] = None; dispute_reason: Optional[str] = None
class UserRoleUpdate(BaseModel): role: UserRole
class AnnouncementCreate(BaseModel): content: str

async def get_db():
    async with async_session() as session: yield session

def validate_telegram_data(init_data: str, bot_token: str) -> dict:
    try:
        parsed_data = dict(parse_qsl(init_data))
        hash_val = parsed_data.pop('hash')
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        if hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest() != hash_val: raise ValueError
        return json.loads(parsed_data['user'])
    except: raise HTTPException(403, "Auth failed")

# --- API ---
@app.get("/")
async def serve_webapp(request: Request): return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/auth")
async def auth_user(data: InitDataSchema, db: AsyncSession = Depends(get_db)):
    tg_user = validate_telegram_data(data.initData, BOT_TOKEN)
    tg_id = tg_user['id']
    full_name = f"{tg_user.get('first_name','')} {tg_user.get('last_name','')}".strip()
    result = await db.execute(select(User).where(User.telegram_id == tg_id))
    user = result.scalar_one_or_none()
    if not user:
        role = UserRole.OWNER if tg_id == OWNER_ID else UserRole.WORKER
        user = User(telegram_id=tg_id, full_name=full_name, role=role)
        db.add(user)
    else:
        user.full_name = full_name
        if tg_id == OWNER_ID: user.role = UserRole.OWNER
    await db.commit()
    await db.refresh(user)
    return {"user": {"id": user.id, "telegram_id": user.telegram_id, "role": user.role, "full_name": user.full_name}}

@app.get("/api/users")
async def get_users(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User))
    return [{"id": u.id, "full_name": u.full_name, "role": u.role} for u in result.scalars().all()]

@app.patch("/api/users/{user_id}/role")
async def update_user_role(user_id: int, role_data: UserRoleUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    tg_user = validate_telegram_data(request.headers.get("X-Telegram-Init-Data"), BOT_TOKEN)
    if tg_user['id'] != OWNER_ID: raise HTTPException(403, "Only Owner")
    user = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if not user or user.telegram_id == OWNER_ID: raise HTTPException(400)
    user.role = role_data.role
    await db.commit()
    return {"status": "ok"}

# --- Tasks ---
@app.get("/api/tasks")
async def get_tasks(request: Request, db: AsyncSession = Depends(get_db)):
    tg_user = validate_telegram_data(request.headers.get("X-Telegram-Init-Data"), BOT_TOKEN)
    user = (await db.execute(select(User).where(User.telegram_id == tg_user['id']))).scalar_one_or_none()
    stmt = select(Task).order_by(Task.deadline.asc())
    if user.role == UserRole.WORKER or request.query_params.get("filter") == "mine":
        stmt = stmt.where(Task.assignee_id == user.id)
    tasks = (await db.execute(stmt)).scalars().all()
    tasks_data = []
    for t in tasks:
        assignee = (await db.execute(select(User).where(User.id == t.assignee_id))).scalar_one_or_none()
        tasks_data.append({
            "id": t.id, "title": t.title, "description": t.description, "status": t.status,
            "deadline": t.deadline, "assignee_name": assignee.full_name if assignee else "Unknown",
            "is_mine": t.assignee_id == user.id, "is_locked": t.is_locked,
            "dispute_reason": t.dispute_reason if (user.role != UserRole.WORKER or t.assignee_id == user.id) else None
        })
    return tasks_data

@app.post("/api/tasks")
async def create_task(task_data: TaskCreate, request: Request, db: AsyncSession = Depends(get_db)):
    tg_user = validate_telegram_data(request.headers.get("X-Telegram-Init-Data"), BOT_TOKEN)
    creator = (await db.execute(select(User).where(User.telegram_id == tg_user['id']))).scalar_one_or_none()
    if creator.role not in [UserRole.ADMIN, UserRole.OWNER]: raise HTTPException(403)
    
    task = Task(title=task_data.title, description=task_data.description, creator_id=creator.id, assignee_id=task_data.assignee_id, deadline=task_data.deadline)
    db.add(task)
    await db.commit()
    
    # Notify Assignee
    assignee = (await db.execute(select(User).where(User.id == task_data.assignee_id))).scalar_one_or_none()
    if assignee: asyncio.create_task(send_notify(assignee.telegram_id, f"📝 Новая задача: {task_data.title}"))
    
    return {"status": "ok"}

@app.patch("/api/tasks/{task_id}")
async def update_task(task_id: int, task_data: TaskUpdate, request: Request, db: AsyncSession = Depends(get_db)):
    tg_user = validate_telegram_data(request.headers.get("X-Telegram-Init-Data"), BOT_TOKEN)
    user = (await db.execute(select(User).where(User.telegram_id == tg_user['id']))).scalar_one_or_none()
    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if not task: raise HTTPException(404)
    
    # Fetch roles involved for notification
    creator = (await db.execute(select(User).where(User.id == task.creator_id))).scalar_one_or_none()
    assignee = (await db.execute(select(User).where(User.id == task.assignee_id))).scalar_one_or_none()

    is_owner = user.role == UserRole.OWNER
    
    if task_data.status == TaskStatus.DISPUTED:
        if task.assignee_id != user.id: raise HTTPException(403)
        if task.is_locked: raise HTTPException(400)
        task.status = TaskStatus.DISPUTED; task.dispute_reason = task_data.dispute_reason
        if creator: asyncio.create_task(send_notify(creator.telegram_id, f"⚠️ Задача оспорена: {task.title}\nПричина: {task_data.dispute_reason}"))
    
    elif task.status == TaskStatus.DISPUTED and task_data.status:
        if not is_owner: raise HTTPException(403)
        task.status = task_data.status; task.dispute_reason = None
        if task_data.status != TaskStatus.DONE: task.is_locked = True
        if assignee: asyncio.create_task(send_notify(assignee.telegram_id, f"🔒 Решение по спору: {task.title}\nСтатус: {task_data.status.value}"))
    
    else:
        # Standard Update
        if user.role == UserRole.WORKER:
            if task.assignee_id != user.id: raise HTTPException(403)
            if task_data.status:
                task.status = task_data.status
                if task_data.status == TaskStatus.DONE and creator:
                    asyncio.create_task(send_notify(creator.telegram_id, f"✅ Задача выполнена: {task.title}\nПроверьте выполнение!"))
        else:
            for k, v in task_data.dict(exclude_unset=True).items(): 
                 if k != 'dispute_reason': setattr(task, k, v)
    
    await db.commit()
    return {"status": "updated"}

# --- Announcements ---
@app.get("/api/announcements")
async def get_announcements(db: AsyncSession = Depends(get_db)):
    res = await db.execute(select(Announcement).order_by(Announcement.created_at.desc()).limit(20))
    return res.scalars().all()

@app.post("/api/announcements")
async def create_announcement(data: AnnouncementCreate, request: Request, db: AsyncSession = Depends(get_db)):
    tg_user = validate_telegram_data(request.headers.get("X-Telegram-Init-Data"), BOT_TOKEN)
    user = (await db.execute(select(User).where(User.telegram_id == tg_user['id']))).scalar_one_or_none()
    if user.role not in [UserRole.ADMIN, UserRole.OWNER]: raise HTTPException(403)
    
    db.add(Announcement(content=data.content, author_name=user.full_name))
    await db.commit()
    
    # Broadcast
    asyncio.create_task(broadcast_notify(db, f"📢 Новое объявление:\n{data.content}"))
    return {"status": "ok"}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    async with async_session() as session:
        if not (await session.execute(select(User).where(User.telegram_id == message.from_user.id))).scalar_one_or_none():
            role = UserRole.OWNER if message.from_user.id == OWNER_ID else UserRole.WORKER
            session.add(User(telegram_id=message.from_user.id, full_name=message.from_user.full_name, role=role))
            await session.commit()
    await message.answer("Task Manager:", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Open App", web_app=WebAppInfo(url=BASE_URL))]]))

async def main():
    async with engine.begin() as conn: await conn.run_sync(Base.metadata.create_all)
    import uvicorn
    config = uvicorn.Config(app=app, host="0.0.0.0", port=8000, loop="asyncio")
    server = uvicorn.Server(config)
    await asyncio.gather(server.serve(), dp.start_polling(bot))

if __name__ == "__main__": asyncio.run(main())
