from fastapi import FastAPI, APIRouter, HTTPException, Depends, Response, Request, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field, EmailStr, ConfigDict
from typing import List, Optional, Dict
import uuid
from datetime import datetime, timezone, timedelta
import bcrypt
import jwt
import httpx
import json
import asyncio
import aiofiles
import shutil

# Google Calendar imports
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request as GoogleRequest
from googleapiclient.discovery import build

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# Create uploads directory
UPLOAD_DIR = ROOT_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

# MongoDB connection
mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

# JWT Configuration
JWT_SECRET = os.environ.get('JWT_SECRET', 'your-super-secret-jwt-key-change-in-production')
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 168  # 7 days

# Google Calendar Configuration (optional - set these in .env if using real Google Calendar)
GOOGLE_CLIENT_ID = os.environ.get('GOOGLE_CALENDAR_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.environ.get('GOOGLE_CALENDAR_CLIENT_SECRET', '')
GOOGLE_REDIRECT_URI = os.environ.get('GOOGLE_CALENDAR_REDIRECT_URI', '')

# Create the main app
app = FastAPI(title="EduAdvise - International Student Counseling Platform")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ==================== WEBSOCKET CONNECTION MANAGER ====================

class ConnectionManager:
    def __init__(self):
        # user_id -> WebSocket
        self.active_connections: Dict[str, WebSocket] = {}
        # room_id -> list of user_ids
        self.chat_rooms: Dict[str, List[str]] = {}
        # For WebRTC signaling: user_id -> pending signals
        self.pending_signals: Dict[str, List[dict]] = {}
    
    async def connect(self, websocket: WebSocket, user_id: str):
        await websocket.accept()
        self.active_connections[user_id] = websocket
        logger.info(f"User {user_id} connected via WebSocket")
    
    def disconnect(self, user_id: str):
        if user_id in self.active_connections:
            del self.active_connections[user_id]
            logger.info(f"User {user_id} disconnected")
    
    async def send_personal_message(self, message: dict, user_id: str):
        if user_id in self.active_connections:
            try:
                await self.active_connections[user_id].send_json(message)
            except Exception as e:
                logger.error(f"Error sending message to {user_id}: {e}")
    
    async def broadcast_to_room(self, room_id: str, message: dict, exclude_user: str = None):
        if room_id in self.chat_rooms:
            for user_id in self.chat_rooms[room_id]:
                if user_id != exclude_user and user_id in self.active_connections:
                    await self.send_personal_message(message, user_id)
    
    def join_room(self, room_id: str, user_id: str):
        if room_id not in self.chat_rooms:
            self.chat_rooms[room_id] = []
        if user_id not in self.chat_rooms[room_id]:
            self.chat_rooms[room_id].append(user_id)
    
    def leave_room(self, room_id: str, user_id: str):
        if room_id in self.chat_rooms and user_id in self.chat_rooms[room_id]:
            self.chat_rooms[room_id].remove(user_id)
    
    def is_user_online(self, user_id: str) -> bool:
        return user_id in self.active_connections

manager = ConnectionManager()

# ==================== EMAIL NOTIFICATION SYSTEM (MOCK) ====================

class EmailNotificationService:
    """Mock email service that logs emails instead of sending them"""
    
    @staticmethod
    async def log_email(to_email: str, subject: str, body: str, email_type: str):
        """Store email in database for tracking"""
        email_log = {
            "email_id": f"email_{uuid.uuid4().hex[:12]}",
            "to_email": to_email,
            "subject": subject,
            "body": body,
            "email_type": email_type,
            "status": "logged",  # In production: pending, sent, failed
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.email_logs.insert_one(email_log)
        logger.info(f"[EMAIL] To: {to_email} | Subject: {subject} | Type: {email_type}")
        return email_log
    
    @staticmethod
    async def send_new_message_notification(to_email: str, sender_name: str, message_preview: str):
        subject = f"New message from {sender_name} - EduAdvise"
        body = f"""
Hello,

You have a new message from {sender_name}:

"{message_preview[:100]}{'...' if len(message_preview) > 100 else ''}"

Log in to EduAdvise to read and reply.

Best regards,
EduAdvise Team
        """
        return await EmailNotificationService.log_email(to_email, subject, body, "new_message")
    
    @staticmethod
    async def send_incoming_call_notification(to_email: str, caller_name: str, call_type: str):
        subject = f"Missed {call_type} call from {caller_name} - EduAdvise"
        body = f"""
Hello,

You missed a {call_type} call from {caller_name} on EduAdvise.

Log in to EduAdvise to connect with them.

Best regards,
EduAdvise Team
        """
        return await EmailNotificationService.log_email(to_email, subject, body, "missed_call")
    
    @staticmethod
    async def send_booking_reminder(to_email: str, user_name: str, other_party_name: str, 
                                     service_name: str, session_time: str, hours_before: int):
        subject = f"Reminder: Session in {hours_before} hour{'s' if hours_before > 1 else ''} - EduAdvise"
        body = f"""
Hello {user_name},

This is a reminder that your session is coming up:

Service: {service_name}
With: {other_party_name}
Time: {session_time}

{'Please be ready to join the video call on time.' if hours_before == 1 else 'Make sure you have prepared any questions or documents you want to discuss.'}

Best regards,
EduAdvise Team
        """
        return await EmailNotificationService.log_email(to_email, subject, body, f"reminder_{hours_before}h")
    
    @staticmethod
    async def send_booking_confirmation(to_email: str, user_name: str, service_name: str, 
                                        counselor_name: str, session_time: str):
        subject = f"Booking Confirmed - {service_name} - EduAdvise"
        body = f"""
Hello {user_name},

Your booking has been confirmed!

Service: {service_name}
Counselor: {counselor_name}
Time: {session_time}

You will receive reminders 24 hours and 1 hour before your session.

Best regards,
EduAdvise Team
        """
        return await EmailNotificationService.log_email(to_email, subject, body, "booking_confirmed")

email_service = EmailNotificationService()

# ==================== IN-APP REMINDER SYSTEM ====================

class ReminderService:
    """Service to manage in-app reminders"""
    
    @staticmethod
    async def create_reminder(user_id: str, booking_id: str, reminder_time: datetime, 
                              reminder_type: str, message: str):
        reminder = {
            "reminder_id": f"reminder_{uuid.uuid4().hex[:12]}",
            "user_id": user_id,
            "booking_id": booking_id,
            "reminder_time": reminder_time.isoformat(),
            "reminder_type": reminder_type,  # "24h", "1h", "now"
            "message": message,
            "is_sent": False,
            "is_read": False,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.reminders.insert_one(reminder)
        return reminder
    
    @staticmethod
    async def get_pending_reminders(user_id: str):
        """Get all pending reminders for a user"""
        now = datetime.now(timezone.utc).isoformat()
        reminders = await db.reminders.find({
            "user_id": user_id,
            "reminder_time": {"$lte": now},
            "is_sent": False
        }, {"_id": 0}).to_list(50)
        return reminders
    
    @staticmethod
    async def mark_reminder_sent(reminder_id: str):
        await db.reminders.update_one(
            {"reminder_id": reminder_id},
            {"$set": {"is_sent": True}}
        )
    
    @staticmethod
    async def mark_reminder_read(reminder_id: str):
        await db.reminders.update_one(
            {"reminder_id": reminder_id},
            {"$set": {"is_read": True}}
        )

reminder_service = ReminderService()

# ==================== WEBRTC TURN SERVER CONFIG ====================

# Free TURN servers from Metered.ca
TURN_SERVERS = {
    "iceServers": [
        {"urls": "stun:stun.l.google.com:19302"},
        {"urls": "stun:stun1.l.google.com:19302"},
        {"urls": "stun:stun.relay.metered.ca:80"},
        {
            "urls": "turn:global.relay.metered.ca:80",
            "username": "free",
            "credential": "free"
        },
        {
            "urls": "turn:global.relay.metered.ca:80?transport=tcp",
            "username": "free",
            "credential": "free"
        },
        {
            "urls": "turn:global.relay.metered.ca:443",
            "username": "free",
            "credential": "free"
        }
    ]
}

# ==================== MODELS ====================

class UserBase(BaseModel):
    model_config = ConfigDict(extra="ignore")
    email: EmailStr
    first_name: str
    last_name: str
    phone: Optional[str] = None

class UserCreate(UserBase):
    password: str
    user_type: str = "student"  # student, counselor, admin

class UserLogin(BaseModel):
    email: EmailStr
    password: str

class User(UserBase):
    user_id: str
    user_type: str
    created_at: datetime
    is_verified: bool = False
    picture: Optional[str] = None

class CounselorProfileCreate(BaseModel):
    credentials: str
    experience_years: int
    specializations: List[str]
    languages: List[str]
    bio: str
    photo: Optional[str] = None
    linkedin_url: Optional[str] = None

class CounselorProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    counselor_id: str
    user_id: str
    credentials: str
    experience_years: int
    specializations: List[str]
    languages: List[str]
    bio: str
    photo: Optional[str] = None
    linkedin_url: Optional[str] = None
    status: str = "pending"  # pending, approved, rejected, suspended
    rating: float = 0.0
    total_reviews: int = 0
    total_sessions: int = 0
    created_at: datetime

class ServiceCreate(BaseModel):
    name: str
    description: str
    duration: int  # in minutes
    price: float
    category: str

class Service(BaseModel):
    model_config = ConfigDict(extra="ignore")
    service_id: str
    counselor_id: str
    name: str
    description: str
    duration: int
    price: float
    category: str
    is_active: bool = True
    created_at: datetime

class StudentProfileCreate(BaseModel):
    country: str
    intended_destination: str
    field_of_interest: str
    budget_range: str

class StudentProfile(BaseModel):
    model_config = ConfigDict(extra="ignore")
    student_id: str
    user_id: str
    country: str
    intended_destination: str
    field_of_interest: str
    budget_range: str
    saved_counselors: List[str] = []
    created_at: datetime

class BookingCreate(BaseModel):
    counselor_id: str
    service_id: str
    date_time: datetime
    student_notes: Optional[str] = None

class Booking(BaseModel):
    model_config = ConfigDict(extra="ignore")
    booking_id: str
    student_id: str
    counselor_id: str
    service_id: str
    date_time: datetime
    duration: int
    status: str = "pending"  # pending, confirmed, completed, cancelled
    student_notes: Optional[str] = None
    counselor_notes: Optional[str] = None
    price: float
    created_at: datetime

class ReviewCreate(BaseModel):
    booking_id: str
    rating: int = Field(ge=1, le=5)
    comment: str

class Review(BaseModel):
    model_config = ConfigDict(extra="ignore")
    review_id: str
    booking_id: str
    student_id: str
    counselor_id: str
    rating: int
    comment: str
    created_at: datetime

class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    message_id: str
    conversation_id: str  # Format: sorted(user1_id, user2_id) joined
    sender_id: str
    receiver_id: str
    content: str
    message_type: str = "text"  # text, system
    read: bool = False
    created_at: datetime

class Conversation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    conversation_id: str
    participants: List[str]  # user_ids
    last_message: Optional[str] = None
    last_message_time: Optional[datetime] = None
    unread_count: Dict[str, int] = {}

class CallSession(BaseModel):
    model_config = ConfigDict(extra="ignore")
    call_id: str
    booking_id: str
    caller_id: str
    receiver_id: str
    call_type: str  # video, voice
    status: str = "initiating"  # initiating, ringing, active, ended
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

class University(BaseModel):
    model_config = ConfigDict(extra="ignore")
    university_id: str
    name: str
    country: str
    city: str
    logo: Optional[str] = None
    cover_image: Optional[str] = None
    ranking: Optional[int] = None
    website: str
    programs: List[str]
    tuition_min: float
    tuition_max: float
    application_deadlines: dict
    popular_majors: List[str]
    description: str
    images: List[str] = []

class UniversityCreate(BaseModel):
    name: str
    country: str
    city: str
    logo: Optional[str] = None
    cover_image: Optional[str] = None
    ranking: Optional[int] = None
    website: str
    programs: List[str]
    tuition_min: float
    tuition_max: float
    application_deadlines: dict
    popular_majors: List[str]
    description: str
    images: List[str] = []

class Guide(BaseModel):
    model_config = ConfigDict(extra="ignore")
    guide_id: str
    country: str
    title: str
    content: str
    timeline: List[dict]
    required_documents: List[str]
    tests_required: List[str]
    visa_info: str
    estimated_cost: str
    post_study_work: str

class BlogPost(BaseModel):
    model_config = ConfigDict(extra="ignore")
    post_id: str
    title: str
    content: str
    category: str
    author: str
    tags: List[str]
    published_at: datetime
    views: int = 0

class FAQ(BaseModel):
    model_config = ConfigDict(extra="ignore")
    faq_id: str
    question: str
    answer: str
    category: str

# ==================== HELPER FUNCTIONS ====================

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())

def create_jwt_token(user_id: str, user_type: str) -> str:
    payload = {
        "user_id": user_id,
        "user_type": user_type,
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRATION_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def get_current_user(request: Request) -> dict:
    # Check cookie first
    session_token = request.cookies.get("session_token")
    if session_token:
        session = await db.user_sessions.find_one({"session_token": session_token}, {"_id": 0})
        if session:
            expires_at = session.get("expires_at")
            if isinstance(expires_at, str):
                expires_at = datetime.fromisoformat(expires_at)
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at > datetime.now(timezone.utc):
                user = await db.users.find_one({"user_id": session["user_id"]}, {"_id": 0})
                if user:
                    return user
    
    # Check Authorization header
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ")[1]
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
            user = await db.users.find_one({"user_id": payload["user_id"]}, {"_id": 0})
            if user:
                return user
        except jwt.ExpiredSignatureError:
            raise HTTPException(status_code=401, detail="Token expired")
        except jwt.InvalidTokenError:
            raise HTTPException(status_code=401, detail="Invalid token")
    
    raise HTTPException(status_code=401, detail="Not authenticated")

async def get_optional_user(request: Request) -> Optional[dict]:
    try:
        return await get_current_user(request)
    except HTTPException:
        return None

# ==================== AUTH ENDPOINTS ====================

@api_router.post("/auth/register")
async def register(user_data: UserCreate):
    existing = await db.users.find_one({"email": user_data.email}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=400, detail="Email already registered")
    
    user_id = f"user_{uuid.uuid4().hex[:12]}"
    user_doc = {
        "user_id": user_id,
        "email": user_data.email,
        "first_name": user_data.first_name,
        "last_name": user_data.last_name,
        "phone": user_data.phone,
        "password_hash": hash_password(user_data.password),
        "user_type": user_data.user_type,
        "is_verified": True,  # Auto-verify for demo
        "picture": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.users.insert_one(user_doc)
    
    token = create_jwt_token(user_id, user_data.user_type)
    del user_doc["password_hash"]
    user_doc.pop("_id", None)
    
    return {"user": user_doc, "token": token}

@api_router.post("/auth/login")
async def login(credentials: UserLogin, response: Response):
    user = await db.users.find_one({"email": credentials.email}, {"_id": 0})
    if not user or not verify_password(credentials.password, user.get("password_hash", "")):
        raise HTTPException(status_code=401, detail="Invalid credentials")
    
    token = create_jwt_token(user["user_id"], user["user_type"])
    
    # Set cookie
    response.set_cookie(
        key="session_token",
        value=token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=JWT_EXPIRATION_HOURS * 3600,
        path="/"
    )
    
    user_data = {k: v for k, v in user.items() if k != "password_hash"}
    return {"user": user_data, "token": token}

@api_router.post("/auth/logout")
async def logout(response: Response, request: Request):
    session_token = request.cookies.get("session_token")
    if session_token:
        await db.user_sessions.delete_one({"session_token": session_token})
    response.delete_cookie("session_token", path="/")
    return {"message": "Logged out successfully"}

@api_router.get("/auth/me")
async def get_me(request: Request):
    user = await get_current_user(request)
    user_data = {k: v for k, v in user.items() if k != "password_hash"}
    return user_data

# REMINDER: DO NOT HARDCODE THE URL, OR ADD ANY FALLBACKS OR REDIRECT URLS, THIS BREAKS THE AUTH
@api_router.post("/auth/google/session")
async def google_session(request: Request, response: Response):
    body = await request.json()
    session_id = body.get("session_id")
    
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id required")
    
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://demobackend.emergentagent.com/auth/v1/env/oauth/session-data",
            headers={"X-Session-ID": session_id}
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=401, detail="Invalid session")
        google_data = resp.json()
    
    email = google_data.get("email")
    name = google_data.get("name", "")
    picture = google_data.get("picture")
    external_session_token = google_data.get("session_token")
    
    # Check if user exists
    user = await db.users.find_one({"email": email}, {"_id": 0})
    
    if not user:
        # Create new user
        user_id = f"user_{uuid.uuid4().hex[:12]}"
        name_parts = name.split(" ", 1)
        first_name = name_parts[0] if name_parts else ""
        last_name = name_parts[1] if len(name_parts) > 1 else ""
        
        user = {
            "user_id": user_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": None,
            "user_type": "student",
            "is_verified": True,
            "picture": picture,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.users.insert_one(user)
        user.pop("_id", None)
    else:
        user_id = user["user_id"]
        # Update picture if changed
        if picture and user.get("picture") != picture:
            await db.users.update_one({"user_id": user_id}, {"$set": {"picture": picture}})
            user["picture"] = picture
    
    # Store session
    session_doc = {
        "user_id": user_id,
        "session_token": external_session_token,
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=7)).isoformat(),
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.user_sessions.insert_one(session_doc)
    
    # Set cookie
    response.set_cookie(
        key="session_token",
        value=external_session_token,
        httponly=True,
        secure=True,
        samesite="none",
        max_age=7 * 24 * 3600,
        path="/"
    )
    
    return {"user": user}

# ==================== COUNSELOR ENDPOINTS ====================

@api_router.get("/counselors")
async def get_counselors(
    specialization: Optional[str] = None,
    language: Optional[str] = None,
    min_price: Optional[float] = None,
    max_price: Optional[float] = None,
    min_rating: Optional[float] = None,
    sort_by: Optional[str] = "rating"
):
    query = {"status": "approved"}
    
    if specialization:
        query["specializations"] = {"$in": [specialization]}
    if language:
        query["languages"] = {"$in": [language]}
    if min_rating:
        query["rating"] = {"$gte": min_rating}
    
    counselors = await db.counselor_profiles.find(query, {"_id": 0}).to_list(100)
    
    # Get user details and services for each counselor
    result = []
    for c in counselors:
        user = await db.users.find_one({"user_id": c["user_id"]}, {"_id": 0, "password_hash": 0})
        services = await db.services.find({"counselor_id": c["counselor_id"], "is_active": True}, {"_id": 0}).to_list(50)
        
        # Filter by price if specified
        if min_price or max_price:
            if not services:
                continue
            prices = [s["price"] for s in services]
            if min_price and min(prices) < min_price:
                continue
            if max_price and max(prices) > max_price:
                continue
        
        c["user"] = user
        c["services"] = services
        result.append(c)
    
    # Sort
    if sort_by == "rating":
        result.sort(key=lambda x: x.get("rating", 0), reverse=True)
    elif sort_by == "price_low":
        result.sort(key=lambda x: min([s["price"] for s in x.get("services", [])] or [0]))
    elif sort_by == "price_high":
        result.sort(key=lambda x: max([s["price"] for s in x.get("services", [])] or [0]), reverse=True)
    elif sort_by == "experience":
        result.sort(key=lambda x: x.get("experience_years", 0), reverse=True)
    
    return result

@api_router.get("/counselors/{counselor_id}")
async def get_counselor(counselor_id: str):
    counselor = await db.counselor_profiles.find_one({"counselor_id": counselor_id}, {"_id": 0})
    if not counselor:
        raise HTTPException(status_code=404, detail="Counselor not found")
    
    user = await db.users.find_one({"user_id": counselor["user_id"]}, {"_id": 0, "password_hash": 0})
    services = await db.services.find({"counselor_id": counselor_id, "is_active": True}, {"_id": 0}).to_list(50)
    reviews = await db.reviews.find({"counselor_id": counselor_id}, {"_id": 0}).to_list(100)
    
    # Get student names for reviews
    for review in reviews:
        student_profile = await db.student_profiles.find_one({"student_id": review["student_id"]}, {"_id": 0})
        if student_profile:
            student_user = await db.users.find_one({"user_id": student_profile["user_id"]}, {"_id": 0, "password_hash": 0})
            review["student_name"] = f"{student_user['first_name']} {student_user['last_name'][0]}." if student_user else "Anonymous"
    
    counselor["user"] = user
    counselor["services"] = services
    counselor["reviews"] = reviews
    
    return counselor

@api_router.post("/counselors/apply")
async def apply_as_counselor(profile_data: CounselorProfileCreate, request: Request):
    user = await get_current_user(request)
    
    # Check if already applied
    existing = await db.counselor_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=400, detail="Already applied as counselor")
    
    counselor_id = f"counselor_{uuid.uuid4().hex[:12]}"
    profile_doc = {
        "counselor_id": counselor_id,
        "user_id": user["user_id"],
        **profile_data.model_dump(),
        "status": "pending",
        "rating": 0.0,
        "total_reviews": 0,
        "total_sessions": 0,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.counselor_profiles.insert_one(profile_doc)
    
    # Update user type
    await db.users.update_one({"user_id": user["user_id"]}, {"$set": {"user_type": "counselor"}})
    
    profile_doc.pop("_id", None)
    return profile_doc

@api_router.get("/counselors/me/profile")
async def get_my_counselor_profile(request: Request):
    user = await get_current_user(request)
    profile = await db.counselor_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=404, detail="Counselor profile not found")
    
    services = await db.services.find({"counselor_id": profile["counselor_id"]}, {"_id": 0}).to_list(50)
    profile["services"] = services
    profile["user"] = {k: v for k, v in user.items() if k != "password_hash"}
    
    return profile

@api_router.put("/counselors/me/profile")
async def update_my_counselor_profile(profile_data: CounselorProfileCreate, request: Request):
    user = await get_current_user(request)
    profile = await db.counselor_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=404, detail="Counselor profile not found")
    
    await db.counselor_profiles.update_one(
        {"counselor_id": profile["counselor_id"]},
        {"$set": profile_data.model_dump()}
    )
    
    updated = await db.counselor_profiles.find_one({"counselor_id": profile["counselor_id"]}, {"_id": 0})
    return updated

# ==================== SERVICE ENDPOINTS ====================

@api_router.post("/services")
async def create_service(service_data: ServiceCreate, request: Request):
    user = await get_current_user(request)
    profile = await db.counselor_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=403, detail="Only counselors can create services")
    
    service_id = f"service_{uuid.uuid4().hex[:12]}"
    service_doc = {
        "service_id": service_id,
        "counselor_id": profile["counselor_id"],
        **service_data.model_dump(),
        "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.services.insert_one(service_doc)
    service_doc.pop("_id", None)
    return service_doc

@api_router.put("/services/{service_id}")
async def update_service(service_id: str, service_data: ServiceCreate, request: Request):
    user = await get_current_user(request)
    profile = await db.counselor_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=403, detail="Only counselors can update services")
    
    service = await db.services.find_one({"service_id": service_id, "counselor_id": profile["counselor_id"]}, {"_id": 0})
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    await db.services.update_one({"service_id": service_id}, {"$set": service_data.model_dump()})
    updated = await db.services.find_one({"service_id": service_id}, {"_id": 0})
    return updated

@api_router.delete("/services/{service_id}")
async def delete_service(service_id: str, request: Request):
    user = await get_current_user(request)
    profile = await db.counselor_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=403, detail="Only counselors can delete services")
    
    result = await db.services.delete_one({"service_id": service_id, "counselor_id": profile["counselor_id"]})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Service not found")
    
    return {"message": "Service deleted"}

# ==================== STUDENT ENDPOINTS ====================

@api_router.post("/students/profile")
async def create_student_profile(profile_data: StudentProfileCreate, request: Request):
    user = await get_current_user(request)
    
    existing = await db.student_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if existing:
        raise HTTPException(status_code=400, detail="Profile already exists")
    
    student_id = f"student_{uuid.uuid4().hex[:12]}"
    profile_doc = {
        "student_id": student_id,
        "user_id": user["user_id"],
        **profile_data.model_dump(),
        "saved_counselors": [],
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.student_profiles.insert_one(profile_doc)
    profile_doc.pop("_id", None)
    return profile_doc

@api_router.get("/students/me/profile")
async def get_my_student_profile(request: Request):
    user = await get_current_user(request)
    profile = await db.student_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    
    profile["user"] = {k: v for k, v in user.items() if k != "password_hash"}
    return profile

@api_router.put("/students/me/profile")
async def update_student_profile(profile_data: StudentProfileCreate, request: Request):
    user = await get_current_user(request)
    profile = await db.student_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    
    await db.student_profiles.update_one(
        {"student_id": profile["student_id"]},
        {"$set": profile_data.model_dump()}
    )
    
    updated = await db.student_profiles.find_one({"student_id": profile["student_id"]}, {"_id": 0})
    return updated

@api_router.post("/students/saved-counselors/{counselor_id}")
async def save_counselor(counselor_id: str, request: Request):
    user = await get_current_user(request)
    profile = await db.student_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    
    if counselor_id not in profile.get("saved_counselors", []):
        await db.student_profiles.update_one(
            {"student_id": profile["student_id"]},
            {"$push": {"saved_counselors": counselor_id}}
        )
    
    return {"message": "Counselor saved"}

@api_router.delete("/students/saved-counselors/{counselor_id}")
async def unsave_counselor(counselor_id: str, request: Request):
    user = await get_current_user(request)
    profile = await db.student_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=404, detail="Student profile not found")
    
    await db.student_profiles.update_one(
        {"student_id": profile["student_id"]},
        {"$pull": {"saved_counselors": counselor_id}}
    )
    
    return {"message": "Counselor removed from saved"}

@api_router.get("/students/saved-counselors")
async def get_saved_counselors(request: Request):
    user = await get_current_user(request)
    profile = await db.student_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        return []
    
    saved_ids = profile.get("saved_counselors", [])
    counselors = []
    for cid in saved_ids:
        counselor = await db.counselor_profiles.find_one({"counselor_id": cid, "status": "approved"}, {"_id": 0})
        if counselor:
            user_data = await db.users.find_one({"user_id": counselor["user_id"]}, {"_id": 0, "password_hash": 0})
            counselor["user"] = user_data
            counselors.append(counselor)
    
    return counselors

# ==================== BOOKING ENDPOINTS ====================

@api_router.post("/bookings")
async def create_booking(booking_data: BookingCreate, request: Request):
    user = await get_current_user(request)
    profile = await db.student_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=403, detail="Only students can create bookings")
    
    service = await db.services.find_one({"service_id": booking_data.service_id}, {"_id": 0})
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    booking_id = f"booking_{uuid.uuid4().hex[:12]}"
    booking_doc = {
        "booking_id": booking_id,
        "student_id": profile["student_id"],
        "counselor_id": booking_data.counselor_id,
        "service_id": booking_data.service_id,
        "date_time": booking_data.date_time.isoformat(),
        "duration": service["duration"],
        "status": "pending",
        "student_notes": booking_data.student_notes,
        "counselor_notes": None,
        "price": service["price"],
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.bookings.insert_one(booking_doc)
    booking_doc.pop("_id", None)
    return booking_doc

@api_router.get("/bookings/student")
async def get_student_bookings(request: Request):
    user = await get_current_user(request)
    profile = await db.student_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        return []
    
    bookings = await db.bookings.find({"student_id": profile["student_id"]}, {"_id": 0}).to_list(100)
    
    # Enrich with counselor and service details
    for booking in bookings:
        counselor = await db.counselor_profiles.find_one({"counselor_id": booking["counselor_id"]}, {"_id": 0})
        if counselor:
            counselor_user = await db.users.find_one({"user_id": counselor["user_id"]}, {"_id": 0, "password_hash": 0})
            counselor["user"] = counselor_user
        booking["counselor"] = counselor
        
        service = await db.services.find_one({"service_id": booking["service_id"]}, {"_id": 0})
        booking["service"] = service
        
        review = await db.reviews.find_one({"booking_id": booking["booking_id"]}, {"_id": 0})
        booking["review"] = review
    
    return bookings

@api_router.get("/bookings/counselor")
async def get_counselor_bookings(request: Request):
    user = await get_current_user(request)
    profile = await db.counselor_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        return []
    
    bookings = await db.bookings.find({"counselor_id": profile["counselor_id"]}, {"_id": 0}).to_list(100)
    
    # Enrich with student and service details
    for booking in bookings:
        student_profile = await db.student_profiles.find_one({"student_id": booking["student_id"]}, {"_id": 0})
        if student_profile:
            student_user = await db.users.find_one({"user_id": student_profile["user_id"]}, {"_id": 0, "password_hash": 0})
            student_profile["user"] = student_user
        booking["student"] = student_profile
        
        service = await db.services.find_one({"service_id": booking["service_id"]}, {"_id": 0})
        booking["service"] = service
    
    return bookings

@api_router.put("/bookings/{booking_id}/status")
async def update_booking_status(booking_id: str, request: Request):
    body = await request.json()
    new_status = body.get("status")
    counselor_notes = body.get("counselor_notes")
    
    _user = await get_current_user(request)  # Authentication check
    booking = await db.bookings.find_one({"booking_id": booking_id}, {"_id": 0})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    update_data = {"status": new_status}
    if counselor_notes:
        update_data["counselor_notes"] = counselor_notes
    
    await db.bookings.update_one({"booking_id": booking_id}, {"$set": update_data})
    
    # Update counselor stats if completed
    if new_status == "completed":
        await db.counselor_profiles.update_one(
            {"counselor_id": booking["counselor_id"]},
            {"$inc": {"total_sessions": 1}}
        )
    
    # Create reminders when booking is confirmed
    if new_status == "confirmed":
        try:
            # Get booking datetime
            booking_dt_str = booking.get("date_time")
            if booking_dt_str:
                if isinstance(booking_dt_str, str):
                    booking_dt = datetime.fromisoformat(booking_dt_str.replace('Z', '+00:00'))
                else:
                    booking_dt = booking_dt_str
                
                # Get student and counselor info
                student_profile = await db.student_profiles.find_one({"student_id": booking["student_id"]}, {"_id": 0})
                counselor_profile = await db.counselor_profiles.find_one({"counselor_id": booking["counselor_id"]}, {"_id": 0})
                service = await db.services.find_one({"service_id": booking["service_id"]}, {"_id": 0})
                
                if student_profile and counselor_profile:
                    student_user = await db.users.find_one({"user_id": student_profile["user_id"]}, {"_id": 0})
                    counselor_user = await db.users.find_one({"user_id": counselor_profile["user_id"]}, {"_id": 0})
                    
                    service_name = service["name"] if service else "Session"
                    student_name = f"{student_user.get('first_name', '')} {student_user.get('last_name', '')}".strip()
                    counselor_name = f"{counselor_user.get('first_name', '')} {counselor_user.get('last_name', '')}".strip()
                    
                    # Create 24h reminder
                    reminder_24h_time = booking_dt - timedelta(hours=24)
                    if reminder_24h_time > datetime.now(timezone.utc):
                        await reminder_service.create_reminder(
                            user_id=student_profile["user_id"],
                            booking_id=booking_id,
                            reminder_time=reminder_24h_time,
                            reminder_type="24h",
                            message=f"Your {service_name} session with {counselor_name} is in 24 hours."
                        )
                        await reminder_service.create_reminder(
                            user_id=counselor_profile["user_id"],
                            booking_id=booking_id,
                            reminder_time=reminder_24h_time,
                            reminder_type="24h",
                            message=f"Your {service_name} session with {student_name} is in 24 hours."
                        )
                    
                    # Create 1h reminder
                    reminder_1h_time = booking_dt - timedelta(hours=1)
                    if reminder_1h_time > datetime.now(timezone.utc):
                        await reminder_service.create_reminder(
                            user_id=student_profile["user_id"],
                            booking_id=booking_id,
                            reminder_time=reminder_1h_time,
                            reminder_type="1h",
                            message=f"Your {service_name} session with {counselor_name} starts in 1 hour."
                        )
                        await reminder_service.create_reminder(
                            user_id=counselor_profile["user_id"],
                            booking_id=booking_id,
                            reminder_time=reminder_1h_time,
                            reminder_type="1h",
                            message=f"Your {service_name} session with {student_name} starts in 1 hour."
                        )
                    
                    # Send email confirmations
                    if student_user.get("email"):
                        await email_service.send_booking_confirmation(
                            to_email=student_user["email"],
                            user_name=student_name,
                            service_name=service_name,
                            counselor_name=counselor_name,
                            session_time=booking_dt.strftime("%B %d, %Y at %I:%M %p UTC")
                        )
        except Exception as e:
            logger.error(f"Error creating reminders: {e}")
    
    updated = await db.bookings.find_one({"booking_id": booking_id}, {"_id": 0})
    return updated

@api_router.post("/bookings/{booking_id}/review")
async def add_review(booking_id: str, review_data: ReviewCreate, request: Request):
    user = await get_current_user(request)
    profile = await db.student_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=403, detail="Only students can add reviews")
    
    booking = await db.bookings.find_one({"booking_id": booking_id, "student_id": profile["student_id"]}, {"_id": 0})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    existing_review = await db.reviews.find_one({"booking_id": booking_id}, {"_id": 0})
    if existing_review:
        raise HTTPException(status_code=400, detail="Review already exists")
    
    review_id = f"review_{uuid.uuid4().hex[:12]}"
    review_doc = {
        "review_id": review_id,
        "booking_id": booking_id,
        "student_id": profile["student_id"],
        "counselor_id": booking["counselor_id"],
        "rating": review_data.rating,
        "comment": review_data.comment,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.reviews.insert_one(review_doc)
    
    # Update counselor rating
    all_reviews = await db.reviews.find({"counselor_id": booking["counselor_id"]}, {"_id": 0}).to_list(1000)
    avg_rating = sum(r["rating"] for r in all_reviews) / len(all_reviews) if all_reviews else 0
    await db.counselor_profiles.update_one(
        {"counselor_id": booking["counselor_id"]},
        {"$set": {"rating": round(avg_rating, 1), "total_reviews": len(all_reviews)}}
    )
    
    review_doc.pop("_id", None)
    return review_doc

# ==================== UNIVERSITY ENDPOINTS ====================

@api_router.get("/universities")
async def get_universities(
    country: Optional[str] = None,
    program: Optional[str] = None,
    min_tuition: Optional[float] = None,
    max_tuition: Optional[float] = None,
    max_ranking: Optional[int] = None,
    search: Optional[str] = None
):
    query = {}
    
    if country:
        query["country"] = country
    if program:
        query["programs"] = {"$in": [program]}
    if max_ranking:
        query["ranking"] = {"$lte": max_ranking}
    if min_tuition:
        query["tuition_min"] = {"$gte": min_tuition}
    if max_tuition:
        query["tuition_max"] = {"$lte": max_tuition}
    if search:
        query["$or"] = [
            {"name": {"$regex": search, "$options": "i"}},
            {"city": {"$regex": search, "$options": "i"}},
            {"popular_majors": {"$in": [search]}}
        ]
    
    universities = await db.universities.find(query, {"_id": 0}).to_list(200)
    return universities

@api_router.get("/universities/{university_id}")
async def get_university(university_id: str):
    university = await db.universities.find_one({"university_id": university_id}, {"_id": 0})
    if not university:
        raise HTTPException(status_code=404, detail="University not found")
    return university

@api_router.post("/universities")
async def create_university(university_data: UniversityCreate, request: Request):
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    university_id = f"university_{uuid.uuid4().hex[:12]}"
    university_doc = {
        "university_id": university_id,
        **university_data.model_dump()
    }
    await db.universities.insert_one(university_doc)
    university_doc.pop("_id", None)
    return university_doc

@api_router.put("/universities/{university_id}")
async def update_university(university_id: str, university_data: UniversityCreate, request: Request):
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    result = await db.universities.update_one(
        {"university_id": university_id},
        {"$set": university_data.model_dump()}
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="University not found")
    
    updated = await db.universities.find_one({"university_id": university_id}, {"_id": 0})
    return updated

@api_router.delete("/universities/{university_id}")
async def delete_university(university_id: str, request: Request):
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    result = await db.universities.delete_one({"university_id": university_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="University not found")
    
    return {"message": "University deleted"}

# ==================== RESOURCES ENDPOINTS ====================

@api_router.get("/guides")
async def get_guides():
    guides = await db.guides.find({}, {"_id": 0}).to_list(50)
    return guides

@api_router.get("/guides/{country}")
async def get_guide(country: str):
    guide = await db.guides.find_one({"country": country.upper()}, {"_id": 0})
    if not guide:
        raise HTTPException(status_code=404, detail="Guide not found")
    return guide

@api_router.get("/blog")
async def get_blog_posts(category: Optional[str] = None):
    query = {} if not category else {"category": category}
    posts = await db.blog_posts.find(query, {"_id": 0}).sort("published_at", -1).to_list(50)
    return posts

@api_router.get("/blog/{post_id}")
async def get_blog_post(post_id: str):
    post = await db.blog_posts.find_one({"post_id": post_id}, {"_id": 0})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    # Increment views
    await db.blog_posts.update_one({"post_id": post_id}, {"$inc": {"views": 1}})
    post["views"] = post.get("views", 0) + 1
    
    return post

@api_router.get("/faq")
async def get_faqs(category: Optional[str] = None):
    query = {} if not category else {"category": category}
    faqs = await db.faqs.find(query, {"_id": 0}).to_list(100)
    return faqs

@api_router.get("/resources/templates")
async def get_templates():
    templates = await db.templates.find({}, {"_id": 0}).to_list(50)
    return templates

# ==================== ADMIN ENDPOINTS ====================

@api_router.get("/admin/pending-counselors")
async def get_pending_counselors(request: Request):
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    counselors = await db.counselor_profiles.find({"status": "pending"}, {"_id": 0}).to_list(100)
    for c in counselors:
        user_data = await db.users.find_one({"user_id": c["user_id"]}, {"_id": 0, "password_hash": 0})
        c["user"] = user_data
    
    return counselors

@api_router.put("/admin/counselors/{counselor_id}/status")
async def update_counselor_status(counselor_id: str, request: Request):
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    body = await request.json()
    new_status = body.get("status")
    feedback = body.get("feedback")
    
    if new_status not in ["approved", "rejected", "suspended"]:
        raise HTTPException(status_code=400, detail="Invalid status")
    
    update_data = {"status": new_status}
    if feedback:
        update_data["admin_feedback"] = feedback
    
    result = await db.counselor_profiles.update_one(
        {"counselor_id": counselor_id},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Counselor not found")
    
    updated = await db.counselor_profiles.find_one({"counselor_id": counselor_id}, {"_id": 0})
    return updated

@api_router.get("/admin/all-counselors")
async def get_all_counselors(request: Request):
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    counselors = await db.counselor_profiles.find({}, {"_id": 0}).to_list(200)
    for c in counselors:
        user_data = await db.users.find_one({"user_id": c["user_id"]}, {"_id": 0, "password_hash": 0})
        c["user"] = user_data
    
    return counselors

@api_router.get("/admin/all-students")
async def get_all_students(request: Request):
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    students = await db.student_profiles.find({}, {"_id": 0}).to_list(200)
    for s in students:
        user_data = await db.users.find_one({"user_id": s["user_id"]}, {"_id": 0, "password_hash": 0})
        s["user"] = user_data
    
    return students

@api_router.get("/admin/all-bookings")
async def get_all_bookings(request: Request):
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    bookings = await db.bookings.find({}, {"_id": 0}).to_list(500)
    return bookings

@api_router.get("/admin/analytics")
async def get_analytics(request: Request):
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    total_students = await db.student_profiles.count_documents({})
    total_counselors = await db.counselor_profiles.count_documents({})
    approved_counselors = await db.counselor_profiles.count_documents({"status": "approved"})
    pending_counselors = await db.counselor_profiles.count_documents({"status": "pending"})
    total_bookings = await db.bookings.count_documents({})
    completed_bookings = await db.bookings.count_documents({"status": "completed"})
    total_universities = await db.universities.count_documents({})
    
    return {
        "total_students": total_students,
        "total_counselors": total_counselors,
        "approved_counselors": approved_counselors,
        "pending_counselors": pending_counselors,
        "total_bookings": total_bookings,
        "completed_bookings": completed_bookings,
        "total_universities": total_universities
    }

# ==================== COUNSELOR ANALYTICS ====================

@api_router.get("/counselors/me/analytics")
async def get_counselor_analytics(request: Request):
    user = await get_current_user(request)
    profile = await db.counselor_profiles.find_one({"user_id": user["user_id"]}, {"_id": 0})
    if not profile:
        raise HTTPException(status_code=404, detail="Counselor profile not found")
    
    bookings = await db.bookings.find({"counselor_id": profile["counselor_id"]}, {"_id": 0}).to_list(500)
    
    total_earnings = sum(b["price"] for b in bookings if b["status"] == "completed")
    completed_sessions = len([b for b in bookings if b["status"] == "completed"])
    
    # Service popularity
    service_counts = {}
    for b in bookings:
        sid = b["service_id"]
        service_counts[sid] = service_counts.get(sid, 0) + 1
    
    services = await db.services.find({"counselor_id": profile["counselor_id"]}, {"_id": 0}).to_list(50)
    service_popularity = []
    for s in services:
        service_popularity.append({
            "name": s["name"],
            "count": service_counts.get(s["service_id"], 0)
        })
    service_popularity.sort(key=lambda x: x["count"], reverse=True)
    
    return {
        "total_earnings": total_earnings,
        "completed_sessions": completed_sessions,
        "average_rating": profile.get("rating", 0),
        "total_reviews": profile.get("total_reviews", 0),
        "service_popularity": service_popularity[:5]
    }

# ==================== SEED DATA ENDPOINT ====================

@api_router.post("/seed")
async def seed_database():
    # Check if already seeded
    existing = await db.universities.count_documents({})
    if existing > 0:
        return {"message": "Database already seeded"}
    
    # Seed Universities
    universities = [
        {"university_id": "uni_mit", "name": "Massachusetts Institute of Technology", "country": "USA", "city": "Cambridge", "ranking": 1, "website": "https://mit.edu", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 55000, "tuition_max": 60000, "application_deadlines": {"fall": "January 1", "spring": "N/A"}, "popular_majors": ["Computer Science", "Engineering", "Physics", "Mathematics"], "description": "MIT is a world-renowned research university known for its excellence in science and technology.", "logo": "https://upload.wikimedia.org/wikipedia/commons/0/0c/MIT_logo.svg", "cover_image": "https://images.unsplash.com/photo-1564981797816-1043664bf78d?w=800", "images": []},
        {"university_id": "uni_stanford", "name": "Stanford University", "country": "USA", "city": "Stanford", "ranking": 3, "website": "https://stanford.edu", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 56000, "tuition_max": 62000, "application_deadlines": {"fall": "January 2", "spring": "N/A"}, "popular_majors": ["Computer Science", "Business", "Biology", "Economics"], "description": "Stanford is a leading research university in Silicon Valley known for entrepreneurship and innovation.", "logo": "https://upload.wikimedia.org/wikipedia/commons/b/b5/Seal_of_Leland_Stanford_Junior_University.svg", "cover_image": "https://images.unsplash.com/photo-1541625602330-2277a4c46182?w=800", "images": []},
        {"university_id": "uni_oxford", "name": "University of Oxford", "country": "UK", "city": "Oxford", "ranking": 4, "website": "https://ox.ac.uk", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 30000, "tuition_max": 45000, "application_deadlines": {"fall": "October 15", "spring": "N/A"}, "popular_majors": ["Law", "Medicine", "Philosophy", "Economics"], "description": "Oxford is the oldest university in the English-speaking world with a rich academic tradition.", "logo": "https://upload.wikimedia.org/wikipedia/commons/f/ff/Oxford-University-Circlet.svg", "cover_image": "https://images.unsplash.com/photo-1580237541049-2d715a09486e?w=800", "images": []},
        {"university_id": "uni_cambridge", "name": "University of Cambridge", "country": "UK", "city": "Cambridge", "ranking": 5, "website": "https://cam.ac.uk", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 28000, "tuition_max": 42000, "application_deadlines": {"fall": "October 15", "spring": "N/A"}, "popular_majors": ["Natural Sciences", "Engineering", "Mathematics", "Economics"], "description": "Cambridge is one of the world's most prestigious universities with over 800 years of history.", "logo": "https://upload.wikimedia.org/wikipedia/commons/c/c3/Coat_of_Arms_of_the_University_of_Cambridge.svg", "cover_image": "https://images.unsplash.com/photo-1594561538859-b54c9b049bf8?w=800", "images": []},
        {"university_id": "uni_toronto", "name": "University of Toronto", "country": "Canada", "city": "Toronto", "ranking": 18, "website": "https://utoronto.ca", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 45000, "tuition_max": 55000, "application_deadlines": {"fall": "January 15", "spring": "September 1"}, "popular_majors": ["Engineering", "Business", "Computer Science", "Life Sciences"], "description": "U of T is Canada's top university known for research excellence and diverse programs.", "logo": "https://upload.wikimedia.org/wikipedia/en/0/04/Utoronto_coa.svg", "cover_image": "https://images.unsplash.com/photo-1569098644584-210bcd375b59?w=800", "images": []},
        {"university_id": "uni_ubc", "name": "University of British Columbia", "country": "Canada", "city": "Vancouver", "ranking": 34, "website": "https://ubc.ca", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 40000, "tuition_max": 50000, "application_deadlines": {"fall": "January 15", "spring": "September 1"}, "popular_majors": ["Engineering", "Science", "Business", "Arts"], "description": "UBC is a global research hub located in beautiful Vancouver.", "logo": "https://upload.wikimedia.org/wikipedia/en/a/a7/UBC_Logo.svg", "cover_image": "https://images.unsplash.com/photo-1590012314607-cda9d9b699ae?w=800", "images": []},
        {"university_id": "uni_melbourne", "name": "University of Melbourne", "country": "Australia", "city": "Melbourne", "ranking": 14, "website": "https://unimelb.edu.au", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 35000, "tuition_max": 48000, "application_deadlines": {"fall": "October 31", "spring": "April 30"}, "popular_majors": ["Medicine", "Law", "Engineering", "Arts"], "description": "Melbourne is Australia's leading university with a vibrant campus life.", "logo": "https://upload.wikimedia.org/wikipedia/en/e/ed/University_of_Melbourne_logo.svg", "cover_image": "https://images.unsplash.com/photo-1577985051167-0d49eec21977?w=800", "images": []},
        {"university_id": "uni_sydney", "name": "University of Sydney", "country": "Australia", "city": "Sydney", "ranking": 19, "website": "https://sydney.edu.au", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 33000, "tuition_max": 46000, "application_deadlines": {"fall": "October 31", "spring": "April 30"}, "popular_majors": ["Business", "Engineering", "Health Sciences", "Law"], "description": "Australia's first university, located in the heart of Sydney.", "logo": "https://upload.wikimedia.org/wikipedia/en/f/fc/University_of_Sydney_logo.svg", "cover_image": "https://images.unsplash.com/photo-1506905925346-21bda4d32df4?w=800", "images": []},
        {"university_id": "uni_tum", "name": "Technical University of Munich", "country": "Germany", "city": "Munich", "ranking": 50, "website": "https://tum.de", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 500, "tuition_max": 2000, "application_deadlines": {"fall": "May 31", "spring": "November 30"}, "popular_majors": ["Engineering", "Computer Science", "Physics", "Architecture"], "description": "TUM is Germany's top technical university with almost free tuition.", "logo": "https://upload.wikimedia.org/wikipedia/commons/c/c8/Logo_of_the_Technical_University_of_Munich.svg", "cover_image": "https://images.unsplash.com/photo-1599946347371-68eb71b16afc?w=800", "images": []},
        {"university_id": "uni_eth", "name": "ETH Zurich", "country": "Switzerland", "city": "Zurich", "ranking": 7, "website": "https://ethz.ch", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 1500, "tuition_max": 3000, "application_deadlines": {"fall": "December 15", "spring": "N/A"}, "popular_majors": ["Computer Science", "Engineering", "Physics", "Mathematics"], "description": "ETH Zurich is one of the world's leading science and technology universities.", "logo": "https://upload.wikimedia.org/wikipedia/commons/6/63/ETH_Z%C3%BCrich_Logo.svg", "cover_image": "https://images.unsplash.com/photo-1530122037265-a5f1f91d3b99?w=800", "images": []},
        {"university_id": "uni_nus", "name": "National University of Singapore", "country": "Singapore", "city": "Singapore", "ranking": 8, "website": "https://nus.edu.sg", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 25000, "tuition_max": 40000, "application_deadlines": {"fall": "February 28", "spring": "N/A"}, "popular_majors": ["Business", "Computing", "Engineering", "Law"], "description": "NUS is Asia's leading university with a global outlook.", "logo": "https://upload.wikimedia.org/wikipedia/en/b/b9/NUS_coat_of_arms.svg", "cover_image": "https://images.unsplash.com/photo-1565967511849-76a60a516170?w=800", "images": []},
        {"university_id": "uni_amsterdam", "name": "University of Amsterdam", "country": "Netherlands", "city": "Amsterdam", "ranking": 53, "website": "https://uva.nl", "programs": ["Undergraduate", "Graduate", "Doctorate"], "tuition_min": 12000, "tuition_max": 20000, "application_deadlines": {"fall": "April 1", "spring": "November 1"}, "popular_majors": ["Business", "Psychology", "Social Sciences", "Humanities"], "description": "UvA is one of Europe's largest research universities in a vibrant city.", "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2d/UvA_logo.svg/1200px-UvA_logo.svg.png", "cover_image": "https://images.unsplash.com/photo-1534351590666-13e3e96b5017?w=800", "images": []}
    ]
    
    # Seed Counselors (with users)
    counselor_photos = [
        "https://images.unsplash.com/photo-1560250097-0b93528c311a?w=400",
        "https://images.unsplash.com/photo-1573496359142-b8d87734a5a2?w=400",
        "https://images.unsplash.com/photo-1507003211169-0a1dd7228f2d?w=400",
        "https://images.unsplash.com/photo-1580489944761-15a19d654956?w=400",
        "https://images.unsplash.com/photo-1472099645785-5658abf4ff4e?w=400",
        "https://images.unsplash.com/photo-1438761681033-6461ffad8d80?w=400",
        "https://images.unsplash.com/photo-1500648767791-00dcc994a43e?w=400",
        "https://images.unsplash.com/photo-1534528741775-53994a69daeb?w=400",
        "https://images.unsplash.com/photo-1506794778202-cad84cf45f1d?w=400",
        "https://images.unsplash.com/photo-1544005313-94ddf0286df2?w=400"
    ]
    
    counselor_data = [
        {"first_name": "Sarah", "last_name": "Johnson", "credentials": "PhD in Education, Certified Educational Consultant", "experience": 12, "specializations": ["USA", "UK"], "languages": ["English", "Spanish"], "bio": "Dr. Sarah Johnson has helped over 500 students achieve their dreams of studying at top universities in the US and UK. Her expertise includes Ivy League applications and scholarship guidance."},
        {"first_name": "Michael", "last_name": "Chen", "credentials": "MBA from Stanford, Former Admissions Officer", "experience": 8, "specializations": ["USA", "Canada"], "languages": ["English", "Mandarin"], "bio": "Michael brings insider knowledge from his time as an admissions officer at a top US university. He specializes in business school applications and career counseling."},
        {"first_name": "Emma", "last_name": "Williams", "credentials": "MA in International Education, UCAS Expert", "experience": 10, "specializations": ["UK", "Europe"], "languages": ["English", "French", "German"], "bio": "Emma is a UK education specialist who has guided hundreds of students through the UCAS system. She also helps with European university applications."},
        {"first_name": "Priya", "last_name": "Sharma", "credentials": "MS in Counseling, Study Abroad Specialist", "experience": 6, "specializations": ["Australia", "Singapore", "UK"], "languages": ["English", "Hindi"], "bio": "Priya specializes in helping students from South Asia find the perfect fit in Australia, Singapore, and the UK. She focuses on STEM programs and scholarship applications."},
        {"first_name": "David", "last_name": "Mueller", "credentials": "Former DAAD Representative, PhD in Engineering", "experience": 15, "specializations": ["Germany", "Europe"], "languages": ["English", "German"], "bio": "David is the go-to expert for German university applications. He helps students navigate the German education system and scholarship opportunities like DAAD."},
        {"first_name": "Jennifer", "last_name": "Park", "credentials": "MA in Higher Education, Test Prep Expert", "experience": 7, "specializations": ["USA", "Canada", "UK"], "languages": ["English", "Korean"], "bio": "Jennifer combines college counseling with test preparation expertise. She helps students maximize their SAT, GRE, and IELTS scores while crafting compelling applications."},
        {"first_name": "Robert", "last_name": "Thompson", "credentials": "EdD, Former University Professor", "experience": 20, "specializations": ["USA", "UK", "Australia"], "languages": ["English"], "bio": "Dr. Thompson brings two decades of experience in higher education. He specializes in doctoral program applications and research-focused academic paths."},
        {"first_name": "Maria", "last_name": "Garcia", "credentials": "MBA, Career Counselor", "experience": 5, "specializations": ["USA", "Canada", "Europe"], "languages": ["English", "Spanish", "Portuguese"], "bio": "Maria helps students align their study abroad goals with career aspirations. She specializes in business and management programs across North America and Europe."},
        {"first_name": "James", "last_name": "Wilson", "credentials": "LLM, Legal Education Consultant", "experience": 9, "specializations": ["UK", "USA"], "languages": ["English"], "bio": "James is a legal education specialist who guides aspiring lawyers through law school applications in the UK and USA, including bar exam preparation strategies."},
        {"first_name": "Lisa", "last_name": "Anderson", "credentials": "MS in Student Affairs, Scholarship Expert", "experience": 11, "specializations": ["USA", "UK", "Canada", "Australia"], "languages": ["English"], "bio": "Lisa is renowned for her success in helping students secure scholarships. She has helped students obtain over $5 million in scholarship funding collectively."}
    ]
    
    # Create counselor users and profiles
    services_template = [
        {"name": "Initial Consultation", "description": "30-minute introductory session to assess your goals and create a personalized roadmap.", "duration": 30, "price": 75, "category": "Consultation"},
        {"name": "University Selection Strategy", "description": "Comprehensive analysis of your profile and recommendations for best-fit universities.", "duration": 60, "price": 150, "category": "Application Guidance"},
        {"name": "SOP/Essay Review", "description": "Detailed feedback on your statement of purpose or application essays.", "duration": 45, "price": 100, "category": "Application Guidance"},
        {"name": "Application Package Review", "description": "Complete review of your application materials including essays, resume, and recommendations.", "duration": 90, "price": 250, "category": "Application Guidance"},
        {"name": "Interview Preparation", "description": "Mock interviews and strategies for university admission interviews.", "duration": 60, "price": 125, "category": "Interview Prep"},
        {"name": "Visa Guidance Session", "description": "Expert guidance on visa applications, documentation, and interview preparation.", "duration": 60, "price": 100, "category": "Visa Support"},
        {"name": "Scholarship Strategy", "description": "Identify scholarship opportunities and create winning applications.", "duration": 75, "price": 175, "category": "Scholarship"}
    ]
    
    for i, c in enumerate(counselor_data):
        user_id = f"user_counselor_{i}"
        counselor_id = f"counselor_{i}"
        
        user_doc = {
            "user_id": user_id,
            "email": f"{c['first_name'].lower()}.{c['last_name'].lower()}@eduadvise.com",
            "first_name": c["first_name"],
            "last_name": c["last_name"],
            "phone": f"+1-555-{100+i:03d}-{1000+i:04d}",
            "password_hash": hash_password("password123"),
            "user_type": "counselor",
            "is_verified": True,
            "picture": counselor_photos[i],
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.users.insert_one(user_doc)
        
        profile_doc = {
            "counselor_id": counselor_id,
            "user_id": user_id,
            "credentials": c["credentials"],
            "experience_years": c["experience"],
            "specializations": c["specializations"],
            "languages": c["languages"],
            "bio": c["bio"],
            "photo": counselor_photos[i],
            "linkedin_url": f"https://linkedin.com/in/{c['first_name'].lower()}{c['last_name'].lower()}",
            "status": "approved",
            "rating": round(4.0 + (i % 10) * 0.1, 1),
            "total_reviews": 10 + i * 5,
            "total_sessions": 50 + i * 20,
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.counselor_profiles.insert_one(profile_doc)
        
        # Create services with varied prices
        for j, svc in enumerate(services_template):
            service_doc = {
                "service_id": f"service_{i}_{j}",
                "counselor_id": counselor_id,
                "name": svc["name"],
                "description": svc["description"],
                "duration": svc["duration"],
                "price": svc["price"] + (i * 10),  # Vary prices by counselor
                "category": svc["category"],
                "is_active": True,
                "created_at": datetime.now(timezone.utc).isoformat()
            }
            await db.services.insert_one(service_doc)
    
    # Seed Guides
    guides = [
        {
            "guide_id": "guide_usa",
            "country": "USA",
            "title": "Complete Guide to Studying in the USA",
            "content": "The United States is home to many of the world's top universities and offers diverse academic programs across all fields...",
            "timeline": [
                {"month": "12-18 months before", "task": "Research universities and programs"},
                {"month": "12 months before", "task": "Take standardized tests (SAT/GRE/TOEFL)"},
                {"month": "9 months before", "task": "Request transcripts and recommendations"},
                {"month": "6 months before", "task": "Submit applications"},
                {"month": "3 months before", "task": "Apply for financial aid and scholarships"},
                {"month": "2 months before", "task": "Apply for F-1 student visa"},
                {"month": "1 month before", "task": "Arrange accommodation and travel"}
            ],
            "required_documents": ["Transcripts", "Test scores (SAT/GRE/TOEFL)", "Letters of recommendation", "Statement of purpose", "Financial documents", "Passport"],
            "tests_required": ["TOEFL/IELTS", "SAT (undergraduate)", "GRE/GMAT (graduate)"],
            "visa_info": "F-1 Student Visa required. Need I-20 from university, proof of financial support, and interview at US embassy.",
            "estimated_cost": "$30,000 - $70,000 per year (tuition + living)",
            "post_study_work": "Optional Practical Training (OPT) allows 12 months work, STEM fields get 36 months total."
        },
        {
            "guide_id": "guide_uk",
            "country": "UK",
            "title": "Complete Guide to Studying in the UK",
            "content": "The United Kingdom offers world-class education with shorter program durations compared to other countries...",
            "timeline": [
                {"month": "12 months before", "task": "Research through UCAS and university websites"},
                {"month": "10 months before", "task": "Take IELTS test"},
                {"month": "September-January", "task": "Submit UCAS application"},
                {"month": "October 15", "task": "Deadline for Oxford/Cambridge"},
                {"month": "January 31", "task": "Regular UCAS deadline"},
                {"month": "After offer", "task": "Apply for Student visa"}
            ],
            "required_documents": ["UCAS application", "Personal statement", "Reference letter", "Transcripts", "IELTS scores", "Portfolio (if applicable)"],
            "tests_required": ["IELTS/TOEFL", "BMAT/LNAT (for specific courses)"],
            "visa_info": "Student visa required. Need CAS from university and proof of funds for tuition + 9 months living costs.",
            "estimated_cost": "£15,000 - £35,000 per year (tuition) + £12,000 - £15,000 (living)",
            "post_study_work": "Graduate Route visa allows 2 years work (3 for PhD) after graduation."
        },
        {
            "guide_id": "guide_canada",
            "country": "CANADA",
            "title": "Complete Guide to Studying in Canada",
            "content": "Canada offers high-quality education at relatively affordable costs with excellent post-study work opportunities...",
            "timeline": [
                {"month": "12 months before", "task": "Research universities and programs"},
                {"month": "10 months before", "task": "Take IELTS/TOEFL"},
                {"month": "8-6 months before", "task": "Submit applications"},
                {"month": "After acceptance", "task": "Apply for study permit"},
                {"month": "2 months before", "task": "Arrange accommodation"}
            ],
            "required_documents": ["Application forms", "Transcripts", "English proficiency scores", "Statement of purpose", "Financial proof", "Study permit application"],
            "tests_required": ["IELTS/TOEFL", "GRE/GMAT (for some programs)"],
            "visa_info": "Study permit required. Need acceptance letter, proof of funds, and clean background check.",
            "estimated_cost": "CAD $20,000 - $40,000 per year (tuition) + CAD $12,000 - $15,000 (living)",
            "post_study_work": "Post-Graduation Work Permit (PGWP) allows work for up to 3 years. Clear pathway to permanent residency."
        },
        {
            "guide_id": "guide_australia",
            "country": "AUSTRALIA",
            "title": "Complete Guide to Studying in Australia",
            "content": "Australia combines high-quality education with an excellent lifestyle and strong post-study work options...",
            "timeline": [
                {"month": "12 months before", "task": "Research universities through official channels"},
                {"month": "9 months before", "task": "Take IELTS/PTE"},
                {"month": "6-4 months before", "task": "Submit applications"},
                {"month": "After CoE", "task": "Apply for student visa (subclass 500)"},
                {"month": "1 month before", "task": "Arrange OSHC and accommodation"}
            ],
            "required_documents": ["Application forms", "Academic transcripts", "English test scores", "Statement of purpose", "Financial capacity proof", "Health insurance (OSHC)"],
            "tests_required": ["IELTS/PTE/TOEFL", "Some courses require portfolios"],
            "visa_info": "Student visa (subclass 500) required. Need CoE, OSHC, financial proof, and genuine temporary entrant requirement.",
            "estimated_cost": "AUD $25,000 - $45,000 per year (tuition) + AUD $21,000 (living)",
            "post_study_work": "Temporary Graduate visa (subclass 485) allows 2-4 years work depending on qualification."
        },
        {
            "guide_id": "guide_germany",
            "country": "GERMANY",
            "title": "Complete Guide to Studying in Germany",
            "content": "Germany offers tuition-free education at public universities with strong industry connections...",
            "timeline": [
                {"month": "18 months before", "task": "Start learning German (if needed)"},
                {"month": "12 months before", "task": "Research programs on DAAD database"},
                {"month": "8 months before", "task": "Take TestDaF/IELTS"},
                {"month": "6 months before", "task": "Apply through uni-assist or directly"},
                {"month": "After admission", "task": "Open blocked account, apply for visa"}
            ],
            "required_documents": ["Higher education entrance qualification", "Language certificates", "CV", "Motivation letter", "Blocked account proof", "Health insurance"],
            "tests_required": ["TestDaF/DSH (German programs)", "IELTS/TOEFL (English programs)"],
            "visa_info": "Student visa required. Need blocked account (€11,208/year), admission letter, and health insurance.",
            "estimated_cost": "€0 - €3,000 per year (tuition at public unis) + €10,000 - €12,000 (living)",
            "post_study_work": "18-month job-seeker visa after graduation. Easy path to EU Blue Card."
        }
    ]
    
    # Seed FAQs
    faqs = [
        {"faq_id": "faq_1", "question": "How early should I start planning for studying abroad?", "answer": "Ideally, start 18-24 months before your intended start date. This gives you time for test preparation, researching universities, and gathering documents.", "category": "Planning"},
        {"faq_id": "faq_2", "question": "What standardized tests do I need?", "answer": "It depends on your destination and program level. Common tests include TOEFL/IELTS for English proficiency, SAT for US undergrad, GRE for graduate programs, and GMAT for business schools.", "category": "Tests"},
        {"faq_id": "faq_3", "question": "How much does studying abroad cost?", "answer": "Costs vary widely by country and university. US: $30-70K/year, UK: £20-45K/year, Canada: CAD $25-45K/year, Germany: €10-15K/year (many programs are tuition-free).", "category": "Finance"},
        {"faq_id": "faq_4", "question": "Can I work while studying?", "answer": "Most countries allow international students to work part-time (usually 20 hours/week during semesters). Check specific visa regulations for your destination.", "category": "Work"},
        {"faq_id": "faq_5", "question": "What scholarships are available?", "answer": "Options include university scholarships, government scholarships (Fulbright, Chevening, DAAD), and private foundations. Our counselors can help identify opportunities matching your profile.", "category": "Finance"},
        {"faq_id": "faq_6", "question": "How do I write a strong Statement of Purpose?", "answer": "Focus on your academic journey, why you chose this program/university, your career goals, and what unique perspective you bring. Be specific and authentic.", "category": "Application"},
        {"faq_id": "faq_7", "question": "When should I apply for a student visa?", "answer": "Generally 3-4 months before your program starts, after receiving your admission letter. Some countries like the US allow applications up to 120 days before.", "category": "Visa"},
        {"faq_id": "faq_8", "question": "What are the post-study work options?", "answer": "Most popular destinations offer work permits after graduation: US (OPT 1-3 years), UK (Graduate Route 2 years), Canada (PGWP up to 3 years), Australia (485 visa 2-4 years).", "category": "Career"}
    ]
    
    # Seed Blog Posts
    blog_posts = [
        {"post_id": "blog_1", "title": "Top 10 Scholarships for International Students in 2025", "content": "Finding funding for your international education journey can be challenging, but there are numerous scholarship opportunities available...", "category": "Scholarships", "author": "EduAdvise Team", "tags": ["scholarships", "funding", "financial aid"], "published_at": datetime.now(timezone.utc).isoformat(), "views": 1523},
        {"post_id": "blog_2", "title": "How to Write a Winning Personal Statement", "content": "Your personal statement is your chance to stand out from thousands of applicants. Here's how to make it memorable...", "category": "Applications", "author": "Dr. Sarah Johnson", "tags": ["personal statement", "application tips", "essays"], "published_at": datetime.now(timezone.utc).isoformat(), "views": 2341},
        {"post_id": "blog_3", "title": "Student Life in Germany: What to Expect", "content": "Germany has become one of the most popular destinations for international students. Here's what daily life looks like...", "category": "Student Life", "author": "David Mueller", "tags": ["germany", "student life", "culture"], "published_at": datetime.now(timezone.utc).isoformat(), "views": 987}
    ]
    
    # Insert all seed data
    await db.universities.insert_many(universities)
    await db.guides.insert_many(guides)
    await db.faqs.insert_many(faqs)
    await db.blog_posts.insert_many(blog_posts)
    
    # Create admin user
    admin_user = {
        "user_id": "user_admin_1",
        "email": "admin@eduadvise.com",
        "first_name": "Admin",
        "last_name": "User",
        "phone": None,
        "password_hash": hash_password("admin123"),
        "user_type": "admin",
        "is_verified": True,
        "picture": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.users.insert_one(admin_user)
    
    return {"message": "Database seeded successfully", "universities": len(universities), "counselors": len(counselor_data), "guides": len(guides)}

# ==================== CHAT ENDPOINTS ====================

def get_conversation_id(user1_id: str, user2_id: str) -> str:
    """Generate consistent conversation ID regardless of order"""
    return "_".join(sorted([user1_id, user2_id]))

@api_router.get("/chat/conversations")
async def get_conversations(request: Request):
    """Get all conversations for current user"""
    user = await get_current_user(request)
    user_id = user["user_id"]
    
    conversations = await db.conversations.find(
        {"participants": user_id},
        {"_id": 0}
    ).sort("last_message_time", -1).to_list(50)
    
    # Enrich with other participant info
    for conv in conversations:
        other_user_id = [p for p in conv["participants"] if p != user_id][0]
        other_user = await db.users.find_one({"user_id": other_user_id}, {"_id": 0, "password_hash": 0})
        conv["other_user"] = other_user
        
        # Check if other user is a counselor
        counselor = await db.counselor_profiles.find_one({"user_id": other_user_id}, {"_id": 0})
        conv["other_counselor"] = counselor
        
        # Get unread count for this user
        conv["unread_count"] = conv.get("unread_counts", {}).get(user_id, 0)
    
    return conversations

@api_router.get("/chat/conversations/{other_user_id}")
async def get_or_create_conversation(other_user_id: str, request: Request):
    """Get or create a conversation with another user"""
    user = await get_current_user(request)
    user_id = user["user_id"]
    
    conversation_id = get_conversation_id(user_id, other_user_id)
    
    # Check if conversation exists
    conv = await db.conversations.find_one({"conversation_id": conversation_id}, {"_id": 0})
    
    if not conv:
        # Create new conversation
        conv = {
            "conversation_id": conversation_id,
            "participants": [user_id, other_user_id],
            "last_message": None,
            "last_message_time": datetime.now(timezone.utc).isoformat(),
            "unread_counts": {user_id: 0, other_user_id: 0},
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.conversations.insert_one(conv)
        conv.pop("_id", None)
    
    # Get other user info
    other_user = await db.users.find_one({"user_id": other_user_id}, {"_id": 0, "password_hash": 0})
    conv["other_user"] = other_user
    
    # Check if other user is a counselor
    counselor = await db.counselor_profiles.find_one({"user_id": other_user_id}, {"_id": 0})
    conv["other_counselor"] = counselor
    
    return conv

@api_router.get("/chat/messages/{conversation_id}")
async def get_messages(conversation_id: str, request: Request, limit: int = 50, before: str = None):
    """Get messages in a conversation"""
    user = await get_current_user(request)
    user_id = user["user_id"]
    
    # Verify user is part of this conversation
    conv = await db.conversations.find_one({"conversation_id": conversation_id}, {"_id": 0})
    if not conv or user_id not in conv["participants"]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    query = {"conversation_id": conversation_id}
    if before:
        query["created_at"] = {"$lt": before}
    
    messages = await db.chat_messages.find(query, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
    messages.reverse()  # Return in chronological order
    
    # Mark messages as read
    await db.chat_messages.update_many(
        {"conversation_id": conversation_id, "receiver_id": user_id, "read": False},
        {"$set": {"read": True}}
    )
    
    # Reset unread count
    await db.conversations.update_one(
        {"conversation_id": conversation_id},
        {"$set": {f"unread_counts.{user_id}": 0}}
    )
    
    return messages

@api_router.post("/chat/messages")
async def send_message(request: Request):
    """Send a chat message (text or file)"""
    user = await get_current_user(request)
    body = await request.json()
    
    receiver_id = body.get("receiver_id")
    content = body.get("content")
    message_type = body.get("message_type", "text")  # text, file, image
    file_data = body.get("file_data")  # Optional: {file_id, filename, url, size}
    
    if not receiver_id:
        raise HTTPException(status_code=400, detail="receiver_id required")
    
    if message_type == "text" and not content:
        raise HTTPException(status_code=400, detail="content required for text messages")
    
    if message_type in ["file", "image"] and not file_data:
        raise HTTPException(status_code=400, detail="file_data required for file messages")
    
    user_id = user["user_id"]
    conversation_id = get_conversation_id(user_id, receiver_id)
    
    # Ensure conversation exists
    conv = await db.conversations.find_one({"conversation_id": conversation_id}, {"_id": 0})
    if not conv:
        conv = {
            "conversation_id": conversation_id,
            "participants": [user_id, receiver_id],
            "last_message": None,
            "last_message_time": datetime.now(timezone.utc).isoformat(),
            "unread_counts": {user_id: 0, receiver_id: 0},
            "created_at": datetime.now(timezone.utc).isoformat()
        }
        await db.conversations.insert_one(conv)
    
    # Create message
    message_id = f"msg_{uuid.uuid4().hex[:12]}"
    message = {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "sender_id": user_id,
        "receiver_id": receiver_id,
        "content": content or (file_data.get("filename") if file_data else ""),
        "message_type": message_type,
        "file_data": file_data,  # Store file info for file messages
        "read": False,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.chat_messages.insert_one(message)
    message.pop("_id", None)
    
    # Update conversation
    await db.conversations.update_one(
        {"conversation_id": conversation_id},
        {
            "$set": {
                "last_message": content[:100],
                "last_message_time": message["created_at"]
            },
            "$inc": {f"unread_counts.{receiver_id}": 1}
        }
    )
    
    # Send via WebSocket if receiver is online
    is_online = manager.is_user_online(receiver_id)
    await manager.send_personal_message({
        "type": "new_message",
        "message": message
    }, receiver_id)
    
    # Send mock email notification if receiver is offline
    if not is_online:
        receiver_user = await db.users.find_one({"user_id": receiver_id}, {"_id": 0})
        sender_name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or "User"
        if receiver_user and receiver_user.get("email"):
            await email_service.send_new_message_notification(
                to_email=receiver_user["email"],
                sender_name=sender_name,
                message_preview=content
            )
    
    return message

@api_router.get("/chat/unread-count")
async def get_unread_count(request: Request):
    """Get total unread message count for current user"""
    user = await get_current_user(request)
    user_id = user["user_id"]
    
    count = await db.chat_messages.count_documents({
        "receiver_id": user_id,
        "read": False
    })
    
    return {"unread_count": count}

# ==================== VIDEO/VOICE CALL ENDPOINTS ====================

@api_router.post("/calls/initiate")
async def initiate_call(request: Request):
    """Initiate a video/voice call (only during confirmed booking)"""
    user = await get_current_user(request)
    body = await request.json()
    
    booking_id = body.get("booking_id")
    call_type = body.get("call_type", "video")  # video or voice
    
    if not booking_id:
        raise HTTPException(status_code=400, detail="booking_id required")
    
    # Verify booking exists and is confirmed
    booking = await db.bookings.find_one({"booking_id": booking_id}, {"_id": 0})
    if not booking:
        raise HTTPException(status_code=404, detail="Booking not found")
    
    if booking["status"] != "confirmed":
        raise HTTPException(status_code=400, detail="Booking must be confirmed to start a call")
    
    # Check if user is part of this booking
    user_id = user["user_id"]
    
    # Get student and counselor user IDs
    student_profile = await db.student_profiles.find_one({"student_id": booking["student_id"]}, {"_id": 0})
    counselor_profile = await db.counselor_profiles.find_one({"counselor_id": booking["counselor_id"]}, {"_id": 0})
    
    if not student_profile or not counselor_profile:
        raise HTTPException(status_code=404, detail="Profiles not found")
    
    student_user_id = student_profile["user_id"]
    counselor_user_id = counselor_profile["user_id"]
    
    if user_id not in [student_user_id, counselor_user_id]:
        raise HTTPException(status_code=403, detail="Not authorized for this booking")
    
    # Determine receiver
    receiver_id = counselor_user_id if user_id == student_user_id else student_user_id
    
    # Create call session
    call_id = f"call_{uuid.uuid4().hex[:12]}"
    call_session = {
        "call_id": call_id,
        "booking_id": booking_id,
        "caller_id": user_id,
        "receiver_id": receiver_id,
        "call_type": call_type,
        "status": "initiating",
        "started_at": None,
        "ended_at": None,
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.call_sessions.insert_one(call_session)
    call_session.pop("_id", None)
    
    # Notify receiver via WebSocket
    caller_user = await db.users.find_one({"user_id": user_id}, {"_id": 0, "password_hash": 0})
    await manager.send_personal_message({
        "type": "incoming_call",
        "call": call_session,
        "caller": caller_user
    }, receiver_id)
    
    return call_session

@api_router.post("/calls/{call_id}/answer")
async def answer_call(call_id: str, request: Request):
    """Answer an incoming call"""
    user = await get_current_user(request)
    user_id = user["user_id"]
    
    call = await db.call_sessions.find_one({"call_id": call_id}, {"_id": 0})
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    
    if call["receiver_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    await db.call_sessions.update_one(
        {"call_id": call_id},
        {"$set": {"status": "active", "started_at": datetime.now(timezone.utc).isoformat()}}
    )
    
    # Notify caller
    await manager.send_personal_message({
        "type": "call_answered",
        "call_id": call_id
    }, call["caller_id"])
    
    updated = await db.call_sessions.find_one({"call_id": call_id}, {"_id": 0})
    return updated

@api_router.post("/calls/{call_id}/reject")
async def reject_call(call_id: str, request: Request):
    """Reject an incoming call"""
    user = await get_current_user(request)
    user_id = user["user_id"]
    
    call = await db.call_sessions.find_one({"call_id": call_id}, {"_id": 0})
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    
    if call["receiver_id"] != user_id:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    await db.call_sessions.update_one(
        {"call_id": call_id},
        {"$set": {"status": "ended", "ended_at": datetime.now(timezone.utc).isoformat()}}
    )
    
    # Notify caller
    await manager.send_personal_message({
        "type": "call_rejected",
        "call_id": call_id
    }, call["caller_id"])
    
    return {"message": "Call rejected"}

@api_router.post("/calls/{call_id}/end")
async def end_call(call_id: str, request: Request):
    """End an active call"""
    user = await get_current_user(request)
    user_id = user["user_id"]
    
    call = await db.call_sessions.find_one({"call_id": call_id}, {"_id": 0})
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    
    if user_id not in [call["caller_id"], call["receiver_id"]]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    await db.call_sessions.update_one(
        {"call_id": call_id},
        {"$set": {"status": "ended", "ended_at": datetime.now(timezone.utc).isoformat()}}
    )
    
    # Notify other party
    other_user_id = call["receiver_id"] if user_id == call["caller_id"] else call["caller_id"]
    await manager.send_personal_message({
        "type": "call_ended",
        "call_id": call_id
    }, other_user_id)
    
    return {"message": "Call ended"}

@api_router.get("/calls/{call_id}")
async def get_call(call_id: str, request: Request):
    """Get call session details"""
    user = await get_current_user(request)
    user_id = user["user_id"]
    
    call = await db.call_sessions.find_one({"call_id": call_id}, {"_id": 0})
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    
    if user_id not in [call["caller_id"], call["receiver_id"]]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    return call

# ==================== WEBRTC SIGNALING ENDPOINTS ====================

@api_router.post("/calls/{call_id}/signal")
async def send_signal(call_id: str, request: Request):
    """Send WebRTC signaling data (offer/answer/ICE candidate)"""
    user = await get_current_user(request)
    body = await request.json()
    
    signal_type = body.get("type")  # offer, answer, ice-candidate
    signal_data = body.get("data")
    
    if not signal_type or not signal_data:
        raise HTTPException(status_code=400, detail="type and data required")
    
    user_id = user["user_id"]
    
    call = await db.call_sessions.find_one({"call_id": call_id}, {"_id": 0})
    if not call:
        raise HTTPException(status_code=404, detail="Call not found")
    
    if user_id not in [call["caller_id"], call["receiver_id"]]:
        raise HTTPException(status_code=403, detail="Not authorized")
    
    # Send to other party
    other_user_id = call["receiver_id"] if user_id == call["caller_id"] else call["caller_id"]
    
    await manager.send_personal_message({
        "type": "webrtc_signal",
        "call_id": call_id,
        "signal_type": signal_type,
        "data": signal_data
    }, other_user_id)
    
    return {"message": "Signal sent"}

# ==================== WEBSOCKET ENDPOINT ====================

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: str):
    """WebSocket connection for real-time chat and call signaling"""
    await manager.connect(websocket, user_id)
    try:
        while True:
            data = await websocket.receive_json()
            
            msg_type = data.get("type")
            
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            
            elif msg_type == "join_conversation":
                conversation_id = data.get("conversation_id")
                if conversation_id:
                    manager.join_room(conversation_id, user_id)
            
            elif msg_type == "leave_conversation":
                conversation_id = data.get("conversation_id")
                if conversation_id:
                    manager.leave_room(conversation_id, user_id)
            
            elif msg_type == "typing":
                conversation_id = data.get("conversation_id")
                if conversation_id:
                    await manager.broadcast_to_room(conversation_id, {
                        "type": "user_typing",
                        "user_id": user_id
                    }, exclude_user=user_id)
            
            elif msg_type == "stop_typing":
                conversation_id = data.get("conversation_id")
                if conversation_id:
                    await manager.broadcast_to_room(conversation_id, {
                        "type": "user_stop_typing",
                        "user_id": user_id
                    }, exclude_user=user_id)
    
    except WebSocketDisconnect:
        manager.disconnect(user_id)
        logger.info(f"User {user_id} disconnected")
    except Exception as e:
        logger.error(f"WebSocket error for {user_id}: {e}")
        manager.disconnect(user_id)

@api_router.get("/users/{user_id}/online")
async def check_user_online(user_id: str, request: Request):
    """Check if a user is online"""
    await get_current_user(request)
    return {"online": manager.is_user_online(user_id)}

# ==================== FILE UPLOAD ENDPOINTS ====================

ALLOWED_EXTENSIONS = {'.pdf', '.doc', '.docx', '.jpg', '.jpeg', '.png', '.gif', '.txt', '.xlsx', '.xls'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

@api_router.post("/upload")
async def upload_file(request: Request, file: UploadFile = File(...)):
    """Upload a file for chat sharing"""
    user = await get_current_user(request)
    
    # Validate file extension
    file_ext = Path(file.filename).suffix.lower()
    if file_ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"File type not allowed. Allowed: {', '.join(ALLOWED_EXTENSIONS)}")
    
    # Read and validate file size
    contents = await file.read()
    if len(contents) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail=f"File too large. Maximum size: {MAX_FILE_SIZE // (1024*1024)}MB")
    
    # Generate unique filename
    file_id = f"file_{uuid.uuid4().hex[:12]}"
    safe_filename = f"{file_id}{file_ext}"
    file_path = UPLOAD_DIR / safe_filename
    
    # Save file
    async with aiofiles.open(file_path, 'wb') as f:
        await f.write(contents)
    
    # Store file metadata in database
    file_doc = {
        "file_id": file_id,
        "original_name": file.filename,
        "stored_name": safe_filename,
        "size": len(contents),
        "content_type": file.content_type,
        "uploaded_by": user["user_id"],
        "created_at": datetime.now(timezone.utc).isoformat()
    }
    await db.uploaded_files.insert_one(file_doc)
    file_doc.pop("_id", None)
    
    # Return file URL
    file_doc["url"] = f"/api/files/{safe_filename}"
    
    return file_doc

@api_router.get("/files/{filename}")
async def get_file(filename: str):
    """Serve uploaded files"""
    file_path = UPLOAD_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    
    # Get content type from database
    file_doc = await db.uploaded_files.find_one({"stored_name": filename}, {"_id": 0})
    content_type = file_doc["content_type"] if file_doc else "application/octet-stream"
    
    async with aiofiles.open(file_path, 'rb') as f:
        content = await f.read()
    
    return Response(content=content, media_type=content_type)

# ==================== TURN SERVER CONFIG ENDPOINT ====================

@api_router.get("/webrtc/config")
async def get_webrtc_config(request: Request):
    """Get WebRTC configuration including TURN servers"""
    await get_current_user(request)
    return TURN_SERVERS

# ==================== REMINDERS ENDPOINTS ====================

@api_router.get("/reminders")
async def get_my_reminders(request: Request):
    """Get pending reminders for current user"""
    user = await get_current_user(request)
    reminders = await reminder_service.get_pending_reminders(user["user_id"])
    return reminders

@api_router.post("/reminders/{reminder_id}/read")
async def mark_reminder_read(reminder_id: str, request: Request):
    """Mark a reminder as read"""
    user = await get_current_user(request)
    
    reminder = await db.reminders.find_one({"reminder_id": reminder_id, "user_id": user["user_id"]}, {"_id": 0})
    if not reminder:
        raise HTTPException(status_code=404, detail="Reminder not found")
    
    await reminder_service.mark_reminder_read(reminder_id)
    return {"message": "Reminder marked as read"}

# ==================== EMAIL LOGS ENDPOINT (for demo/testing) ====================

@api_router.get("/admin/email-logs")
async def get_email_logs(request: Request, limit: int = 50):
    """Get email logs (admin only - for demo purposes)"""
    user = await get_current_user(request)
    if user.get("user_type") != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    
    logs = await db.email_logs.find({}, {"_id": 0}).sort("created_at", -1).limit(limit).to_list(limit)
    return logs

# ==================== ROOT ENDPOINT ====================

@api_router.get("/")
async def root():
    return {"message": "EduAdvise API - International Student Counseling Platform"}

# Include the router in the main app
app.include_router(api_router)

# Mount uploads directory for static file serving
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
